"""Repository execution in private clones with optional, fixed user-owned checks."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import uuid

from . import context, providers


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


def create(store, root, provider, prompt, checks, done=None, max_turns=8):
    root = Path(root).expanduser().resolve()
    if len(checks) > 12:
        raise ValueError('At most 12 explicit --check JSON argument arrays are supported')
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
        'authority': 'Explicit task creation; local isolated edits and optional user-owned checks only'}
    store.save(task, 'received', 'development_created', task['data']['development'])
    task['data']['options'] = {'model': None}
    from .simple_loop import initialize
    initialize(store, task, done, max_turns)
    return task


def check(store, task, lease_fds=(), simplified=False):
    """Run only the original user-supplied argv, never a command proposed by the model."""
    dev = task['data']['development']
    if task['data'].get('mode') == 'attached':
        if not dev.get('base'):
            dev['base'] = git(task['workspace'], 'rev-parse', 'HEAD').decode().strip()
    elif git(task['workspace'], 'rev-parse', 'HEAD').decode().strip() != dev['base']:
        raise ValueError('Isolated repository HEAD changed; inspect it before continuing')
    from . import recovery
    from .telemetry import recorder
    emit = recorder(store, task['id'], task['provider'])
    directory = store.home / 'runs' / task['id']
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    tree = fingerprint(task['workspace'])
    progress = task['data'].get('check_progress') or {'tree': tree, 'results': []}
    active = task['data'].get('active_check')
    saved = None
    if active:
        receipt = Path(active['receipt'])
        receipt.resolve().relative_to(directory.resolve())
        saved = json.loads(receipt.read_text())
        if saved['task_id'] != task['id'] or saved['argv'] != dev['checks'][saved['index']]:
            raise ValueError('Verification receipt does not match the fixed command')
        if saved['status'] != 'returned' and recovery.process_alive(saved):
            task['data'].update(reason='The original verification command is still running; waiting without replay.', reason_code='recovering')
            store.save(task, 'checking', 'check_recovery_waiting')
            return False
    if progress['tree'] != tree and not (saved and saved.get('tree_after') == tree):
        progress = {'tree': tree, 'results': []}
        saved = None
    results = list(progress['results'])
    for index, argv in enumerate(task['data']['development']['checks']):
        if index < len(results):
            if results[index]['argv'] != argv:
                raise ValueError('Saved verification result changed its command')
            continue
        if not saved or saved['index'] != index or saved.get('status') != 'returned' or saved.get('tree_after') != fingerprint(task['workspace']):
            call_id = uuid.uuid4().hex
            path = directory / (call_id + '.check.log')
            receipt = directory / (call_id + '.check.receipt.json')
            saved = {'task_id': task['id'], 'call_id': call_id, 'receipt': str(receipt), 'provider': 'check',
                     'argv': argv, 'index': index, 'status': 'prepared', 'process': None, 'exit_code': None,
                     'log': str(path), 'seconds': None}
            recovery.atomic_json(receipt, saved)
            task['data']['active_check'] = {'receipt': str(receipt), 'index': index}
            task['data']['check_progress'] = progress
            store.save(task, 'checking', 'check_started', {'index': index + 1, 'layer': 'evidence'})
            with path.open('wb') as output:
                os.chmod(path, 0o600)
                runner = [__import__('sys').executable, str(Path(__file__).with_name('native_runner.py')), str(receipt), *argv]
                process = subprocess.Popen(runner, cwd=task['workspace'], stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=lease_fds)
                try:
                    from .process_health import Monitor
                    monitor = Monitor(process, path, None, emit, call_id)
                    monitor.sample(force=True)
                    while process.poll() is None:
                        try:
                            process.wait(timeout=.25)
                        except subprocess.TimeoutExpired:
                            monitor.sample()
                    monitor.sample(force=True)
                except BaseException:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=3)
                    except ProcessLookupError:
                        pass
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise
            saved = json.loads(receipt.read_text())
            if saved.get('status') != 'returned':
                raise ValueError('Verification supervisor did not record an exit receipt')
        path = Path(saved['log'])
        with path.open('rb') as output:
            output.seek(max(0, path.stat().st_size - 8000))
            tail = output.read().decode('utf-8', 'replace')
        code = saved['exit_code']
        results.append({'path': 'command:' + str(index + 1), 'kind': 'command', 'argv': argv,
                        'passed': code == 0, 'exit_code': code, 'timed_out': False,
                        'log': str(path), 'output_tail': tail})
        progress = {'tree': fingerprint(task['workspace']), 'results': results}
        task['data']['check_progress'] = progress
        task['data'].pop('active_check', None)
        store.save(task, 'checking', 'check_finished', {'index': index + 1, 'exit_code': code,
                   'seconds': saved.get('seconds'), 'layer': 'evidence'})
        saved = None
    dev = task['data']['development']
    changed = git(task['workspace'], 'diff', '--no-ext-diff', '--no-textconv', '--name-only', '-z', dev['base'], '--')
    untracked = git(task['workspace'], 'ls-files', '--others', '--exclude-standard', '-z')
    dev['changed_paths'] = sorted({os.fsdecode(p) for p in (changed + untracked).split(b'\0') if p})
    patch = directory / (uuid.uuid4().hex + '.tracked.patch')
    patch.write_bytes(git(task['workspace'], 'diff', '--no-ext-diff', '--no-textconv', '--binary', dev['base'], '--'))
    os.chmod(patch, 0o600)
    dev['tracked_patch'] = str(patch)
    dev['untracked_paths'] = [os.fsdecode(p) for p in untracked.split(b'\0') if p]
    task['data'].pop('check_progress', None)
    task['data'].pop('active_check', None)
    task['data']['evidence'] = results
    task['data']['development']['verified_tree'] = fingerprint(task['workspace'])
    passed = all(r['passed'] for r in results)
    task['data']['development']['phase'] = 'checking'
    store.save(task, 'checking', 'checks_recorded', {'checks': results, 'passed': passed})
    return True
