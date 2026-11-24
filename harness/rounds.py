"""Rounds of one ledger, and what the user does when a round is complete.

The user, 2026-11-24 14:41: a task whose goals and verification all passed is complete. When the same session goes on
with a new request, that request is the ledger's next round: its own goals and its own verification, the rounds before
it stay complete. The brief opens with the round being worked on, not with the first request (measured that day: one
ledger had been verified three times over three unrelated requests, and its brief still opened with the first one, so
a fresh context took the first request for the work to do).

* A round closes when the independent review verifies the task (attach.run_verification): data['rounds'] gains
  {n, goal, items, started_at, closed_at, turn}, and a completion offer opens.
* The next substantial request from the session that did the work reopens the task (lifecycle.reopen_verified):
  data['round'] becomes {n: n + 1, goal: that request}.
* The completion offer: the user is told the round is complete and picks with their next line - end (handoff, then
  memory when a brain is connected, then /exit), go on (the next request opens the next round) or leave it as it is.
  A line that picks nothing is a request like any other: a substantial one opens the next round.
"""
import re

from .store import now

CHOICES = {
    'end': ('종료', '종료해', '종료해줘', '종료해 줘', '세션 종료', '끝', '끝내', '끝내기', '끝내줘', 'end', 'exit', 'quit', 'close'),
    'continue': ('이어서', '이어서 하기', '이어서 진행', '계속', '계속하기', 'continue', 'go on'),
    'keep': ('그대로', '그대로 두기', '그대로 둬', '그대로 두자', '두기', 'keep', 'leave', 'leave it'),
}
LABELS = {'ko': ('종료', '이어서', '그대로 두기'), 'en': ('end', 'continue', 'keep')}
HANGUL = re.compile('[\uac00-\ud7a3]')


def _one(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def current(data, task=None):
    """The round being worked on (or, while the task is verified, the one just completed): {n, goal, at}."""
    found = data.get('round')
    if found:
        return found
    goal = data.get('goal') or (task or {}).get('prompt') or ''
    return {'n': 1, 'goal': goal, 'at': data.get('attached_at') or (task or {}).get('created')}


def completed(data):
    return data.get('rounds') or []


def korean(data, task=None):
    return bool(HANGUL.search(current(data, task).get('goal') or ''))


def met_items(data):
    """The D items the last verdict found met: what this round completed."""
    last = (data.get('verdicts') or [None])[-1] or {}
    return [row['id'] for row in last.get('items') or [] if row.get('status') == 'met' and str(row.get('id', '')).startswith('D')]


def complete(data, task):
    """The review verified the task: the round being worked on is complete. Called under the task lock, before the
    verified save. Idempotent for one round (a verdict reused after an interruption does not count it twice)."""
    this = current(data, task)
    rows = data.setdefault('rounds', [])
    if rows and rows[-1].get('n') == this['n']:
        return rows[-1]
    row = {'n': this['n'], 'goal': _one(this.get('goal'), 600), 'items': met_items(data),
           'started_at': this.get('at'), 'closed_at': now(), 'turn': data.get('turns', 0)}
    rows.append(row)
    del rows[:-50]
    data['round'] = {'n': this['n'], 'goal': this.get('goal') or '', 'at': this.get('at')}
    data['completion'] = {'round': this['n'], 'at': row['closed_at'], 'state': 'open', 'told': []}
    return row


def begin_next(data, text, session=None, by='prompt'):
    """A request after a verified round: the next round, its goal that request."""
    rows = completed(data)
    n = (rows[-1]['n'] if rows else current(data)['n']) + 1
    data['round'] = {'n': n, 'goal': (text or '')[:600], 'at': now(), 'session': session, 'by': by}
    offer = data.get('completion')
    if offer and offer.get('state') == 'open':
        offer.update(state='continue', answered_at=now(), by=by)
    return data['round']


def offer_open(task):
    offer = task['data'].get('completion') or {}
    return task['status'] == 'verified' and offer.get('state') == 'open'


def choice(text):
    """'end' | 'continue' | 'keep' for a line that only picks one of the three; None for anything else."""
    words = (text or '').strip().strip('[]()<>"\'.,!?~ ').lower()
    words = ' '.join(words.split())
    if not words or len(words) > 16:
        return None
    for name, options in CHOICES.items():
        if words in options:
            return name
    return None


def offer_text(task, korean_text=None):
    """The completion notice: which round, how many goals were verified, and the three choices."""
    data = task['data']
    rows = completed(data)
    row = rows[-1] if rows else {'n': current(data, task)['n'], 'items': met_items(data), 'goal': current(data, task)['goal']}
    ko = korean(data, task) if korean_text is None else korean_text
    count = len(row.get('items') or [])
    if ko:
        return ('Orbit: 라운드 %d 완료 - 목표 %d개 검증 통과 (task %s: %s). 다음 줄에 입력하세요: '
                '종료 (핸드오프 → 기억 → /exit) · 이어서 (다음 요청이 라운드 %d) · 그대로 두기'
                % (row['n'], count, task['id'], _one(row.get('goal'), 80), row['n'] + 1))
    return ('Orbit: round %d complete - %d goals verified (task %s: %s). Type one on the next line: end (handoff, '
            'memory, /exit) · continue (the next request opens round %d) · keep (leave the session as it is)'
            % (row['n'], count, task['id'], _one(row.get('goal'), 80), row['n'] + 1))


def goal_line(data, task, local, limit=300):
    """The brief's first line after the first round: the round, how many are complete, the first request short."""
    rows = completed(data)
    if not rows:
        return None
    this = current(data, task)
    first = rows[0].get('goal') or data.get('goal') or task['prompt']
    if task['status'] == 'verified' and rows[-1]['n'] == this['n']:
        head = 'ROUND %d ✓ verified (%d of %d rounds complete)' % (this['n'], len(rows), len(rows))
    else:
        head = 'ROUND %d (since %s; %d earlier round%s complete)' % (
            this['n'], local(this.get('at'), False), len(rows), '' if len(rows) == 1 else 's')
    return '%s: %s · FIRST REQUEST (round 1, verified): %s' % (head, _one(this.get('goal'), limit), _one(first, 120))


def offer_line(task, prefix):
    """The brief's line while the offer is open: tell the user the three choices, do nothing else."""
    if not offer_open(task):
        return None
    return ('COMPLETE: %s The user picks with their next line; do not start new work on your own. A substantial new '
            'request opens the next round (new goals and a new review).' % offer_text(task))


def review_view(data, task):
    """What the reviewer reads about rounds: earlier rounds were verified, the current one is judged now."""
    rows = completed(data)
    if not rows:
        return None
    this = current(data, task)
    return {'current': {'n': this['n'], 'goal': _one(this.get('goal'), 600)},
            'verified_before': [{'n': r['n'], 'goal': _one(r.get('goal'), 200), 'items': r.get('items')} for r in rows[-5:]]}


def handoff_lines(data, task, local):
    rows = completed(data)
    if not rows:
        return []
    this = current(data, task)
    out = ['## Rounds', '']
    for row in rows[-10:]:
        out.append('- Round %d ✓ %s · %s · goals %s' % (row['n'], local(row.get('closed_at')), _one(row.get('goal'), 160),
                                                       ' '.join(row.get('items') or []) or '-'))
    if this['n'] > rows[-1]['n']:
        out.append('- Round %d (open since %s) · %s' % (this['n'], local(this.get('at')), _one(this.get('goal'), 160)))
    out.append('')
    return out


# ---------------------------------------------------------------- ending a session

EXIT_LOG = 'hooks.log'


def exit_steps(host, presses=0):
    """Type /exit into the session's pane: after a prompt the hook blocked, empty the input it was put back into first."""
    from .typist import clear_steps
    steps = [('wait', 0.8)] + clear_steps(presses) + [('text', '/exit')]
    if host == 'codex':
        steps += [('wait', 1.2)]          # Codex takes an Enter right behind typed text as part of a paste
    return steps + [('key', 'enter')]


def end(store, task_id, session, reason='ended', why_text='', terminal=None, spawn=None, host=None, presses=0,
        state='completed', remaining=None, close_task=True):
    """End one session of a task in order: handoff (a checkpoint and the Markdown handoff), memory (when a brain is
    configured for the task), then /exit typed into its pane. The task closes (reason `reason`; `state` completed or
    stopped). Returns {'steps': [(name, outcome)], 'typed': bool}."""
    from . import continuity, typist
    steps = []
    task = store.get(task_id)
    data = task['data']
    this = current(data, task)
    if state == 'completed':
        next_step = 'Round %d is complete and verified; the session was ended (%s). A new request starts the next round.' % (
            this['n'], reason)
    else:
        next_step = 'Stopped with work left (%s): %s open item%s - continue the open D items.' % (
            reason, remaining if remaining is not None else 'some', '' if remaining == 1 else 's')
    try:
        continuity.record_checkpoint(store, task_id, next_step, notes=why_text or ('Written by Orbit when the session '
                                     'was ended (%s).' % reason), by='orbit', kind='end')
        steps.append(('handoff', 'written', ''))
    except (RuntimeError, ValueError, OSError) as error:
        steps.append(('handoff', 'failed', str(error)[:120]))
    task = store.get(task_id)
    memory = (task['data'].get('memory') or {})
    if memory.get('mode') in ('brain', 'both'):
        try:
            from .memory import enqueue_checkpoint, flush
            with store.lock('task:' + task_id):
                fresh = store.get(task_id)
                enqueue_checkpoint(store, fresh)
                found = flush(store, store.get(task_id))
            steps.append(('memory', 'stored' if any(r.get('stored') for r in found) else ('queued' if found else 'kept'), ''))
        except (RuntimeError, ValueError, OSError) as error:
            steps.append(('memory', 'failed', str(error)[:120]))
    else:
        steps.append(('memory', 'skipped', ''))
    try:
        with store.lock('task:' + task_id):
            task = store.get(task_id)
            data = task['data']
            record = {'at': now(), 'reason': reason, 'from': task['status'], 'state': state, 'session': session,
                      'round': this['n']}
            if remaining is not None:
                record['remaining'] = remaining
            offer = data.get('completion')
            if offer and offer.get('state') == 'open':
                offer.update(state='end', answered_at=record['at'], by=reason)
            if not close_task:                          # another live session still works on it
                store.save(task, task['status'], 'session_closed', dict(record, layer='control'))
            elif task['status'] != 'closed':
                data['closed'] = record
                data['closures'] = (data.get('closures') or [])[-9:] + [record]
                data.update(reason_code='closed_' + reason, reason=(
                    'Round %d complete; the session was ended (%s).' % (this['n'], reason) if state == 'completed'
                    else 'Stopped with work left; the session was ended (%s).' % reason))
                store.save(task, 'closed', 'task_closed', dict(record, layer='control'))
            else:
                store.save(task, 'closed', 'session_closed', dict(record, layer='control'))
        steps.append(('task', state if close_task else 'kept_open', ''))
    except (RuntimeError, ValueError) as error:
        steps.append(('task', 'failed', str(error)[:120]))
    typed = False
    if terminal:
        try:
            (spawn or typist.spawn)(terminal, exit_steps(host or data.get('host') or 'claude', presses),
                                    store.home / EXIT_LOG, home=store.home)
            typed = True
            steps.append(('exit', 'typing', ''))
        except (OSError, ValueError) as error:
            steps.append(('exit', 'failed', str(error)[:120]))
    else:
        steps.append(('exit', 'no_terminal', ''))
    if session:
        try:
            store.end_session(session)
        except (RuntimeError, ValueError):
            pass
    return {'steps': steps, 'typed': typed}


STEP_WORDS = {
    'ko': {'handoff': '핸드오프', 'memory': '기억', 'task': '장부', 'exit': '/exit', 'written': '저장', 'failed': '실패',
           'stored': '저장', 'queued': '대기열', 'kept': '이미 저장됨', 'skipped': '브레인 미연결로 건너뜀',
           'completed': '완결로 닫음', 'stopped': '멈춤으로 닫음', 'kept_open': '다른 세션이 쓰는 중이라 열어 둠', 'typing': '입력 중', 'no_terminal': '입력할 터미널 없음'},
    'en': {'handoff': 'handoff', 'memory': 'memory', 'task': 'ledger', 'exit': '/exit', 'written': 'written',
           'failed': 'failed', 'stored': 'stored', 'queued': 'queued', 'kept': 'already stored',
           'skipped': 'skipped (no brain configured)', 'completed': 'closed as complete', 'stopped': 'closed as stopped',
           'kept_open': 'kept open (another session works on it)',
           'typing': 'typing', 'no_terminal': 'no terminal to type into'},
}


def steps_text(result, ko=True):
    words = STEP_WORDS['ko' if ko else 'en']
    return ' → '.join('%s %s%s' % (words.get(name, name), words.get(outcome, outcome), (' (' + detail + ')') if detail else '')
                      for name, outcome, detail in result['steps'])


def answer(store, task, session, picked, host=None, terminal=None, spawn=None, typed_text=''):
    """The user picked one of the three after a complete round. Returns the line the hook shows (the prompt runs no turn)."""
    from . import typist
    ko = korean(task['data'], task)
    n = current(task['data'], task)['n']
    if picked == 'end':
        terminal = typist.detect() if terminal is None else terminal
        result = end(store, task['id'], session, reason='ended_by_user', terminal=terminal, spawn=spawn, host=host,
                     presses=typist.clear_presses(typed_text))
        if ko:
            return 'Orbit: 라운드 %d 완료로 이 세션을 닫습니다 - %s%s' % (
                n, steps_text(result), '' if result['typed'] else ' · 이 터미널엔 Orbit 이 입력할 수 없어 /exit 를 직접 입력하세요')
        return 'Orbit: round %d is complete; ending this session - %s%s' % (
            n, steps_text(result, False), '' if result['typed'] else ' · Orbit cannot type here: type /exit yourself')
    with store.lock('task:' + task['id']):
        task = store.get(task['id'])
        offer = task['data'].get('completion') or {}
        if offer.get('state') != 'open':
            return None
        offer.update(state=picked, answered_at=now(), by='user', session=session)
        store.save(task, task['status'], 'completion_answered', {'choice': picked, 'session': session, 'round': n,
                                                                  'layer': 'control'})
    if (host or task['data'].get('host') or 'claude') == 'claude':
        # Claude Code puts the line its hook blocked back into the input: left there, the user's next request was sent
        # glued to it ("그대로 두기이제 note.txt 에…", the live probe 2026-11-24 15:05). The typist empties it.
        terminal = typist.detect() if terminal is None else terminal
        if terminal:
            try:
                (spawn or typist.spawn)(terminal, [('wait', 0.8)] + typist.clear_steps(typist.clear_presses(typed_text)),
                                        store.home / EXIT_LOG, home=store.home)
            except (OSError, ValueError):
                pass
    if picked == 'continue':
        return ('Orbit: 이어서 합니다 - 다음 요청이 라운드 %d 로 시작됩니다 (목표와 검증을 새로 세웁니다).' % (n + 1) if ko else
                'Orbit: going on - your next request opens round %d (new goals, a new review).' % (n + 1))
    return ('Orbit: 그대로 둡니다 - 세션은 열려 있고, 다음 요청이 오면 라운드 %d 로 이어집니다.' % (n + 1) if ko else
            'Orbit: left as it is - the session stays open; a new request opens round %d.' % (n + 1))
