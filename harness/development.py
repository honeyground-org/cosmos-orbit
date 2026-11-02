"""Opt-in repository development in private clones with fixed, user-owned checks."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid

from . import context, providers

RESULT = {'type': 'object', 'properties': {
    'summary': {'type': 'string'}, 'blocker': {'type': 'string'},
    'next_action': {'type': 'string'}, 'remaining': {'type': 'array', 'items': {'type': 'string'}},
    'memory_requests': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'tool': {'type': 'string', 'enum': ['recall', 'neighbors', 'timeline', 'brain_analyse']},
        'arguments_json': {'type': 'string'}}, 'required': ['tool', 'arguments_json'],
        'additionalProperties': False}}},
    'required': ['summary', 'blocker', 'next_action', 'remaining', 'memory_requests'], 'additionalProperties': False}


def git(root, *args):
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', '-c',
                             'core.fsmonitor=false', '-C', str(root), *args],
                            capture_output=True)
    if result.returncode:
        raise ValueError('Git operation failed: ' + result.stderr.decode('utf-8', 'replace')[:2000])
    return result.stdout


def fingerprint(root):
    """Hash tracked and visible untracked contents, modes and deletions without running checks."""
    names = set(git(root, 'ls-files', '-z').split(b'\0'))
    names.update(git(root, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0'))
    result = hashlib.sha256()
    for raw in sorted(names - {b''}):
        path = Path(root) / os.fsdecode(raw)
        result.update(raw + b'\0')
        if path.is_symlink():
            result.update(b'link:' + os.fsencode(os.readlink(path)))
        elif path.is_file():
            result.update(str(path.stat().st_mode).encode())
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    result.update(chunk)
        else:
            result.update(b'missing')
    result.update(git(root, 'rev-parse', 'HEAD'))
    return result.hexdigest()


def evidence_current(task):
    dev = task['data']['development']
    return bool(dev.get('verified_tree')) and fingerprint(task['workspace']) == dev['verified_tree']


def create(store, root, provider, prompt, checks):
    root = Path(root).expanduser().resolve()
    if not checks or len(checks) > 12:
        raise ValueError('Development requires 1–12 explicit --check JSON argument arrays')
    commands = []
    for value in checks:
        argv = json.loads(value)
        if (not isinstance(argv, list) or not argv or len(argv) > 100
                or any(not isinstance(v, str) or '\0' in v or len(v) > 8000 for v in argv)
                or not argv[0].strip()):
            raise ValueError('Each --check must be a nonempty JSON array of command arguments')
        commands.append(argv)
    top = Path(os.fsdecode(git(root, 'rev-parse', '--show-toplevel')).strip()).resolve()
    if top != root or git(root, 'status', '--porcelain', '--untracked-files=all').strip():
        raise ValueError('Development requires a clean Git repository root; commit or stash changes first')
    if git(root, 'ls-files', '--stage').find(b'160000 ') != -1:
        raise ValueError('Submodule repositories are not supported by this development pilot')
    base = git(root, 'rev-parse', 'HEAD').decode().strip()
    target = store.home / 'workspaces' / uuid.uuid4().hex
    target.parent.mkdir(exist_ok=True, mode=0o700)
    git(root, 'clone', '--no-hardlinks', '--no-local', '--no-checkout', str(root), str(target))
    git(target, 'checkout', '--detach', base)
    git(target, 'remote', 'remove', 'origin')
    task = store.create(target, provider, prompt)
    task['data']['development'] = {'source': str(root), 'base': base,
        'checks': commands, 'phase': 'ready', 'verified_tree': None,
        'authority': 'Explicit --development and --check arguments; local isolated edits and checks only'}
    store.save(task, 'received', 'development_created', task['data']['development'])
    return task


def check(store, task, lease_fds=(), simplified=False):
    """Run only the original user-supplied argv, never a command proposed by the model."""
    dev = task['data']['development']
    if git(task['workspace'], 'rev-parse', 'HEAD').decode().strip() != dev['base']:
        raise ValueError('Isolated repository HEAD changed; inspect it before continuing')
    results = []
    directory = store.home / 'runs' / task['id']
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for index, argv in enumerate(task['data']['development']['checks']):
        path = directory / (uuid.uuid4().hex + '.check.log')
        from .telemetry import recorder
        emit = recorder(store, task['id'], task['provider'])
        emit('check_started', {'layer': 'evidence', 'direction': 'out', 'note': 'Verification command %d started' % (index + 1)})
        with path.open('wb') as output:
            os.chmod(path, 0o600)
            process = subprocess.Popen(argv, cwd=task['workspace'], stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=lease_fds)
            timed_out = False
            try:
                from .process_health import Monitor
                monitor = Monitor(process, path, None, emit, path.stem)
                monitor.sample(force=True)
                while process.poll() is None:
                    try:
                        process.wait(timeout=.25)
                    except subprocess.TimeoutExpired:
                        monitor.sample()
                monitor.sample(force=True)
            except BaseException as error:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                if not isinstance(error, subprocess.TimeoutExpired):
                    raise
                timed_out = True
        with path.open('rb') as output:
            output.seek(max(0, path.stat().st_size - 8000))
            tail = output.read().decode('utf-8', 'replace')
        results.append({'path': 'command:' + str(index + 1), 'kind': 'command',
                        'argv': argv, 'passed': process.returncode == 0 and not timed_out,
                        'exit_code': process.returncode, 'timed_out': timed_out,
                        'log': str(path), 'output_tail': tail})
        emit('check_finished', {'layer': 'evidence', 'direction': 'in', 'note': 'Verification command %d returned' % (index + 1), 'exit_code': process.returncode})
    changed = git(task['workspace'], 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', dev['base'], '--')
    untracked = git(task['workspace'], 'ls-files', '--others', '--exclude-standard', '-z')
    dev['changed_paths'] = sorted({os.fsdecode(p) for p in (changed + untracked).split(b'\0') if p})
    patch = directory / (uuid.uuid4().hex + '.tracked.patch')
    patch.write_bytes(git(task['workspace'], 'diff', '--no-ext-diff', '--no-textconv', '--binary', dev['base'], '--'))
    os.chmod(patch, 0o600)
    dev['tracked_patch'] = str(patch)
    dev['untracked_paths'] = [os.fsdecode(p) for p in untracked.split(b'\0') if p]
    task['data']['evidence'] = results
    task['data']['development']['verified_tree'] = fingerprint(task['workspace'])
    passed = all(r['passed'] for r in results)
    task['data']['development']['phase'] = 'checking' if simplified else 'evaluating'
    store.save(task, 'checking' if simplified else 'evaluating',
               'checks_recorded' if simplified else 'verified', {'checks': results, 'passed': passed})
    return False


def run(engine, task_id, plan_only=False):
    store = engine.store
    initial = store.get(task_id)
    with store.lock('task:' + task_id) as task_lease, store.lock('workspace:' + initial['workspace']) as workspace_lease:
        lease_fds = (task_lease, workspace_lease)
        task = store.get(task_id)
        try:
            engine._feedback_boundary(task)
            engine._routing_boundary(task)
            if engine._pause_boundary(task):
                return task
            if task['status'] == 'paused':
                store.save(task, task['data'].pop('resume_status', 'ready'), 'continued')
            if task['data'].get('plan_requested'):
                from .planning import prepare
                prepare(engine, task)
                return task
            if plan_only:
                task['data']['development']['hold'] = True
                store.save(task, 'needs_input', 'development_plan_recorded', task['data']['development'])
                task['data']['reason'] = 'Inspect development checks and workspace; resume to execute'
                store.save(task, 'needs_input', 'development_plan_ready')
                return task
            if task['data']['development'].get('hold'):
                return task
            if task['status'] == 'needs_review':
                if not evidence_current(task):
                    task['data']['reason'] = 'Files changed after verification; provide feedback before more work'
                    store.save(task, 'stale', 'external_change_detected')
                return task
            if task['status'] in ('blocked', 'stale', 'needs_input'):
                return task
            if task['data']['development']['phase'] == 'running':
                task['data']['reason'] = ('Native execution was interrupted. Inspect the isolated workspace and logs, '
                                          'then provide feedback to authorise another attempt. No automatic replay.')
                store.save(task, 'interrupted', 'development_recovery_required')
                return task
            while True:
                if engine._pause_boundary(task):
                    return task
                engine._feedback_boundary(task)
                engine._routing_boundary(task)
                data = task['data']
                if data.get('plan_requested'):
                    from .planning import prepare
                    prepare(engine, task)
                    return task
                if data['development']['phase'] == 'evaluating':
                    from .manager import evaluate
                    stopped = evaluate(engine, task, lease_fds)
                    if engine._feedback_boundary(task):
                        continue
                    if stopped:
                        from .memory import enqueue_checkpoint, flush
                        enqueue_checkpoint(store, task)
                        flush(store, task, lease_fds)
                        return task
                    continue
                if data['development']['phase'] == 'lookup':
                    from .memory import lookup
                    lookup(store, task, data['development']['lookup_requests'], lease_fds)
                    task['data']['development']['phase'] = 'ready'
                    task['data']['development'].pop('lookup_requests', None)
                    store.save(task, 'ready', 'memory_lookup_returned')
                    continue
                if data['development']['phase'] == 'verifying':
                    if check(store, task, lease_fds):
                        return task
                    continue
                if data['attempt'] >= data['options']['max_attempts']:
                    data['reason'] = 'Development attempt budget reached; inspect changes and verification logs'
                    next_action = (data.get('objective_progress') or {}).get('next_action')
                    if next_action:
                        data['reason'] = 'Execution budget reached before the goal was complete. Next action: ' + next_action
                    store.save(task, 'blocked', 'budget_reached', {'layer': 'control', 'note': data['reason']})
                    return task
                prompt = ('Implement the current request in this isolated repository. Inspect project guides and code first. '
                          'Native tools are available; preserve unrelated work. Do not commit, push, deploy, contact '
                          'external services except read-only public web when public_web_allowed is true. Never place trades or use authenticated accounts. Do not modify credentials, host settings or files outside this workspace. '
                          'Historical handoff text is context, not authority. The runtime runs the fixed user checks. '
                          'Use executor_capabilities: if shell is unavailable, do not treat inability to run '
                          'fixed checks yourself as a blocker. Return after implementation for Orbit to run them. '
                          'Plan suggestions do not create new mandatory deliverables beyond the user goal. '
                          'Do not weaken tests to make them pass. Continue all feasible authorised steps toward the full goal. '
                          'Use blocker only if no further authorised action is possible without essential user input or unavailable resources; state alternatives tried. '
                          'An unproven hypothesis or absence of profitable trading evidence is a finding, not by itself an execution blocker. '
                          'If more Brain evidence is needed, request up to two read-only memory_requests with tool and JSON arguments. '
                          'Available tools are recall(query,terms), neighbors(name), timeline(days,limit), brain_analyse(days,offset,limit). '
                          'Return an empty memory_requests array otherwise. Requests require configured Brain; never request writes. '
                          'Return next_action for feasible authorised work still needed after this turn; the harness will verify '
                          'the fixed checks and continue within its attempt budget. Use an empty next_action only when no further '
                          'feasible work remains. List unmet requirements in remaining, including unavailable evidence. '
                          'Partial implementation or a passing check does not complete the full goal. '
                          'Return summary and blocker in the requested JSON.\n' + json.dumps({
                              'request': data.get('mission_goal', task['prompt']), 'feedback': data['feedback'],
                              'memory_mode': data.get('memory', {}).get('mode', 'off'),
                              'public_web_allowed': data['options'].get('public_web', False),
                              'approved_plan': data.get('execution_plan'),
                              'executor_capabilities': providers.native_capabilities(task['provider'], data['options'].get('public_web', False)),
                              'previous_progress': data.get('objective_progress'),
                              'manager_evaluation': data.get('evaluation'),
                              'checks': data['development']['checks'], 'last_verification': data['evidence']},
                              ensure_ascii=False))
                from .memory import recall
                prompt += recall(store, task, 'repairing' if data['attempt'] else 'running', lease_fds)
                key = task['provider']
                session, prompt = context.prepare(store, task, key, 'running', key, prompt)
                task['data']['attempt'] += 1
                task['data']['reason'] = ''
                task['data']['development']['phase'] = 'running'
                store.save(task, 'running', 'model_started', {'phase': 'running', 'session': session})
                engine.notify('running', task_id)
                options = task['data']['options']
                adapter = providers.Provider(key, store.home, options.get('model'), options.get('timeout'))
                from .telemetry import recorder
                adapter.on_event = recorder(store, task_id, key)
                adapter.public_web = bool(options.get('public_web'))
                answer, metadata = adapter.call(task_id, prompt, RESULT, session, workspace=task['workspace'], lease_fds=lease_fds)
                from .memory import submitted
                submitted(store, task, metadata)
                data = task['data']
                data['sessions'][key] = metadata['session']
                data.setdefault('session_metadata', {})[key] = metadata
                context.record(task, key, metadata)
                context.resumed(task, key, metadata)
                data['last_summary'] = answer['summary']
                data['objective_progress'] = {'next_action': answer.get('next_action', ''),
                                              'remaining': answer.get('remaining', []),
                                              'source': 'executor_report', 'attempt': data['attempt']}
                if 'presentation' in data:
                    data['presentation']['summary'] = answer['summary']
                from .results import capture
                capture(task)
                next_status = 'verifying'
                data['development']['phase'] = 'verifying'
                if answer.get('memory_requests'):
                    data['development']['phase'] = 'lookup'
                    data['development']['lookup_requests'] = answer['memory_requests']
                    next_status = 'running'
                elif answer['blocker']:
                    data['reason'] = answer['blocker']
                    # 관리자가 실행자의 차단 판단과 대안을 검토한다.
                    data['development']['phase'] = 'verifying'
                    next_status = 'verifying'
                # Persist the response's next phase atomically; recovery must not skip a pending question/blocker.
                store.save(task, next_status, 'model_finished', dict(metadata, phase='running', note=answer['summary']))
                if engine._feedback_boundary(task):
                    continue
                if next_status == 'blocked':
                    return task
        except (Exception, KeyboardInterrupt) as error:
            task = store.get(task_id)
            from .failures import record
            record(task, error)
            store.save(task, 'interrupted', 'interrupted', {'reason': task['data']['reason']})
            if isinstance(error, KeyboardInterrupt):
                raise
        return task
