"""Attach Orbit to the user's own Claude Code or Codex session.

One task record ("ledger") per project at a time. Host hooks (SessionStart · UserPromptSubmit · Stop · PreCompact) read the
current task and return a short brief; the executor is the user's own session, so Orbit never calls a model here
except for the one independent review after a completion claim. Nothing in this module changes task files.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

from .store import now

DEFAULT_KEY = 'auto'            # preference: 'on' | 'off'  (global default)
PROJECT_PREFIX = 'project:'     # preference per project root: 'on' | 'off'
HOOK_MARK = 'cosmos-orbit hook'  # identifies Orbit entries inside host hook files
HOOK_EVENTS = (('SessionStart', 'session-start', 20), ('UserPromptSubmit', 'prompt', 20), ('Stop', 'stop', 600),
               ('PreCompact', 'pre-compact', 30))
HOST_EVENTS = {'claude': HOOK_EVENTS + (('SessionEnd', 'session-end', 10),), 'codex': HOOK_EVENTS}
CLOSED = ('verified', 'stalled', 'interrupted')
CLAIM_WORDS = re.compile(r'(완료|끝났|마쳤|구현했|수정했|반영했|\bdone\b|completed|finished|implemented|all set|\bfixed\b|verified)', re.I)
BRIEF_LIMIT = 2400
MIN_TASK_PROMPT = 20


# ---------------------------------------------------------------- settings

def project_root(path=None):
    """Git top level when inside a repository, otherwise the directory itself."""
    path = Path(path or os.getcwd()).expanduser().resolve()
    try:
        result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve(), True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return path, False


def normalise(value):
    value = str(value or '').strip().lower()
    if value in ('on', '1', 'true', 'yes'):
        return 'on'
    if value in ('off', '0', 'false', 'no'):
        return 'off'
    return None


def resolve(store, root):
    """Winner first: ORBIT environment → project setting → global default → off."""
    root = Path(root).resolve()
    env = normalise(os.environ.get('ORBIT') or os.environ.get('COSMOS_ORBIT'))
    if env:
        return env, 'environment'
    value = normalise(store.preference(PROJECT_PREFIX + str(root)))
    if value:
        return value, 'project'
    value = normalise(store.preference(DEFAULT_KEY))
    if value:
        return value, 'default'
    return 'off', 'unset'


def set_switch(store, value, path=None, global_default=False):
    value = normalise(value)
    if value is None:
        raise ValueError('Use on or off')
    if global_default:
        store.preference(DEFAULT_KEY, value)
        return {'scope': 'default', 'value': value}
    root, _ = project_root(path)
    store.preference(PROJECT_PREFIX + str(root), value)
    return {'scope': 'project', 'root': str(root), 'value': value}


def settings_view(store, path=None):
    root, is_git = project_root(path)
    value, source = resolve(store, root)
    projects = {}
    for row in store.db.execute("SELECT key, value FROM preferences WHERE key LIKE ?", (PROJECT_PREFIX + '%',)):
        projects[row[0][len(PROJECT_PREFIX):]] = row[1]
    current = store.attached(str(root))
    return {'home': str(store.home), 'hooks': {host: hook_status(host) for host in ('claude', 'codex')},
            'default': normalise(store.preference(DEFAULT_KEY)) or 'off', 'projects': projects,
            'here': {'root': str(root), 'git': is_git, 'orbit': value, 'source': source,
                     'task': current, 'effective': value == 'on' and is_git},
            'environment': os.environ.get('ORBIT') or os.environ.get('COSMOS_ORBIT'),
            'handoff': __import__('harness.continuity', fromlist=['threshold_view']).threshold_view(store, root)}


# ---------------------------------------------------------------- host hooks

def host_hook_file(host):
    if host == 'claude':
        return Path(os.environ.get('CLAUDE_CONFIG_DIR', '~/.claude')).expanduser() / 'settings.json'
    if host == 'codex':
        return Path(os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'hooks.json'
    raise ValueError('Host must be claude or codex')


def _same_code(found):
    """True when an installed console script imports exactly this package (compared by file content)."""
    try:
        target = Path(found).resolve()
        mine = Path(__file__).resolve()
        for candidate in target.parent.parent.glob('lib/python*/site-packages/harness/attach.py'):
            if candidate.resolve() == mine or candidate.read_bytes() == mine.read_bytes():
                return True
    except OSError:
        pass
    return False


def launcher():
    """The shortest command that runs this exact code: `cosmos-orbit` on PATH when it is current, else the checkout.

    An installed launcher may carry older code than the checkout the hooks run from (seen 2026-11-03: the
    executor was told to call `cosmos-orbit done`, the installed copy did not know the command)."""
    found = shutil.which('cosmos-orbit')
    if found and _same_code(found):
        return ['cosmos-orbit']
    checkout = Path(__file__).resolve().parents[1] / 'bin' / 'cosmos-orbit'
    if checkout.is_file():
        return [sys.executable, str(checkout)]
    if found:
        return [found]
    return [sys.executable, '-m', 'harness.cli']


def orbit_command(store=None):
    """How the executor should call Orbit from its shell: --home whenever the store is not the default.

    Always spelled out, never left to HARNESS_HOME: a sandboxed executor shell (Codex) does not inherit the
    environment the hooks ran with (seen 2026-11-03)."""
    parts = [shlex.quote(p) for p in launcher()]
    if store is not None:
        default = Path('~/.local/share/local-harness').expanduser().resolve()
        if store.home != default:
            parts += ['--home', shlex.quote(str(store.home))]
    return ' '.join(parts)


def hook_launcher():
    """Hooks need an absolute command: the installed script when it carries this code, else this checkout."""
    found = shutil.which('cosmos-orbit')
    if found and _same_code(found):
        return [found]
    checkout = Path(__file__).resolve().parents[1] / 'bin' / 'cosmos-orbit'
    return [sys.executable, str(checkout)] if checkout.is_file() else [sys.executable, '-m', 'harness.cli']


def hook_entries(host, home=None):
    base = ' '.join(shlex.quote(p) for p in hook_launcher())
    if home:
        base += ' --home ' + shlex.quote(str(home))
    entries = {}
    for event, name, timeout in HOST_EVENTS.get(host, HOOK_EVENTS):
        entries[event] = {'matcher': '', 'hooks': [{'type': 'command', 'timeout': timeout, 'statusMessage': 'Orbit',
                                                    'command': base + ' hook ' + name + ' --host ' + host}]}
    return entries


def _is_ours(entry):
    return any(HOOK_MARK in str(hook.get('command', '')) for hook in (entry.get('hooks') or []) if isinstance(hook, dict))


def _read(path):
    if not path.is_file():
        return {}
    with path.open(encoding='utf-8') as handle:
        raw = handle.read(2000001)
    if len(raw) > 2000000:
        raise ValueError('Host hook file is too large to edit safely: ' + str(path))
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise ValueError('Host hook file must contain a JSON object: ' + str(path))
    return data


def _write(path, data, backup=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.is_file():
        shutil.copy2(path, path.with_name(path.name + '.bak-orbit-' + time.strftime('%Y%m%d-%H%M%S')))
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
    os.replace(temporary, path)


def install_hooks(host, home=None):
    path = host_hook_file(host)
    data = _read(path)
    hooks = data.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('hooks must be an object in ' + str(path))
    wanted = hook_entries(host, home)
    for event, entry in wanted.items():
        current = [e for e in (hooks.get(event) or []) if isinstance(e, dict) and not _is_ours(e)]
        current.append(entry)
        hooks[event] = current
    _write(path, data)
    return {'host': host, 'file': str(path), 'installed': True, 'events': list(wanted)}


def uninstall_hooks(host):
    path = host_hook_file(host)
    data = _read(path)
    hooks = data.get('hooks') or {}
    removed = 0
    for event in list(hooks):
        kept = [e for e in hooks[event] if not (isinstance(e, dict) and _is_ours(e))]
        removed += len(hooks[event]) - len(kept)
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    if removed:
        _write(path, data)
    return {'host': host, 'file': str(path), 'removed': removed}


def hook_status(host):
    path = host_hook_file(host)
    try:
        hooks = _read(path).get('hooks') or {}
    except ValueError as error:
        return {'file': str(path), 'installed': False, 'error': str(error)}
    wanted = HOST_EVENTS.get(host, HOOK_EVENTS)
    present = [event for event, _, _ in wanted if any(isinstance(e, dict) and _is_ours(e) for e in hooks.get(event) or [])]
    return {'file': str(path), 'installed': len(present) == len(wanted), 'events': present,
            'cli': bool(shutil.which(host))}


# ---------------------------------------------------------------- task record

def current_task(store, root):
    root = Path(root).resolve()
    task_id = store.attached(str(root))
    if not task_id:
        return None
    try:
        task = store.get(task_id)
    except ValueError:
        store.detach(str(root))
        return None
    if task['data'].get('mode') != 'attached':
        return None
    return task


def resolve_task(store, root, session, source=None):
    """The task this session works on. Several sessions can be open in one project at once, and a new session
    (a new terminal, or /clear) does not say which session it follows. So a session inherits a task only when
    that is unambiguous: a handoff checkpoint invited the next session (one taker), or nobody else is working
    on the project's latest task (its sessions ended — Claude Code reports SessionEnd)."""
    root = Path(root).resolve()
    if not session:
        return current_task(store, root)
    row = store.session_task(session)
    if row:
        try:
            task = store.get(row['task_id'])
            if task['data'].get('mode') == 'attached' and Path(task['workspace']).resolve() == root:
                if row.get('ended'):
                    store.bind_session(session, task['id'], root)  # it came back (resume): live again, nobody else takes it
                return task
        except ValueError:
            pass
    for item in store.list():
        # Tasks from before session binding list their sessions in the ledger: a member finds its own task.
        if Path(item['workspace']).resolve() != root or item['status'] in CLOSED:
            continue
        task = store.get(item['id'])
        if task['data'].get('mode') == 'attached' and session in (task['data'].get('session_ids') or []) \
                and not store.task_sessions(task['id']):
            store.bind_session(session, task['id'], root)
            return task
    invited = []
    for item in store.list():
        if Path(item['workspace']).resolve() != root or item['status'] in CLOSED:
            continue
        task = store.get(item['id'])
        state = task['data'].get('handoff_state') or {}
        if task['data'].get('mode') == 'attached' and state.get('state') in ('checkpointed', 'told'):
            invited.append((state.get('at') or task['updated'], task))
    for _, candidate in sorted(invited, key=lambda pair: pair[0], reverse=True):
        try:
            with store.lock('task:' + candidate['id']):
                task = store.get(candidate['id'])
                state = task['data'].get('handoff_state') or {}
                if state.get('state') not in ('checkpointed', 'told'):
                    continue                             # another new session took this invitation first
                state.update(state='adopted', adopted_by=session)
                store.save(task, task['status'], 'handoff_adopted', {'session': session, 'layer': 'context'})
                store.bind_session(session, task['id'], root)
                return store.get(task['id'])
        except (RuntimeError, ValueError):
            continue                                     # busy: never adopt without the lock
    latest = current_task(store, root)
    if latest is None:
        return None
    owners = store.task_sessions(latest['id'])
    if not owners:
        prompts = latest['data'].get('prompts') or []
        creator = next((p.get('session') for p in prompts if p.get('session')), None)
        if creator and creator != session:
            store.bind_session(creator, latest['id'], root)  # tasks from before session binding: the creator owns it
            owners = store.task_sessions(latest['id'])
    if not [r for r in owners if not r['ended'] and r['session'] != session]:
        store.bind_session(session, latest['id'], root)
        return latest
    return None  # another live session owns the project's latest task; this one starts its own


def _head(root):
    try:
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def parse_checks(values):
    commands = []
    for value in values or []:
        argv = json.loads(value) if isinstance(value, str) else value
        if (not isinstance(argv, list) or not argv or len(argv) > 100
                or any(not isinstance(v, str) or '\0' in v or len(v) > 8000 for v in argv) or not argv[0].strip()):
            raise ValueError('Each --check must be a nonempty JSON array of command arguments')
        commands.append(argv)
    if len(commands) > 12:
        raise ValueError('At most 12 fixed checks are supported')
    return commands


def create(store, root, prompt, host, session=None, checks=(), done=None, max_turns=8):
    """Create the ledger for a session that already exists; no clone, no model call."""
    if host not in ('codex', 'claude'):
        raise ValueError('Host must be claude or codex')
    root, is_git = project_root(root)
    if not is_git:
        raise ValueError('Orbit attaches to a Git repository root; initialise one first: ' + str(root))
    prompt = (prompt or '').strip()
    if not prompt or len(prompt) > 24000:
        raise ValueError('Supply a request of 1–24000 characters')
    commands = parse_checks(checks)
    previous = store.attached(str(root))
    task = store.create(root, host, prompt)
    data = task['data']
    data['development'] = {'source': str(root), 'base': _head(root), 'checks': commands, 'phase': 'ready',
                           'verified_tree': None,
                           'authority': "Attached to the user's own CLI session; Orbit runs the fixed checks and one independent review only"}
    data['options'] = {'model': None}
    data.update(mode='attached', host=host, done_evidence={}, prompts=[{'at': now(), 'text': prompt[:600], 'session': session}],
                briefs={'served': 0, 'last_at': None}, verification_pending=False,
                session_ids=[session] if session else [], attached_at=now(), previous_task=previous)
    store.save(task, 'received', 'options_recorded', data['options'])
    from .simple_loop import initialize
    initialize(store, task, done, max_turns)
    task['data']['development']['phase'] = 'ready'
    store.save(task, 'working', 'attached', {'workspace': str(root), 'host': host, 'session': session, 'checks': len(commands)})
    store.attach(str(root), task['id'])
    if session:
        store.bind_session(session, task['id'], root)
    return task


def detach(store, root=None, task_id=None):
    if task_id:
        task = store.get(task_id)
        root = Path(task['workspace'])
    else:
        root, _ = project_root(root)
        task = current_task(store, root)
    store.detach(str(root))
    if task and task['status'] not in CLOSED:
        with store.lock('task:' + task['id']):
            task = store.get(task['id'])
            task['data'].update(reason='Detached by the user; the ledger is kept.', reason_code='detached', verification_pending=False)
            task['data']['development']['phase'] = 'ready'
            store.save(task, 'stalled', 'detached', {'workspace': str(root)})
    return {'workspace': str(root), 'task': task['id'] if task else None}


def record_prompt(store, task, text, session=None):
    """Keep the conversation in the ledger. A requirement change binds the review and must revise the goals;
    a question or a go-ahead is only recorded. If the worker owns the task this instant, the prompt is queued."""
    text = (text or '').strip()
    if not text:
        return task
    try:
        with store.lock('task:' + task['id']):
            task = store.get(task['id'])
            _apply_prompt(store, task, text, session)
    except RuntimeError:
        store.enqueue(task['id'], text, None)  # applied by drain_inbox at the next hook, claim or review end
    return store.get(task['id'])


def _apply_prompt(store, task, text, session=None):
    from . import requirements
    data = task['data']
    prompts = data.setdefault('prompts', [])
    kind = requirements.classify(text)
    if kind == 'system':
        return  # a host notification, not the user's words
    prompts.append({'at': now(), 'text': text[:600], 'session': session, 'kind': kind})
    del prompts[:-30]
    if session:
        sessions = data.setdefault('session_ids', [])
        if session not in sessions:
            sessions.append(session)
            del sessions[:-20]
    if kind == 'requirement' or task['status'] == 'needs_input':
        data.setdefault('feedback', []).append({'text': text[:4000], 'revision': task['revision'], 'message_id': None,
                                                 'at': now(), 'source': 'prompt', 'kind': kind})
    status = task['status']
    if task['status'] == 'needs_input':
        data.update(reason='', reason_code='', verification_pending=False)
        data['development']['phase'] = 'ready'
        data['resumes'].append({'turn': data.get('turns', 0), 'because': 'feedback', 'sent': [text[:600]]})
        status = 'working'
    if kind == 'requirement' and data.get('done_fixed'):
        requirements.mark_pending(data, text, 'prompt')
        store.save(task, status, 'requirement_added', {'chars': len(text), 'session': session, 'rev': data.get('done_rev') or 0,
                                                       'text': text[:200], 'layer': 'control'})
    elif status != task['status']:
        store.save(task, status, 'feedback_applied', {'source': 'prompt', 'chars': len(text)})
    else:
        store.save(task, status, 'prompt_recorded', {'chars': len(text), 'session': session, 'kind': kind})


def drain_inbox(store, task):
    """Apply prompts that arrived while the task was busy, in order. Returns the fresh task."""
    queued = store.inbox(task['id'])
    if not queued:
        return task
    try:
        with store.lock('task:' + task['id']):
            current = store.get(task['id'])
            for message in queued:
                _apply_prompt(store, current, message['text'], None)
                current = store.get(task['id'])
            with store.db:
                for message in queued:
                    store.db.execute('UPDATE inbox SET consumed=1 WHERE id=? AND task_id=?', (message['id'], task['id']))
            return store.get(task['id'])
    except RuntimeError:
        return task


def cli_feedback(store, task_id, text):
    """A correction typed on the command line. In an attached session the executor's own shell can run this,
    so it is recorded as source 'cli' and the reviewer weighs it as a claim; the user's prompts are source 'prompt'."""
    text = (text or '').strip()
    if not text or len(text) > 24000:
        raise ValueError('Feedback must be nonempty and at most 24000 characters')
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        data = task['data']
        data.setdefault('feedback', []).append({'text': text[:4000], 'revision': task['revision'], 'message_id': None,
                                                 'at': now(), 'source': 'cli'})
        status = task['status']
        if status == 'needs_input':
            data.update(reason='', reason_code='', verification_pending=False)
            data['development']['phase'] = 'ready'
            status = 'working'
        store.save(task, status, 'feedback_applied', {'source': 'cli', 'chars': len(text)})
    return store.get(task_id)


def set_done(store, task_id, texts, origin='executor', add=False, keep=False, checks=()):
    """Write a revision of the result goals (D items) and verification goals (fixed checks, append-only)."""
    from . import requirements
    commands = parse_checks(checks)
    task = drain_inbox(store, store.get(task_id))
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        data = task['data']
        if data.get('mode') != 'attached':
            raise ValueError('done applies to attached tasks; use feedback --done for managed tasks')
        if task['status'] in CLOSED:
            raise ValueError('This task is closed (' + task['status'] + '); attach a new request first')
        row = requirements.revise(data, list(texts), origin, add=add, keep=keep, checks=commands)
        if commands:
            data['development']['verified_tree'] = None  # new checks run at the next claim
        kind = 'done_kept' if row['kept'] else 'goal_fixed'
        store.save(task, task['status'], kind, {'done': data['done'], 'origin': origin, 'rev': row['rev'],
                                                'added': row['added'], 'checks_added': row['checks_added'], 'because': row['because'][:200]})
    return store.get(task_id)


def record_evidence(store, task_id, item_id, note=None, command=None, file=None, by=None):
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        data = task['data']
        if data.get('mode') != 'attached':
            raise ValueError('evidence applies to attached tasks')
        ids = [d['id'] for d in data.get('done') or []]
        if item_id not in ids:
            raise ValueError('Unknown completion item ' + item_id + '; known: ' + ', '.join(ids))
        if item_id.startswith('C'):
            raise ValueError('Fixed checks are run by Orbit; record evidence for D items or G0')
        entry = {'at': now(), 'kind': 'note', 'note': (note or '')[:2000], 'by': by or data.get('host')}
        root = Path(task['workspace']).resolve()
        if command:
            argv = parse_checks([command])[0]
            started = time.monotonic()
            try:
                result = subprocess.run(argv, cwd=str(root), stdin=subprocess.DEVNULL, capture_output=True)
                output = (result.stdout + result.stderr).decode('utf-8', 'replace')
                code = result.returncode
            except OSError as error:
                output, code = str(error), 127
            entry.update(kind='command', argv=argv, exit_code=code, output_tail=output[-4000:],
                         seconds=round(time.monotonic() - started, 3))
        if file:
            target = (root / file).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                raise ValueError('Evidence file must be a regular file inside the workspace: ' + file)
            digest = hashlib.sha256()
            size = 0
            with target.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    digest.update(chunk)
                    size += len(chunk)
            entry.update(kind='file' if entry['kind'] == 'note' else entry['kind'], path=str(target.relative_to(root)),
                         sha256=digest.hexdigest(), size=size)
        if entry['kind'] == 'note' and not entry['note']:
            raise ValueError('Give a note, a command or a file')
        rows = data.setdefault('done_evidence', {}).setdefault(item_id, [])
        rows.append(entry)
        del rows[:-20]
        ok = entry.get('exit_code') in (None, 0)
        store.save(task, task['status'], 'evidence_recorded',
                   {'id': item_id, 'kind': entry['kind'], 'ok': ok, 'exit_code': entry.get('exit_code'), 'layer': 'evidence'})
    return entry


def claim(store, task_id, summary='', remaining=(), question=''):
    """The executor's completion claim. Checks run now; the independent review is queued (or run with wait)."""
    from .simple_loop import stop_for_input
    drain_inbox(store, store.get(task_id))
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        data = task['data']
        if data.get('mode') != 'attached':
            raise ValueError('claim applies to attached tasks')
        if task['status'] in CLOSED:
            raise ValueError('This task is closed (' + task['status'] + '); attach a new request first')
        remaining = [r.strip() for r in remaining if r and r.strip()]
        pending = data.get('requirements_pending')
        if pending and not remaining and not question.strip():
            raise ValueError('The request changed after completion definitions rev %s: "%s". Revise the result and verification goals '
                             'first (cosmos-orbit done %s --add \'…\' [--check …]) or confirm they still cover it (cosmos-orbit done %s --keep).'
                             % (pending.get('rev') or 0, pending['text'][:200], task_id, task_id))
        answer = {'summary': (summary or '')[:4000], 'finished': not remaining and not question.strip(),
                  'remaining': remaining, 'question': question.strip(), 'done_proposed': []}
        data['executor_result'] = answer
        data['claimed_at'] = now()
        data['last_summary'] = answer['summary']
        data.setdefault('presentation', {})['summary'] = answer['summary']
        data['turns'] = data.get('turns', 0) + 1
        data['attempt'] = data['turns']
        data.update(reason='', reason_code='')
        if answer['question']:
            data['verification_pending'] = False
            data['objective_progress'] = {'remaining': remaining, 'next_action': answer['question'], 'source': 'executor_report'}
            store.save(task, task['status'], 'claimed', {'finished': False, 'question': True})
            stop_for_input(store, task, answer['question'])
            return task
        if not answer['finished']:
            data['verification_pending'] = False
            data['development']['phase'] = 'ready'
            data['objective_progress'] = {'remaining': remaining, 'next_action': '\n'.join(remaining), 'source': 'executor_report'}
            store.save(task, 'working', 'claimed', {'finished': False, 'remaining': remaining})
            return task
        data['verification_pending'] = True
        data['objective_progress'] = {'remaining': [], 'next_action': '', 'source': 'executor_report'}
        data['development']['phase'] = 'checking'
        data.pop('verification_only', None)
        data.pop('retry_at', None)
        data['retry_count'] = 0
        store.save(task, 'checking', 'claimed', {'finished': True, 'turn': data['turns']})
    return task


def run_verification(engine, task_id, checks_only=False):
    """Fixed checks, then (unless checks_only) one fresh-context review. Callers hold no locks."""
    from . import recovery, verifier
    from .development import check
    from .simple_loop import stop_for_input
    store = engine.store
    initial = store.get(task_id)
    with store.lock('task:' + task_id) as task_lease, store.lock('workspace:' + initial['workspace']) as workspace_lease:
        leases = (task_lease, workspace_lease)
        task = store.get(task_id)
        data = task['data']
        if not data.get('verification_pending') or task['status'] in CLOSED or not recovery.due(task):
            return task
        try:
            from .development import evidence_current
            if data.get('checks_done_for') != data.get('turns') or not evidence_current(task):
                data['development']['phase'] = 'checking'
                data['checks_done_for'] = data.get('turns')
                store.save(task, 'checking', 'verification_requested', {'turn': data.get('turns', 0)})
                engine.notify('checking', task_id)
                check(store, task, leases, simplified=True)
                data = task['data']
            if not verifier.fixed_checks(task):
                failed = [row for row in data['evidence'] if not row['passed']]
                sent = ['Failed ' + row['path'] + ' (exit ' + str(row['exit_code']) + '): ' + ' '.join(row['argv']) for row in failed]
                sent.append('\n'.join(row['output_tail'] for row in failed)[-4000:])
                data['resumes'].append({'turn': data['turns'], 'because': 'checks_failed', 'sent': sent})
                data.update(verification_pending=False, reason='\n'.join(sent)[:2000], reason_code='checks_failed',
                            last_checks_failed_at=now())
                data['objective_progress'] = {'remaining': sent[:1], 'next_action': 'Fix the failing fixed check, then claim again.', 'source': 'runtime'}
                data['development']['phase'] = 'ready'
                data.pop('checks_done_for', None)
                store.save(task, 'working', 'resumed', {'because': 'checks_failed', 'sent': sent, 'turn': data['turns']})
                return task
            if checks_only:
                return task
            data['development']['phase'] = 'verifying'
            try:
                verdict = verifier.evaluate(engine, task, leases)
            except ValueError as error:
                task = store.get(task_id)
                data = task['data']
                data.update(verification_pending=False)
                data['development']['phase'] = 'ready'
                data.pop('checks_done_for', None)
                if data.get('requirements_pending') or 'changed during verification' in str(error):
                    # The binding moved under the review (a new requirement or correction): back to work, not a question.
                    data.update(reason='The request changed during the review; revise the goals and claim again.', reason_code='requirements_changed')
                    store.save(task, 'working', 'resumed', {'because': 'requirements_changed', 'turn': data.get('turns', 0)})
                    return task
                stop_for_input(store, task, str(error), data.get('reason_code') or 'verifier_rejected')
                return task
            data = task['data']
            data['development']['phase'] = 'ready'
            data.update(verification_pending=False)
            data.pop('checks_done_for', None)
            from .requirements import classify, mark_pending
            arrived = [m for m in store.inbox(task_id) if classify(m['text']) == 'requirement']
            if verdict['outcome'] == 'verified' and arrived:
                for message in store.inbox(task_id):
                    _apply_prompt(store, task, message['text'], None)
                    task = store.get(task_id)
                with store.db:
                    store.db.execute('UPDATE inbox SET consumed=1 WHERE task_id=?', (task_id,))
                data = task['data']
                data.update(reason='The request changed during the review; revise the goals and claim again.', reason_code='requirements_changed')
                store.save(task, 'working', 'resumed', {'because': 'requirements_changed', 'turn': data.get('turns', 0)})
                return task
            if verdict['outcome'] == 'verified':
                data.update(reason='', reason_code='')
                data['objective_progress'] = {'remaining': [], 'next_action': '', 'source': 'runtime'}
                store.save(task, 'verified', 'completion_verified', {'turn': data['turns']})
                engine.notify('verified', task_id)
                if data.get('memory', {}).get('mode') in ('brain', 'both'):
                    from .memory import enqueue_checkpoint, flush
                    enqueue_checkpoint(store, task)
                    flush(store, task, leases)
                return task
            if verdict['outcome'] == 'needs_input':
                stop_for_input(store, task, verdict['question'] or 'Review scope departure: ' + '; '.join(verdict['extra']),
                               'scope_drift' if verdict['extra'] else 'question')
                return task
            sent = [row['id'] + ': ' + row['status'] + ' ' + '; '.join(row['evidence'])
                    for row in verdict['items'] if row['status'] != 'met'] + verdict['missing'] + verdict['fix']
            data['resumes'].append({'turn': data['turns'], 'because': 'verdict', 'sent': sent})
            data.update(reason='\n'.join(sent)[:2000], reason_code='unmet')
            data['objective_progress'] = {'remaining': sent, 'next_action': 'Address the unmet items, record evidence, then claim again.', 'source': 'runtime'}
            store.save(task, 'working', 'resumed', {'because': 'verdict', 'sent': sent, 'turn': data['turns']})
            return task
        except recovery.ProviderFailure as error:
            task = store.get(task_id)
            if error.metadata:
                recovery.record_metadata(store, task, error.metadata, 'verifying')
            task['data'].pop('active_call', None)
            task['data']['development']['phase'] = 'checking'
            recovery.defer(store, task, error)
            if task['status'] == 'needs_input':
                task['data']['verification_pending'] = False
                store.save(task, 'needs_input', 'needs_input', {'question': str(error), 'reason': 'provider_failure'})
            return task
        except (ValueError, OSError) as error:
            task = store.get(task_id)
            task['data'].update(verification_pending=False)
            task['data']['development']['phase'] = 'ready'
            task['data'].pop('checks_done_for', None)
            stop_for_input(store, task, str(error), 'check_error')
            return task


# ---------------------------------------------------------------- brief

def _one_line(text, limit):
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:limit]


def _epoch(stamp):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _local(stamp, with_date=True):
    """Ledger stamps are UTC ISO; people read local wall-clock time."""
    import datetime
    try:
        moment = datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).astimezone()
        return moment.strftime('%Y-%m-%d %H:%M' if with_date else '%H:%M')
    except (ValueError, TypeError):
        return str(stamp or '')[:16]


def brief_text(store, task, event='prompt', for_digest=False):
    data = task['data']
    prefix = orbit_command(store)
    tid = task['id']
    last = (data.get('verdicts') or [None])[-1]
    statuses = {i['id']: i for i in (last or {}).get('items', [])}
    marks = {'met': '✓', 'unmet': '✗', 'unclear': '?'}
    head = '[orbit] task %s · attached %s · status %s' % (tid, _local(data.get('attached_at') or task['created']), task['status'])
    if data.get('reason_code'):
        head += ' (' + data['reason_code'] + ')'
    if data.get('verification_pending'):
        head += ' · independent verification pending'
    lines = [head, 'GOAL: ' + _one_line(data.get('goal') or task['prompt'], 400)]
    feedback = data.get('feedback') or []
    if feedback:
        lines.append('CORRECTIONS (%d): ' % len(feedback) + ' · '.join(
            _local(f.get('at'), False) + ' "' + _one_line(f['text'], 160) + '"' for f in feedback[-4:]))
    done = data.get('done') or []
    evidence = data.get('done_evidence') or {}
    checks = data.get('evidence') or []
    d_items = [d for d in done if not d['id'].startswith('C') and d['id'] != 'G0']
    if d_items:
        parts = []
        for d in done:
            if d['id'] == 'G0' and d['id'] not in statuses:
                continue
            if d['id'].startswith('C'):
                index = int(d['how'].split(':')[1]) - 1
                row = checks[index] if index < len(checks) else None
                argv = data['development']['checks'][index]
                state = ('✓' if row['passed'] else '✗ exit %s' % row['exit_code']) if row else '○ not run'
                parts.append('%s %s %s' % (d['id'], state, ' '.join(argv)[:80]))
                continue
            verdict = statuses.get(d['id'])
            mark = marks.get(verdict['status'], '○') if verdict else '○'
            count = len(evidence.get(d['id']) or [])
            parts.append('%s %s%s %s' % (d['id'], mark, ' evidence %d' % count if count else '', _one_line(d['text'], 140)))
        lines.append(('DONE (rev %s): ' % data['done_rev'] if data.get('done_rev') else 'DONE: ') + ' · '.join(parts))
    else:
        lines.append("DONE: none yet — propose 1–12 completion definitions: %s done %s 'D1 …' 'D2 …' [--check '[\"…\"]']" % (prefix, tid))
    from .requirements import pending_line
    changed = pending_line(data, prefix, tid)
    if changed and task['status'] not in CLOSED:
        lines.append(changed)
    if last:
        unmet = ['%s %s — %s' % (i['id'], i['status'], _one_line('; '.join(i.get('evidence') or []), 200))
                 for i in last.get('items', []) if i.get('status') != 'met']
        verdict_line = 'LAST VERDICT (turn %s): %s' % (last.get('turn'), last.get('outcome'))
        if unmet:
            verdict_line += ' · ' + ' · '.join(unmet)
        for key in ('extra', 'missing', 'fix'):
            if last.get(key):
                verdict_line += ' · %s: %s' % (key, _one_line(' | '.join(last[key]), 300))
        if last.get('question'):
            verdict_line += ' · question: ' + _one_line(last['question'], 300)
        lines.append(verdict_line)
    if task['status'] == 'needs_input' and data.get('reason'):
        lines.append('WAITING FOR THE USER: ' + _one_line(data['reason'], 400))
    elif data.get('reason_code') == 'checks_failed':
        lines.append('FIXED CHECK FAILED: ' + _one_line(data.get('reason'), 600))
    elif data.get('reason_code') == 'unmet':
        lines.append('UNMET (independent review): ' + _one_line(data.get('reason'), 600))
    remaining = (data.get('executor_result') or {}).get('remaining') or []
    if remaining and task['status'] == 'working' and data.get('reason_code') not in ('checks_failed', 'unmet'):
        lines.append('REMAINING (your last claim): ' + _one_line(' · '.join(remaining), 400))
    if event == 'session-start' and len(data.get('prompts') or []) > 1:
        lines.append('RECENT PROMPTS: ' + ' · '.join('"' + _one_line(p['text'], 100) + '"' for p in data['prompts'][-3:]))
    from .continuity import handoff_lines
    continuity = handoff_lines(data, prefix, tid, closed=task['status'] in CLOSED)
    if for_digest:
        state = (data.get('handoff_state') or {})
        continuity = [line.split(':')[0] for line in continuity] + [str(state.get('session')) + str(state.get('state'))]
    lines.extend(continuity)
    tail = []
    if task['status'] == 'verified':
        tail.append('VERIFIED. The next substantial request starts a new task.')
    elif task['status'] == 'stalled':
        tail.append('CLOSED (%s). Attach a new request: %s attach "…"' % (data.get('reason_code') or 'stalled', prefix))
    else:
        tail.append('RECORD evidence per D item (C items are Orbit\'s fixed checks): %s evidence %s D1 --note "…" [--command \'["python3","-m","pytest"]\'] [--file path]' % (prefix, tid))
        tail.append('CLAIM when done (checks run, one independent review follows): %s claim %s --summary "…" [--remaining "…"] · '
                    'a D item you defined is wrong: %s done %s \'D1 …\' (G0, the original goal, always binds)' % (prefix, tid, prefix, tid))
        tail.append('Orbit never commits or pushes. Evidence must come from commands actually run; a claim without evidence is returned unmet.')
    if for_digest:
        # The user's own words are already in the session; they must not re-send the whole brief on every prompt.
        lines = [line for line in lines if not line.startswith(('CORRECTIONS', 'REQUIREMENTS CHANGED'))]
    return _fit(lines, tail, BRIEF_LIMIT)


def _fit(lines, tail, limit):
    """Keep the header and the action lines whole; shorten the middle (longest lines first) to fit."""
    head, middle = lines[:2], list(lines[2:])
    def size():
        return len('\n'.join(head + middle + tail))
    while size() > limit and middle:
        longest = max(range(len(middle)), key=lambda i: len(middle[i]))
        overflow = size() - limit
        if len(middle[longest]) - overflow >= 120:
            middle[longest] = middle[longest][:len(middle[longest]) - overflow - 1] + '…'
        else:
            middle[longest] = middle[longest][:119] + '…' if len(middle[longest]) > 120 else ''
            middle = [m for m in middle if m]
            if size() > limit and all(len(m) <= 120 for m in middle):
                middle.pop(0)
    text = '\n'.join(head + middle + tail)
    return text if len(text) <= limit else text[:limit - 1] + '…'


def brief_digest(store, task):
    return hashlib.sha256(brief_text(store, task, 'digest', for_digest=True).encode('utf-8')).hexdigest()[:16]


def serve_brief(store, task, event, session=None):
    """Full brief at session start and whenever its content changed for this session; otherwise nothing.

    A brief that is already in the session's context costs tokens on every later turn if it is repeated."""
    data = task['data']
    briefs = data.get('briefs') or {}
    digest = brief_digest(store, task)
    session = session or (data.get('context_live') or {}).get('session')
    seen = (briefs.get('digests') or {}).get(session) if session else None
    pending = (data.get('requirements_pending') or {}).get('at') or ''
    if (event == 'prompt' and session and seen == digest and not data.get('context_dropped')
            and pending and (briefs.get('pending_seen') or {}).get(session) != pending and task['status'] not in CLOSED):
        # Only the request changed: one line, not the whole brief again.
        from .requirements import pending_line
        line = '[orbit] task %s · %s' % (task['id'], pending_line(data, orbit_command(store), task['id']))
        try:
            with store.lock('task:' + task['id']):
                task = store.get(task['id'])
                briefs = task['data'].setdefault('briefs', {})
                briefs.setdefault('pending_seen', {})[session] = pending
                briefs['served_short'] = briefs.get('served_short', 0) + 1
                briefs['chars'] = briefs.get('chars', 0) + len(line)
                store.save(task, task['status'], 'brief_served', {'event': event, 'chars': len(line), 'session': session, 'short': True})
        except (RuntimeError, ValueError):
            pass
        return line
    if event == 'prompt' and session and seen == digest and not data.get('context_dropped'):
        try:
            with store.lock('task:' + task['id']):
                task = store.get(task['id'])
                task['data'].setdefault('briefs', {}).setdefault('skipped', 0)
                task['data']['briefs']['skipped'] += 1
                store.save(task, task['status'], 'brief_skipped', {'event': event, 'reason': 'unchanged'})
        except (RuntimeError, ValueError):
            pass
        return None
    text = brief_text(store, task, event)
    try:
        with store.lock('task:' + task['id']):
            task = store.get(task['id'])
            briefs = task['data'].setdefault('briefs', {'served': 0, 'last_at': None})
            briefs['served'] = briefs.get('served', 0) + 1
            briefs['chars'] = briefs.get('chars', 0) + len(text)
            briefs['last_at'] = now()
            if session:
                digests = briefs.setdefault('digests', {})
                digests[session] = digest
                briefs.setdefault('pending_seen', {})[session] = pending
                for old in list(digests)[:-20]:
                    digests.pop(old, None)
            task['data'].pop('context_dropped', None)
            store.save(task, task['status'], 'brief_served', {'event': event, 'chars': len(text), 'session': session})
    except (RuntimeError, ValueError):
        pass  # The worker may own the task during verification; the brief is still served.
    return text


# ---------------------------------------------------------------- hook entry

def hook_log(store, message):
    try:
        with (store.home / 'hooks.log').open('a', encoding='utf-8') as handle:
            handle.write(now() + ' ' + message + '\n')
    except OSError:
        pass


def handle(store, event, payload, host):
    """Return the JSON object the host expects, or None for silence. Never raises."""
    try:
        return _handle(store, event, payload, host)
    except Exception as error:  # a hook must not break the user's session
        hook_log(store, event + ' error: ' + repr(error))
        return None


def _handle(store, event, payload, host):
    cwd = payload.get('cwd') or os.getcwd()
    root, is_git = project_root(cwd)
    value, _ = resolve(store, root)
    if value != 'on' or not is_git:
        return None
    session = payload.get('session_id')
    if event == 'session-end':
        if session:
            store.end_session(session)  # the event that frees its task for the next session
        return None
    task = resolve_task(store, root, session, payload.get('source'))
    from . import continuity

    def watched(current, source=None):
        current = drain_inbox(store, current)
        try:
            return continuity.observe(store, current, payload, host, event, source)
        except RuntimeError:
            return store.get(current['id'])  # the worker owns the task this instant; measure next time

    if event == 'session-start':
        if task is None:
            return None
        task = watched(task, payload.get('source'))
        return _context('SessionStart', serve_brief(store, task, 'session-start', session))
    if event == 'pre-compact':
        if task is None or task['status'] in CLOSED:
            return None
        task = watched(task, 'compact')
        try:
            continuity.auto_checkpoint(store, task, payload.get('trigger') or 'auto')
            with store.lock('task:' + task['id']):
                task = store.get(task['id'])
                task['data']['context_dropped'] = True
                store.save(task, task['status'], 'compacted', {'trigger': payload.get('trigger') or 'auto', 'layer': 'context',
                                                               'tokens': (task['data'].get('context_live') or {}).get('tokens')})
        except (RuntimeError, ValueError) as error:
            hook_log(store, 'pre-compact checkpoint skipped: ' + str(error))
        return None
    if event == 'prompt':
        text = (payload.get('prompt') or '').strip()
        from .requirements import classify
        if classify(text) == 'system':
            return None  # background-task and monitor notifications never create or change a task
        if text.startswith('/') or not text:
            return _context('UserPromptSubmit', serve_brief(store, watched(task), 'prompt', session)) if task else None
        if task is None or task['status'] in CLOSED:
            if len(text) < MIN_TASK_PROMPT:
                return _context('UserPromptSubmit', serve_brief(store, watched(task), 'prompt', session)) if task else None
            task = create(store, root, text, host, session)
        else:
            try:
                task = record_prompt(store, task, text, session)
            except RuntimeError:
                hook_log(store, 'prompt not recorded (task busy) ' + task['id'])
        task = watched(task)
        return _context('UserPromptSubmit', serve_brief(store, task, 'prompt', session))
    if event == 'stop':
        if task is None:
            return None
        task = watched(task)  # measure every stop, including the one after a block
        if payload.get('stop_hook_active'):
            # Never block twice in a row; a message to the user does not continue the turn, so it may go now
            # (this is the stop right after the checkpoint the executor was asked for).
            return context_decision(store, task, settle=task['data'].get('verification_pending') or task['status'] in CLOSED,
                                    message_only=True)
        primary = stop_decision(store, task, payload)
        if primary and primary.get('decision') == 'block':
            return primary  # the context decision waits for the next stop
        settle = bool(primary) or task['data'].get('verification_pending') or task['status'] in CLOSED
        secondary = context_decision(store, store.get(task['id']), settle=settle)
        if primary and secondary and 'systemMessage' in secondary:
            return {'systemMessage': primary['systemMessage'] + '\n' + secondary['systemMessage']}
        return primary or secondary
    return None


def _context(name, text):
    if not text:
        return None
    return {'hookSpecificOutput': {'hookEventName': name, 'additionalContext': text}}


def stop_decision(store, task, payload):
    data = task['data']
    prefix = orbit_command(store)
    last_prompt = (data.get('prompts') or [{}])[-1].get('at') or ''
    if data.get('reason_code') == 'checks_failed' and (data.get('last_checks_failed_at') or '') > last_prompt:
        return {'decision': 'block', 'reason': 'Orbit: a fixed check failed after your completion claim. Fix it now and claim again.\n'
                                              + str(data.get('reason') or '')[:3000]}
    if data.get('verification_pending'):
        from .recovery import ensure_worker
        if not store.locked('worker'):
            try:
                ensure_worker(store, idle_exit=90)
            except (ValueError, OSError) as error:
                hook_log(store, 'worker start failed: ' + str(error))
        return {'systemMessage': 'Orbit: fixed checks passed; the independent review of task %s runs in the background. The verdict arrives in the next brief or `%s status %s`.' % (task['id'], prefix, task['id'])}
    pending = data.get('requirements_pending')
    if pending and not pending.get('asked') and task['status'] not in CLOSED:
        try:
            with store.lock('task:' + task['id']):
                task = store.get(task['id'])
                if task['data'].get('requirements_pending'):
                    task['data']['requirements_pending']['asked'] = now()
                    store.save(task, task['status'], 'requirement_asked', {'rev': pending.get('rev'), 'layer': 'control'})
        except (RuntimeError, ValueError):
            return None
        return {'decision': 'block', 'reason': (
            'Orbit: the user changed the request at %s ("%s"). Before you continue, revise the result goals and, if it needs one, '
            'the verification: %s done %s --add \'<what the change requires>\' [--check \'["python3","-m","pytest","…"]\'] '
            '(or the same command with your rewritten items, or with --keep if the current items already cover it). Fixed checks can only be added.'
            % (requirements_hhmm(pending['at']), pending['text'][:300], prefix, task['id']))}
    message = str(payload.get('last_assistant_message') or '')
    executor = data.get('executor_result') or {}
    d_items = [d for d in data.get('done') or [] if not d['id'].startswith('C') and d['id'] != 'G0']
    claimed_this_turn = (data.get('claimed_at') or '') > last_prompt
    if (task['status'] == 'working' and message and CLAIM_WORDS.search(message[-1500:]) and not executor.get('finished')
            and not claimed_this_turn and (data.get('claim_nudged_turn') != data.get('turns') or not d_items)):
        try:
            with store.lock('task:' + task['id']):
                task = store.get(task['id'])
                task['data']['claim_nudged_turn'] = task['data'].get('turns', 0)
                store.save(task, task['status'], 'claim_nudged', {'chars': len(message)})
        except (RuntimeError, ValueError):
            pass
        hint = ('If the work is complete, record evidence per item and run `%s claim %s --summary "…"`; if not, run `%s claim %s --remaining "…"` or simply continue. '
                % (prefix, task['id'], prefix, task['id']))
        if not d_items:
            hint = ('Propose the completion definitions first: `%s done %s \'D1 …\' \'D2 …\'`, then record evidence and claim. ' % (prefix, task['id'])) + hint
        return {'decision': 'block', 'reason': 'Orbit: this reads like a completion claim for task %s, but nothing was claimed and nothing is verified. ' % task['id'] + hint}
    return None


def context_decision(store, task, settle=False, message_only=False):
    """At the handoff point: one checkpoint request to the executor, then one message to the user.

    settle: the executor has just claimed (or the task is closed). The ledger already holds the whole state,
    so no checkpoint is requested; the user is told once that a fresh context continues lean."""
    data = task['data']
    live = data.get('context_live') or {}
    state = data.get('handoff_state') or {}
    if not live.get('session') or state.get('session') != live.get('session'):
        return None
    prefix = orbit_command(store)
    closed = task['status'] in CLOSED or settle
    if state.get('state') == 'recommended' and not closed:
        reply = {'decision': 'block', 'reason': (
            "Orbit: this session's context is %s tokens (handoff point %s). Record a checkpoint now so a fresh session can "
            "continue without re-reading it: %s checkpoint %s --next '<the next concrete step>' --notes '<approach, decisions "
            "and anything the ledger does not already hold>' [--file <file in progress>]. Keep it short; goal, corrections, "
            "evidence and verdicts are already in the ledger. Then end the turn."
            % (_k(live.get('tokens')), _k(state.get('limit')), prefix, task['id']))}
        new_state = 'asked'
    elif state.get('state') in ('checkpointed',) or (state.get('state') == 'recommended' and closed):
        checkpointed = state.get('state') == 'checkpointed'
        reply = {'systemMessage': (
            'Orbit: context %s tokens ≥ handoff point %s for task %s. %sType /clear (Claude Code) or /new (Codex) to continue '
            'in a fresh context; %s automatically. Change the point: cosmos-orbit context 300k · turn off: cosmos-orbit context off'
            % (_k(live.get('tokens')), _k(state.get('limit')), task['id'],
               'Checkpoint saved. ' if checkpointed else ('After the verdict arrives, ' if task['data'].get('verification_pending') else ''),
               'the brief and checkpoint return' if checkpointed else 'the brief returns'))}
        new_state = 'told'
    else:
        return None
    if message_only and 'decision' in reply:
        return None
    try:
        with store.lock('task:' + task['id']):
            task = store.get(task['id'])
            task['data'].setdefault('handoff_state', {})['state'] = new_state
            store.save(task, task['status'], 'handoff_' + new_state, {'tokens': live.get('tokens'), 'layer': 'context'})
    except (RuntimeError, ValueError):
        return None
    return reply


def _k(value):
    return '%dk' % round(value / 1000) if isinstance(value, (int, float)) and value else '?'


def requirements_hhmm(stamp):
    from .requirements import _hhmm
    return _hhmm(stamp)


# ---------------------------------------------------------------- projections

def handoff_markdown(store, task):
    data = task['data']
    last = (data.get('verdicts') or [None])[-1]
    statuses = {i['id']: i for i in (last or {}).get('items', [])}
    evidence = data.get('done_evidence') or {}
    checks = data.get('evidence') or []
    goal = data.get('goal') or task['prompt']
    lines = ['# Orbit handoff — ' + _one_line(goal.splitlines()[0] if goal.strip() else task['id'], 120), '',
             '| | |', '|---|---|',
             '| Task | `%s` |' % task['id'],
             '| Workspace | `%s` |' % task['workspace'],
             '| Entrance | %s · %s |' % ('attached to your CLI session' if data.get('mode') == 'attached' else 'isolated clone', data.get('host') or task['provider']),
             '| Status | **%s**%s |' % (task['status'], ' (' + data['reason_code'] + ')' if data.get('reason_code') else ''),
             '| Attached | %s |' % _local(data.get('attached_at') or task['created']),
             '| Updated | %s |' % _local(task['updated']),
             '| Claims | %s |' % data.get('turns', 0),
             '', '## Goal (verbatim)', '', goal.strip(), '']
    feedback = data.get('feedback') or []
    lines += ['## Corrections (%d)' % len(feedback), '']
    lines += ['- %s %s' % (_local(f.get('at')), f['text'].strip()) for f in feedback] or ['- none']
    lines += ['', '## Completion definitions', '', '| ID | Definition | Executor evidence | Independent review |', '|---|---|---|---|']
    for d in data.get('done') or []:
        if d['id'].startswith('C'):
            index = int(d['how'].split(':')[1]) - 1
            row = checks[index] if index < len(checks) else None
            lines.append('| %s | fixed check `%s` | %s | — |' % (d['id'], ' '.join(data['development']['checks'][index]),
                                                                 ('exit %s' % row['exit_code']) if row else 'not run'))
            continue
        rows = evidence.get(d['id']) or []
        summary = '; '.join(('`%s` exit %s' % (' '.join(e['argv'])[:60], e['exit_code'])) if e['kind'] == 'command'
                            else ('`%s` sha %s' % (e.get('path'), (e.get('sha256') or '')[:12])) if e['kind'] == 'file'
                            else _one_line(e.get('note'), 120) for e in rows[-3:]) or '—'
        verdict = statuses.get(d['id'])
        review = ('%s — %s' % (verdict['status'], _one_line('; '.join(verdict.get('evidence') or []), 200))) if verdict else 'not reviewed'
        lines.append('| %s | %s | %s | %s |' % (d['id'], _one_line(d['text'], 200), summary, review))
    lines += ['', '## Independent review', '']
    if last:
        lines.append('- Outcome **%s** at turn %s' % (last.get('outcome'), last.get('turn')))
        for key in ('extra', 'missing', 'fix'):
            for item in last.get(key) or []:
                lines.append('- %s: %s' % (key, item))
        if last.get('question'):
            lines.append('- question: ' + last['question'])
    else:
        lines.append('- none yet')
    if data.get('reason'):
        lines += ['', '## Stop reason', '', '`%s` — %s' % (data.get('reason_code') or task['status'], data['reason'].strip())]
    changed = (data.get('development') or {}).get('changed_paths') or []
    lines += ['', '## Changed files since attach (%d)' % len(changed), '']
    lines += ['- `%s`' % p for p in changed[:60]] or ['- not measured yet (measured at each claim)']
    checkpoints = data.get('checkpoints') or []
    if checkpoints:
        lines += ['', '## Latest checkpoint', '']
        row = checkpoints[-1]
        lines.append('- %s · %s · %s tokens · by %s' % (_local(row['at']), row.get('kind'), _k(row.get('tokens')), row.get('by')))
        lines.append('- Next: ' + row['next'])
        if row.get('notes'):
            lines.append('- Notes: ' + row['notes'])
        if row.get('files'):
            lines.append('- Files in progress: ' + ', '.join('`%s`' % f['path'] for f in row['files']))
        if row.get('changed'):
            lines.append('- Changed since attach: ' + ', '.join('`%s`' % p for p in row['changed'][:30]))
    sessions = data.get('session_log') or []
    if sessions:
        lines += ['', '## Sessions (%d)' % len(sessions), '']
        lines += ['- %s · %s · started at %s · peak %s tokens' % (_local(r.get('started')), r.get('reason'), _k(r.get('start')), _k(r.get('peak')))
                  for r in sessions[-8:]]
    prompts = data.get('prompts') or []
    lines += ['', '## Recent prompts (%d recorded)' % len(prompts), '']
    lines += ['- %s %s' % (_local(p['at'], False), _one_line(p['text'], 200)) for p in prompts[-8:]] or ['- none']
    calls = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM events WHERE task_id=? AND kind='model_finished'", (task['id'],))]
    events = store.db.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND kind!='process_observation'", (task['id'],)).fetchone()[0]
    seconds = round(sum(c.get('seconds') or 0 for c in calls), 1)
    lines += ['', '## Ledger', '',
              '- events %d · model calls %d (%.1f s, all independent review) · briefs served %d' % (
                  events, len(calls), seconds, (data.get('briefs') or {}).get('served', 0)),
              '', '## Continue', '',
              '1. Open Claude Code or Codex in `%s` with Orbit on (`cosmos-orbit on`); the brief above returns automatically.' % task['workspace'],
              '2. Or read it on demand: `cosmos-orbit brief %s` · full record: `cosmos-orbit status %s`.' % (task['id'], task['id']),
              '3. Historical: this file is a projection of the ledger, not an authority over the current request.', '']
    return '\n'.join(lines)


def write_handoff(store, task, path):
    text = handoff_markdown(store, task)
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.replace(temporary, target)
    try:
        with store.lock('task:' + task['id']):
            task = store.get(task['id'])
            store.save(task, task['status'], 'handoff_written', {'path': str(target), 'chars': len(text)})
    except (RuntimeError, ValueError):
        pass
    return str(target)


def lessons(store, minimum=2):
    """Corrections repeated across tasks are guard-rule candidates; this only counts, it never writes rules."""
    groups = {}
    for summary in store.list():
        task = store.get(summary['id'])
        for item in task['data'].get('feedback') or []:
            text = str(item.get('text') or '').strip()
            if not text:
                continue
            key = re.sub(r'[^\w가-힣]+', ' ', text.lower()).strip()[:80]
            group = groups.setdefault(key, {'text': text[:200], 'count': 0, 'tasks': []})
            group['count'] += 1
            group['tasks'].append({'id': task['id'], 'at': item.get('at') or task['updated'], 'workspace': task['workspace']})
    rows = [g for g in groups.values() if g['count'] >= minimum]
    rows.sort(key=lambda g: (-g['count'], g['text']))
    return rows
