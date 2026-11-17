"""Orbit's round: every INTERVAL seconds, look at every open task the way a manager walks the floor.

Two things the hooks cannot do on their own, because a session sitting at its input line runs no hook:

- A task waiting for the user's answer is told: at once, again while it keeps waiting, and before the 12-hour close
  (attention.notice).
- A session that stopped with work left, that no supervisor is driving, is sent back to work.

Measured 2026-11-16 over the two weeks before: the supervisor a Stop starts left after 15 unchanged minutes 30 times,
mostly on 'awaiting_stop' (16: the continuation had been typed for that stop, or no stop was recorded) and
'host_turn_active' (6: a turn the Stop hook had sent back, that had long ended). Nothing started it again, because
only the next Stop does and an idle session never stops again. About ten tasks with work left then sat at their input
line until the 12-hour close.

The round decides from what it sees, not from that bookkeeping: the task needs work (manager.diagnose, the same reason
the Stop guard uses; a question for the user never counts), the session's last hook was IDLE_BEFORE ago or more, no
supervisor holds it, and its screen shows an empty input line. It types only where the supervisor would: a known
terminal (cmux, tmux), automatic continuation on (`cosmos-orbit supervision auto`), the hourly typing bound
(wake.allowed). One idle stretch is continued at most MAX_NUDGES times; a session that does not move after that, or
one Orbit may not type into, is the user's to look at, and they are told once.

The dashboard service runs the round in the background (`cosmos-orbit dashboard`, which the login service keeps
running); `cosmos-orbit patrol --once` runs one round by hand. ORBIT_PATROL=off turns the background round off.
"""
import datetime
import json
import os
import sys
import threading
import time

INTERVAL = 60          # seconds between two rounds
IDLE_BEFORE = 5 * 60   # a session's last hook at least this long ago: a supervisor, the Stop guard or the user act first
MAX_NUDGES = 2         # continuations typed into one idle stretch before the user is told instead
KEY = 'patrol:'
OPEN = ('working', 'needs_input')


def _age(stamp, at):
    try:
        return at - datetime.datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return None


def _left(task):
    from .manager import diagnose
    found = diagnose(task)
    return ' · '.join(found['items'][:3]) or found['label']


def _state(store, key, idle_at):
    try:
        state = json.loads(store.preference(key) or '{}')
    except ValueError:
        state = {}
    if state.get('idle_at') != idle_at:                 # the session moved since: a new idle stretch
        state = {'idle_at': idle_at, 'sent': 0, 'told': False}
    return state


def _tell_once(store, task, session, key, state, body, runner, desktop):
    from . import attention
    if state.get('told'):
        return False
    state['told'] = True
    store.preference(key, json.dumps(state, sort_keys=True))
    shown = attention.deliver(store, task, body, runner, desktop)
    with store.db:
        store._event(task['id'], 'patrol_told', {'session': session, 'shown': shown, 'layer': 'control'})
    return True


def nudge(store, task, session, runner=None, at=None, desktop=None):
    """Send one idle session of an unfinished task back to work, or say why not (the outcome string)."""
    from . import attention, supervision, typist, wake
    run = runner or wake._run
    desktop = attention.desktop_command if desktop is None else desktop
    at = time.time() if at is None else at
    tid = task['id']
    binding = store.session_task(session)
    if not binding or binding['ended'] or binding['task_id'] != tid:
        return 'not_bound'
    if store.locked('supervisor:%s:%s' % (tid, session)):
        return 'supervised'                              # its supervisor is on it
    phase = store.db.execute('SELECT state, at FROM terminals WHERE session=?', (session,)).fetchone()
    if not phase:
        return 'unknown_session'
    if phase['state'] != 'idle':
        return 'host_turn_active'
    idle = _age(phase['at'], at)
    if idle is None or idle < IDLE_BEFORE:
        return 'recent'
    from .routes import session_limited
    if session_limited(store, session, task['data'].get('host') or task.get('provider'), at):
        return 'limited'                                 # stopped at a usage limit: a continuation would only fail;
        # the user was told at the stop (StopFailure) and picks a route at their next prompt
    key = '%s%s:%s' % (KEY, tid, session)
    state = _state(store, key, phase['at'])
    terminal, reason = wake.resolve(store, session=session)
    if not terminal:
        # A terminal Orbit cannot read or type into (neither cmux nor tmux): the user is the only way back. A session
        # a newer one replaced in its pane, or one never seen, is not the user's to be told about.
        if reason == 'no_terminal' and _tell_once(
                store, task, session, key, state,
                'Orbit: task %s stopped with work left; continue it in its session: %s' % (tid, _left(task)), run, desktop):
            return 'told'
        return reason
    code, screen = run(wake.screen_command(terminal))
    if code != 0:
        found = supervision.locate(terminal, tid, run)   # the pane its hooks named is gone (supervision.locate)
        if found:
            terminal = found
            code, screen = run(wake.screen_command(terminal))
    seen = wake.read_state(screen) if code == 0 else 'unreadable'
    if seen != 'empty':
        return seen                                      # busy, the user's typing, or a screen it cannot read
    if state['sent'] >= MAX_NUDGES or supervision.mode(store) != 'auto':
        why = ('continued %d times without moving' % state['sent'] if state['sent'] >= MAX_NUDGES
               else 'automatic continuation is off')
        _tell_once(store, task, session, key, state,
                   'Orbit: task %s stopped with work left (%s): %s' % (tid, why, _left(task)), run, desktop)
        return 'gave_up' if state['sent'] >= MAX_NUDGES else 'notify_only'
    permitted, gap = wake.allowed(store.home, terminal['target'])
    if not permitted or gap:
        return 'throttled'
    try:
        with store.lock('task:' + tid):
            latest = store.get(tid)
            if store.paused(tid) or not supervision.needs_work(latest) or supervision.handoff_pending(latest):
                return 'changed'
            again = store.db.execute('SELECT state, at FROM terminals WHERE session=?', (session,)).fetchone()
            if not again or (again['state'], again['at']) != (phase['state'], phase['at']):
                return 'changed'
            code, screen = run(wake.screen_command(terminal))
            if code or wake.read_state(screen) != 'empty':
                return 'input_changed'
            state['sent'] += 1                           # persisted before sending: a crash never types it twice
            store.preference(key, json.dumps(state, sort_keys=True))
            with store.db:  # an event only: the typed line's own prompt hook is the session's activity
                store._event(tid, 'patrol_continued', {'session': session, 'nudge': state['sent'],
                                                       'idle_seconds': int(idle), 'target': terminal['target'],
                                                       'layer': 'control'})
            for argv in (typist.command(terminal, text=supervision.RESUME_LINE), typist.command(terminal, key='enter')):
                if run(argv)[0]:
                    return 'send_failed'
            wake.record_wake(store.home, terminal['target'])
    except RuntimeError:
        return 'task_busy'                               # a review holds the task: the next round decides again
    return 'continued'


def sweep(store, runner=None, at=None, desktop=None):
    """One round over the ledger. Returns [(task_id, session or None, outcome)] for what it looked at."""
    from . import attach, attention, supervision
    from .manager import diagnose
    at = time.time() if at is None else at
    stamp = datetime.datetime.fromtimestamp(at, datetime.timezone.utc)
    results = []
    rows = store.db.execute('SELECT id FROM tasks WHERE status IN (%s)' % ','.join('?' * len(OPEN)), OPEN).fetchall()
    for (task_id,) in rows:
        try:
            task = store.get(task_id)
            if task['data'].get('mode') != 'attached' or store.paused(task_id):
                continue
            if attach.resolve(store, task['workspace'])[0] != 'on':
                continue
            found = diagnose(task, handoff=supervision.handoff_pending(task))
            if found['reason'] == 'waiting_for_user':
                kind = attention.notice(store, task, runner, stamp,
                                        attention.desktop_command if desktop is None else desktop)
                results.append((task_id, None, 'told_' + kind if kind else 'waiting'))
                continue
            if not found['resume']:
                continue
            for row in store.task_sessions(task_id):
                if not row['ended']:
                    results.append((task_id, row['session'], nudge(store, task, row['session'], runner, at, desktop)))
        except Exception as error:  # noqa: BLE001 - one task's trouble never ends the round for the others
            results.append((task_id, None, 'error: ' + repr(error)[:160]))
    return results


def run_forever(home, interval=INTERVAL, sleep=time.sleep, rounds=None):
    """The background round: one process at a time (a second dashboard skips while another one patrols)."""
    from .store import Store
    done = 0
    while rounds is None or done < rounds:
        store = Store(home)
        try:
            with store.lock('patrol'):
                for task_id, session, outcome in sweep(store):
                    if outcome in ('continued', 'told', 'gave_up', 'notify_only', 'send_failed') or \
                            outcome.startswith(('told_', 'error')):
                        print('%s patrol %s %s %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), task_id, session or '-', outcome),
                              file=sys.stderr, flush=True)
        except RuntimeError:
            pass                                         # another process is on its round
        except Exception as error:  # noqa: BLE001 - the dashboard must keep serving whatever a round meets
            print('patrol round failed: %r' % (error,), file=sys.stderr, flush=True)
        finally:
            store.close()
        done += 1
        sleep(interval)


def start_background(home):
    """Start the round in a daemon thread of this process (the dashboard). Off with ORBIT_PATROL=off."""
    if os.environ.get('ORBIT_PATROL', '').lower() in ('0', 'off', 'false', 'no'):
        return None
    thread = threading.Thread(target=run_forever, args=(home,), name='orbit-patrol', daemon=True)
    thread.start()
    return thread
