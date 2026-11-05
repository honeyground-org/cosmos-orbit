"""A task's life after its work.

The user, 2026-11-05: a verified result is complete; when the user goes on in the same session the task is working
again and its goals change; a task that is done (or left) and then nothing happens closes after a while; and a
closed task the user wants back a long time later is picked from a list of closed tasks.

* verified is done. The session that did the work may go on: its next substantial request, while the task is
  still open, reopens it (working, the goals to revise) instead of starting a new task.
* A task nobody touched for IDLE_SECONDS closes: a verified one (reason verified) and an unfinished one whose
  sessions went quiet (reason idle). Nothing is deleted; its sessions are ended, so no new terminal takes it.
* A closed task comes back only when asked: `cosmos-orbit list --closed`, then `cosmos-orbit continue <id>`.
  Orbit never links a new request to a closed task by itself; when one looks related it says so in one line.
"""
import datetime
import re
import time

from .store import now

IDLE_SECONDS = 12 * 3600   # as long as an open handoff invitation lasts (continuity.INVITE_TTL_SECONDS)
SWEEP_EVERY = 300          # the whole ledger at most this often from hooks; the session's own task on every hook
SWEEP_KEY = 'lifecycle_swept_at'
CLOSABLE = ('verified', 'working', 'needs_input')
SUGGEST_SECONDS = 7 * 86400
SUGGEST_MIN = 0.3          # share of the request's words found in the closed task's goal and last checkpoints
SUGGEST_SHARED = 2         # … and at least this many of them


def _utc(at=None):
    return at or datetime.datetime.now(datetime.timezone.utc)


def _age(stamp, at=None):
    try:
        return (_utc(at) - datetime.datetime.fromisoformat(str(stamp))).total_seconds()
    except (TypeError, ValueError):
        return None


def last_activity(store, task):
    """The newest of the task's own record and the last hook of each live session bound to it."""
    stamps = [task['updated']]
    for row in store.task_sessions(task['id']):
        if row['ended']:
            continue
        seen = store.db.execute('SELECT at FROM terminals WHERE session=?', (row['session'],)).fetchone()
        stamps.append(seen['at'] if seen else row['at'])
    return max(stamps)


def due(store, task, at=None):
    """'verified' or 'idle' when this task should close now; otherwise None."""
    data = task['data']
    if data.get('mode') != 'attached' or task['status'] not in CLOSABLE or data.get('verification_pending'):
        return None                                      # a queued review is Orbit's own work, not the user's silence
    age = _age(last_activity(store, task), at)
    if age is None or age <= IDLE_SECONDS:
        return None
    return 'verified' if task['status'] == 'verified' else 'idle'


def close(store, task_id, at=None):
    """Close one task when it is due (decided again under its lock). Returns the reason, or None."""
    try:
        if not due(store, store.get(task_id), at):
            return None                                  # the common case: no lock (a review holds it for minutes)
        with store.lock('task:' + task_id):
            task = store.get(task_id)
            why = due(store, task, at)
            if not why:
                return None
            data = task['data']
            record = {'at': now(), 'reason': why, 'from': task['status'], 'idle_since': last_activity(store, task)}
            data['closed'] = record
            data['closures'] = (data.get('closures') or [])[-9:] + [record]
            data.update(reason_code='closed_' + why, reason=(
                'Verified, then nothing for %d hours: closed.' if why == 'verified'
                else 'Nothing happened for %d hours: closed with its work unfinished.') % (IDLE_SECONDS // 3600))
            store.save(task, 'closed', 'task_closed', dict(record, layer='control'))
        for row in store.task_sessions(task_id):
            if not row['ended']:
                store.end_session(row['session'])        # no new terminal takes a closed task as "nobody on it"
        return why
    except (RuntimeError, ValueError):
        return None                                      # busy this instant: the next hook decides again


def sweep(store, session=None, at=None):
    """Run first by every hook, before the hook itself counts as activity. The session's own task is decided on every
    hook (its prompt must see the state it is really in); the rest of the ledger at most every SWEEP_EVERY seconds."""
    ids = []
    row = store.session_task(session) if session else None
    if row and row['task_id']:
        ids.append(row['task_id'])
    try:
        last = float(store.preference(SWEEP_KEY) or 0)
    except ValueError:
        last = 0
    if at is not None or time.time() - last >= SWEEP_EVERY:
        store.preference(SWEEP_KEY, repr(time.time()))
        cutoff = (_utc(at) - datetime.timedelta(seconds=IDLE_SECONDS)).isoformat()
        ids += [r[0] for r in store.db.execute('SELECT id FROM tasks WHERE status IN (?,?,?) AND updated < ?',
                                                CLOSABLE + (cutoff,))]
    closed = []
    for task_id in dict.fromkeys(ids):
        why = close(store, task_id, at)
        if why:
            closed.append((task_id, why))
    return closed


def worked_on(task, session):
    """Whether this session did work on the task (made a request on it), not merely inherited it afterwards."""
    data = task['data']
    return bool(session) and (session in (data.get('session_ids') or [])
                              or any(p.get('session') == session for p in data.get('prompts') or []))


def reopen_verified(store, task_id, session, text):
    """The session that did the work goes on after verification: the task is working again and its goals change.
    Returns the fresh task, or None when it is not (or no longer) a verified task this session worked on."""
    from .attach import _apply_prompt
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        if task['status'] != 'verified' or not worked_on(task, session):
            return None
        data = task['data']
        data['reopens'] = (data.get('reopens') or [])[-9:] + [
            {'at': now(), 'from': 'verified', 'session': session, 'by': 'prompt', 'text': text[:200]}]
        data.update(reason='', reason_code='reopened', verification_pending=False)
        data['development']['phase'] = 'ready'
        store.save(task, 'working', 'task_reopened', {'from': 'verified', 'session': session, 'by': 'prompt', 'layer': 'control'})
        _apply_prompt(store, task, text, session)        # records it and marks the goals to revise
    store.attach(task['workspace'], task_id)             # the folder's current task again
    return store.get(task_id)


def closed_tasks(store, workspace=None, limit=20):
    """Closed tasks, newest first: goal, when and why it closed, its last checkpoint."""
    from pathlib import Path
    rows = []
    where = str(Path(workspace).resolve()) if workspace else None
    for item in store.db.execute("SELECT id FROM tasks WHERE status='closed' ORDER BY updated DESC"):
        task = store.get(item[0])
        if where and task['workspace'] != where:
            continue
        data = task['data']
        checkpoint = (data.get('checkpoints') or [{}])[-1]
        closed = data.get('closed') or {}
        rows.append({'id': task['id'], 'workspace': task['workspace'], 'goal': _one(task['prompt'], 160),
                     'closed_at': closed.get('at'), 'reason': closed.get('reason'), 'was': closed.get('from'),
                     'last_activity': closed.get('idle_since'),
                     'checkpoint': _one(checkpoint.get('next') or '', 200) or None,
                     'continue': 'cosmos-orbit continue ' + task['id']})
        if len(rows) >= limit:
            break
    return rows


STOP = frozenset('the and for with from this that into then than also please now just keep make sure task work '
                 'continue finish start again next add new'.split())


def _words(text):
    """Comparable words: a Latin word by its first five letters (migrate ~ migrating), a Hangul word by its letter
    pairs (로그인을 ~ 로그인 페이지: particles differ, the pairs match). Character pairs across a whole English
    sentence matched unrelated requests through 'th', 'er', 'in' (measured in verify_lifecycle)."""
    found = set()
    for word in re.findall(r'[0-9A-Za-z]+|[\uac00-\ud7a3]+', (text or '').lower()):
        if word[0] >= '\uac00':
            found.update(word[i:i + 2] for i in range(len(word) - 1))
        elif len(word) >= 3 and word not in STOP:
            found.add(word[:5])
    return found


def suggestion(store, root, session, text, exclude=None, at=None):
    """One closed task the new request may continue, or None. Never a link: the line only names it.

    The session's own closed task first (the same terminal coming back); otherwise the closed task in this folder,
    closed within SUGGEST_SECONDS, whose goal and last checkpoint share the most of the request's character pairs."""
    from pathlib import Path
    root = str(Path(root).resolve())
    asked = _words(text)
    best = None
    for item in store.db.execute("SELECT id FROM tasks WHERE status='closed' AND workspace=? ORDER BY updated DESC", (root,)):
        if item[0] == exclude:
            continue
        task = store.get(item[0])
        closed = (task['data'].get('closed') or {}).get('at')
        age = _age(closed, at)
        if age is None or age > SUGGEST_SECONDS:
            continue
        if session and worked_on(task, session):
            return task
        theirs = _words(task['prompt'] + ' ' + ' '.join(c.get('next') or '' for c in (task['data'].get('checkpoints') or [])[-2:]))
        shared = len(asked & theirs)
        score = shared / len(asked) if asked else 0
        if shared >= SUGGEST_SHARED and score >= SUGGEST_MIN and (best is None or score > best[0]):
            best = (score, task)
    return best[1] if best else None


def suggestion_line(task, session, prefix='cosmos-orbit'):
    closed = task['data'].get('closed') or {}
    return ('CLOSED EARLIER HERE (a suggestion, not linked): %s "%s" — closed %s (%s). Only if the user means to '
            'continue that task: %s continue %s%s' % (
                task['id'], _one(task['prompt'], 100), _local(closed.get('at')), closed.get('reason') or 'idle',
                prefix, task['id'], (' --session ' + session) if session else ''))


def reopen(store, task_id, session=None, pane=None, host=None):
    """`cosmos-orbit continue <id>`: the user takes a closed (or verified, or stopped) task back.

    With a session (named, or the Claude Code session the command runs in) that session works on it from its next
    prompt; a task that session had just started for the same request (no goals yet, only its own prompts) closes
    and its requests move onto the task taken back. Without one, the next new session in this folder (or, after a
    /clear, in this pane) takes it, as a handoff would."""
    from .attach import _apply_prompt, NO_TASK
    task = store.get(task_id)
    if task['data'].get('mode') != 'attached':
        raise ValueError('continue applies to attached tasks; use resume for a managed task')
    carried = []
    left = None
    if session:
        row = store.session_task(session)
        if row and row['task_id'] and row['task_id'] not in (task_id, NO_TASK):
            left, carried = _release_fresh(store, row['task_id'], session, task_id)
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        data = task['data']
        was = task['status']
        if was in ('checking', 'verifying') or data.get('verification_pending'):
            raise ValueError('Task %s is being verified; wait for the verdict' % task_id)
        status = 'needs_input' if (was == 'closed' and (data.get('closed') or {}).get('from') == 'needs_input') \
            or was == 'needs_input' else 'working'
        data['reopens'] = (data.get('reopens') or [])[-9:] + [
            {'at': now(), 'from': was, 'session': session, 'by': 'continue', 'closed': data.get('closed')}]
        data.pop('closed', None)
        if was != 'needs_input':
            data.update(reason='', verification_pending=False)
        data['reason_code'] = 'reopened'
        data['development']['phase'] = 'ready'
        if not session:
            data['handoff_state'] = {'session': None, 'state': 'told', 'at': now(), 'told_at': now(), 'pane': pane,
                                     'reopened': True}
        store.save(task, status, 'task_reopened', {'from': was, 'session': session, 'by': 'continue',
                                                   'carried': len(carried), 'left': left, 'layer': 'control'})
        for text in carried:
            _apply_prompt(store, task, text, session)
    store.attach(task['workspace'], task_id)
    if session:
        store.bind_session(session, task_id, task['workspace'])
    fresh = store.get(task_id)
    return {'id': task_id, 'status': fresh['status'], 'was': was, 'session': session,
            'taken_by': session or 'the next new session in ' + task['workspace'],
            'left': left, 'carried': len(carried)}


def _release_fresh(store, other_id, session, into):
    """The task the session had just started for the request that made the user continue: when it has no goals and
    only this session's prompts, it closes (reason continued) and its requests move. Otherwise it is left alone."""
    with store.lock('task:' + other_id):
        other = store.get(other_id)
        data = other['data']
        prompts = data.get('prompts') or []
        if (other['status'] not in ('working', 'needs_input', 'received') or data.get('done_fixed')
                or any(p.get('session') != session for p in prompts)):
            return None, []
        record = {'at': now(), 'reason': 'continued', 'from': other['status'], 'into': into}
        data['closed'] = record
        data['closures'] = (data.get('closures') or [])[-9:] + [record]
        data.update(reason='The session continued task %s instead; its requests moved there.' % into,
                    reason_code='closed_continued')
        store.save(other, 'closed', 'task_closed', dict(record, layer='control'))
    # Its first prompt created it (recorded without a kind); later ones count when they asked for something.
    return other_id, [other['prompt']] + [p['text'] for p in prompts[1:] if p.get('kind') in ('requirement', 'unsure')]


def _one(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _local(stamp):
    try:
        return datetime.datetime.fromisoformat(str(stamp)).astimezone().strftime('%m-%d %H:%M')
    except (TypeError, ValueError):
        return str(stamp or '')[:16]
