"""Orbit as the middle manager at every stop: why the session stopped, and what happens next.

A turn ends for one of a few reasons, and each needs a different next step. A generic "continue" was sent for
all of them, so a session that had finished its work was told to keep working, and one waiting for the user's
answer had a supervisor terminal opened for nothing. The manager reads the reason from the ledger (no model
call), records it as the task's state, and gives the one instruction that fits:

  unfinished work   → continue with the exact items that have no evidence yet (or that the review found unmet)
  finished work     → verify it against the goal and claim; the fixed checks and one independent review decide,
                      and a pass closes the task as complete
  a question        → wait for the user; nothing is resumed and nothing nags
  a review running  → wait; a pass completes the task, unmet items come back as the next instruction
"""
from .store import now

LABELS = {
    'complete': 'verified against the goal: complete',
    'closed': 'closed',
    'paused': 'paused by the user',
    'waiting_for_user': 'waiting for the user',
    'verifying': 'claimed; fixed checks and the independent review are deciding',
    'handoff': 'a context handoff is in progress',
    'draft': 'not yet confirmed as work',
    'requirements_changed': 'the request changed',
    'checks_failed': 'a fixed check failed after the claim',
    'review_unmet': 'the independent review found unmet items',
    'undefined': 'no completion definition yet',
    'finished_unclaimed': 'the work looks finished but is not verified',
    'interrupted': 'stopped before the work was finished',
}
RESUME = ('requirements_changed', 'checks_failed', 'review_unmet', 'finished_unclaimed', 'interrupted')
# 'undefined' (no D items yet) is recorded and shown in the brief, but never blocks a stop or opens a supervisor:
# a turn that only explored or answered a question has nothing to resume (verify_attach: "stop with nothing to say").
WAIT = ('verifying',)  # a supervisor waits through the review and resumes the session if it comes back unmet


def _short(text, limit=90):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _row(reason, next_step, items=(), **extra):
    return dict(reason=reason, label=LABELS[reason], next=next_step, items=list(items),
                resume=reason in RESUME, supervise=reason in RESUME or reason in WAIT, **extra)


def diagnose(task, paused=False, message='', handoff=False):
    """Why this task's session stopped and what should happen next. Pure: reads the task only."""
    from .attach import CLOSED, claims_completion
    data = task['data']
    status = task['status']
    executor = data.get('executor_result') or {}
    if status == 'verified':
        return _row('complete', 'Nothing: the independent review verified the result against the goal.')
    if status in CLOSED:
        return _row('closed', 'Nothing: the task is closed (%s).' % (data.get('reason_code') or status))
    if paused:
        return _row('paused', 'Nothing until the user resumes the task.')
    # Waiting only while the task is actually in needs_input. The user's answer returns the task to working but leaves
    # executor_result.question behind; reading that stale question as "still waiting" released the stop guard and
    # opened a supervisor that closed again within a second, on every turn (seen 2026-11-04, 15:58-16:13).
    if status == 'needs_input' or (data.get('requirements_pending') or {}).get('unsure'):
        question = data.get('reason') or executor.get('question') or (data.get('requirements_pending') or {}).get('text')
        return _row('waiting_for_user', 'Nothing until the user answers: ' + _short(question or 'the open question', 300))
    if data.get('verification_pending') or status in ('checking', 'verifying'):
        return _row('verifying', 'Wait: a pass closes the task as complete; unmet items come back as the next instruction.')
    if handoff:
        return _row('handoff', 'Checkpoint, then continue in a fresh context.')
    if data.get('requirements_pending'):
        return _row('requirements_changed', 'Revise the result goals for the changed request (done --add, or --keep), then continue.',
                    [_short(data['requirements_pending'].get('text'), 200)])
    if data.get('reason_code') == 'changed_during_review':
        return _row('finished_unclaimed', 'The review passed on the state you claimed, but your session changed files during '
                    'it: finish what you were changing, record evidence, then claim again so the current state is reviewed.')
    if data.get('reason_code') == 'checks_failed':
        return _row('checks_failed', 'Fix the failing fixed check, then claim again.', [_short(data.get('reason'), 300)])
    d_items = [d for d in data.get('done') or [] if d['id'].startswith('D')]
    if data.get('reason_code') == 'unmet':
        last = (data.get('verdicts') or [{}])[-1]
        items = ['%s %s' % (row['id'], row.get('status')) for row in last.get('items', []) if row.get('status') != 'met']
        items += [_short(fix, 200) for fix in (last.get('fix') or [])[:3]]
        return _row('review_unmet', 'Fix what the independent review found unmet, record new evidence, then claim again.', items)
    if not d_items:
        if data.get('draft'):
            return _row('draft', 'Nothing yet: define the completion if this prompt asked for work.')
        return _row('undefined', 'Define the completion items first, then work through them.')
    evidence = data.get('done_evidence') or {}
    missing = [d for d in d_items if not evidence.get(d['id'])]
    # The executor's own list of what is left counts until the user speaks again: an answer or a correction after
    # the claim makes it yesterday's list (it named the question the user has since answered).
    last_prompt = ((data.get('prompts') or [{}])[-1]).get('at') or ''
    remaining = (executor.get('remaining') or []) if (data.get('claimed_at') or '') >= last_prompt else []
    said_done = claims_completion(message)
    if not remaining and (not missing or said_done):
        return _row('finished_unclaimed',
                    'Verify the result against the goal now: re-read GOAL and every correction, check each D item against '
                    'its evidence%s, then claim. A pass closes the task as complete; unmet items come back to you.'
                    % (' (no evidence yet: ' + ', '.join(d['id'] for d in missing) + ')' if missing else ''),
                    ['%s %s' % (d['id'], _short(d['text'])) for d in missing])
    items = ['%s %s' % (d['id'], _short(d['text'])) for d in missing] + [_short(r, 200) for r in remaining]
    checkpoint = (data.get('checkpoints') or [{}])[-1].get('next')
    if checkpoint:
        items.append('checkpoint: ' + _short(checkpoint, 200))
    return _row('interrupted', 'Continue with the unfinished items, in order; record evidence for each, then claim.', items)


def observe(store, task, session=None, message=''):
    """Diagnose at a stop and record the state when it changed. Returns the diagnosis."""
    from .supervision import handoff_pending
    found = diagnose(task, paused=store.paused(task['id']), message=message, handoff=handoff_pending(task))
    with store.lock('task:' + task['id']):
        latest = store.get(task['id'])
        previous = latest['data'].get('manager') or {}
        if (previous.get('reason'), previous.get('next'), previous.get('items')) != (found['reason'], found['next'], found['items']):
            latest['data']['manager'] = {'at': now(), 'session': session, 'reason': found['reason'],
                                         'next': found['next'], 'items': found['items']}
            store.save(latest, latest['status'], 'manager_decision',
                       {'session': session, 'reason': found['reason'], 'next': found['next'][:300], 'layer': 'control'})
    return found


def instruction(store, task, found):
    """The text a session receives when the manager sends it back to work."""
    from .attach import orbit_command
    prefix, tid = orbit_command(store), task['id']
    lines = ['Orbit (manager): you stopped — %s.' % found['label']]
    progress = progress_line(task)
    if progress:
        lines.append(progress)
    lines.append('NEXT: ' + found['next'])
    if found['items']:
        lines.append('ITEMS: ' + ' · '.join(found['items']))
    if found['reason'] == 'undefined':
        lines.append("Run: %s done %s 'D1 …' 'D2 …'" % (prefix, tid))
    elif found['reason'] == 'requirements_changed':
        lines.append("Run: %s done %s --add 'D… what the change requires'   (or --keep)" % (prefix, tid))
    elif found['reason'] in ('interrupted', 'finished_unclaimed', 'review_unmet', 'checks_failed'):
        lines.append('Evidence: %s evidence %s D1 --command \'["…"]\' | --file PATH | --note "…"   ·   then: %s claim %s --summary "…"'
                     % (prefix, tid, prefix, tid))
    lines.append('A progress report is not a reason to stop. If a real dependency needs the user, record it: '
                 '%s claim %s --question "specific missing input".' % (prefix, tid))
    return '\n'.join(lines)[:2400]


def progress_line(task):
    """Where the work stands against the goal, item by item: ✓ the review found it met (or evidence is recorded and
    no review judged it yet), ✗ the latest review found it unmet, · nothing recorded yet."""
    data = task['data']
    items = [d for d in data.get('done') or [] if d['id'].startswith('D')]
    if not items:
        return None
    evidence = data.get('done_evidence') or {}
    last = (data.get('verdicts') or [{}])[-1]
    judged = {row['id']: row.get('status') for row in last.get('items') or []} if last.get('done_fixed') == data.get('done_fixed') else {}
    marks = []
    for item in items:
        status = judged.get(item['id'])
        marks.append(item['id'] + (' ✓' if status == 'met' else ' ✗' if status in ('unmet', 'unclear') else
                                   ' ✓' if evidence.get(item['id']) else ' ·'))
    return 'PROGRESS (✓ met or evidenced · ✗ review found unmet · · nothing yet): ' + ' '.join(marks)


def brief_line(task):
    """One line for the brief: the manager's latest decision while the task is open."""
    state = task['data'].get('manager') or {}
    if not state.get('reason') or state['reason'] in ('complete', 'closed'):
        return None
    from .attach import _local
    line = 'MANAGER (%s): %s → next: %s' % (_local(state.get('at'), False), LABELS.get(state['reason'], state['reason']),
                                           _short(state.get('next'), 220))
    if state.get('items'):
        line += ' · ' + _short(' · '.join(state['items']), 220)
    return line
