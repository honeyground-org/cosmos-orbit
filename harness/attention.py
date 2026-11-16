"""Tell the user when a task waits for them, and again while it keeps waiting.

Measured 2026-11-16 over the two weeks before (68 attached tasks): 17 tasks stopped to wait for the user's answer and
12 of them were never shown anything. Only a review's question was told (supervision.tell, once, in the terminal);
a session that asked the user itself (claim --question) was told nothing, because the manager reads a waiting task as
"nothing resumes, nothing nags" and no supervisor starts for it. 15 of the 17 closed after 12 hours untouched with the
question unanswered.

A question is now told the moment the task is seen waiting, again after each REMIND interval, and once more
CLOSING_BEFORE the 12-hour close (lifecycle.IDLE_SECONDS). Telling is not activity: the task closes when it would have.

Channels: the session's terminal (cmux posts a native macOS banner with its notification; tmux only shows a line in
its status bar) and, when the terminal cannot post a banner itself (tmux, an unknown terminal, a pane that is gone), a
desktop notification (macOS Notification Center, Linux notify-send). Nothing is ever typed: a question is the user's.
"""
import datetime
import hashlib
import json
import os
import shutil
import sys

REMIND = (30 * 60, 2 * 3600)   # seconds after the first notice: reminders while the answer has not come
CLOSING_BEFORE = 3600          # one more notice this long before an untouched task closes
KEY = 'attention:'


def _short(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def question(task):
    """The question the task waits on, as the manager reads it."""
    data = task['data']
    return (data.get('reason') or (data.get('executor_result') or {}).get('question')
            or (data.get('requirements_pending') or {}).get('text') or 'see the brief')


def waiting(store, task):
    """True when the manager reads this open attached task as waiting for the user."""
    from .attach import CLOSED
    from .manager import diagnose
    if task['data'].get('mode') != 'attached' or task['status'] in CLOSED:
        return False
    return diagnose(task, paused=store.paused(task['id']))['reason'] == 'waiting_for_user'


def mark(task):
    """One waiting episode: a new question (another claim, another verdict, another text) is a new episode."""
    data = task['data']
    raw = json.dumps([task['status'], len(data.get('verdicts') or []), data.get('claimed_at') or '',
                      data.get('reason_code') or '', question(task)], ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _epoch(at):
    return (at or datetime.datetime.now(datetime.timezone.utc)).timestamp()


def _wait_words(seconds):
    return '%d min' % (seconds // 60) if seconds < 3600 else '%d h' % (seconds // 3600)


def due(state, episode, at_epoch, idle_age):
    """Which notice is due now: 'asked', 'reminder', 'closing' or None."""
    from .lifecycle import IDLE_SECONDS
    if not state or state.get('mark') != episode:
        return 'asked'
    if not state.get('closing') and idle_age is not None and idle_age >= IDLE_SECONDS - CLOSING_BEFORE:
        return 'closing'
    sent = state.get('reminders', 0)
    if sent < len(REMIND) and at_epoch - state.get('first', at_epoch) >= REMIND[sent]:
        return 'reminder'
    return None


def text(task, kind, state=None):
    tid, asked = task['id'], _short(question(task), 160)
    if kind == 'asked':
        return 'Orbit: your answer is needed - task %s: %s' % (tid, asked)
    if kind == 'reminder':
        return 'Orbit: still waiting for your answer (%s) - task %s: %s' % (
            _wait_words(REMIND[(state or {}).get('reminders', 0)]), tid, asked)
    return 'Orbit: task %s closes in about %s unless you answer: %s' % (tid, _wait_words(CLOSING_BEFORE), asked)


def desktop_command(body, title='Orbit'):
    """The argv of a desktop notification on this machine, or None (off, or no notifier)."""
    if os.environ.get('ORBIT_DESKTOP_NOTIFY', '').lower() in ('0', 'off', 'false', 'no'):
        return None
    if sys.platform == 'darwin' and shutil.which('osascript'):
        quote = lambda value: '"%s"' % value.replace('\\', '\\\\').replace('"', '\\"')  # noqa: E731
        return ['osascript', '-e', 'display notification %s with title %s sound name "Glass"' % (quote(body), quote(title))]
    if shutil.which('notify-send'):
        return ['notify-send', title, body]
    return None


def deliver(store, task, body, runner=None, desktop=desktop_command):
    """Show `body` in every live session's terminal of the task, and on the desktop when no terminal posted a banner.
    Returns the channels that took it (e.g. ['cmux'], ['tmux', 'desktop'], ['desktop'])."""
    from . import supervision, wake
    run = runner or wake._run
    shown, banner = [], False
    for row in store.task_sessions(task['id']):
        if row['ended']:
            continue
        terminal, _ = wake.resolve(store, session=row['session'])
        if not terminal:
            continue
        code, _ = run(wake.notify_command(terminal, body))
        if code != 0:                                  # the pane its hooks named is gone: the one naming the task
            found = supervision.locate(terminal, task['id'], run, idle=False)
            if found:
                terminal = found
                code, _ = run(wake.notify_command(terminal, body))
        if code == 0:
            shown.append(terminal['kind'])
            banner = banner or terminal['kind'] == 'cmux'   # cmux notify posts the native banner itself
    if not banner:
        argv = desktop(body) if desktop else None
        if argv and run(argv)[0] == 0:
            shown.append('desktop')
    return shown


def _claim(store, key, old, new):
    """Compare-and-set one preference: two processes deciding the same notice at once send it once."""
    with store.db:
        if old is None:
            return store.db.execute('INSERT OR IGNORE INTO preferences(key, value) VALUES(?, ?)', (key, new)).rowcount == 1
        return store.db.execute('UPDATE preferences SET value=? WHERE key=? AND value=?', (new, key, old)).rowcount == 1


def notice(store, task, runner=None, at=None, desktop=desktop_command):
    """Tell the user about a waiting task when a notice is due. Returns the kind sent ('asked', 'reminder',
    'closing'), 'already_told' (a review's question that supervision.tell showed), or None (nothing due)."""
    from . import lifecycle, supervision
    task = store.get(task['id'])
    if not waiting(store, task):
        return None
    key = KEY + task['id']
    old = store.preference(key)
    try:
        state = json.loads(old) if old else None
    except ValueError:
        state = None
    episode, now_epoch = mark(task), _epoch(at)
    kind = due(state, episode, now_epoch, lifecycle._age(lifecycle.last_activity(store, task), at))
    if not kind:
        return None
    if kind == 'asked':
        new = {'mark': episode, 'first': now_epoch, 'reminders': 0, 'closing': False}
    else:
        new = dict(state, last=now_epoch)
        if kind == 'closing':
            new.update(closing=True, reminders=len(REMIND))   # nothing after the last notice before the close
        else:
            new['reminders'] = state.get('reminders', 0) + 1
    if not _claim(store, key, old, json.dumps(new, sort_keys=True)):
        return None
    live = [row['session'] for row in store.task_sessions(task['id']) if not row['ended']]
    if kind == 'asked' and live and task['status'] == 'needs_input':
        # A review's question is told by supervision.tell when its verdict is delivered: one notice per question.
        # Claiming tell's own mark first makes the two exclusive in either order.
        fresh = [s for s in live if supervision.claim_told(store, task, s)]
        if not fresh:
            with store.db:
                store._event(task['id'], 'attention_told', {'kind': kind, 'shown': [], 'already_told': True,
                                                            'layer': 'control'})
            return 'already_told'
    shown = deliver(store, task, text(task, kind, state), runner, desktop)
    with store.db:  # an event only: telling is not activity, so the task still closes when it would have
        store._event(task['id'], 'attention_told', {'kind': kind, 'shown': shown, 'layer': 'control'})
    return kind
