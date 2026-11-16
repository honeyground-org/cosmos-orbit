#!/usr/bin/env python3
"""Model-free check of the handoff summary: what a fresh session is handed after many handoffs.

Observed 2026-11-16 (measured on the real ledger): 18 of 73 attached tasks filled the 2,400-character brief, among
them every long task (9-10 checkpoints, 25-58 corrections). The brief showed the first request, the last four
corrections and the last checkpoint, and cut the longest line first. A rule from the first day, the result of a
subject finished the day before and the fact that the subject changed at all were gone.

① replays that shape with synthetic data through the calls that exist before and after the change (attach,
record_checkpoint without summary arguments, brief_text): this section fails on the old code, with a control that
passes on both. ② checks the order in which a full brief gives way (also fails on the old code, with a control),
③ the session-written summary and Orbit's model-free checks. Exit 0 = pass.
"""
import datetime
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID'):
    os.environ.pop(name, None)
for _marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
    os.environ.pop(_marker, None)
from harness import attach, continuity  # noqa: E402
from harness.store import Store  # noqa: E402
from types import SimpleNamespace as _Namespace  # noqa: E402
from unittest.mock import patch as _no_process  # noqa: E402
from harness import evolve as _evolve, recovery as _recovery, supervision as _supervision  # noqa: E402
_no_process.object(_recovery, 'ensure_worker', return_value=None).start()
_no_process.object(_evolve, 'maybe_start', return_value=None).start()
_no_process.object(_supervision, 'subprocess', _Namespace(
    Popen=lambda *a, **k: _Namespace(pid=None), run=subprocess.run, DEVNULL=subprocess.DEVNULL,
    SubprocessError=subprocess.SubprocessError)).start()
# An installed Orbit prints `cosmos-orbit`; a test store would spell out an interpreter, a checkout and --home on every
# command line (about 1,200 of the 2,400 characters), which no real brief carries.
_no_process.object(attach, 'orbit_command', return_value='cosmos-orbit').start()

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:400]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def git(root, *args):
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True, capture_output=True)


def make_repo(base, name):
    root = base / name
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'app.py').write_text('print("hello")\n')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'files')
    return root


# Four days that ended before the checkpoints below are written (they are stamped with the real clock).
START = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=6)).replace(minute=0, second=0, microsecond=0)


STRIDE = 300  # minutes between two requests: 19 requests span four days


def at(minutes):
    return (START + datetime.timedelta(minutes=minutes)).isoformat()


# Three subjects over four days, as the long tasks had them. The rule in correction 2 holds to the end.
SUBJECT_A = [
    'Restart the brain and Orbit services from the latest main and confirm that both of them answer again',
    'never push to main directly: every change goes through a pull request that I merge myself',
    'the restart has to include the embedding service and the vector store, not only the brain process',
    'after the restart the dashboard must answer with status 200 on its usual port, please check that too',
    'the brain service restart should also reload the graph store so the recall answers come back again',
    'check the restart log of the brain service and the graph store again to see that nothing failed there',
]
SUBJECT_B = [
    'Next: a desktop notification whenever a session halts to wait for my reply to a question it asked',
    'remind again after thirty minutes and two hours when nobody answers the waiting session notification',
    'the waiting notification must reach the terminal pane too, not only the desktop notification centre',
    'one more waiting notification right before the twelve hour idle close of the session, then stop reminding',
    'the reminder for a waiting session must not fire while the user is typing an answer into the session',
    'show the waiting notification state on the dashboard so I can see which session waits for an answer',
]
SUBJECT_C = [
    'Rework the handoff summary so that a fresh context gets what matters now and what matters overall',
    'the handoff summary must not replay every correction from the very beginning of the whole task again',
    'the handoff summary should say when the conversation changed its subject and what the earlier subject produced',
    'keep the decisions from the first day in the handoff summary even when they are old, if they still hold',
    'the handoff summary needs a size limit for each of its parts so that a long task still fits the brief',
    'when the brief is full the handoff summary should drop the least important lines first, not the longest',
]
LATEST = 'make the handoff summary mark the moment the conversation changed subject, with the earlier result'


def seed_long_task(store, root, session):
    """One attached task with 3 subjects, 19 corrections, 9 checkpoints and six items (four met)."""
    attach.handle(store, 'prompt', {'cwd': str(root), 'session_id': session, 'prompt': SUBJECT_A[0]}, 'claude')
    task = attach.current_task(store, root)
    tid = task['id']
    data = task['data']
    data['goal'] = SUBJECT_A[0]
    data['attached_at'] = at(0)
    prompts, feedback = [], []
    minute = 0
    for subject in (SUBJECT_A, SUBJECT_B, SUBJECT_C):
        for i, text in enumerate(subject):
            prompts.append({'at': at(minute), 'text': text, 'session': session, 'kind': 'requirement'})
            if not (subject is SUBJECT_A and i == 0):
                feedback.append({'at': at(minute), 'text': text, 'source': 'prompt', 'kind': 'requirement'})
            minute += STRIDE
    prompts.append({'at': at(minute), 'text': LATEST, 'session': session, 'kind': 'requirement'})
    feedback.append({'at': at(minute), 'text': LATEST, 'source': 'prompt', 'kind': 'requirement'})
    data['prompts'], data['feedback'] = prompts, feedback
    data['done'] = [{'id': 'G0', 'text': 'The original goal', 'how': 'inspect', 'origin': 'orbit'},
                    {'id': 'D5', 'text': 'D5 rewrite the handoff summary at each checkpoint', 'how': 'inspect', 'origin': 'executor'},
                    {'id': 'D6', 'text': 'D6 mark subject changes in the summary', 'how': 'inspect', 'origin': 'executor'}]
    data['done_history'] = [{'rev': 1, 'at': at(10), 'added': ['D1', 'D2']},
                            {'rev': 2, 'at': at(6 * STRIDE + 10), 'added': ['D3', 'D4']},
                            {'rev': 3, 'at': at(12 * STRIDE + 10), 'added': ['D5', 'D6']}]
    data['done_closed'] = [
        {'id': 'D1', 'text': 'D1 services restarted', 'state': 'met', 'at': at(5 * STRIDE), 'evidence': ['brain and Orbit answer after the restart']},
        {'id': 'D2', 'text': 'D2 dashboard 200', 'state': 'met', 'at': at(5 * STRIDE + 5), 'evidence': ['dashboard answered 200']},
        {'id': 'D3', 'text': 'D3 waiting notification', 'state': 'met', 'at': at(11 * STRIDE), 'evidence': ['notification delivered in the replay']},
        {'id': 'D4', 'text': 'D4 reminders', 'state': 'met', 'at': at(11 * STRIDE + 5), 'evidence': ['reminders at 30 min and 2 h']}]
    store.save(task, 'working', 'test_seed', {})
    for n in range(9):                                     # nine handoffs, written the way sessions wrote them
        continuity.record_checkpoint(store, tid, 'continue with step %d of the current subject' % (n + 1),
                                     notes='working notes of handoff %d' % (n + 1))
    return tid


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-consolidation-'))
    store = Store(base / 'home')
    root = make_repo(base, 'repo')
    attach.set_switch(store, 'on', root)

    print('① a long task after nine handoffs: what the fresh session is handed (fails on the code before the change)')
    tid = seed_long_task(store, root, 'long-1')
    task = store.get(tid)
    brief = attach.brief_text(store, task, 'session-start')
    check('the brief fits its limit', len(brief) <= attach.BRIEF_LIMIT, len(brief))
    check('a rule from the first day (correction 1 of 19) is still there', 'never push to main' in brief, brief)
    check('the subject changes are marked', 'SHIFTS' in brief and 'Next: a desktop notification' in brief
          and 'Rework the handoff summary' in brief, brief)
    check('…with what each earlier subject produced (goal → result)', 'met D1 D2' in brief and 'met D3 D4' in brief, brief)
    check('the current focus is named, apart from the first request', 'NOW (' in brief and 'open D5' in brief, brief)
    check('the latest request is in the brief', 'mark the moment the conversation changed subject' in brief, brief)
    listed = sum(1 for text in SUBJECT_A[1:] + SUBJECT_B + SUBJECT_C if text[:40] in brief)
    noise = [t for t in (SUBJECT_A[3], SUBJECT_A[4], SUBJECT_A[5], SUBJECT_B[1], SUBJECT_B[3], SUBJECT_B[5]) if t[:40] in brief]
    check('the finished subjects\' ordinary corrections are not replayed (%d of 18 corrections appear, each as a rule, '
          'a subject change or a recent prompt)' % listed, not noise and listed <= 9, noise or listed)

    print('   control: a short task without a handoff is briefed as before')
    other = make_repo(base, 'other')
    attach.set_switch(store, 'on', other)
    attach.handle(store, 'prompt', {'cwd': str(other), 'session_id': 'short-1',
                                    'prompt': 'Add a footer with the version number to the report page'}, 'claude')
    short = attach.current_task(store, other)
    short['data']['feedback'] = [{'at': at(1), 'text': 'use the version from pyproject', 'source': 'prompt'},
                                 {'at': at(2), 'text': 'grey text, small', 'source': 'prompt'}]
    store.save(short, short['status'], 'test_seed', {})
    plain = attach.brief_text(store, store.get(short['id']), 'session-start')
    check('control: corrections listed, no summary before the first handoff',
          'CORRECTIONS (2)' in plain and 'use the version from pyproject' in plain and 'NOW (' not in plain, plain)

    print('② a full brief gives way by importance, not by length (fails on the code before the change)')
    head = ['[orbit] task t', 'GOAL: g']
    now_line = 'NOW (summary): ' + 'n' * 560
    lines = head + [now_line, 'RECENT PROMPTS: ' + 'r' * 280, 'CONTEXT (the user\'s earlier prompts in this session): ' + 'c' * 280,
                    'CLOSED 4 · ✔ met D1 D2 D3 D4']
    fitted = attach._fit(lines, ['RECORD …'], 900)
    check('the current focus stays whole while the earlier-prompt context gives way', now_line in fitted
          and 'CONTEXT (the user' not in fitted and len(fitted) <= 900, fitted)
    control = attach._fit(lines, ['RECORD …'], 5000)
    check('control: nothing is cut when it fits', all(line in control for line in lines), control)

    print('③ the session writes the summary; Orbit checks it without a model call')
    try:
        from harness import consolidate
    except ImportError as error:
        check('the handoff summary module exists', False, error)
        return finish()
    row = continuity.record_checkpoint(store, tid, 'write the subject-change marker', now_text='D5 and D6 open: write the marker next',
                                       keep=['PRs only: never push to main', 'the restart covers embedding and vector store'],
                                       shifts=['11-14 → handoff summary (before: notifications → met D3 D4)'])
    summary = row['summary']
    check('the session\'s sections are kept as written', summary['by'] == 'session' and summary['now'].startswith('D5 and D6 open')
          and [k['text'] for k in summary['keep']][:2] == ['PRs only: never push to main', 'the restart covers embedding and vector store'], summary)
    brief = attach.brief_text(store, store.get(tid), 'session-start')
    check('the brief leads with the session\'s summary', 'NOW (summary' in brief and 'by the session' in brief
          and 'D5 and D6 open' in brief and 'K1 PRs only' in brief, brief)
    row = continuity.record_checkpoint(store, tid, 'next', now_text='D6 open', keep=['the restart covers embedding and vector store'])
    summary = row['summary']
    carried = [k for k in summary['keep'] if k.get('by') == 'carried']
    check('a previous keep item the session left out is carried over and named',
          carried and carried[0]['text'] == 'PRs only: never push to main'
          and any('K1' in w and '--retire K1' in w for w in summary['warnings']), summary)
    row = continuity.record_checkpoint(store, tid, 'next', now_text='D6 open', keep=['the restart covers embedding and vector store'],
                                       retire=['K2'])
    summary = row['summary']
    check('…until the session retires it (--retire)', all(k['text'] != 'PRs only: never push to main' for k in summary['keep'])
          and not any('PRs only' in w for w in summary['warnings']), summary)
    check('the previous shifts stay when the session adds none', any('handoff summary' in s['text'] for s in summary['shifts']), summary)
    row = continuity.record_checkpoint(store, tid, 'next', now_text='x' * 2000, keep=['rule %d holds for this task' % i for i in range(12)])
    summary = row['summary']
    check('each section is bounded (now %d chars, keep %d items) and the cut is named' % (len(summary['now']), len(summary['keep'])),
          len(summary['now']) <= consolidate.NOW_LIMIT and len(summary['keep']) <= consolidate.KEEP_MAX
          and any('now was 2000' in w for w in summary['warnings']) and any('keep held' in w for w in summary['warnings']), summary)

    task = store.get(tid)
    later = datetime.datetime.now(datetime.timezone.utc)      # after the previous checkpoint, as a new request is
    task['data']['prompts'].append({'at': later.isoformat(), 'text': 'Switch to the billing export: the invoices need the VAT number column in the CSV file',
                                    'session': 'long-1', 'kind': 'requirement'})
    task['data']['prompts'].append({'at': (later + datetime.timedelta(seconds=30)).isoformat(), 'text': 'the invoices CSV export also needs the VAT number column header in English',
                                    'session': 'long-1', 'kind': 'requirement'})
    store.save(task, task['status'], 'test_seed', {})
    row = continuity.record_checkpoint(store, tid, 'next', now_text='billing export', keep=['rule 0 holds for this task'])
    check('a new subject the session did not mark is named as a candidate',
          any('possible subject change' in w and 'billing export' in w for w in row['summary']['warnings']), row['summary']['warnings'])
    request = consolidate.request_text(store.get(tid)['data'], store.get(tid), 'cosmos-orbit', tid)
    check('the checkpoint request shows the previous summary and what is new since',
          'PREVIOUS SUMMARY' in request and 'K1 rule 0 holds' in request and '--retire K2' in request, request)

    print('   the session writes no summary: Orbit builds one from the ledger')
    fresh_root = make_repo(base, 'fresh')
    attach.set_switch(store, 'on', fresh_root)
    tid2 = seed_long_task(store, fresh_root, 'long-2')
    row = continuity.auto_checkpoint(store, store.get(tid2), 'PreCompact') or continuity.record_checkpoint(store, tid2, 'continue')
    summary = row['summary']
    check('rule-built: by Orbit, the prohibition kept, open items as now, subject changes found',
          summary['by'] == 'orbit' and any('never push to main' in k['text'] for k in summary['keep'])
          and 'open D5' in summary['now'] and len(summary['shifts']) >= 2, summary)
    task2 = store.get(tid2)
    for cp in task2['data']['checkpoints']:
        cp.pop('summary', None)                            # a ledger written before summaries existed
    store.save(task2, task2['status'], 'test_seed', {})
    legacy = attach.brief_text(store, store.get(tid2), 'session-start')
    check('a checkpoint from before this change still gets a summary (computed, not stored)',
          'NOW (summary' in legacy and 'by Orbit' in legacy and 'never push to main' in legacy, legacy)
    check('the brief digest ignores the summary (a new checkpoint does not re-send the brief to its own session)',
          not any(attach.brief_text(store, store.get(tid2), 'digest', for_digest=True).count(p) for p in ('NOW (', 'KEEP (', 'SHIFTS (')))

    return finish()


def finish():
    print('\n%s' % ('PASS' if not FAILED else 'FAILED: %d — %s' % (len(FAILED), '; '.join(FAILED))))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
