#!/usr/bin/env python3
"""Model-free check of the attached entrance: switch precedence, hook silence, ledger, evidence, claim, review, handoff.

The independent reviewer is replaced by a fake provider so no CLI is called. Every assertion has a control
(the same path must stay silent or fail when it should). Exit 0 = pass.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import attach, providers  # noqa: E402
from harness.engine import Engine  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def git(root, *args):
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True, capture_output=True)


def make_repo(base):
    root = base / 'repo'
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'calc.py').write_text('def add(a, b):\n    return a - b\n')
    (root / 'verify.py').write_text('import calc, sys\nsys.exit(0 if calc.add(2, 3) == 5 else 1)\n')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'files')
    return root


class FakeReviewer:
    """Stands in for providers.Provider: returns a verdict decided by the test, records the prompt it saw."""
    verdict = None
    prompts = []

    def __init__(self, name, home, model=None, timeout=None):
        self.name, self.home = name, Path(home)

    def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
        FakeReviewer.prompts.append(prompt)
        directory = self.home / 'runs' / task_id
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / 'fake.events.jsonl'
        # One successful tool observation, as the real reviewer must produce.
        log.write_text(json.dumps({'type': 'item.completed', 'item': {'id': 'c1', 'type': 'command_execution', 'command': 'python3 verify.py', 'exit_code': 0}}) + '\n')
        metadata = {'call_id': 'fake', 'session': 'fake-session', 'provider': self.name, 'requested_model': None,
                    'reported_models': ['fake'], 'context': {'tokens': 1, 'source': 'fake', 'window': None},
                    'seconds': 0.1, 'usage': {'input_tokens': 10, 'output_tokens': 5}, 'log': str(log), 'invocation': ''}
        if callable(getattr(self, 'on_call', None)):
            pass
        return json.loads(json.dumps(FakeReviewer.verdict)), metadata


def verify_supervision(base):
    from unittest.mock import patch
    from harness import supervision, wake
    store = Store(base / 'supervision-home')
    root = base / 'repo'
    attach.set_switch(store, 'on', global_default=True)
    supervision.configure(store, 'auto')
    task = attach.create(store, root, 'Complete both stages of the requested work', 'codex', 'resume-session',
                         done=['Both stages verified'])
    tid = task['id']
    attach.claim(store, tid, remaining=['second stage'])
    terminal = {'kind': 'tmux', 'cli': 'tmux', 'target': '%99'}
    calls = []
    def runner(argv):
        calls.append(argv)
        return 0, '› Ask a follow-up question\n\n'
    wake.note(store, 'resume-session', 'codex', 'stop', terminal)
    supervision.record_stop(store, store.get(tid), 'resume-session')
    wake.note(store, 'resume-session', 'codex', 'prompt', terminal)
    check('supervisor: empty-looking screen cannot resume an active turn',
          supervision.tick(store, tid, 'resume-session', runner) == 'host_turn_active' and not calls)
    wake.note(store, 'resume-session', 'codex', 'stop', terminal)
    check('supervisor: a newer terminal event invalidates the old stop',
          supervision.tick(store, tid, 'resume-session', runner) == 'host_turn_active' and not calls)
    supervision.record_stop(store, store.get(tid), 'resume-session')
    check('supervisor: stopped empty host resumes once',
          supervision.tick(store, tid, 'resume-session', runner) == 'continued'
          and len([c for c in calls if 'send-keys' in c]) == 2)
    store.set_paused(tid, True)
    check('supervisor: pending user pause prevents resume before status transitions',
          supervision.tick(store, tid, 'resume-session', runner) == 'settled'
          and supervision.stop_block(store, store.get(tid)) is None)
    store.set_paused(tid, False)
    calls.clear()
    check('supervisor: repeated observation never repeats a delivery',
          supervision.tick(store, tid, 'resume-session', runner) == 'awaiting_stop' and not calls)
    reviewing = store.get(tid)
    reviewing['data']['verification_pending'] = True
    store.save(reviewing, 'verifying', 'test_review')
    with store.lock('worker'), store.lock('task:' + tid):
        check('supervisor: actual verifying state keeps observer waiting',
              supervision.tick(store, tid, 'resume-session', runner) == 'reviewing')
    reviewing['data']['verification_pending'] = False
    store.save(reviewing, 'working', 'test_review_finished')
    # Reproduce the incident: stale queued claim + workspace held by another task.
    stale = store.get(tid)
    stale['data'].update(verification_pending=True, requirements_pending={'text': 'Install and verify the result', 'unsure': False})
    store.save(stale, 'checking', 'test_stale_claim')
    wake.note(store, 'resume-session', 'codex', 'stop', terminal)
    supervision.record_stop(store, store.get(tid), 'resume-session')
    with store.lock('workspace:' + stale['workspace']), patch.object(wake, 'allowed', return_value=(True, 0)):
        check('supervisor: stale queued claim reopens and resumes despite another workspace owner',
              supervision.tick(store, tid, 'resume-session', runner) == 'continued')
    recovered = store.get(tid)
    check('supervisor: recovery leaves truthful unfinished state', recovered['status'] == 'working'
          and not recovered['data']['verification_pending'] and not recovered['data']['executor_result']['finished'])
    attach.set_done(store, tid, [], keep=True)
    with patch('harness.recovery.ensure_worker') as start_worker:
        queued = store.get(tid)
        queued['data']['verification_pending'] = True
        store.save(queued, 'checking', 'test_worker_lost')
        check('supervisor: missing verification worker is restarted',
              supervision.tick(store, tid, 'resume-session', runner) == 'worker_stopped' and start_worker.call_count == 1)
        queued['data']['verification_pending'] = False
        store.save(queued, 'working', 'test_worker_recovered')
    supervision.begin_turn(store, store.get(tid), 'resume-session')
    with patch.object(supervision, 'ensure'), patch.object(attach, '_pane', return_value=terminal):
        for repeated in (False, True):
            decision = attach.handle(store, 'stop', {'cwd': str(root), 'session_id': 'resume-session',
                                     'stop_hook_active': repeated}, 'codex')
            check('supervisor: unfinished stop stays blocked, repeated=' + str(repeated),
                  decision and decision.get('decision') == 'block')
    check('supervisor: repeated Stop blocks are bounded',
          supervision.stop_block(store, dict(store.get(tid), data=dict(store.get(tid)['data'],
              supervision={'resume-session': {'stop_count': 3}}))) is None)
    pending = store.get(tid)
    pending['data']['handoff_state'] = {'session': 'resume-session', 'state': 'recommended', 'limit': 350000}
    store.save(pending, pending['status'], 'test_handoff')
    check('supervisor: context handoff takes precedence', supervision.stop_block(store, pending) is None
          and supervision.tick(store, tid, 'resume-session', runner) == 'awaiting_handoff')
    decision = attach.context_decision(store, pending)
    check('supervisor: unfinished task still requests a context checkpoint', decision and decision.get('decision') == 'block')
    attach.claim(store, tid, remaining=['source unavailable'], question='Which source should be used?')
    check('supervisor: real input requirement releases the stop guard',
          supervision.stop_block(store, store.get(tid)) is None)
    store.close()


def main():
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'  # calc.py is rewritten within one second; a stale .pyc would fake a failure
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-attach-'))
    home = base / 'home'
    root = make_repo(base)
    store = Store(home)
    engine = Engine(store)
    os.environ.pop('ORBIT', None)
    os.environ.pop('COSMOS_ORBIT', None)

    print('① switch precedence')
    check('unset → off', attach.resolve(store, root) == ('off', 'unset'))
    attach.set_switch(store, 'on', global_default=True)
    check('default on', attach.resolve(store, root) == ('on', 'default'))
    attach.set_switch(store, 'off', root)
    check('project off beats default on', attach.resolve(store, root) == ('off', 'project'))
    os.environ['ORBIT'] = 'on'
    check('environment beats project', attach.resolve(store, root) == ('on', 'environment'))
    os.environ.pop('ORBIT')
    attach.set_switch(store, 'on', root)
    view = attach.settings_view(store, root)
    check('settings view', view['here']['orbit'] == 'on' and view['here']['git'] and view['default'] == 'on')

    print('② hooks: silence when off / outside git (controls), brief when on')
    prompt = {'cwd': str(root), 'prompt': 'Fix add so that add(2, 3) returns 5 and stop using subtraction', 'session_id': 's1'}
    attach.set_switch(store, 'off', root)
    check('off → silent', attach.handle(store, 'prompt', prompt, 'claude') is None)
    attach.set_switch(store, 'on', root)
    outside = base / 'plain'
    outside.mkdir()
    check('outside git → silent', attach.handle(store, 'prompt', dict(prompt, cwd=str(outside)), 'claude') is None)
    check('short prompt, no task → silent', attach.handle(store, 'prompt', dict(prompt, prompt='hi'), 'claude') is None)
    out = attach.handle(store, 'prompt', prompt, 'claude')
    text = out['hookSpecificOutput']['additionalContext']
    check('prompt → task created + brief', out['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit' and text.startswith('[orbit] task'))
    task = attach.current_task(store, root)
    check('task attached · working · goal = prompt', task and task['status'] == 'working' and task['data']['goal'] == prompt['prompt'])
    check('brief names done proposal command', ' done ' + task['id'] in text and 'DONE: none yet' in text)
    out2 = attach.handle(store, 'prompt', dict(prompt, prompt='please keep going'), 'claude')
    task = store.get(task['id'])
    check('second prompt recorded, no new task', attach.current_task(store, root)['id'] == task['id'] and len(task['data']['prompts']) == 2)
    start = attach.handle(store, 'session-start', {'cwd': str(root), 'source': 'compact'}, 'claude')
    check('session-start (compact) → brief with recent prompts', start and 'RECENT PROMPTS' in start['hookSpecificOutput']['additionalContext'])
    check('stop with nothing to say → silent', attach.handle(store, 'stop', {'cwd': str(root), 'stop_hook_active': False, 'last_assistant_message': 'Looking at the file.'}, 'claude') is None)
    nudge = attach.handle(store, 'stop', {'cwd': str(root), 'stop_hook_active': False, 'last_assistant_message': 'I fixed it, all done.'}, 'claude')
    check('stop with completion wording and no claim → block once', nudge and nudge.get('decision') == 'block' and ' done ' + task['id'] in nudge['reason'])
    check('stop_hook_active → silent (no loop)', attach.handle(store, 'stop', {'cwd': str(root), 'stop_hook_active': True, 'last_assistant_message': 'all done'}, 'claude') is None)

    print('③ done · evidence · claim with a failing fixed check')
    tid = task['id']
    attach.set_done(store, tid, ['add(2, 3) returns 5', 'subtraction is not used'])
    task = store.get(tid)
    check('done D1 D2 (+G0)', [d['id'] for d in task['data']['done']] == ['G0', 'D1', 'D2'] and task['data']['done_fixed']['origin'] == 'executor')
    try:
        attach.record_evidence(store, tid, 'D9', note='x')
        check('unknown item rejected', False)
    except ValueError:
        check('unknown item rejected', True)
    entry = attach.record_evidence(store, tid, 'D1', note='ran verify', command='["python3","verify.py"]')
    check('command evidence recorded with exit 1', entry['kind'] == 'command' and entry['exit_code'] == 1)
    # Give the task a fixed check through the attached ledger (as attach --check would) to exercise the recovery loop.
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['development']['checks'] = [['python3', 'verify.py']]
        from harness.simple_loop import completion_items
        task['data']['done'] = completion_items(['add(2, 3) returns 5', 'subtraction is not used'], task['data']['development']['checks'], 'executor')
        store.save(task, task['status'], 'goal_fixed', {'origin': 'test'})
    attach.claim(store, tid, summary='fixed add')
    task = attach.run_verification(engine, tid, checks_only=True)
    check('claim → fixed check fails → working(checks_failed), no review', task['status'] == 'working' and task['data']['reason_code'] == 'checks_failed'
          and not task['data']['verification_pending'] and not task['data'].get('verdicts'))
    blocked = attach.handle(store, 'stop', {'cwd': str(root), 'stop_hook_active': False, 'last_assistant_message': 'claimed'}, 'claude')
    check('stop after failed check → block with failure tail', blocked and blocked.get('decision') == 'block' and 'verify.py' in blocked['reason'])
    brief = attach.brief_text(store, store.get(tid))
    check('brief carries FIXED CHECK FAILED and C1 ✗', 'FIXED CHECK FAILED' in brief and 'C1 ✗' in brief)

    print('④ fix → claim → fake independent review: unmet, then verified')
    (root / 'calc.py').write_text('def add(a, b):\n    return a + b\n')
    entry = attach.record_evidence(store, tid, 'D1', note='verify after fix', command='["python3","verify.py"]')
    check('evidence exit 0 after fix', entry['exit_code'] == 0)
    attach.record_evidence(store, tid, 'D2', file='calc.py', note='no minus')
    task = store.get(tid)
    check('file evidence has sha', task['data']['done_evidence']['D2'][-1]['sha256'])
    original = providers.Provider
    providers.Provider = FakeReviewer
    try:
        FakeReviewer.verdict = {'items': [{'id': 'G0', 'status': 'met', 'evidence': ['goal addressed']},
                                          {'id': 'D1', 'status': 'met', 'evidence': ['verify.py exit 0']},
                                          {'id': 'D2', 'status': 'unmet', 'evidence': ['comment still says minus']},
                                          {'id': 'C1', 'status': 'met', 'evidence': ['host check exit 0']}],
                                'extra': [], 'missing': [], 'fix': ['remove the stale comment'], 'question': ''}
        attach.claim(store, tid, summary='fixed add, verify passes')
        task = attach.run_verification(engine, tid)
        check('checks pass → one review call', task['data']['reason_code'] == 'unmet' and task['status'] == 'working' and len(task['data']['verdicts']) == 1)
        check('reviewer saw executor evidence as claims', 'executor_evidence' in FakeReviewer.prompts[-1] and 'claims to check' in FakeReviewer.prompts[-1])
        brief = attach.brief_text(store, task)
        check('brief shows D2 ✗ and the fix', 'D2 ✗' in brief and 'remove the stale comment' in brief)
        stop = attach.handle(store, 'stop', {'cwd': str(root), 'stop_hook_active': False, 'last_assistant_message': 'claimed'}, 'claude')
        check('stop after unmet verdict → continue the unfinished task', stop and stop.get('decision') == 'block')
        FakeReviewer.verdict = {'items': [{'id': 'G0', 'status': 'met', 'evidence': ['goal addressed']},
                                          {'id': 'D1', 'status': 'met', 'evidence': ['verify.py exit 0']},
                                          {'id': 'D2', 'status': 'met', 'evidence': ['no subtraction in calc.py']},
                                          {'id': 'C1', 'status': 'met', 'evidence': ['host check exit 0']}],
                                'extra': [], 'missing': [], 'fix': [], 'question': ''}
        attach.claim(store, tid, summary='comment removed')
        task = attach.run_verification(engine, tid)
        check('second claim → verified', task['status'] == 'verified' and len(task['data']['verdicts']) == 2)
        FakeReviewer.verdict = {'items': [{'id': 'G0', 'status': 'met', 'evidence': ['x']}, {'id': 'D1', 'status': 'met', 'evidence': ['x']},
                                          {'id': 'D2', 'status': 'met', 'evidence': ['x']}, {'id': 'C1', 'status': 'met', 'evidence': ['x']}],
                                'extra': ['rewrote README'], 'missing': [], 'fix': [], 'question': ''}
        # Control: a new task whose reviewer reports scope drift must stop for the user, never verify.
        task2 = attach.create(store, root, 'Rename add to plus and keep behaviour', 'claude', checks=['["python3","verify.py"]'], done=['plus(2, 3) returns 5', 'add still works'])
        attach.claim(store, task2['id'], summary='renamed')
        task2 = attach.run_verification(engine, task2['id'])
        check('control: extra → needs_input(scope_drift), not verified', task2['status'] == 'needs_input' and task2['data']['reason_code'] == 'scope_drift')
        answer = attach.handle(store, 'prompt', {'cwd': str(root), 'prompt': 'The README change is fine, keep it.', 'session_id': 's2'}, 'claude')
        task2 = store.get(task2['id'])
        check('prompt answers the question → feedback applied, working', task2['status'] == 'working' and task2['data']['feedback'][-1]['source'] == 'prompt' and 'CORRECTIONS (1)' in answer['hookSpecificOutput']['additionalContext'])
    finally:
        providers.Provider = original

    print('④-b command-line corrections are marked as such; the brief does not invite the executor to write them')
    fb = attach.cli_feedback(store, task2['id'], 'D3 is outdated; parts 2 and 3 are in scope')
    check('cli feedback recorded with source cli, task keeps working', fb['data']['feedback'][-1]['source'] == 'cli' and fb['status'] == 'working', fb['data']['feedback'][-1])
    brief = attach.brief_text(store, fb)
    check('executor brief has no feedback invitation', ' feedback ' + task2['id'] not in brief and ' done ' + task2['id'] in brief)
    from harness import verifier
    import inspect
    check('reviewer is told cli corrections are claims', 'source "cli"' in inspect.getsource(verifier.evaluate))

    print('⑤ projections: handoff, status snapshot, lessons, detach')
    path = attach.write_handoff(store, store.get(tid), base / 'HANDOFF.md')
    text = Path(path).read_text()
    check('handoff has goal, done table, review, continue', all(x in text for x in ('## Goal', '| D1 |', 'Outcome **verified**', '## Continue', 'exit 0')))
    from harness.observer import snapshot, terminal
    snap = snapshot(store, tid)['tasks'][0]
    check('snapshot: attached · verified · evidence · briefs', snap['mode'] == 'attached' and snap['status'] == 'verified'
          and snap['done_evidence']['D1'][-1]['exit_code'] == 0 and snap['briefs']['served'] >= 2, snap['briefs'])
    check('unchanged brief is not repeated in the same session', (snap['briefs'] or {}).get('skipped', 0) >= 1, snap['briefs'])
    snap2 = snapshot(store, task2['id'])['tasks'][0]
    check('snapshot: attached working is not unattended', snap2['status'] == 'working' and snap2['operation']['title_key'] == 'simple.attached')
    rendered = terminal(snapshot(store), 'en')
    check('terminal twin shows entrance line', 'entrance: attached' in rendered)
    for i in range(2):
        with store.lock('task:' + tid):
            t = store.get(tid)
            t['data']['feedback'].append({'text': 'Never use Serper.', 'revision': 0, 'message_id': None})
            store.save(t, t['status'], 'feedback_applied', {})
    rows = attach.lessons(store, 2)
    check('lessons groups the repeated correction', rows and rows[0]['count'] == 2 and 'Serper' in rows[0]['text'])
    check('lessons control: singletons excluded', all(r['count'] >= 2 for r in rows))
    attach.detach(store, root)
    task2 = store.get(task2['id'])
    check('detach → stalled(detached), attachment cleared', task2['status'] == 'stalled' and task2['data']['reason_code'] == 'detached' and store.attached(str(root)) is None)
    check('next long prompt starts a new task', attach.handle(store, 'prompt', dict(prompt, prompt='Now add a multiply function with tests'), 'claude')
          and attach.current_task(store, root)['id'] not in (tid, task2['id']))

    print('⑥ hooks install / uninstall round trip (temporary host files)')
    os.environ['CLAUDE_CONFIG_DIR'] = str(base / 'claude')
    os.environ['CODEX_HOME'] = str(base / 'codex')
    (base / 'claude').mkdir()
    (base / 'claude' / 'settings.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo keep'}]}]}, 'other': 1}))
    for host in ('claude', 'codex'):
        attach.install_hooks(host)
        attach.install_hooks(host)  # idempotent
        status = attach.hook_status(host)
        check(host + ' installed once', status['installed'] and status['events'] == [e for e, _, _ in attach.HOST_EVENTS[host]], status)
    data = json.loads((base / 'claude' / 'settings.json').read_text())
    ours = [e for e in data['hooks']['Stop'] if attach._is_ours(e)]
    check('claude: existing Stop hook kept, ours added exactly once', len(data['hooks']['Stop']) == 2 and len(ours) == 1 and data['other'] == 1)
    check('backup written', any(p.name.startswith('settings.json.bak-orbit-') for p in (base / 'claude').iterdir()))
    for host in ('claude', 'codex'):
        attach.uninstall_hooks(host)
        check(host + ' uninstalled', not attach.hook_status(host)['installed'])
    data = json.loads((base / 'claude' / 'settings.json').read_text())
    check('claude: foreign hook survives uninstall', data['hooks']['Stop'][0]['hooks'][0]['command'] == 'echo keep' and 'UserPromptSubmit' not in data['hooks'])
    store.close()
    verify_supervision(base)
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
