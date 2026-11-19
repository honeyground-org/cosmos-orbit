#!/usr/bin/env python3
"""A review that leaves out a withdrawal is rejected for what it is, not as a changed request (2026-11-19). Model-free.

The incident, from the ledger: an attached task had 24 withdrawals awaiting judgement (D1–D24, superseded) besides its
open items. Orbit asked the reviewer for 41 IDs; two reviews in a row answered the 17 live ones — the withdrawals sat
in `closed` too, and the prompt said not to reassess closed items. `assess` rejected each reply, and because another
session had edited a file in the shared folder meanwhile, the rejection was reported as "The request changed during
the review; revise the goals and claim again." Nothing had changed; claiming again repeated it (7–10 minutes and
~150k tokens a review), and the second review had found every item met.

① the prompt names every ID to assess and says a withdrawal awaiting judgement is one of them
② a reply that leaves out a withdrawal while another session edits a file: rejected as such, with the missing ID
③ the same reply with nothing moving: the same rejection (control)
④ the same reply while the user's request really changes: still "the request changed" (control)
⑤ a complete reply while another session edits a file: verified, the withdrawal approved (control)
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
for _marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):  # sessions a person opened, not delegated ones
    os.environ.pop(_marker, None)
from harness import attach, providers, requirements, verifier  # noqa: E402
from harness.engine import Engine  # noqa: E402
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
    """Answers with `verdict(done_ids)`; `during` runs while the review holds the task lock."""
    verdict = None
    prompts = []
    during = None

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
        return FakeReviewer.verdict([d['id'] for d in payload['done']]), {
            'call_id': 'r%d' % len(FakeReviewer.prompts), 'session': 'rev', 'provider': self.name, 'requested_model': None,
            'reported_models': ['fake'], 'context': {'tokens': 1, 'source': 'fake', 'window': None}, 'seconds': 0.1,
            'usage': {}, 'log': str(log), 'invocation': ''}


def every_met(skip=()):
    """All met — leaving out `skip`, as the reviewers did with the withdrawals."""
    def build(ids):
        return {'items': [{'id': i, 'status': 'met', 'evidence': ['observed ' + i]} for i in ids if i not in skip],
                'extra': [], 'missing': [], 'fix': [], 'question': '', 'reopen': []}
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


def with_withdrawal(store, root, goal):
    """D1 and D2 open, then the user rewords the first: D1 superseded by D3, a withdrawal awaiting judgement."""
    tid = attach.create(store, root, goal, 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, tid, ['the camera shows a level line', 'the shot can be saved right away'])
    user_says(store, tid, 'the level line should fade once the picture is level')
    attach.set_done(store, tid, [], replace=[('D1', 'the level line shows while tilted and fades once level')],
                    because='the user reworded the level line')
    return tid


def review(engine, store, tid, build, during=None):
    FakeReviewer.verdict, FakeReviewer.during = build, during
    attach.claim(store, tid, summary='claim')
    return attach.run_verification(engine, tid)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-withdrawn-review-'))
    store = Store(base / 'home')
    engine = Engine(store)
    original = providers.Provider
    providers.Provider = FakeReviewer
    try:
        run(base, store, engine)
    finally:
        providers.Provider = original
    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


def run(base, store, engine):
    root = make_repo(base, 'shared-folder')
    attach.set_switch(store, 'on', root)
    other = lambda name: (lambda: (root / name).write_text('another session wrote this during the review\n'))

    print('① the prompt names every ID to assess, the withdrawal among them')
    tid = with_withdrawal(store, root, 'Guide the shot with a level line, A')
    task = store.get(tid)
    check('setup: D1 is a withdrawal awaiting judgement, D2 and D3 are open',
          [d['id'] for d in requirements.pending_withdrawals(task['data'])] == ['D1']
          and [d['id'] for d in task['data']['done'] if d['id'].startswith('D')] == ['D2', 'D3'],
          (requirements.pending_withdrawals(task['data']), task['data']['done']))
    task = review(engine, store, tid, every_met(), during=other('notes-a.md'))
    prompt = FakeReviewer.prompts[-1]
    expected = [d['id'] for d in json.loads(prompt[prompt.index('\n{') + 1:])['done']]
    check('the prompt lists the IDs to assess, the withdrawal included',
          'items must hold exactly these %d IDs, each once' % len(expected) in prompt and ', '.join(expected) in prompt
          and 'D1' in expected, expected)
    check('…and says a withdrawal awaiting judgement is a done ID, not a closed item to leave alone',
          'A withdrawal no review has judged yet is in closed too' in prompt and 'it is a done ID' in prompt)

    print('⑤ (control) a complete reply while another session edits a file')
    check('verified, and the withdrawal approved', task['status'] == 'verified'
          and next(d for d in task['data']['done_closed'] if d['id'] == 'D1').get('approved') is True,
          (task['status'], task['data'].get('reason_code'), task['data'].get('reason')))

    print('② a reply that leaves out the withdrawal while another session edits a file (the incident)')
    tid = with_withdrawal(store, root, 'Guide the shot with a level line, B')
    task = review(engine, store, tid, every_met(skip=('D1',)), during=other('notes-b.md'))
    reason = task['data'].get('reason') or ''
    check('not reported as a changed request', task['data'].get('reason_code') != 'requirements_changed'
          and 'request changed' not in reason, (task['status'], task['data'].get('reason_code'), reason))
    check('…but as a rejected reply that stops for a person', task['status'] == 'needs_input'
          and task['data'].get('reason_code') == 'verifier_rejected', (task['status'], task['data'].get('reason_code')))
    check('…naming the ID it left out', 'missing: D1' in reason, reason)
    check('…and the withdrawal still awaits judgement (nothing approved on a rejected reply)',
          next(d for d in task['data']['done_closed'] if d['id'] == 'D1').get('approved') is None)

    print('③ (control) the same reply with nothing moving')
    tid = with_withdrawal(store, root, 'Guide the shot with a level line, C')
    task = review(engine, store, tid, every_met(skip=('D1',)))
    check('the same rejection, naming the ID', task['status'] == 'needs_input'
          and task['data'].get('reason_code') == 'verifier_rejected' and 'missing: D1' in (task['data'].get('reason') or ''),
          (task['status'], task['data'].get('reason_code'), task['data'].get('reason')))

    print('④ (control) the same reply while the user\'s request really changes')
    tid = with_withdrawal(store, root, 'Guide the shot with a level line, D')
    task = review(engine, store, tid, every_met(skip=('D1',)),
                  during=lambda: attach.record_prompt(store, store.get(tid), 'Also record video with the guide on it', 's1'))
    # The message waits in the inbox (the review holds the task lock); the bound request itself did not move, so the
    # unreadable reply is rejected as before this change — and the message is kept for the prompt hook, not dropped.
    check('the unreadable reply is still rejected, naming the ID',
          task['status'] == 'needs_input' and task['data'].get('reason_code') == 'verifier_rejected'
          and 'missing: D1' in (task['data'].get('reason') or ''),
          (task['status'], task['data'].get('reason_code'), task['data'].get('reason')))
    check('…and the message that arrived is kept, waiting to be applied',
          any('Also record video' in m['text'] for m in store.inbox(tid)), store.inbox(tid))

    print('assess names what is wrong with the IDs')
    done = [{'id': i} for i in ('G0', 'D2', 'D3', 'D1')]
    rows = [{'id': i, 'status': 'met', 'evidence': ['x']} for i in ('G0', 'D2', 'D2', 'D9')]
    try:
        verifier.assess({'items': rows, 'extra': [], 'missing': [], 'fix': [], 'question': '', 'reopen': []}, done)
        check('a reply with a missing, an unexpected and a repeated ID is refused', False)
    except ValueError as error:
        text = str(error)
        check('a reply with a missing, an unexpected and a repeated ID is refused, each named',
              text.startswith('Verifier must assess every completion ID exactly once')
              and 'missing: D3, D1' in text and 'unexpected: D9' in text and 'repeated: D2' in text, text)
    full = [{'id': i, 'status': 'met', 'evidence': ['x']} for i in ('G0', 'D2', 'D3', 'D1')]
    check('(control) a complete reply is accepted',
          verifier.assess({'items': full, 'extra': [], 'missing': [], 'fix': [], 'question': '', 'reopen': []}, done)[1] == 'verified')


if __name__ == '__main__':
    sys.exit(main())
