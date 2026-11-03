#!/usr/bin/env python3
"""Model-free check that a changed request always revises the result and verification goals.

Covers the attached entrance (classification, short notice, claim gate, one Stop request, --add/--keep/--check,
user-fixed items, prompts queued while busy, a change arriving during the review) and the unattended loop
(a correction marks the definitions stale and the executor's revision is adopted). Every rule has a control.
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

from harness import attach, providers, requirements  # noqa: E402
from harness.engine import Engine  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def make_repo(base, name):
    root = base / name
    root.mkdir()
    for args in (['init', '-q'], ['add', '.'], ['commit', '-q', '-m', 'init', '--allow-empty']):
        if args[0] == 'add':
            (root / 'verify.py').write_text('print("ok")\n')
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True, capture_output=True)
    return root


def kinds(store, task_id):
    return [r['kind'] for r in store.events(task_id)]


class FakeReviewer:
    verdict = None
    prompts = []
    during = None  # a callable run while the review holds the task lock

    def __init__(self, name, home, model=None, timeout=None):
        self.name, self.home = name, Path(home)

    def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
        FakeReviewer.prompts.append(prompt)
        if FakeReviewer.during:
            FakeReviewer.during()
        log = self.home / 'runs' / task_id / 'review.events.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({'type': 'item.completed', 'item': {'id': 'r', 'type': 'command_execution', 'command': 'cat', 'exit_code': 0}}) + '\n')
        return json.loads(json.dumps(FakeReviewer.verdict)), {
            'call_id': 'r%d' % len(FakeReviewer.prompts), 'session': 'rev', 'provider': self.name, 'requested_model': None,
            'reported_models': ['fake'], 'context': {'tokens': 1, 'source': 'fake', 'window': None}, 'seconds': 0.1,
            'usage': {}, 'log': str(log), 'invocation': ''}


def all_met(task):
    return {'items': [{'id': d['id'], 'status': 'met', 'evidence': ['observed']} for d in task['data']['done']],
            'extra': [], 'missing': [], 'fix': [], 'question': ''}


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-requirements-'))
    store = Store(base / 'home')
    engine = Engine(store)

    print('① classification: a question or a go-ahead does not change what must be delivered')
    for text in ('계속', '진행해', 'ok', 'continue', 'ㅇㅋ', '이어서 진행해.'):
        check('trivial: ' + text, requirements.classify(text) == 'trivial')
    for text in ('명령언ㄴ 어떻게 돼?', 'what are the commands?'):
        check('question: ' + text, requirements.classify(text) == 'question')
    for text in ('고쳐서 반영해줘', 'Also support CSV export', '그런 요구가 있을때마다 결과 목표와 검증 목표를 업데이트 해야 합니다.'):
        check('requirement: ' + text, requirements.classify(text) == 'requirement')

    print('② attached: a change is recorded, binds the review, and gets one short notice')
    root = make_repo(base, 'repo')
    attach.set_switch(store, 'on', root)
    pay = {'cwd': str(root), 'session_id': 's1'}
    attach.handle(store, 'prompt', dict(pay, prompt='Build an inventory report with totals per item'), 'claude')
    task = attach.current_task(store, root)
    tid = task['id']
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['development']['checks'] = [['python3', 'verify.py']]
        store.save(task, task['status'], 'test_setup', {})
    task = attach.set_done(store, tid, ['Totals per item are printed', 'Empty inventory prints nothing'])
    check('first definitions = rev 1, history initial', task['data']['done_rev'] == 1 and task['data']['done_history'][0]['because'] == 'initial')
    attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    check('go-ahead → no change (control)', attach.handle(store, 'prompt', dict(pay, prompt='계속'), 'claude') is None
          and not store.get(tid)['data'].get('requirements_pending'))
    attach.handle(store, 'prompt', dict(pay, prompt='How are totals rounded?'), 'claude')
    check('question → no change (control)', not store.get(tid)['data'].get('requirements_pending'))
    out = attach.handle(store, 'prompt', dict(pay, prompt='Also support CSV export of the report'), 'claude')
    line = out['hookSpecificOutput']['additionalContext'] if out else ''
    task = store.get(tid)
    check('change recorded: requirement_added, pending, feedback source prompt',
          'requirement_added' in kinds(store, tid) and task['data']['requirements_pending']['text'].startswith('Also support CSV')
          and task['data']['feedback'][-1]['source'] == 'prompt' and task['data']['feedback'][-1]['kind'] == 'requirement')
    check('one short line, not the whole brief again', line.startswith('[orbit] task') and 'REQUIREMENTS CHANGED' in line
          and 'GOAL:' not in line and len(line) < 900, line[:200])

    print('③ the claim gate and one Stop request')
    try:
        attach.claim(store, tid, summary='report done')
        check('finished claim refused while the change is unrevised', False)
    except ValueError as error:
        check('finished claim refused while the change is unrevised', 'done ' + tid in str(error) and '--keep' in str(error))
    partial = attach.claim(store, tid, summary='CSV pending', remaining=['CSV export'])
    check('a partial claim is still allowed (control)', partial['status'] == 'working')
    stop = attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Working on CSV.'), 'claude')
    check('Stop asks once to revise the goals', stop and stop.get('decision') == 'block' and '--add' in stop['reason'] and '--keep' in stop['reason'], stop)
    again = attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Still on CSV.'), 'claude')
    check('asked once', not (again and 'changed the request' in again.get('reason', '')), again)

    print('④ revising: --add with a new verification command; existing checks never change')
    task = attach.set_done(store, tid, ['CSV export writes one row per item'], add=True, checks=['["python3","-c","print(2)"]'])
    data = task['data']
    check('rev 2: D1 D2 kept, D3 added', data['done_rev'] == 2 and [d['id'] for d in data['done'] if d['id'][0] == 'D'] == ['D1', 'D2', 'D3']
          and data['done_history'][-1]['added'] == ['D3'], data['done_history'][-1])
    check('verification goal added: C1 unchanged, C2 new', data['development']['checks'] == [['python3', 'verify.py'], ['python3', '-c', 'print(2)']]
          and [d['id'] for d in data['done'] if d['id'][0] == 'C'] == ['C1', 'C2'])
    check('pending cleared and recorded as the reason', not data.get('requirements_pending') and data['done_history'][-1]['because'].startswith('Also support CSV'))
    attach.handle(store, 'prompt', dict(pay, prompt='Sort the report by item name'), 'claude')
    task = attach.set_done(store, tid, [], keep=True)
    check('--keep: rev 3, same items, marked kept', task['data']['done_rev'] == 3 and task['data']['done_history'][-1]['kept'] is True
          and len([d for d in task['data']['done'] if d['id'][0] == 'D']) == 3)
    try:
        attach.set_done(store, tid, [], checks=['not json'])
        check('malformed check refused', False)
    except ValueError:
        check('malformed check refused', True)

    print('⑤ the review is bound by the change; a change during the review reopens the work')
    original = providers.Provider
    providers.Provider = FakeReviewer
    try:
        task = store.get(tid)
        FakeReviewer.verdict = all_met(task)
        attach.claim(store, tid, summary='report with CSV, sorted')
        FakeReviewer.during = lambda: attach.record_prompt(store, store.get(tid), 'Add a grand total line at the end', 's1')
        task = attach.run_verification(engine, tid)
        FakeReviewer.during = None
        check('reviewer saw the user change in its binding', 'Also support CSV export' in FakeReviewer.prompts[-1] and '"source": "prompt"' in FakeReviewer.prompts[-1])
        check('prompt during the review was queued, not lost', 'feedback_queued' in kinds(store, tid))
        check('verified verdict with a change in flight → working(requirements_changed), not verified',
              task['status'] == 'working' and task['data']['reason_code'] == 'requirements_changed'
              and task['data']['requirements_pending']['text'].startswith('Add a grand total'), (task['status'], task['data'].get('reason_code')))
        task = attach.set_done(store, tid, ['A grand total line ends the report'], add=True)
        FakeReviewer.verdict = all_met(task)
        attach.claim(store, tid, summary='with grand total')
        task = attach.run_verification(engine, tid)
        check('after revising (rev 4) the next review verifies', task['status'] == 'verified' and task['data']['done_rev'] == 4)
    finally:
        providers.Provider = original
        FakeReviewer.during = None

    print('⑥ user-fixed definitions: the executor extends, never replaces')
    root2 = make_repo(base, 'repo2')
    task2 = attach.create(store, root2, 'Add a --json flag to the report', 'claude', done=['--json prints valid JSON'])
    check('user definitions = rev 1 from creation', task2['data']['done_rev'] == 1 and task2['data']['done_fixed']['origin'] == 'user')
    try:
        attach.set_done(store, task2['id'], ['something else'])
        check('executor replace of user items refused', False)
    except ValueError:
        check('executor replace of user items refused', True)
    task2 = attach.set_done(store, task2['id'], ['--json keeps key order stable'], add=True)
    check('--add extends user items, origins kept', [d['origin'] for d in task2['data']['done'] if d['id'][0] == 'D'] == ['user', 'executor'])

    legacy = attach.create(store, make_repo(base, 'repo-legacy'), 'Legacy task before revisions', 'claude')
    with store.lock('task:' + legacy['id']):
        legacy = store.get(legacy['id'])
        legacy['data']['done'] = legacy['data']['done'] + [{'id': 'D1', 'text': 'old item', 'how': 'inspect', 'origin': 'executor'}]
        legacy['data']['done_fixed'] = {'at': '2026-11-01T00:00:00+00:00', 'origin': 'executor', 'turn': 0}
        store.save(legacy, legacy['status'], 'test_setup', {})
    legacy = attach.set_done(store, legacy['id'], ['new item'], add=True)
    check('definitions from before revisions count as rev 1; the change is rev 2', legacy['data']['done_rev'] == 2
          and legacy['data']['done_history'][0]['because'].startswith('initial'), legacy['data']['done_history'])

    print('⑦ busy task: a prompt is queued and applied at the next hook')
    attach.set_switch(store, 'on', root2)
    pay2 = {'cwd': str(root2), 'session_id': 'u1'}
    with store.lock('task:' + task2['id']):
        attach.handle(store, 'prompt', dict(pay2, prompt='Also add a --pretty option'), 'claude')
    check('queued while locked', len(store.inbox(task2['id'])) == 1 and not store.get(task2['id'])['data'].get('requirements_pending'))
    attach.handle(store, 'stop', dict(pay2, stop_hook_active=True, last_assistant_message='x'), 'claude')
    task2 = store.get(task2['id'])
    check('drained at the next hook → pending change', not store.inbox(task2['id']) and task2['data']['requirements_pending']['text'] == 'Also add a --pretty option')

    print('⑧ unattended: a correction marks the definitions stale; the executor revision is adopted')
    from harness.development import create
    root3 = make_repo(base, 'repo3')
    managed = create(store, root3, 'claude', 'Write a slugify function', [], None, 5)
    managed['data']['memory'] = {'mode': 'off'}
    managed['data']['options'].update(auto_handoff=False)
    store.save(managed, 'received', 'test_setup', {})
    mid = managed['id']
    calls = []

    class FakeExecutor:
        def __init__(self, name, home, model=None, timeout=None):
            self.home = Path(home)

        def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
            calls.append(prompt)
            n = len(calls)
            (Path(workspace) / ('w%d.txt' % n)).write_text(str(n))
            log = self.home / 'runs' / task_id / ('e%d.jsonl' % n)
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text('')
            proposals = {1: ['slugify lowercases and joins words with dashes'],
                         2: ['slugify lowercases and joins words with dashes', 'slugify strips accents'],
                         3: ['should be ignored: not stale']}[n] if n <= 3 else []
            if n == 1:
                store.enqueue(task_id, 'Also strip accents (é → e)', None)  # the user corrects while turn 1 runs
            answer = {'summary': 'turn %d' % n, 'finished': False, 'remaining': ['more'], 'question': 'stop for the test' if n == 3 else '',
                      'done_proposed': proposals}
            return answer, {'call_id': 'x%d' % n, 'session': 'ex', 'provider': 'claude', 'requested_model': None, 'reported_models': [],
                            'context': {'tokens': 1000, 'source': 'fake', 'window': None}, 'seconds': 0.1, 'usage': {}, 'log': str(log), 'invocation': ''}

    providers.Provider = FakeExecutor
    try:
        engine.run(mid)
    finally:
        providers.Provider = original
    managed = store.get(mid)
    texts = [d['text'] for d in managed['data']['done'] if d['id'][0] == 'D']
    check('turn 2 prompt told the executor the definitions are stale', '"done_stale": true' in calls[1])
    check('revision adopted: rev 2 covers the correction', managed['data']['done_rev'] == 2 and texts[-1] == 'slugify strips accents', texts)
    check('not stale → a proposal is ignored (control)', 'should be ignored: not stale' not in texts)
    check('history: initial, then the correction as the reason', [h['because'][:12] for h in managed['data']['done_history']] == ['initial', 'Also strip a'],
          [h['because'] for h in managed['data']['done_history']])

    print('⑨ projections')
    from harness.observer import snapshot, terminal
    snap = snapshot(store, tid)['tasks'][0]
    check('snapshot: done_rev and history', snap['done_rev'] == 4 and len(snap['done_history']) == 4)
    check('terminal twin prints the revision', 'rev 4' in terminal(snapshot(store, tid), 'en'))
    brief = attach.brief_text(store, store.get(task2['id']))
    check('brief: DONE (rev 2) and the REQUIREMENTS CHANGED line', 'DONE (rev 2)' in brief and 'REQUIREMENTS CHANGED' in brief)
    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
