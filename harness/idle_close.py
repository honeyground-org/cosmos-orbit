"""Close sessions nobody used for days, in order, and tell finished work from work left behind.

The user, 2026-11-24 14:41: sessions nobody uses should close by themselves, and a closed one must say whether its
work was finished or left unfinished. Measured that day: of five live Claude Code sessions, two had had no input for
21-22 hours (558 MB and 237 MB resident); the ledger's 12-hour close (lifecycle) closes a task but never ends the
session process, so the terminals stayed open and kept their memory.

* On by default; the limit is the time since the session's last prompt from the user (DEFAULT_DAYS). Settings:
  `cosmos-orbit context idle-close 3d | on | off | reset` (<home>/settings.json, section "idle_close").
* A session is closed only where Orbit can see it is safe: its pane shows an empty input line. Claude Code's prompt
  suggestion (grey text in the input after a turn) reads the same as text the user typed on a plain screen (cmux gives
  no colours; measured 2026-11-24 in a live pane), so a session showing one is left too: the user's draft is never
  lost, and `promptSuggestionEnabled: false` lets those sessions close. A turn in progress, a
  permission question or a screen Orbit cannot read (anything but an empty input), text typed and not sent, a review
  in progress, or a session the user kept (`cosmos-orbit sessions --keep <session>`) is left alone.
* Closing is the same order as the user's own "종료" (rounds.end): handoff, memory when a brain is configured, then
  /exit typed into the pane. A task whose round was verified closes complete; one with work left closes stopped,
  with the number of open items. A task another live session still works on stays open.
* `cosmos-orbit sessions --idle` shows what would be closed now and why the rest is left (nothing is closed).
* The session list ("세션 목록") shows three groups: in progress, stopped (work left; a number continues it by handoff)
  and complete.

The background round (patrol, every minute in the dashboard service) runs it at most every SWEEP_EVERY seconds.
"""
import datetime
import json
import os
import re
import shlex
import time

DEFAULT_DAYS = 3
SECTION = 'idle_close'
SWEEP_EVERY = 600
SWEEP_KEY = 'idle_close_swept_at'
KEEP_PREFIX = 'idle_keep:'
LIVE = ('working', 'needs_input', 'verified', 'checking', 'verifying')
BUSY_RECENT = 3600       # a 'busy' hook state this recent is a turn; older, the screen decides (an interrupted turn
                         # never reports its end)
LIST_DAYS = 7            # closed tasks the session list shows


def _epoch(stamp):
    try:
        return datetime.datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return None


def parse_days(value):
    """'3d', '3', '2.5 days', '12h', '3일' -> days (float > 0)."""
    text = str(value).strip().lower()
    found = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(d|day|days|일|h|hour|hours|시간)?', text)
    if not found:
        raise ValueError('idle-close takes a duration (3d, 12h), on, off or reset: %r' % value)
    number = float(found.group(1))
    days = number / 24 if (found.group(2) or 'd') in ('h', 'hour', 'hours', '시간') else number
    if days <= 0:
        raise ValueError('idle-close needs a duration above zero (or off)')
    return days


def settings(store):
    """{'on', 'days', 'source'} from <home>/settings.json, or the built-in default."""
    from .continuity import read_settings
    data, _ = read_settings(store)
    section = data.get(SECTION) if isinstance(data.get(SECTION), dict) else {}
    try:
        days = parse_days(section['days']) if 'days' in section else DEFAULT_DAYS
    except ValueError:
        days = DEFAULT_DAYS
    return {'on': section.get('on', True) is not False, 'days': days,
            'source': 'settings.json' if section else 'built-in'}


def configure(store, value=None):
    """Show (None) or change the setting: on | off | reset | a duration."""
    from .continuity import update_settings
    if value is not None:
        word = str(value).strip().lower()
        if word == 'reset':
            update_settings(store, lambda section: section.clear(), name=SECTION)
        elif word in ('on', 'off'):
            update_settings(store, lambda section: section.update(on=word == 'on'), name=SECTION)
        else:
            days = parse_days(word)
            update_settings(store, lambda section: section.update(on=True, days=days), name=SECTION)
    view = settings(store)
    view['changed'] = value is not None
    view['note'] = ('sessions with no prompt from the user for %g days are closed (handoff, memory, /exit) where the pane '
                    'shows an empty input; preview: cosmos-orbit sessions --idle' % view['days']) if view['on'] else 'off'
    return view


def keep(store, session, on=True):
    with store.db:
        if on:
            store.db.execute('INSERT OR REPLACE INTO preferences(key, value) VALUES(?, ?)', (KEEP_PREFIX + session, '1'))
        else:
            store.db.execute('DELETE FROM preferences WHERE key=?', (KEEP_PREFIX + session,))
    return {'session': session, 'kept': on}


def kept(store, session):
    return store.preference(KEEP_PREFIX + session) == '1'


def last_prompt(store, task, session):
    """When the user last typed in this session (its own prompts on the task; else when it was bound to it)."""
    stamps = [p.get('at') for p in task['data'].get('prompts') or [] if p.get('session') == session and p.get('at')]
    if stamps:
        return max(stamps)
    row = store.db.execute('SELECT at FROM session_tasks WHERE session=?', (session,)).fetchone()
    return row['at'] if row else None


def remaining(task):
    """Open D items the last review did not find met."""
    data = task['data']
    last = (data.get('verdicts') or [None])[-1] or {}
    met = {row['id'] for row in last.get('items') or [] if row.get('status') == 'met'}
    return len([d for d in data.get('done') or [] if str(d.get('id', '')).startswith('D') and d['id'] not in met])


def classify(task):
    """'completed' when the task's round was verified, otherwise 'stopped'."""
    return 'completed' if task['status'] == 'verified' else 'stopped'


def decide(store, task, session, at=None, runner=None, days=None):
    """One live session: {'action': 'close' | 'skip', 'why', ...}."""
    from . import wake
    at = time.time() if at is None else at
    days = settings(store)['days'] if days is None else days
    stamp = last_prompt(store, task, session)
    age = (at - _epoch(stamp)) / 86400 if _epoch(stamp) else None
    out = {'session': session, 'task': task['id'], 'workspace': task['workspace'], 'last_prompt': stamp,
           'idle_days': round(age, 2) if age is not None else None, 'state': classify(task), 'remaining': remaining(task)}

    def skip(why):
        return dict(out, action='skip', why=why)
    if kept(store, session):
        return skip('kept')
    if age is None or age < days:
        return skip('recent')
    if task['data'].get('verification_pending') or task['status'] in ('checking', 'verifying'):
        return skip('reviewing')
    phase = store.db.execute('SELECT state, at FROM terminals WHERE session=?', (session,)).fetchone()
    if phase and phase['state'] == 'busy' and (at - (_epoch(phase['at']) or 0)) < BUSY_RECENT:
        return skip('turn')
    terminal, reason = wake.resolve(store, session=session)
    if not terminal:
        return skip(reason or 'no_terminal')
    code, screen = (runner or wake._run)(wake.screen_command(terminal))
    seen = wake.read_state(screen) if code == 0 else 'unreadable'
    if seen != 'empty':
        return skip({'busy': 'turn', 'typing': 'unsent_input'}.get(seen, 'not_at_input'))
    return dict(out, action='close', why='idle', terminal=terminal)


def candidates(store):
    """(task, session) for every live session bound to an attached task that is not closed."""
    rows = store.db.execute('SELECT id FROM tasks WHERE status IN (%s)' % ','.join('?' * len(LIVE)), LIVE).fetchall()
    for (task_id,) in rows:
        task = store.get(task_id)
        if task['data'].get('mode') != 'attached':
            continue
        for row in store.task_sessions(task_id):
            if not row['ended']:
                yield task, row['session']


def sweep(store, runner=None, at=None, apply=True, force=False, spawn=None):
    """One pass. `apply` False: only the decisions (the preview). Returns the decisions, closed ones with 'result'."""
    from .rounds import end
    at = time.time() if at is None else at
    view = settings(store)
    if apply and not view['on']:
        return []
    if apply and not force:
        try:
            last = float(store.preference(SWEEP_KEY) or 0)
        except ValueError:
            last = 0
        if at - last < SWEEP_EVERY:
            return []
        store.preference(SWEEP_KEY, repr(at))
    pairs = list(candidates(store))
    decisions = [decide(store, task, session, at, runner, view['days']) for task, session in pairs]
    if not apply:
        return decisions
    closing = {(d['task'], d['session']) for d in decisions if d['action'] == 'close'}
    for decision in decisions:
        if decision['action'] != 'close':
            continue
        tid, session = decision['task'], decision['session']
        task = store.get(tid)
        others = [r['session'] for r in store.task_sessions(tid)
                  if not r['ended'] and r['session'] != session and (tid, r['session']) not in closing]
        decision['result'] = end(store, tid, session, reason='idle_close', terminal=decision.pop('terminal'),
                                 host=task['data'].get('host'), state=decision['state'],
                                 remaining=decision['remaining'] if decision['state'] == 'stopped' else None,
                                 why_text='Closed by Orbit: no prompt from the user for %.1f days (limit %g).' % (
                                     decision['idle_days'], view['days']),
                                 close_task=not others, spawn=spawn)
        closing.discard((tid, session))
        with store.db:
            store._event(tid, 'idle_closed', {'session': session, 'state': decision['state'], 'layer': 'control',
                                               'idle_days': decision['idle_days'], 'remaining': decision['remaining'],
                                               'task_closed': not others})
    for decision in decisions:
        decision.pop('terminal', None)
    return decisions


WHY = {'kept': ('제외(keep)', 'kept by the user'), 'recent': ('최근 사용', 'used recently'),
       'reviewing': ('검토 중', 'a review is running'), 'turn': ('턴 진행 중', 'a turn is running'),
       'unsent_input': ('입력창에 글이 있음 (보내지 않은 글이거나 Claude Code 추천 문구 — 화면만으로는 구분 불가)',
                        'text in the input (typed and not sent, or a Claude Code prompt suggestion: a plain screen cannot tell)'),
       'not_at_input': ('입력창이 아님(권한 질문 등)', 'not at an empty input (a permission question?)'),
       'no_terminal': ('입력할 수 없는 터미널', 'a terminal Orbit cannot type into'),
       'unknown_session': ('터미널 기록 없음', 'no terminal recorded'), 'replaced': ('같은 창에 새 세션', 'replaced in its pane'),
       'idle': ('정리 대상', 'would be closed')}


def preview_text(store, decisions):
    view = settings(store)
    lines = ['Orbit idle close: %s · limit %g days since the last prompt (%s)' % (
        'on' if view['on'] else 'OFF', view['days'], view['source'])]
    if not decisions:
        return lines[0] + '\nNo live session.'
    for d in sorted(decisions, key=lambda d: (d['action'] != 'close', -(d['idle_days'] or 0))):
        state = 'complete' if d['state'] == 'completed' else 'stopped, %d open' % d['remaining']
        lines.append('  %-5s %s  task %s  idle %s d  %s  — %s' % (
            'CLOSE' if d['action'] == 'close' else 'keep', d['session'][:8], d['task'],
            d['idle_days'] if d['idle_days'] is not None else '?', state, WHY.get(d['why'], (d['why'], d['why']))[1]))
    lines.append('Keep one session: cosmos-orbit sessions --keep <session> · change: cosmos-orbit context idle-close 3d|off')
    return '\n'.join(lines)


# ---------------------------------------------------------------- the session list's stopped / complete groups

def ended_rows(store, days=LIST_DAYS, at=None):
    """Closed tasks of the last `days`: 'stopped' (work left) and 'completed', for the session list."""
    at = time.time() if at is None else at
    rows = []
    for (task_id,) in store.db.execute("SELECT id FROM tasks WHERE status='closed' ORDER BY updated DESC LIMIT 200"):
        task = store.get(task_id)
        data = task['data']
        closed = data.get('closed') or {}
        when = _epoch(closed.get('at'))
        if data.get('mode') != 'attached' or not when or at - when > days * 86400:
            continue
        reason = closed.get('reason')
        if reason in ('continued',):
            continue
        state = closed.get('state') or ('completed' if closed.get('from') == 'verified' else 'stopped')
        left = closed.get('remaining') if closed.get('remaining') is not None else remaining(task)
        rows.append({'session': closed.get('session') or task_id, 'host': data.get('host') or 'claude', 'state': state,
                     'folder': task['workspace'], 'pane': None,
                     'last_local': datetime.datetime.fromtimestamp(when).strftime('%Y-%m-%d %H:%M'),
                     'resume': 'cosmos-orbit continue ' + task_id, 'goal': _one(task['prompt'], 160),
                     'last_request': '', 'next': _one(((data.get('checkpoints') or [{}])[-1]).get('next') or '', 200) or None,
                     'task': task_id, 'remaining': left})
    return rows


def take_up(store, row, terminal, run=None):
    """A stopped (or complete) task picked from the list: Orbit takes it back for the next new session in its folder
    (a handoff), and opens that session in a new tab with the line that picks the carried request up."""
    from .lifecycle import reopen
    from .typist import CARRY_LINE
    from .restore import _run, _name
    try:
        reopen(store, row['task'])
    except (RuntimeError, ValueError) as error:
        return False, 'not taken back: ' + str(error)[:120]
    run = run or _run
    folder = row['folder'] if row['folder'] and os.path.isdir(row['folder']) else None
    line = ('codex ' if row['host'] == 'codex' else 'claude ') + shlex.quote(CARRY_LINE)
    kind = (terminal or {}).get('kind')
    if kind == 'cmux':
        argv = [terminal['cli'], 'new-workspace', '--name', _name(row), '--command', line, '--focus', 'true']
        if folder:
            argv[2:2] = ['--cwd', folder]
        code, out = run(argv)
        if code == 0:
            return True, 'continued by handoff in a new tab'
    elif kind == 'tmux':
        code, out = run([terminal['cli'], 'new-window', '-n', _name(row)] + (['-c', folder] if folder else []) + [line])
        if code == 0:
            return True, 'continued by handoff in a new tmux window'
    return True, 'taken back: start a session in %s and it continues there' % (folder or 'its folder')


def _one(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2)
