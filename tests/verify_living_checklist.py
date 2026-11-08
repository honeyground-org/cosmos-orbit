#!/usr/bin/env python3
"""Model-free check of the living checklist: the goal stays, the list of completion items moves.

A conversation keeps adding work, rewording it and withdrawing it while one thing is being completed. Orbit keeps that
list honest: IDs are permanent, what a clean review found met is closed and not judged again (a reviewer may reopen
it with an observation), a withdrawal or a reworded item is reviewed against the user's own words before it stands,
the current objective follows the conversation while the original request still binds, and evidence commands Orbit
ran on the host are reported apart from the executor's notes. Every rule has a control.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH'):
    os.environ.pop(name, None)

from harness import attach, manager, providers, requirements, verifier  # noqa: E402
from harness.engine import Engine  # noqa: E402
from harness.evidence import evidence_bundle  # noqa: E402
from harness.simple_loop import completion_items  # noqa: E402
from harness.store import Store  # noqa: E402

# Nothing in this suite starts a process (see verify_requirements for why).
from types import SimpleNamespace as _Namespace  # noqa: E402
from unittest.mock import patch as _no_process  # noqa: E402
from harness import evolve as _evolve, recovery as _recovery, supervision as _supervision  # noqa: E402
_no_process.object(_recovery, 'ensure_worker', return_value=None).start()
_no_process.object(_evolve, 'maybe_start', return_value=None).start()
_no_process.object(_supervision, 'subprocess', _Namespace(
    Popen=lambda *a, **k: _Namespace(pid=None), run=__import__('subprocess').run,
    DEVNULL=__import__('subprocess').DEVNULL, SubprocessError=__import__('subprocess').SubprocessError)).start()

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


class FakeReviewer:
    """Answers with whatever `verdict(done_ids)` returns for the IDs the review was asked to assess."""
    verdict = None
    prompts = []
    during = None   # run while the review holds the task lock (a message arriving mid-review)

    def __init__(self, name, home, model=None, timeout=None):
        self.name, self.home = name, Path(home)

    def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
        FakeReviewer.prompts.append(prompt)
        if FakeReviewer.during:
            FakeReviewer.during()
            FakeReviewer.during = None
        payload = json.loads(prompt[prompt.index('\n{') + 1:])
        log = self.home / 'runs' / task_id / 'review.events.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({'type': 'item.completed', 'item': {'id': 'r', 'type': 'command_execution', 'command': 'cat', 'exit_code': 0}}) + '\n')
        return FakeReviewer.verdict([d['id'] for d in payload['done']], payload), {
            'call_id': 'r%d' % len(FakeReviewer.prompts), 'session': 'rev', 'provider': self.name, 'requested_model': None,
            'reported_models': ['fake'], 'context': {'tokens': 1, 'source': 'fake', 'window': None}, 'seconds': 0.1,
            'usage': {}, 'log': str(log), 'invocation': ''}


def answer(statuses, default='met', reopen=(), fix=()):
    def build(ids, payload):
        return {'items': [{'id': i, 'status': statuses.get(i, default), 'evidence': ['observed ' + i]} for i in ids],
                'extra': [], 'missing': [], 'fix': list(fix), 'question': '', 'reopen': list(reopen)}
    return build


def make_repo(base, name):
    root = base / name
    root.mkdir()
    (root / 'verify.py').write_text('print("ok")\n')
    for args in (['init', '-q'], ['add', '.'], ['commit', '-q', '-m', 'init']):
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True, capture_output=True)
    return root


def user_says(store, tid, text):
    """A prompt the user typed: Orbit marks the pending change exactly as the prompt hook does."""
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data'].setdefault('prompts', []).append({'at': requirements.now(), 'text': text, 'session': 's1'})
        requirements.mark_pending(task['data'], text, 'prompt')
        store.save(task, task['status'], 'requirement_marked', {})


def review(engine, store, tid, build):
    FakeReviewer.verdict = build
    attach.claim(store, tid, summary='claim')
    return attach.run_verification(engine, tid)


def open_ids(task):
    return [d['id'] for d in task['data']['done'] if d['id'].startswith('D')]


def closed(task):
    return {d['id']: d for d in task['data'].get('done_closed') or []}


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-checklist-'))
    store = Store(base / 'home')
    engine = Engine(store)
    original = providers.Provider
    providers.Provider = FakeReviewer
    try:
        run(base, store, engine)
    finally:
        providers.Provider = original
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'all checks passed') + ' · ' + str(base))
    return 1 if FAILED else 0


def run(base, store, engine):
    root = make_repo(base, 'app')
    print('① permanent IDs')
    task = attach.create(store, root, 'Build the report page', 'claude', checks=['["python3","verify.py"]'])
    tid = task['id']
    attach.set_done(store, tid, ['report renders', 'totals are correct'])
    task = attach.set_done(store, tid, ['CSV export'], add=True)
    check('--add issues the next number', open_ids(task) == ['D1', 'D2', 'D3'], open_ids(task))
    task = attach.set_done(store, tid, ['D1 report renders', 'totals are correct to the cent', 'CSV export'])
    check('a rewrite keeps word-for-word items (an ID prefix aside) and numbers reworded ones anew',
          open_ids(task) == ['D1', 'D4', 'D3'], open_ids(task))
    check('…the reworded item is kept as superseded, awaiting review', closed(task).get('D2', {}).get('state') == 'superseded'
          and closed(task)['D2'].get('approved') is None, closed(task))
    check('control: a reworded item never keeps its ID, even when the text names it',
          attach.set_done(store, tid, ['D1 report renders', 'D4 totals are correct to the cent', 'D3 CSV and PDF export'])
          and open_ids(store.get(tid)) == ['D1', 'D4', 'D5'], open_ids(store.get(tid)))
    data = {'mode': 'attached', 'done': [], 'done_history': [{'added': ['D9']}], 'verdicts': [{'items': [{'id': 'D11'}]}],
            'done_evidence': {'D7': []}}
    check('an ID used anywhere in the ledger is never issued again', requirements._id_issuer(data)() == 'D12')
    check('control: a managed list is still numbered by position',
          [d['id'] for d in completion_items(['a', 'b'], [], 'user') if d['id'].startswith('D')] == ['D1', 'D2'])
    try:
        completion_items(['a'], [], 'user', ['G0'])
        check('an ID that is not a D number is refused', False)
    except ValueError:
        check('an ID that is not a D number is refused', True)

    print('② a withdrawal is reviewed against the user\'s words')
    task = store.get(tid)
    review_ids = [d['id'] for d in requirements.review_items(task['data'])]
    check('the review assesses the open items and every withdrawal awaiting judgement',
          {'D2', 'D3'} <= set(review_ids) and 'D1' in review_ids, review_ids)
    task = review(engine, store, tid, answer({'D2': 'met', 'D3': 'unmet', 'D5': 'unclear'}))
    check('a withdrawal found supported is approved', closed(task)['D2'].get('approved') is True, closed(task)['D2'])
    check('a withdrawal found unmet puts the obligation back on the open list', 'D3' in open_ids(task), open_ids(task))
    check('…and the next instruction says so', any('D3' in s and 'withdrawal' in s for s in task['data']['resumes'][-1]['sent']),
          task['data']['resumes'][-1]['sent'])
    check('items found met are closed with the review that found them',
          closed(task).get('D1', {}).get('state') == 'met' and closed(task)['D1'].get('verdict_turn') is not None, closed(task).get('D1'))
    check('control: an unclear item stays open', 'D5' in open_ids(task), open_ids(task))
    verdict_event = [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM events WHERE task_id=? AND kind='verdict'", (tid,))][-1]
    check('the list change is saved with the verdict (one save)', verdict_event.get('applied', {}).get('closed') == ['D1', 'D4']
          or 'D1' in verdict_event.get('applied', {}).get('closed', []), verdict_event.get('applied'))

    print('③ closed items are not judged again; a reviewer may reopen one with an observation')
    task = store.get(tid)
    check('the next review does not assess closed items', 'D1' not in [d['id'] for d in requirements.review_items(task['data'])])
    check('…but sees them', any(row['id'] == 'D1' for row in requirements.review_view(task['data'])))
    task = review(engine, store, tid, answer({}, default='unclear', reopen=[{'id': 'D1', 'evidence': ['the page 500s now']},
                                                                            {'id': 'D9', 'evidence': ['unknown']},
                                                                            {'id': 'D4', 'evidence': []}]))
    check('a reopen with an observation puts D1 back', 'D1' in open_ids(task) and task['data']['done'][1]['id'] == 'D1'
          or 'D1' in open_ids(task), open_ids(task))
    check('control: a reopen of an unknown ID or without an observation is ignored',
          [r['id'] for r in task['data']['verdicts'][-1].get('reopen')] == ['D1'], task['data']['verdicts'][-1].get('reopen'))
    check('a reopen makes the outcome at least working', task['data']['verdicts'][-1]['outcome'] == 'working')
    check('…and reaches the next instruction', any(s.startswith('D1: reopened') for s in task['data']['resumes'][-1]['sent']))
    check('a result saved before reopen existed is still read',
          verifier.assess({'items': [{'id': 'G0', 'status': 'met', 'evidence': ['x']}], 'extra': [], 'missing': [], 'fix': [],
                           'question': ''}, [{'id': 'G0'}])[1] == 'verified')

    print('④ only a clean review closes or approves')
    user_says(store, tid, 'also add a chart')
    task = attach.set_done(store, tid, ['a chart of monthly totals'], add=True)
    def arrives():
        with store.db:
            store.db.execute('INSERT INTO inbox(task_id, at, text, consumed) VALUES (?,?,?,0)',
                             (tid, requirements.now(), 'and make it blue please'))
    FakeReviewer.during = arrives
    before = set(closed(store.get(tid)))
    task = review(engine, store, tid, answer({}))
    check('a review with a requirement waiting in the inbox closes nothing',
          set(closed(task)) == before and task['data']['verdicts'][-1]['applied']['clean'] is False,
          task['data']['verdicts'][-1].get('applied'))
    with store.db:
        store.db.execute('UPDATE inbox SET consumed=1 WHERE task_id=?', (tid,))
    FakeReviewer.during = None

    print('⑤ the limit is on open items; closed ones do not count')
    t2 = attach.create(store, root, 'Twelve-step migration', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, t2, ['step %d' % i for i in range(1, 13)])
    task = review(engine, store, t2, answer({'D12': 'unmet'}))
    check('eleven met items closed, one open', len(open_ids(task)) == 1 and len(closed(task)) == 11, open_ids(task))
    user_says(store, t2, 'add eleven more steps')
    task = attach.set_done(store, t2, ['more %d' % i for i in range(1, 12)], add=True)
    check('twelve open after closing eleven is allowed', len(open_ids(task)) == 12)
    user_says(store, t2, 'and one more')
    try:
        attach.set_done(store, t2, ['one too many'], add=True)
        check('control: a thirteenth open item is refused', False)
    except ValueError:
        check('control: a thirteenth open item is refused', True)
    with store.lock('task:' + t2):
        task = store.get(t2)
        requirements.reopen(task['data'], 'D1', ['regressed'])
        store.save(task, task['status'], 'test_reopen', {})
    task = attach.set_done(store, t2, [], keep=True)
    check('a reopened item may exceed the limit, and --keep still works', len(open_ids(task)) == 13, len(open_ids(task)))

    print('⑥ reductions and the objective need the user\'s words')
    t3 = attach.create(store, root, 'Ship the onboarding flow', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, t3, ['welcome screen', 'email verification', 'tutorial'])
    for what, call in (('drop', lambda: attach.set_done(store, t3, [], drop=['D3'], because='not needed')),
                       ('goal', lambda: attach.set_done(store, t3, [], goal='Ship onboarding without the tutorial'))):
        try:
            call()
            check('control: %s without a pending user change is refused' % what, False)
        except ValueError:
            check('control: %s without a pending user change is refused' % what, True)
    user_says(store, t3, '튜토리얼은 빼자. 온보딩은 환영 화면과 이메일 인증까지만.')
    task = attach.set_done(store, t3, [], drop=['D3'], because='the user removed the tutorial',
                           goal='Ship onboarding: welcome screen and email verification')
    row = closed(task)['D3']
    check('the drop records the user\'s own words, not the executor\'s paraphrase',
          row['state'] == 'dropped' and row['user_words']['text'].startswith('튜토리얼은 빼자') and row['because'] == 'the user removed the tutorial', row)
    check('the objective changes and the original request does not',
          task['data']['objective']['text'].startswith('Ship onboarding:') and task['data']['goal'] == 'Ship the onboarding flow')
    check('…and the change is one revision that answers the pending change',
          task['data']['done_history'][-1]['changes'] == ['objective', 'dropped D3'] and not task['data'].get('requirements_pending'),
          task['data']['done_history'][-1])
    brief = attach.brief_text(store, task)
    check('the brief shows the current goal and the original request', 'GOAL (current' in brief and 'ORIGINAL REQUEST (still binds)' in brief, brief[:300])
    check('the brief shows the withdrawal awaiting review', '− D3 dropped (awaiting review)' in brief, brief)
    task = review(engine, store, t3, answer({'D3': 'unclear'}))
    check('control: a withdrawal the review left unclear blocks completion', task['status'] != 'verified'
          and closed(task)['D3'].get('approved') is None)
    payload = json.loads(FakeReviewer.prompts[-1][FakeReviewer.prompts[-1].index('\n{') + 1:])
    check('the reviewer receives the objective, the closed list and the WITHDRAWN item',
          payload['objective']['text'].startswith('Ship onboarding:') and any(d['id'] == 'D3' and d['text'].startswith('WITHDRAWN')
                                                                             for d in payload['done']))
    try:
        attach.set_done(store, t3, [], replace=[('D9', 'x')])
        check('control: replacing an unknown item is refused', False)
    except ValueError:
        check('control: replacing an unknown item is refused', True)

    print('⑦ every item closed: the task is defined, G0 and the checks remain')
    task = review(engine, store, t3, answer({}))
    check('a clean pass verifies the task', task['status'] == 'verified', task['status'])
    t4 = attach.create(store, root, 'Fix the login bug', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, t4, ['login works'])
    task = review(engine, store, t4, answer({'G0': 'unclear'}))
    check('D1 closed while G0 is unclear', open_ids(task) == [] and 'D1' in closed(task))
    found = manager.diagnose(task)
    check('the manager does not ask to define items again', found['reason'] != 'undefined', found)
    brief = attach.brief_text(store, task)
    check('the brief says every item is closed', 'OPEN: none' in brief and 'DONE: none yet' not in brief, brief)
    check('the progress line says G0 and the checks remain', 'every item closed' in (manager.progress_line(task) or ''),
          manager.progress_line(task))
    markdown = attach.handoff_markdown(store, task)
    check('the handoff lists the closed items', '## Closed items (1)' in markdown and '✔ met' in markdown)

    print('⑧ commands Orbit ran are host observations; notes stay claims')
    t5 = attach.create(store, root, 'Check the service', 'claude')['id']
    attach.set_done(store, t5, ['the service answers'])
    attach.record_evidence(store, t5, 'D1', note='looked fine', command='["python3","verify.py"]')
    attach.record_evidence(store, t5, 'D1', note='I checked by hand')
    bundle = evidence_bundle(store.get(t5))
    host = bundle['host_observations'].get('D1') or []
    check('the Orbit-run command is a host observation with its exit code and output',
          len(host) == 1 and host[0]['exit_code'] == 0 and 'ok' in host[0]['output_tail'], host)
    claims = bundle['executor_evidence']['D1']
    check('control: its note and the hand-written note stay claims, without the output',
          claims[0].get('output') == 'see host_observations' and claims[1]['note'] == 'I checked by hand', claims)
    attach.set_done(store, t5, ['the service answers'], keep=True)
    task = review(engine, store, t5, answer({}))
    entry = attach.record_evidence(store, t4, 'D1', note='closed items take evidence too')
    check('a closed item takes evidence (the argument for reopening it)', entry['note'].startswith('closed items'))


if __name__ == '__main__':
    sys.exit(main())
