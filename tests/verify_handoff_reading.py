#!/usr/bin/env python3
"""Model-free check that a handoff no longer clears the answer the user is reading.

Why (measured 2026-11-24, task 28d857f94be3, session 786c8c9b): at 15:51:23 KST a 1,160-character answer ended with a
proposal and a question ("… D26(…) 진행할까요?"). The session was past its handoff point with a checkpoint saved, so at
15:51:24 the manager read the stop as "handoff" and the supervisor typed /clear 1.2 s after the answer was written: the
question vanished before the user could read it. The executor had recorded no question (claim --question), so the
stop counted as work left. The fresh session then took four minutes to work out the question again and asked it twice.
Replayed with synthetic data:

  ① an answer that ends by asking the user (or handing them a choice) is recognised, also without a question mark
  ② such a stop is the user's: no automatic handoff, no supervisor; their reply starts the handoff and carries the end
     of the answer below the ceiling too, and the conversation that takes over shows that end again
  ③ after a report the supervisor's handoff waits a reading time in proportion to the answer (bounded); input typed
     meanwhile cancels it, and the fresh conversation shows the report's end again
  ④ a /clear the user typed themselves shows the end of the previous answer too

Every rule has a control. Nothing types into a terminal or starts a model: the store is a temporary folder, the
terminal, the typist and the supervisor's launch are fakes.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for _name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH', 'ORBIT_HANDOFF_CEILING', 'TMUX',
              'TMUX_PANE', 'CMUX_PANEL_ID', 'CMUX_SURFACE_ID', 'HARNESS_HOME', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID',
              'CODEX_THREAD_ID'):
    os.environ.pop(_name, None)  # never type into the terminal running this check; sessions a person opened
from harness import attach, continuity, supervision, typist  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []
CLI = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit')]
PANE = {'kind': 'cmux', 'cli': '/bin/echo', 'target': 'PANEL-0'}   # the pane the hooks see now (each scenario its own)
# Read leniently, so an older checkout fails check by check instead of stopping at the first missing name.
asks_user = getattr(attach, 'asks_user', lambda message: None)
answer_waits = getattr(supervision, 'answer_waits', lambda task, session=None: False)
reading_seconds = getattr(supervision, 'reading_seconds', lambda chars: None)
READ_MAX = getattr(supervision, 'READ_MAX_SECONDS', 120)

RULE = '─' * 60
EMPTY = 'some earlier output\n' + RULE + '\n❯\xa0\n' + RULE + '\n  ? for shortcuts\n'
TYPING = EMPTY.replace('❯\xa0', '❯\xa0and also the chart')

# The shape of the 15:51:23 answer (a table, a list, a closing question), in synthetic words.
QUESTION = '\n'.join([
    'It was not today\'s install. The records session tried an automatic handoff at 14:15 and it failed.',
    '', '| time | what happened |', '|---|---|', '| 13:52 | handoff at 310k |', '| 14:16 | the clear did not take |', '',
    '**What to fix (D26 in the ledger)**', '- retry a failed handoff a bounded number of times',
    '- measure why the mod could not clear', '',
    '진단 근거는 \`.orbit-evidence/x/d25_check.sh\`로 남겼습니다. D26(재시도 + 모드 실패 원인 수정) 진행할까요?'])
REPORT = '\n'.join(['Records tab, step one of two.', ''] + ['- chart %d draws the weekly totals from the store' % n
                                                            for n in range(1, 40)] + ['', '수면 차트를 마저 그리고 있습니다.'])


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:400]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'app.py').write_text('print("hello")\n')
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '-m', 'f'], check=True)
    return root


def turn(path, tokens):
    row = {'type': 'assistant', 'message': {'id': 'm%d' % tokens, 'model': 'claude-test',
                                             'usage': {'input_tokens': 5, 'cache_creation_input_tokens': 995,
                                                       'cache_read_input_tokens': tokens - 1000}}}
    with open(path, 'a') as stream:
        stream.write(json.dumps(row) + '\n')


def tell(store, root, base, session, last):
    """Drive one session past the handoff point with a checkpoint saved and work left; its turn ends with \`last\`.
    Returns (task id, hook payload, the reply of the last Stop)."""
    PANE['target'] = 'PANEL-' + session
    path = base / (session + '.jsonl')
    turn(path, 50_000)
    pay = {'cwd': str(root), 'session_id': session, 'transcript_path': str(path)}
    attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    attach.handle(store, 'prompt', dict(pay, prompt='Build the records tab with two charts and keep the old screens'), 'claude')
    tid = attach.current_task(store, root)['id']
    subprocess.run(CLI + ['--home', str(store.home), 'done', tid, 'D1 the records tab shows both charts'],
                   capture_output=True, text=True, timeout=60, check=True)   # work is left: the supervisor goes on
    turn(path, 310_000)
    attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining charts'), 'claude')
    attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Half done.'), 'claude')
    continuity.record_checkpoint(store, tid, 'Draw the sleep chart in records.tsx', notes='feeding chart done')
    reply, blocked = None, 0
    while blocked < 6:
        reply = attach.handle(store, 'stop', dict(pay, stop_hook_active=blocked > 0, last_assistant_message=last), 'claude')
        if (reply or {}).get('decision') != 'block':
            break
        blocked += 1
    return tid, pay, reply


def age_stop(store, tid, session, seconds):
    """Move this session's last stop (and the hook that recorded its pane) \`seconds\` into the past."""
    from datetime import datetime, timedelta
    with store.lock('task:' + tid):
        task = store.get(tid)
        mark = task['data']['supervision'][session]
        then = (datetime.fromisoformat(mark['stop_at']) - timedelta(seconds=seconds)).isoformat()
        mark['stop_at'] = then
        if mark.get('answer'):
            mark['answer']['at'] = then
        store.save(task, task['status'], 'test_setup', {})
    with store.db:
        row = store.db.execute('SELECT at FROM terminals WHERE session=?', (session,)).fetchone()
        earlier = (datetime.fromisoformat(row['at']) - timedelta(seconds=seconds)).isoformat()
        store.db.execute('UPDATE terminals SET at=? WHERE session=?', (earlier, session))


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-handoff-reading-'))
    store = Store(base / 'home')
    spawned, launched, screen = [], MagicMock(), {'now': EMPTY}
    typed = []
    real_detect, real_spawn, real_subprocess = typist.detect, typist.spawn, supervision.subprocess
    typist.detect = lambda env=None: dict(PANE)
    typist.spawn = lambda terminal, steps, log, home=None, task=None: spawned.append((terminal, steps, task))
    supervision.subprocess = SimpleNamespace(Popen=launched, run=MagicMock(), DEVNULL=subprocess.DEVNULL,
                                             SubprocessError=subprocess.SubprocessError)   # no real supervisor

    def runner(argv):
        typed.append(argv)
        return 0, screen['now']

    def tick(tid, session):
        return supervision.tick(store, tid, session, runner)

    continuity.set_handoff_mode(store, 'auto')
    try:
        print('① an answer that ends by asking the user is recognised')
        check('the 15:51:23 answer (a table, a list, "… 진행할까요?" on its last line) asks the user', asks_user(QUESTION) is True)
        for text in ('결과를 확인해 주세요.', 'Shall I open the pull request?', 'Let me know if you want the CSV export too.',
                     '두 방법 중 어느 쪽으로 하실래요?\n| A | B |'):
            check('…%r asks the user (a closing period, English, a table row after the question)' % text[-28:],
                  asks_user(text) is True)
        for text in (REPORT, 'PR #76 을 올렸습니다.', '왜 그랬을까요? 원인은 캐시였습니다.', '', 'Merged into main.\n\`\`\`'):
            check('control: %r does not ask (a report, a question mid-answer, nothing)' % text[-28:], asks_user(text) is False)
        check('control: the claim rule is untouched ("완료했습니다. 확인해 주세요." still reads as a claim to record)',
              attach.claims_completion('구현을 완료했습니다. 확인해 주세요.'))

        print('② a stop that asks the user is theirs: no handoff over it; their reply carries the answer')
        root_q = make_repo(base, 'repo-question')
        attach.set_switch(store, 'on', root_q)
        tid, pay, reply = tell(store, root_q, base, 'q1', QUESTION)
        task = store.get(tid)
        state = task['data'].get('handoff_state') or {}
        check('control: the 15:51 situation — past the point, checkpoint saved, work left, auto mode, a pane Orbit types in',
              state.get('state') == 'told' and supervision.needs_work(task) and supervision.handoff_pending(task), state)
        check('the stop is the user\'s to answer (answer_waits), so the supervisor does not continue it (handoff_continues)',
              answer_waits(task) and not supervision.handoff_continues(store, task), task['data']['supervision'].get('q1'))
        shown = (reply or {}).get('systemMessage', '')
        check('the Stop tells the user nothing starts before their reply (it said "starts a fresh context by itself once '
              'this turn ends")', 'nothing starts before you reply' in shown and 'once this turn ends' not in shown, shown)
        count = len(spawned)
        outcome = tick(tid, 'q1')
        check('the supervisor waits for the answer: awaiting_answer, nothing typed (it handed off 1.2 s after the answer)',
              outcome == 'awaiting_answer' and len(spawned) == count and not typed, (outcome, typed[-2:]))
        check('…and no supervisor was launched for that stop', launched.call_count == 0, launched.call_count)
        clock = iter(range(0, 10 ** 6, 100))
        supervision.main(str(store.home), tid, 'q1', sleep=lambda s: None, clock=lambda: next(clock))
        outcomes = [json.loads(r['payload']).get('outcome') if isinstance(r['payload'], str) else r['payload'].get('outcome')
                    for r in store.events(tid) if r['kind'] == 'supervision_observed']
        check('…a supervisor that was running leaves at once on it (not 15 idle minutes)',
              outcomes[-1:] == ['awaiting_answer'], outcomes[-3:])
        answer = attach.handle(store, 'prompt', dict(pay, prompt='네 D26 진행해'), 'claude')
        carried = store.get(tid)['data'].get('carried_prompt') or {}
        check("the user's reply starts the handoff (blocked, carried, typed)",
              (answer or {}).get('decision') == 'block' and carried.get('text') == '네 D26 진행해' and len(spawned) == count + 1,
              (answer, carried))
        check('…with the end of the answer it replies to, below the ceiling too',
              not carried.get('ceiling') and '진행할까요?' in (carried.get('answer') or ''), carried)
        PANE['target'] = 'PANEL-q1'
        start = attach.handle(store, 'session-start', {'cwd': str(root_q), 'session_id': 'q2', 'source': 'clear'}, 'claude')
        first = attach.handle(store, 'prompt', {'cwd': str(root_q), 'session_id': 'q2', 'prompt': typist.CARRY_LINE}, 'claude')
        shown = (first or {}).get('systemMessage', '')
        context = ((start or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')   # the fresh model's start
        check('the conversation that takes over shows the end of the previous answer on screen, the question included',
              'The previous answer ended with:' in shown and '진행할까요?' in shown, shown)
        check('…and its model gets the reply and the answer it replies to, word for word',
              '네 D26 진행해' in context and '진행할까요?' in context and 'which the user read last' in context, context[-600:])
        later = attach.handle(store, 'prompt', {'cwd': str(root_q), 'session_id': 'q2', 'prompt': 'Also show the weekly average'}, 'claude')
        check('…once: the next prompt shows nothing more', not (later or {}).get('systemMessage'), later)

        root_c = make_repo(base, 'repo-continue')
        attach.set_switch(store, 'on', root_c)
        PANE['target'] = 'PANEL-c1'
        pay_c = {'cwd': str(root_c), 'session_id': 'c1'}
        attach.handle(store, 'prompt', dict(pay_c, prompt='Build the records tab with two charts and keep the old screens'), 'claude')
        tid_c = attach.current_task(store, root_c)['id']
        attach.set_done(store, tid_c, ['the records tab shows both charts'])
        for _ in range(4):                               # past the Stop guard's own blocks: the supervisor's turn
            attach.handle(store, 'stop', dict(pay_c, stop_hook_active=True, last_assistant_message=QUESTION), 'claude')
        count = len(typed)
        check('below the handoff point too: no continuation line typed over a question (measured live: typed twice)',
              tick(tid_c, 'c1') == 'awaiting_answer' and not [a for a in typed[count:] if supervision.RESUME_LINE in ' '.join(a)],
              typed[count:])
        from harness import patrol
        check('…nor a patrol nudge an hour later', patrol.nudge(store, store.get(tid_c), 'c1', runner, at=__import__('time').time() + 3600)
              == 'awaiting_answer')
        attach.handle(store, 'stop', dict(pay_c, stop_hook_active=True, last_assistant_message=REPORT), 'claude')
        check('control: after a report the continuation is typed as before',
              tick(tid_c, 'c1') == 'continued' and [a for a in typed[count:] if supervision.RESUME_LINE in ' '.join(a)])

        print('③ after a report the supervisor hands off after a reading time; typing cancels it')
        check('reading time grows with the answer and is bounded (100 → 20 s, 1,160 → 97 s, 5,000 → %d s, none → 0)' % READ_MAX,
              [reading_seconds(n) for n in (100, 1160, 5000, 0)] == [20, 97, READ_MAX, 0],
              [reading_seconds(n) for n in (100, 1160, 5000, 0)])
        root_r = make_repo(base, 'repo-report')
        attach.set_switch(store, 'on', root_r)
        tid, pay, reply = tell(store, root_r, base, 'r1', REPORT)
        task = store.get(tid)
        seconds = reading_seconds(len(REPORT.strip())) or 0
        shown = (reply or {}).get('systemMessage', '')
        check('control: a report does not wait for the user (the work goes on by itself)',
              not answer_waits(task) and supervision.handoff_continues(store, task), task['data']['supervision'].get('r1'))
        check('the Stop says when: after %d s to read this answer, whose end the fresh conversation shows again' % seconds,
              ('after %d s to read this answer' % seconds) in shown and 'shows its end again' in shown, shown)
        count = len(spawned)
        check('right after the stop the supervisor waits (reading), nothing typed',
              tick(tid, 'r1') == 'reading' and len(spawned) == count)
        age_stop(store, tid, 'r1', seconds - 5)
        check('…still reading 5 s before the time is up', tick(tid, 'r1') == 'reading' and len(spawned) == count)
        age_stop(store, tid, 'r1', 10)
        screen['now'] = TYPING
        check('control: the user began typing meanwhile — nothing is started over their input',
              tick(tid, 'r1') == 'typing' and len(spawned) == count)
        screen['now'] = EMPTY
        check('once the time is up and the input is empty, the supervisor hands off',
              tick(tid, 'r1') == 'handed_off' and len(spawned) == count + 1)
        carried = store.get(tid)['data'].get('carried_prompt') or {}
        check('…carrying the continuation and the end of the report',
              carried.get('by') == 'supervisor' and '수면 차트를 마저 그리고 있습니다.' in (carried.get('answer') or ''), carried)
        PANE['target'] = 'PANEL-r1'
        start = attach.handle(store, 'session-start', {'cwd': str(root_r), 'session_id': 'r2', 'source': 'clear'}, 'claude')
        first = attach.handle(store, 'prompt', {'cwd': str(root_r), 'session_id': 'r2', 'prompt': typist.CARRY_LINE}, 'claude')
        shown = (first or {}).get('systemMessage', '')
        tail = shown.split('The previous answer ended with:\n', 1)[-1]
        check('the fresh conversation shows the report\'s end again, from a line start, at most %d characters'
              % getattr(continuity, 'SCREEN_ANSWER_CHARS', 700),
              '수면 차트를 마저 그리고 있습니다.' in tail and tail.startswith('…- chart')
              and len(tail) <= getattr(continuity, 'SCREEN_ANSWER_CHARS', 700) + 1, shown)
        context = ((start or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
        check('…and its model is told the user asked nothing new, with the end of the report',
              'Orbit started this fresh context by itself' in context and '수면 차트' in context, context[-500:])
        root_l = make_repo(base, 'repo-legacy')
        attach.set_switch(store, 'on', root_l)
        tid, pay, _ = tell(store, root_l, base, 'l1', 'Half done.')
        supervision.record_stop(store, store.get(tid), 'l1')          # a stop that recorded no answer (as before)
        count = len(spawned)
        check('control: a stop that recorded no answer hands off at once, as before (no reading time to measure)',
              tick(tid, 'l1') == 'handed_off' and len(spawned) == count + 1)

        print('④ a /clear the user types shows the end of the previous answer too')
        continuity.set_handoff_mode(store, 'tell')
        root_m = make_repo(base, 'repo-manual')
        attach.set_switch(store, 'on', root_m)
        tid, pay, reply = tell(store, root_m, base, 'm1', QUESTION)
        check('control: handoff mode tell — the user is asked for /clear', 'type /clear' in (reply or {}).get('systemMessage', ''),
              reply)
        PANE['target'] = 'PANEL-m1'
        attach.handle(store, 'session-start', {'cwd': str(root_m), 'session_id': 'm2', 'source': 'clear'}, 'claude')
        first = attach.handle(store, 'prompt', {'cwd': str(root_m), 'session_id': 'm2', 'prompt': '이어 받았어.'}, 'claude')
        shown = (first or {}).get('systemMessage', '')
        check('the first message after their /clear shows the takeover and the question they were reading',
              ('took over task ' + tid) in shown and 'The previous answer ended with:' in shown and '진행할까요?' in shown, shown)
    finally:
        typist.detect, typist.spawn, supervision.subprocess = real_detect, real_spawn, real_subprocess
    print('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS · all checks passed — 0 failed')
    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
