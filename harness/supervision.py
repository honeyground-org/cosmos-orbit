"""Continue unfinished attached turns, without a model call or interruption of a busy host.

Stop hooks block an unfinished turn. A detached observer covers hosts that return to
an input line anyway, and reviews that finish after the executor has become idle.
Only a known empty terminal is typed into; pause, input requests, off and session
replacement cancel supervision. Unsupported surfaces are reported, never guessed.
"""
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import time

RESUME_LINE = 'Orbit: continue the unfinished task recorded in your ledger.'
POLL = 3.0
MODE_KEY = 'continuation_mode'
MAX_STOP_BLOCKS = 2


def mode(store):
    from .continuity import handoff_mode
    return store.preference(MODE_KEY) or handoff_mode(store)


def configure(store, value=None):
    if value is not None:
        if value not in ('auto', 'tell'):
            raise ValueError('supervision mode is auto or tell')
        store.preference(MODE_KEY, value)
    return {'mode': mode(store), 'source': 'explicit' if store.preference(MODE_KEY) else 'context default',
            'stop_guard': True, 'automatic_terminal_resume': mode(store) == 'auto',
            'supported_terminals': ['cmux', 'tmux']}


def needs_work(task):
    data = task['data']
    if task['status'] != 'working' or data.get('verification_pending') or data.get('draft'):
        return False
    if (data.get('requirements_pending') or {}).get('unsure'):
        return False
    if (data.get('executor_result') or {}).get('question'):
        return False
    return bool((data.get('executor_result') or {}).get('remaining') or
                (data.get('objective_progress') or {}).get('remaining') or
                any(d['id'].startswith('D') for d in data.get('done') or []))


def handoff_pending(task):
    data = task['data']
    state = data.get('handoff_state') or {}
    return (state.get('session') == (data.get('context_live') or {}).get('session')
            and state.get('state') in ('recommended', 'asked', 'checkpointed', 'told', 'auto'))


def begin_turn(store, task, session):
    if not session:
        return
    with store.lock('task:' + task['id']):
        task = store.get(task['id'])
        mark = task['data'].setdefault('supervision', {}).setdefault(session, {})
        mark.update(stop_count=0, stop_at=None)
        store.save(task, task['status'], 'supervision_turn', {'session': session, 'layer': 'control'})


def stop_block(store, task):
    if not needs_work(task) or handoff_pending(task):
        return None
    session = (task['data'].get('context_live') or {}).get('session')
    mark = task['data'].get('supervision', {}).get(session, {})
    if mark.get('stop_count', 0) > MAX_STOP_BLOCKS:
        return None  # The rate-limited observer takes over; no unbounded synchronous Stop loop.
    from .attach import orbit_command
    data = task['data']
    remaining = ((data.get('objective_progress') or {}).get('remaining') or
                 (data.get('executor_result') or {}).get('remaining') or [])
    return {'decision': 'block', 'reason': (
        'Orbit: this task is still working, not verified. Continue the remaining authorised work now; '
        'a progress report or --remaining is not permission to stop.\n' + '\n'.join(remaining)[:2000] +
        '\nWhen all work is complete, record evidence and claim. If a real dependency needs user input, '
        'record it with `%s claim %s --question "specific missing input"`; do not invent a blocker.'
        % (orbit_command(store), task['id']))}


def record_stop(store, task, session):
    if not session:
        return
    from .store import now
    with store.lock('task:' + task['id']):
        task = store.get(task['id'])
        mark = task['data'].setdefault('supervision', {}).setdefault(session, {})
        mark['stop_at'] = now()
        mark['stop_count'] = mark.get('stop_count', 0) + 1
        store.save(task, task['status'], 'supervision_stop', {'session': session, 'layer': 'control'})


def tick(store, task_id, session, runner=None):
    """One observation. No typing on busy/unknown/user input; at most once per stopped turn."""
    from . import attach, continuity, typist, wake
    runner = runner or wake._run
    task = store.get(task_id)
    binding = store.session_task(session)
    if not binding or binding['ended'] or binding['task_id'] != task_id:
        return 'session_ended'
    if attach.resolve(store, task['workspace'])[0] != 'on':
        return 'disabled'
    if task['status'] not in ('working', 'checking', 'verifying'):
        return 'settled'
    if (task['data'].get('executor_result') or {}).get('question'):
        return 'input_required'
    terminal, reason = wake.resolve(store, session=session)
    if not terminal:
        return reason
    if task['data'].get('verification_pending') or task['status'] in ('checking', 'verifying'):
        return 'reviewing'
    if not needs_work(task):
        return 'no_work'
    if handoff_pending(task):
        return 'awaiting_handoff'
    mark = task['data'].get('supervision', {}).get(session, {})
    if not mark.get('stop_at') or mark.get('sent_for') == mark['stop_at']:
        return 'awaiting_stop'
    phase = store.db.execute('SELECT state,at FROM terminals WHERE session=?', (session,)).fetchone()
    if not phase or phase['state'] != 'idle' or phase['at'] > mark['stop_at']:
        return 'host_turn_active'
    code, screen = runner(wake.screen_command(terminal))
    state = wake.read_state(screen) if code == 0 else 'unreadable'
    if state != 'empty':
        return state
    if mode(store) != 'auto':
        runner(wake.notify_command(terminal, 'Orbit: unfinished work is waiting; automatic continuation is disabled.'))
        return 'notify_only'
    permitted, gap = wake.allowed(store.home, terminal['target'])
    if not permitted or gap:
        return 'throttled'
    # Re-read the binding, goal and input immediately before sending. Never clear input.
    with store.lock('task:' + task_id):
        latest = store.get(task_id)
        current = latest['data'].get('supervision', {}).get(session, {})
        if not needs_work(latest) or current.get('stop_at') != mark['stop_at'] or current.get('sent_for') == mark['stop_at']:
            return 'changed'
        if wake.resolve(store, session=session)[0] != terminal:
            return 'replaced'
        phase = store.db.execute('SELECT state,at FROM terminals WHERE session=?', (session,)).fetchone()
        if not phase or phase['state'] != 'idle' or phase['at'] > mark['stop_at']:
            return 'host_turn_active'
        code, screen = runner(wake.screen_command(terminal))
        if code or wake.read_state(screen) != 'empty':
            return 'input_changed'
        # Persist before sending: a crash after Enter cannot submit the same continuation twice.
        current['sent_for'] = mark['stop_at']
        store.save(latest, latest['status'], 'continuation_attempted', {'session': session, 'layer': 'control'})
        for argv in (typist.command(terminal, text=RESUME_LINE), typist.command(terminal, key='enter')):
            if runner(argv)[0]:
                return 'send_failed'
        wake.record_wake(store.home, terminal['target'])
    return 'continued'


def ensure(store, task, session):
    if not session or task['status'] not in ('working', 'checking', 'verifying'):
        return
    from . import wake
    terminal, reason = wake.resolve(store, session=session)
    if not terminal:
        with store.db:
            store._event(task['id'], 'supervision_unavailable', {'session': session, 'reason': reason, 'layer': 'control'})
        return
    key = 'supervisor:' + task['id'] + ':' + session
    if store.locked(key):
        return
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    if terminal['kind'] == 'cmux':
        # cmux's process-only socket policy rejects orphaned daemons. Use a real,
        # unfocused cmux workspace, rather than relaxing authentication or spoofing it.
        boot = ('import sys;sys.path.insert(0,' + repr(root) + ');'
                'from harness.supervision import main;main(*sys.argv[1:],close_workspace=True)')
        command = 'exec ' + ' '.join(shlex.quote(x) for x in
                    [sys.executable, '-c', boot, str(store.home), task['id'], session])
        result = subprocess.run([terminal['cli'], 'new-workspace', '--name', 'Orbit supervisor ' + task['id'][:6],
                                 '--cwd', str(store.home), '--command', command, '--focus', 'false'],
                                capture_output=True, text=True, timeout=15)
        with store.db:
            store._event(task['id'], 'supervision_launch', {'session': session,
                         'transport': 'cmux_workspace', 'ok': result.returncode == 0,
                         'error': result.stderr[:200] if result.returncode else '', 'layer': 'control'})
        return
    with (store.home / 'supervision.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-m', 'harness.supervision', str(store.home), task['id'], session],
                         cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True)


def main(home, task_id, session, close_workspace=False):
    from .store import Store
    store = Store(home)
    last = None
    terminal_outcomes = {'disabled', 'settled', 'input_required', 'session_ended', 'no_terminal',
                         'unknown_session', 'replaced', 'notify_only', 'send_failed'}
    try:
        with store.lock('supervisor:' + task_id + ':' + session):
            while True:
                try:
                    outcome = tick(store, task_id, session)
                except RuntimeError:
                    outcome = 'task_busy'
                if outcome != last:
                    with store.db:
                        store._event(task_id, 'supervision_observed', {'session': session, 'outcome': outcome, 'layer': 'control'})
                    last = outcome
                if outcome in terminal_outcomes:
                    return
                time.sleep(POLL)
    except RuntimeError:
        return
    finally:
        store.close()
        if close_workspace and os.environ.get('CMUX_WORKSPACE_ID'):
            cli = os.environ.get('CMUX_BUNDLED_CLI_PATH') or '/Applications/cmux.app/Contents/Resources/bin/cmux'
            subprocess.run([cli, 'close-workspace', '--workspace', os.environ['CMUX_WORKSPACE_ID']],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


if __name__ == '__main__':
    main(*sys.argv[1:])
