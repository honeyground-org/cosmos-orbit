#!/usr/bin/env python3
"""Rounds of one ledger and the completion notice (2026-11-24 user request, D21). Model-free, with controls.

① a passing review fixes the round complete and opens a completion notice (the reviewer is a fake that finds all met)
② the notice reaches the user once: in the terminal line a review outcome uses, and at the next Stop
③ "그대로 두기" / "이어서" answer it without a model turn; a line that picks nothing is not taken for a choice
④ the next substantial request opens round 2: its own goal, the brief opens with it (not the first request), and the
   round before stays complete
⑤ round 2's review is told which rounds were verified before, and closes round 2
⑥ "종료" ends the session in order: handoff, memory (when a brain is configured), then /exit typed into its pane;
   the task closes complete and the session ends
Exit 0 = pass. On the main before rounds every check fails (the module is missing), it does not crash.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as _Namespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH', 'CLAUDE_CODE_SESSION_ID',
             'CODEX_THREAD_ID', 'TMUX', 'TMUX_PANE', 'CMUX_SURFACE_ID', 'CMUX_PANEL_ID', 'CMUX_WORKSPACE_ID',
             'CLAUDECODE'):
    os.environ.pop(name, None)
from harness import attach, providers, supervision  # noqa: E402
from harness.engine import Engine  # noqa: E402
from harness.store import Store  # noqa: E402
try:
    from harness import rounds  # noqa: E402
except ImportError:              # before the change: the checks below fail one by one
    rounds = None

from harness import evolve as _evolve, recovery as _recovery  # noqa: E402
patch.object(_recovery, 'ensure_worker', return_value=None).start()
patch.object(_evolve, 'maybe_start', return_value=None).start()
patch.object(supervision, 'subprocess', _Namespace(
    Popen=lambda *a, **k: _Namespace(pid=None), run=subprocess.run,
    DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError)).start()

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


class AllMet:
    """A reviewer that finds every item met and keeps the payload it was sent."""
    payloads = []

    def __init__(self, name, home, model=None, timeout=None):
        self.name, self.home = name, Path(home)

    def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
        payload = json.loads(prompt[prompt.index('\n{') + 1:])
        AllMet.payloads.append(payload)
        log = self.home / 'runs' / task_id / 'review.events.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({'type': 'item.completed', 'item': {'id': 'r', 'type': 'command_execution',
                                                                       'command': 'cat', 'exit_code': 0}}) + '\n')
        answer = {'items': [{'id': d['id'], 'status': 'met', 'evidence': ['observed ' + d['id']]} for d in payload['done']],
                  'extra': [], 'missing': [], 'fix': [], 'question': '', 'reopen': []}
        return answer, {'call_id': 'r%d' % len(AllMet.payloads), 'session': 'rev', 'provider': self.name,
                        'requested_model': None, 'reported_models': ['fake'],
                        'context': {'tokens': 1, 'source': 'fake', 'window': None}, 'seconds': 0.1,
                        'usage': {}, 'log': str(log), 'invocation': ''}


def make_repo(base, name):
    root = base / name
    root.mkdir()
    (root / 'app.py').write_text('TOTAL = 1\n')
    for args in (['init', '-q'], ['add', '.'], ['commit', '-q', '-m', 'init']):
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True,
                       capture_output=True)
    return root


def hook(store, root, event, session, **payload):
    return attach.handle(store, event, dict(payload, cwd=str(root), session_id=session), 'claude')


def context_of(reply):
    return ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''


def work_and_review(engine, store, root, tid, item, change):
    """The executor's turn: a change, a D item with command evidence, a claim; then the (fake) review."""
    (root / 'app.py').write_text(change)
    attach.set_done(store, tid, [item], add=True)
    number = [d['id'] for d in store.get(tid)['data']['done'] if d['id'].startswith('D')][-1]
    attach.record_evidence(store, tid, number, command='["python3","-m","py_compile","app.py"]')
    attach.claim(store, tid, summary='done')
    return attach.run_verification(engine, tid)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-rounds-'))
    store = Store(base / 'home')
    engine = Engine(store)
    original = providers.Provider
    providers.Provider = AllMet
    try:
        run(base, store, engine)
    except Exception as error:  # noqa: BLE001 - before the change a missing piece must fail the run, not hide it
        import traceback
        traceback.print_exc()
        FAILED.append('crashed: ' + repr(error)[:200])
    finally:
        providers.Provider = original
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'all checks passed') + ' · ' + str(base))
    return 1 if FAILED else 0


def run(base, store, engine):
    root = make_repo(base, 'repo')
    attach.set_switch(store, 'on', root)
    first = '월간 리포트 페이지에 CSV 내보내기 버튼을 만들어 줘'
    hook(store, root, 'session-start', 'A', source='startup')
    hook(store, root, 'prompt', 'A', prompt=first)
    tid = attach.resolve_task(store, root, 'A')['id']

    print('① a passing review fixes the round complete and opens the completion notice')
    task = work_and_review(engine, store, root, tid, 'the CSV button downloads every row', 'TOTAL = 2\n')
    data = task['data']
    check('control: the fake review verified the task', task['status'] == 'verified', task['status'])
    done_rounds = data.get('rounds') or []
    check('round 1 is recorded complete with the item it met',
          len(done_rounds) == 1 and done_rounds[0].get('n') == 1 and done_rounds[0].get('items') == ['D1'], done_rounds)
    check('…its goal is the request it answered', first in (done_rounds[0].get('goal') if done_rounds else ''), done_rounds)
    check('a completion notice is open', (data.get('completion') or {}).get('state') == 'open', data.get('completion'))
    verified_events = [e for e in store.events(tid) if e['kind'] == 'completion_verified']
    check("the ledger's completion event names the round",
          bool(verified_events) and verified_events[-1]['payload'].get('round') == 1,
          verified_events[-1]['payload'] if verified_events else None)

    print('② the notice reaches the user: the outcome line and the next Stop, once')
    told = supervision.outcome_text(store.get(tid))
    check('the outcome line names the round, the verified count and the three choices',
          all(w in told for w in ('라운드 1 완료', '목표 1개', '종료', '이어서', '그대로 두기')), told)
    stop = hook(store, root, 'stop', 'A', last_assistant_message='CSV 버튼을 추가했습니다.')
    message = (stop or {}).get('systemMessage') or ''
    check('the Stop after the review shows the notice', '라운드 1 완료' in message, stop)
    again = hook(store, root, 'stop', 'A', last_assistant_message='')
    check('…once per session (control: the next Stop does not repeat it)',
          '라운드 1 완료' not in ((again or {}).get('systemMessage') or ''), again)
    brief = context_of(hook(store, root, 'prompt', 'A', prompt='고마워요'))
    check('a thank-you leaves the notice open and the brief carries it',
          'COMPLETE:' in brief and store.get(tid)['status'] == 'verified', brief[:400])

    print('③ the user answers without a model turn; other lines are not choices')
    check('control: a request is not a choice', rounds.choice('이제 엑셀 내보내기도 추가해 줘') is None)
    check('control: "이어서 진행해 줘" is not one of the three either', rounds.choice('이어서 진행해 줘') is None)
    check('the three choices are read in Korean and English, with brackets',
          [rounds.choice(t) for t in ('종료', '[이어서]', '그대로 두기', 'exit', 'Continue', 'keep.')]
          == ['end', 'continue', 'keep', 'end', 'continue', 'keep'])
    emptied = []
    fake_cli = base / 'cmux'
    fake_cli.write_text('#!/bin/sh\nexit 0\n')
    fake_cli.chmod(0o755)
    with patch('harness.typist.spawn', lambda terminal, steps, log, home=None, task=None, report=None:
               emptied.append((terminal, steps))), \
            patch.dict(os.environ, {'CMUX_PANEL_ID': 'panel-a', 'CMUX_BUNDLED_CLI_PATH': str(fake_cli)}):
        keep = hook(store, root, 'prompt', 'A', prompt='그대로 두기')
    check('…and the line Claude Code puts back into the input is emptied (nothing else is typed)',
          len(emptied) == 1 and emptied[0][0].get('target') == 'panel-a'
          and [s[0] for s in emptied[0][1]] == ['wait', 'clear'], emptied)
    check('"그대로 두기" is answered by Orbit (the prompt runs no turn)',
          (keep or {}).get('decision') == 'block' and '그대로 둡니다' in (keep or {}).get('reason', ''), keep)
    after = store.get(tid)
    check('…the notice is closed as keep and the task stays verified',
          (after['data'].get('completion') or {}).get('state') == 'keep' and after['status'] == 'verified',
          (after['status'], after['data'].get('completion')))
    passed = hook(store, root, 'prompt', 'A', prompt='그대로 두기')
    check('control: with the notice closed the same line is not intercepted', (passed or {}).get('decision') != 'block',
          passed)

    print('④ the next substantial request opens round 2')
    second = '이제 CSV 버튼 옆에 엑셀 내보내기 버튼도 추가해 줘'
    reply = hook(store, root, 'prompt', 'A', prompt=second)
    task = store.get(tid)
    check('same task, working again', attach.resolve_task(store, root, 'A')['id'] == tid and task['status'] == 'working',
          task['status'])
    check('round 2 is open with the new request as its goal',
          (task['data'].get('round') or {}).get('n') == 2 and (task['data'].get('round') or {}).get('goal') == second,
          task['data'].get('round'))
    check('round 1 stays complete', len(task['data'].get('rounds') or []) == 1)
    lines = context_of(reply).splitlines()
    check('the brief opens with round 2 and its request, not the first request',
          len(lines) > 1 and lines[1].startswith('ROUND 2') and second in lines[1] and not lines[1].startswith('GOAL'),
          lines[:2])
    check('…and says how many rounds are complete', len(lines) > 1 and '1 earlier round complete' in lines[1], lines[:2])

    print("⑤ round 2's review knows the rounds before, and closes round 2")
    task = work_and_review(engine, store, root, tid, 'an Excel button sits next to the CSV button', 'TOTAL = 3\n')
    view = (AllMet.payloads[-1] or {}).get('rounds') or {}
    check('the reviewer is told round 2 is judged and round 1 was verified',
          (view.get('current') or {}).get('n') == 2 and [r['n'] for r in view.get('verified_before') or []] == [1], view)
    check('control: the first review had no rounds to tell', AllMet.payloads[0].get('rounds') is None, AllMet.payloads[0].get('rounds'))
    rows = task['data'].get('rounds') or []
    check('round 2 is complete with its own item', task['status'] == 'verified' and len(rows) == 2
          and rows[1]['n'] == 2 and 'D2' in rows[1]['items'] and 'D1' not in rows[1]['items'], rows)
    cont = hook(store, root, 'prompt', 'A', prompt='이어서')
    check('"이어서" is answered: the next request opens round 3', (cont or {}).get('decision') == 'block'
          and '라운드 3' in (cont or {}).get('reason', ''), cont)
    handoff = attach.handoff_markdown(store, store.get(tid))
    check('the handoff lists the rounds', '## Rounds' in handoff and 'Round 1 ✓' in handoff and 'Round 2 ✓' in handoff,
          handoff[:600])

    print('⑥ "종료": handoff → memory → /exit, the task closes complete, the session ends')
    root_e = make_repo(base, 'repo-end')
    attach.set_switch(store, 'on', root_e)
    hook(store, root_e, 'session-start', 'E', source='startup')
    hook(store, root_e, 'prompt', 'E', prompt='설정 화면에 다크 모드 토글을 추가해 줘')
    eid = attach.resolve_task(store, root_e, 'E')['id']
    work_and_review(engine, store, root_e, eid, 'the toggle switches the theme', 'TOTAL = 4\n')
    with store.lock('task:' + eid):                    # a brain is configured for this task (the write itself is faked)
        t = store.get(eid)
        t['data']['memory'] = {'mode': 'brain', 'remember': True, 'outbox': [], 'project_id': 'p'}
        store.save(t, t['status'], 'memory_configured', {})
    order, typed = [], []
    original_record = __import__('harness.continuity', fromlist=['record_checkpoint']).record_checkpoint

    def recording_checkpoint(*args, **kwargs):
        order.append('handoff')
        return original_record(*args, **kwargs)

    with patch('harness.continuity.record_checkpoint', recording_checkpoint), \
            patch('harness.memory.enqueue_checkpoint', lambda store, task: order.append('memory')), \
            patch('harness.memory.flush', lambda store, task, lease_fds=(): [{'id': 'x', 'stored': True}]), \
            patch('harness.typist.spawn', lambda terminal, steps, log, home=None, task=None, report=None:
                  (order.append('exit'), typed.append((terminal, steps)))), \
            patch.dict(os.environ, {'CMUX_PANEL_ID': 'panel-e', 'CMUX_BUNDLED_CLI_PATH': str(fake_cli)}):
        ended = hook(store, root_e, 'prompt', 'E', prompt='종료')
    reason = (ended or {}).get('reason', '')
    check('"종료" is answered by Orbit with the steps it took', (ended or {}).get('decision') == 'block'
          and '핸드오프 저장' in reason and '기억 저장' in reason and '/exit 입력 중' in reason, ended)
    check('in order: handoff, then memory, then /exit', order == ['handoff', 'memory', 'exit'], order)
    steps = typed[0][1] if typed else []
    check('/exit is typed into the pane after the blocked line is emptied',
          typed and typed[0][0].get('target') == 'panel-e' and ('text', '/exit') in steps and steps[-1] == ('key', 'enter')
          and any(s[0] == 'clear' for s in steps) and steps.index(('text', '/exit')) > [s[0] for s in steps].index('clear'),
          steps)
    closed = store.get(eid)
    record = closed['data'].get('closed') or {}
    check('the task closes complete (reason ended_by_user)', closed['status'] == 'closed'
          and record.get('reason') == 'ended_by_user' and record.get('state') == 'completed', (closed['status'], record))
    check('a checkpoint and the Markdown handoff were written', any(c.get('kind') == 'end' for c in closed['data'].get('checkpoints') or [])
          and (store.home / 'runs' / eid / 'handoff.md').is_file())
    check('the session is ended', bool((store.session_task('E') or {}).get('ended')), store.session_task('E'))


if __name__ == '__main__':
    sys.exit(main())
