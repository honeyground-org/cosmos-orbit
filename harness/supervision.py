"""Continue unfinished attached turns, without a model call or interruption of a busy host.

Stop hooks block an unfinished turn. A detached observer covers hosts that return to
an input line anyway, and reviews that finish after the executor has become idle.
Only a known empty terminal is typed into; pause, input requests, off and session
replacement cancel supervision. Unsupported surfaces are reported, never guessed.
"""
import json
import os
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
    """True when the manager would send the session back to work (unfinished, unverified or returned unmet)."""
    from .manager import diagnose
    return diagnose(task)['resume']


def handoff_pending(task):
    """The live session waits for a handoff: the guard must not send it back to work past the handoff point.
    The same open states as continuity.OPEN_STATES: after a failed automatic /clear (auto_failed) the session
    waits for the user's /clear exactly as after 'told'."""
    from .continuity import OPEN_STATES
    data = task['data']
    state = data.get('handoff_state') or {}
    return (state.get('session') == (data.get('context_live') or {}).get('session')
            and state.get('state') in OPEN_STATES)


def _ended_key(task_id, session):
    return 'turn_ended:%s:%s' % (task_id, session)


def _ended_outside(store, task_id, session, mark):
    """True when this turn ended while the task lock was busy, so its end was recorded outside the task (below)."""
    stamp = store.preference(_ended_key(task_id, session)) or ''
    return bool(stamp) and stamp >= (mark.get('turn_at') or '')


def _end_outside(store, task_id, session):
    """The task lock is held (an independent review holds it for minutes), but the turn did end: say so where the
    next prompt looks. Measured 2026-11-05 (Codex e2e): the Stop came during the review, 'Another harness process
    owns this task', the turn stayed open, and the next request was taken for one typed during a turn."""
    from .store import now
    store.preference(_ended_key(task_id, session), now())


def begin_turn(store, task, session):
    """A prompt reached the session. True when a turn was already running: the user typed it while the session
    worked (the host delivers it into that turn), so nothing may treat it as a prompt at an idle input line."""
    if not session:
        return False
    from .store import now
    with store.lock('task:' + task['id']):
        task = store.get(task['id'])
        mark = task['data'].setdefault('supervision', {}).setdefault(session, {})
        working = bool(mark.get('turn_open')) and not _ended_outside(store, task['id'], session, mark)
        mark.update(stop_count=0, stop_at=None, turn_open=True, turn_at=now())
        store.save(task, task['status'], 'supervision_turn', {'session': session, 'layer': 'control',
                                                              **({'during_turn': True} if working else {})})
    store.preference(_ended_key(task['id'], session), '')
    return working


def reset_turn(store, task_id, session):
    """A session (re)starts at its input line: a turn left open by a killed or interrupted process is over.
    (An Escape interrupt sends no Stop either: the prompt after one counts as during a turn, so at most that one
    prompt waits for the handoff; it is never lost.)"""
    if not session:
        return
    try:
        with store.lock('task:' + task_id):
            task = store.get(task_id)
            mark = (task['data'].get('supervision') or {}).get(session)
            if mark and mark.get('turn_open'):
                mark['turn_open'] = False
                store.save(task, task['status'], 'supervision_turn_reset', {'session': session, 'layer': 'control'})
    except RuntimeError:
        _end_outside(store, task_id, session)


def continue_turn(store, task_id, session):
    """The Stop hook sent the turn back: it keeps running, so a prompt typed now arrives during a turn."""
    if not session:
        return
    store.preference(_ended_key(task_id, session), '')   # a Stop recorded outside the lock no longer ends it
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        mark = task['data'].setdefault('supervision', {}).setdefault(session, {})
        if not mark.get('turn_open'):
            mark['turn_open'] = True
            store.save(task, task['status'], 'supervision_turn_continued', {'session': session, 'layer': 'control'})


def stop_block(store, task, message=''):
    """Send the session back with the manager's instruction for why it stopped; None when it may stop."""
    if store.paused(task['id']) or handoff_pending(task):
        return None
    from . import manager
    found = manager.diagnose(task, message=message)
    if not found['resume']:
        return None
    session = (task['data'].get('context_live') or {}).get('session')
    mark = task['data'].get('supervision', {}).get(session, {})
    if mark.get('stop_count', 0) > MAX_STOP_BLOCKS:
        return None  # The rate-limited observer takes over; no unbounded synchronous Stop loop.
    return {'decision': 'block', 'reason': manager.instruction(store, task, found)}


def record_stop(store, task, session):
    if not session:
        return
    from .store import now
    try:
        with store.lock('task:' + task['id']):
            task = store.get(task['id'])
            mark = task['data'].setdefault('supervision', {}).setdefault(session, {})
            mark['stop_at'] = now()
            mark['stop_count'] = mark.get('stop_count', 0) + 1
            mark['turn_open'] = False  # until the Stop hook's reply sends the turn back (continue_turn)
            store.save(task, task['status'], 'supervision_stop', {'session': session, 'layer': 'control'})
    except RuntimeError:
        _end_outside(store, task['id'], session)
        raise


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
    if store.paused(task_id) or task['status'] not in ('working', 'checking', 'verifying'):
        return 'settled'
    from .manager import diagnose
    if diagnose(task)['reason'] == 'waiting_for_user':
        return 'input_required'  # a question the user already answered is stale, not a reason to stop supervising
    terminal, reason = wake.resolve(store, session=session)
    if not terminal:
        return reason
    if task['data'].get('verification_pending'):
        if task['data'].get('requirements_pending'):
            try:
                task = attach.reconcile_verification(store, task_id)
            except RuntimeError:
                return 'reviewing'
        if task['data'].get('verification_pending'):
            from .recovery import ensure_worker
            if not store.locked('worker'):
                ensure_worker(store, idle_exit=90)
            return attach.verification_progress(store, task)['state']
    if task['status'] in ('checking', 'verifying'):
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
    from . import manager, wake
    if not manager.diagnose(task, paused=store.paused(task['id']), handoff=handoff_pending(task))['supervise']:
        return  # waiting for the user, a handoff, a draft: nothing to resume, so no supervisor (and no terminal)
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
    # A background process, in cmux as in tmux: no terminal of its own. It used to open an "Orbit supervisor"
    # cmux workspace per task, on the belief that cmux refuses orphaned processes. Measured 2026-11-05 with
    # the default socket mode: a detached process re-parented to launchd (ppid 1) read the pane 5 s and
    # 70 s later (exit 0), as the detached typist and waker always have. The workspaces were the cost: 28
    # opened for 9 tasks in two and a half hours, unexplained, the kind a user closes on sight.
    with (store.home / 'supervision.log').open('ab') as log:
        process = subprocess.Popen([sys.executable, '-m', 'harness.supervision', str(store.home), task['id'], session],
                                   cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
    with store.db:
        store._event(task['id'], 'supervision_launch', {'session': session, 'transport': 'detached',
                     'terminal': terminal['kind'], 'layer': 'control',
                     'pid': process.pid if isinstance(process.pid, int) else None})


IDLE_EXIT = 15 * 60     # seconds one unchanged outcome may last before the supervisor leaves
REVIEW_WAIT = 60 * 60   # a running review may take longer (seen: 864 s); its verdict can need a resume
REVIEW_OUTCOMES = {'reviewing', 'queued', 'checking', 'verifying', 'task_busy'}


def main(home, task_id, session, close_workspace=False, sleep=time.sleep, clock=time.monotonic):
    """Watch one session's task until there is nothing left to do for it.

    Every Stop hook starts a supervisor again when one is needed, so leaving costs nothing: one stays only
    while a stop it could continue may still come. Observed 2026-11-05: two supervisors had polled for
    16 hours, one on 'no_work', one on 'awaiting_stop' for a Codex session idle since the day before
    (Codex reports no SessionEnd)."""
    from .store import Store
    store = Store(home)
    last, since = None, clock()
    terminal_outcomes = {'disabled', 'settled', 'input_required', 'session_ended', 'no_terminal',
                         'unknown_session', 'replaced', 'notify_only', 'send_failed', 'no_work'}
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
                    last, since = outcome, clock()
                if outcome in terminal_outcomes:
                    return
                if clock() - since >= (REVIEW_WAIT if outcome in REVIEW_OUTCOMES else IDLE_EXIT):
                    with store.db:
                        store._event(task_id, 'supervision_observed', {'session': session, 'outcome': 'idle_exit',
                                     'after': outcome, 'layer': 'control'})
                    return                               # the next Stop starts a supervisor again if one is needed
                sleep(POLL)
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
