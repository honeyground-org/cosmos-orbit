#!/usr/bin/env python3
"""Model-free check of a failed automatic handoff: it is tried again, and the conversation that takes over says so.

Why (measured 2026-11-24, task b10750fb7cc5, session 4e99f2c2): at 14:15 KST Orbit blocked a queued agent message to
hand a 305k session off; the Claude Code mod took the handoff, but the next queued teammate message ran a turn 75 ms
later, the mod's /clear never took effect, and 25 s later the handoff was marked auto_failed. Nothing tried again: the
session asked the user for /clear at every stop until it reached 778k tokens. When the user then typed /clear, the
fresh session showed nothing at all, and the user typed "이어 받았어." to start it. Replayed with synthetic data:

  ① a message that arrives while the handoff is under way goes with it (blocked, carried), so the session stays idle
  ② the mod reports its own failure at once, with its reason (handoff-claim --failed)
  ③ a failed handoff is tried again at the next stop or request, typing /clear itself, at most three attempts
  ④ the conversation that took a task over says so on screen once: task, previous size, checkpoint, carried request

Every rule has a control. Nothing types into a terminal or starts a model: the store is a temporary folder, the
terminal and the typist are fakes.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

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
FAKE = {'kind': 'cmux', 'cli': '/bin/echo', 'target': 'PANEL-R'}
# Read leniently, so an older checkout fails check by check instead of stopping at the first missing name.
INFLIGHT_SECONDS = getattr(continuity, 'INFLIGHT_SECONDS', 45)
INFLIGHT_PREFIX = getattr(continuity, 'INFLIGHT_PREFIX', 'Orbit: a handoff to a fresh context is under way')
RETRY_LIMIT = getattr(continuity, 'RETRY_LIMIT', 3)


def attempts(state):
    return getattr(continuity, 'attempts', lambda s: 0)(state)


def retry_ready(state):
    return getattr(continuity, 'retry_ready', lambda s: False)(state)


def takeover_notice(store, session, host):
    return getattr(continuity, 'takeover_notice', lambda *a: None)(store, session, host)
CLI = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit')]


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


def tell(store, root, base, session, tokens=310_000):
    """Drive one session to 'told': past the point, checkpointed, told, its turn ended."""
    path = base / (session + '.jsonl')
    turn(path, 50_000)
    pay = {'cwd': str(root), 'session_id': session, 'transcript_path': str(path)}
    attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    attach.handle(store, 'prompt', dict(pay, prompt='Build the records tab with two charts and keep the old screens'), 'claude')
    tid = attach.current_task(store, root)['id']
    subprocess.run(CLI + ['--home', str(store.home), 'done', tid, 'D1 the records tab shows both charts'],
                   capture_output=True, text=True, timeout=60, check=True)   # work is left: the supervisor goes on
    turn(path, tokens)
    attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining charts'), 'claude')
    attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Half done.'), 'claude')
    continuity.record_checkpoint(store, tid, 'Draw the sleep chart in records.tsx', notes='feeding chart done')
    end_turn(store, pay)
    return tid, pay


def end_turn(store, pay):
    blocked = 0
    while blocked < 6:
        reply = attach.handle(store, 'stop', dict(pay, stop_hook_active=blocked > 0, last_assistant_message='Answered.'), 'claude')
        if (reply or {}).get('decision') != 'block':
            return blocked
        blocked += 1
    raise AssertionError('the Stop hook never let the turn end')


def state_of(store, tid):
    return store.get(tid)['data'].get('handoff_state') or {}


def shift(store, tid, key, seconds):
    from datetime import datetime, timedelta, timezone
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['handoff_state'][key] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
        store.save(task, task['status'], 'test_setup', {})


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-handoff-retry-'))
    store = Store(base / 'home')
    typed = []
    real_detect, real_spawn = typist.detect, typist.spawn
    typist.detect = lambda env=None: FAKE
    typist.spawn = lambda terminal, steps, log, home=None, task=None: typed.append((terminal, steps, task))
    continuity.set_handoff_mode(store, 'auto')
    try:
        print('① a message that arrives while the handoff is under way goes with it, and the session stays idle')
        root = make_repo(base, 'repo-race')
        attach.set_switch(store, 'on', root)
        tid, pay = tell(store, root, base, 'r1')
        continuity.mod_hello(store, 'r1')
        first = attach.handle(store, 'prompt', dict(pay, prompt='<agent-message from="records-ui">#2129 merged, TestFlight run started</agent-message>'), 'claude')
        took, _ = continuity.claim_handoff(store, 'r1', 'mod')
        check('the first queued message hands off (blocked, HANDOFF_PREFIX) and the mod takes it',
              (first or {}).get('decision') == 'block' and first['reason'].startswith(continuity.HANDOFF_PREFIX) and took == tid,
              first)
        count = len(typed)
        second = attach.handle(store, 'prompt', dict(pay, prompt='<teammate-message>records19 is idle: the three steps are done</teammate-message>'), 'claude')
        carried = store.get(tid)['data'].get('carried_prompt') or {}
        check('the next queued message 75 ms later is blocked too (it used to start a turn and the /clear was lost)',
              (second or {}).get('decision') == 'block' and second['reason'].startswith(INFLIGHT_PREFIX), second)
        check('…its reason is not the mod\'s "clear now" line (the mod must not clear twice)',
              not (second or {}).get('reason', '').startswith(continuity.HANDOFF_PREFIX), second)
        check('…both messages go to the fresh session, in order', '#2129 merged' in carried.get('text', '')
              and carried.get('text', '').index('#2129') < carried.get('text', '').find('records19 is idle'), carried)
        check('…and no second handoff starts (nothing typed, still the same attempt)',
              len(typed) == count and state_of(store, tid).get('attempts') == 1 and state_of(store, tid).get('claimed_by') == 'mod',
              (len(typed) - count, state_of(store, tid)))
        shift(store, tid, 'auto_at', INFLIGHT_SECONDS + 5)
        late = attach.handle(store, 'prompt', dict(pay, prompt='Add the growth chart as well please'), 'claude')
        check('control: past %d s the handoff is no longer "under way": the message is not carried by that rule'
              % INFLIGHT_SECONDS, not (late or {}).get('reason', '').startswith(INFLIGHT_PREFIX), late)

        print('② the mod reports its own failure at once, with its reason')
        root_m = make_repo(base, 'repo-mod-fail')
        attach.set_switch(store, 'on', root_m)
        tid_m, pay_m = tell(store, root_m, base, 'm1')
        continuity.mod_hello(store, 'm1')
        attach.handle(store, 'prompt', dict(pay_m, prompt='Add CSV export for the weekly report'), 'claude')
        none_yet = subprocess.run(CLI + ['--home', str(store.home), 'handoff-claim', '--session', 'm1', '--failed', 'x'],
                                  capture_output=True, text=True, timeout=60)
        check('control: before the mod took it, --failed records nothing (exit 1)',
              none_yet.returncode == 1 and state_of(store, tid_m).get('state') == 'auto', (none_yet.stdout, state_of(store, tid_m)))
        continuity.claim_handoff(store, 'm1', 'mod')
        done = subprocess.run(CLI + ['--home', str(store.home), 'handoff-claim', '--session', 'm1', '--failed',
                                     'no fresh session took the task over within 20 s of the /clear'],
                              capture_output=True, text=True, timeout=60)
        row = [r for r in store.events(tid_m) if r['kind'] == 'handoff_auto_failed'][-1:]
        payload = json.loads(row[0]['payload']) if row and isinstance(row[0]['payload'], str) else (row[0]['payload'] if row else {})
        check('the mod took it and failed: recorded now, auto_failed, the reason is the mod\'s own',
              done.returncode == 0 and state_of(store, tid_m).get('state') == 'auto_failed'
              and payload.get('reason', '').startswith('mod: no fresh session'), (done.stdout, done.stderr[-200:], payload))
        check('…with the attempt number and whether another follows', payload.get('attempt') == 1 and payload.get('retry') is True, payload)
        brief = '\n'.join(continuity.handoff_lines(store.get(tid_m)['data'], 'cosmos-orbit', tid_m))
        check('…and the session is told Orbit tries again (not only "ask the user to /clear")',
              'attempt 1 of %d' % RETRY_LIMIT in brief and 'tries again' in brief, brief)

        print('③ a failed handoff is tried again, typing /clear itself, at most %d attempts' % RETRY_LIMIT)
        latest = store.get(tid_m)
        check('the supervisor may start the fresh context again (handoff_continues)',
              supervision.handoff_continues(store, latest), state_of(store, tid_m))
        count = len(typed)
        with store.lock('task:' + tid_m):
            latest = store.get(tid_m)
            started = continuity.continue_in_fresh_context(store, latest, 'm1', 'claude', FAKE)
        carried = store.get(tid_m)['data'].get('carried_prompt') or {}
        steps = typed[-1][1] if len(typed) > count else []
        check('…attempt 2 starts at the next stop and types /clear itself: no grace for the mod that failed',
              started and state_of(store, tid_m).get('attempts') == 2 and state_of(store, tid_m).get('state') == 'auto'
              and 'mod_grace' not in [k for k, _ in steps] and not state_of(store, tid_m).get('claimed_by'),
              (started, state_of(store, tid_m), steps[:4]))
        check('…the request the failed attempt carried is still the one carried (not replaced by "continue")',
              'CSV export' in carried.get('text', '') and continuity.CONTINUE_TEXT not in carried.get('text', ''), carried)
        continuity.auto_failed(store, tid_m, 'the fresh session did not take the task over within 25s')
        count = len(typed)
        again = attach.handle(store, 'prompt', dict(pay_m, prompt='Also add the PDF export with page numbers'), 'claude')
        steps = typed[-1][1] if len(typed) > count else []
        carried = store.get(tid_m)['data'].get('carried_prompt') or {}
        check("attempt 3 at the user's next request: blocked and carried, typed (no mod grace though the mod is loaded)",
              (again or {}).get('decision') == 'block' and state_of(store, tid_m).get('attempts') == 3
              and len(typed) == count + 1 and 'mod_grace' not in [k for k, _ in steps], (again, state_of(store, tid_m), steps[:4]))
        check('…the unreceived request and the new one both travel', 'CSV export' in carried.get('text', '')
              and 'PDF export' in carried.get('text', ''), carried)
        continuity.auto_failed(store, tid_m, 'the fresh session did not take the task over within 25s')
        latest = store.get(tid_m)
        count = len(typed)
        with store.lock('task:' + tid_m):
            third = continuity.continue_in_fresh_context(store, store.get(tid_m), 'm1', 'claude', FAKE)
        brief = '\n'.join(continuity.handoff_lines(store.get(tid_m)['data'], 'cosmos-orbit', tid_m))
        check('after %d failed attempts: no more (the supervisor waits, nothing typed)' % RETRY_LIMIT,
              not supervision.handoff_continues(store, latest) and not third and len(typed) == count, state_of(store, tid_m))
        check('…and the session asks the user for /clear', 'ask the user to type /clear' in brief and '3 attempts' in brief, brief)
        attempted = attach.handle(store, 'prompt', dict(pay_m, prompt='Then add an XLSX export too'), 'claude')
        check('…a request then is not handed off again by itself (control for the limit)',
              not (attempted or {}).get('reason', '').startswith(continuity.HANDOFF_PREFIX), attempted)
        root_o = make_repo(base, 'repo-old-record')
        attach.set_switch(store, 'on', root_o)
        tid_o, pay_o = tell(store, root_o, base, 'o1')
        attach.handle(store, 'prompt', dict(pay_o, prompt='Add a dark theme to the settings page'), 'claude')
        with store.lock('task:' + tid_o):
            task = store.get(tid_o)
            task['data']['handoff_state'].pop('attempts', None)          # written before attempts were counted
            store.save(task, task['status'], 'test_setup', {})
        continuity.auto_failed(store, tid_o, 'the fresh session did not take the task over within 25s')
        check('control: a record from before attempts were counted holds one attempt, so it is tried again',
              attempts(state_of(store, tid_o)) == 1 and retry_ready(state_of(store, tid_o)), state_of(store, tid_o))

        print('④ the conversation that took a task over says so on screen, once')
        root_n = make_repo(base, 'repo-notice')
        attach.set_switch(store, 'on', root_n)
        tid_n, pay_n = tell(store, root_n, base, 'n1')
        attach.handle(store, 'prompt', dict(pay_n, prompt='Add the sleep chart with weekly totals'), 'claude')
        start = attach.handle(store, 'session-start', {'cwd': str(root_n), 'session_id': 'n2', 'source': 'clear'}, 'claude')
        check('not spent at the SessionStart after /clear (Claude Code does not show that one: d27_shows_probe.sh) — '
              'the brief still goes to the model', not (start or {}).get('systemMessage')
              and 'CARRIED OVER' in ((start or {}).get('hookSpecificOutput') or {}).get('additionalContext', ''), start)
        carry = attach.handle(store, 'prompt', {'cwd': str(root_n), 'session_id': 'n2', 'prompt': typist.CARRY_LINE}, 'claude')
        shown = (carry or {}).get('systemMessage', '')
        check('its first prompt (the carry line) shows it took the task over: task id, previous size, checkpoint, request',
              ('took over task ' + tid_n) in shown and '310k' in shown and 'Draw the sleep chart' in shown
              and 'Carried over: Add the sleep chart' in shown, shown)
        later = attach.handle(store, 'prompt', {'cwd': str(root_n), 'session_id': 'n2', 'prompt': 'Also show the weekly average'}, 'claude')
        check('…once: the next prompt shows nothing more', not (later or {}).get('systemMessage'), later)
        root_f = make_repo(base, 'repo-notice-failed')
        attach.set_switch(store, 'on', root_f)
        tid_f, pay_f = tell(store, root_f, base, 'f1')
        attach.handle(store, 'prompt', dict(pay_f, prompt='Add the sleep chart with weekly totals'), 'claude')
        continuity.auto_failed(store, tid_f, 'the fresh session did not take the task over within 25s')
        attach.handle(store, 'prompt', dict(pay_f, prompt='What is left?'), 'claude')   # the held request is done here
        end_turn(store, pay_f)
        with store.lock('task:' + tid_f):           # the attempts are spent: only the user's /clear is left (15:55 KST)
            task = store.get(tid_f)
            task['data']['handoff_state']['attempts'] = RETRY_LIMIT
            store.save(task, task['status'], 'test_setup', {})
        attach.handle(store, 'session-start', {'cwd': str(root_f), 'session_id': 'f2', 'source': 'clear'}, 'claude')
        first = attach.handle(store, 'prompt', {'cwd': str(root_f), 'session_id': 'f2', 'prompt': '이어 받았어.'}, 'claude')
        shown = (first or {}).get('systemMessage', '')
        check("the user's own /clear after failed attempts, nothing waiting: the first message shows the takeover and that "
              'the task goes on from the checkpoint (15:56 KST nothing said so)',
              ('took over task ' + tid_f) in shown and 'goes on from the checkpoint' in shown, (shown, first))
        root_t = make_repo(base, 'repo-notice-typist')
        attach.set_switch(store, 'on', root_t)
        tid_t, pay_t = tell(store, root_t, base, 't1')
        attach.handle(store, 'prompt', dict(pay_t, prompt='Add the sleep chart with weekly totals'), 'claude')
        with store.lock('task:' + tid_t):          # the typist handed the pane over after the fresh session's start
            task = store.get(tid_t)
            task['data']['handoff_state'].update(state='adopted', adopted_by='t2', adopted_from='auto')
            store.save(task, task['status'], 'handoff_adopted', {'session': 't2', 'by': 'typist'})
            store.bind_session('t2', tid_t, str(root_t))
        carry = attach.handle(store, 'prompt', {'cwd': str(root_t), 'session_id': 't2', 'prompt': typist.CARRY_LINE}, 'claude')
        check('the typist handed the pane over after SessionStart: the carry line\'s prompt shows it instead',
              ('took over task ' + tid_t) in (carry or {}).get('systemMessage', ''), carry)
        plain = attach.handle(store, 'session-start', {'cwd': str(make_repo(base, 'repo-plain')), 'session_id': 'p1',
                                                       'source': 'startup'}, 'claude')
        check('control: a session that took nothing over shows nothing', not (plain or {}).get('systemMessage'), plain)
        root_c = make_repo(base, 'repo-codex')
        attach.set_switch(store, 'on', root_c)
        tid_c, pay_c = tell(store, root_c, base, 'c1')
        with store.lock('task:' + tid_c):
            task = store.get(tid_c)
            task['data']['handoff_state'].update(state='adopted', adopted_by='c2', adopted_from='auto')
            store.save(task, task['status'], 'test_setup', {})
            store.bind_session('c2', tid_c, str(root_c))
        check('control: Codex is left as it was (no systemMessage)', takeover_notice(store, 'c2', 'codex') is None)
    finally:
        typist.detect, typist.spawn = real_detect, real_spawn
    print('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS · all checks passed — 0 failed')
    shutil.rmtree(base, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
