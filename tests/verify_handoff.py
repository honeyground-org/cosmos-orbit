#!/usr/bin/env python3
"""Model-free check of how a handoff ends: by the user, by a compaction, by expiry, or by Orbit typing it.

Two observed failures are replayed with synthetic data:
  - a session told to hand off kept working, its host compacted it twice, and 18 hours later an unrelated new
    session in the same folder took its task over (the open handoff never closed and never expired);
  - the control: /clear 13 minutes after the message hands the task over, as it should.
Auto mode is checked with a fake terminal: nothing is typed into a real one, except in section ⑥, which types
into a throwaway tmux session when tmux is installed. Every rule has a control.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID'):
    os.environ.pop(name, None)  # never type into the terminal running this check

from harness import attach, continuity, requirements, typist  # noqa: E402
from harness.store import Store, now  # noqa: E402

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
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


def kinds(store, tid):
    return [row['kind'] for row in store.events(tid)]


def tell(store, root, base, session, tokens=210_000):
    """Drive one session to 'told': past the point, asked once, checkpointed, told."""
    path = base / (session + '.jsonl')
    turn(path, 50_000)
    pay = {'cwd': str(root), 'session_id': session, 'transcript_path': str(path)}
    attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    attach.handle(store, 'prompt', dict(pay, prompt='Refactor app.py into a module with tests and keep the output identical'), 'claude')
    tid = attach.current_task(store, root)['id']
    turn(path, tokens)
    attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining tests'), 'claude')
    attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Half done.'), 'claude')
    continuity.record_checkpoint(store, tid, 'Write tests/test_mod.py', notes='mod.py extracted')
    told = attach.handle(store, 'stop', dict(pay, stop_hook_active=True, last_assistant_message='checkpoint saved'), 'claude')
    return tid, pay, path, told


def end_turn(store, pay):
    """Stops until the Stop hook lets the turn end (it may send it back first). Returns how often it did."""
    blocked = 0
    while blocked < 6:
        reply = attach.handle(store, 'stop', dict(pay, stop_hook_active=blocked > 0, last_assistant_message='Answered.'), 'claude')
        if (reply or {}).get('decision') != 'block':
            return blocked
        blocked += 1
    raise AssertionError('the Stop hook never let the turn end')


def age(store, tid, seconds):
    """Move the handoff's times back, as if it was told `seconds` ago."""
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store.lock('task:' + tid):
        task = store.get(tid)
        state = task['data']['handoff_state']
        for key in ('at', 'checkpoint_at', 'told_at'):
            state[key] = stamp
        store.save(task, task['status'], 'test_setup', {})


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-handoff-'))
    store = Store(base / 'home')
    continuity.set_threshold(store, '200k', global_default=True)  # the scenarios below drive sessions past 200k

    print('① a compaction closes the open handoff, and the session is measured again from there')
    root = make_repo(base, 'repo-compact')
    attach.set_switch(store, 'on', root)
    tid, pay, path, told = tell(store, root, base, 'a1')
    check('told: the message says Orbit cannot clear the session itself, and names /clear and /new',
          told and 'cannot clear this session itself' in told.get('systemMessage', '') and '/clear' in told['systemMessage']
          and '/new' in told['systemMessage'], told)
    check('the told time is recorded', bool(store.get(tid)['data']['handoff_state'].get('told_at')))
    turn(path, 32_000)  # the host compacted the session: 210k -> 32k, same session id
    attach.handle(store, 'prompt', dict(pay, prompt='how far did the tests get?'), 'claude')
    state = store.get(tid)['data']['handoff_state']
    check('compaction observed → the handoff is closed (state compacted)', state.get('state') == 'compacted', state)
    other = attach.handle(store, 'session-start', {'cwd': str(root), 'session_id': 'a2', 'source': 'startup'}, 'claude')
    check('an unrelated new session in the same folder does not take the task (the observed defect)',
          (store.session_task('a2') or {}).get('task_id') != tid and not other, store.session_task('a2'))
    turn(path, 150_000)
    attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining tests'), 'claude')
    check('…below the point again after compaction: silent', store.get(tid)['data']['handoff_state']['state'] == 'compacted')
    turn(path, 215_000)
    out = attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining tests'), 'claude')
    brief = out['hookSpecificOutput']['additionalContext'] if out else ''
    check('…past the point again: recommended again in the same session (re-armed)',
          store.get(tid)['data']['handoff_state']['state'] == 'recommended' and 'CONTEXT 215k' in brief, brief[-300:])
    check('two recommendations in the ledger', kinds(store, tid).count('handoff_recommended') == 2)

    # the PreCompact hook closes it too, before any measurement shows the drop
    root_b = make_repo(base, 'repo-precompact')
    attach.set_switch(store, 'on', root_b)
    tid_b, pay_b, _, _ = tell(store, root_b, base, 'b1')
    attach.handle(store, 'pre-compact', dict(pay_b, trigger='auto'), 'claude')
    check('PreCompact closes the open handoff as well', store.get(tid_b)['data']['handoff_state']['state'] == 'compacted')

    print('② an open handoff expires; a prompt /clear still hands over (control)')
    root_c = make_repo(base, 'repo-expiry')
    attach.set_switch(store, 'on', root_c)
    tid_c, pay_c, _, _ = tell(store, root_c, base, 'c1')
    age(store, tid_c, 13 * 60)
    attach.handle(store, 'session-start', {'cwd': str(root_c), 'session_id': 'c2', 'source': 'clear'}, 'claude')
    check('13 minutes after the message, /clear hands the task over (control)',
          (store.session_task('c2') or {}).get('task_id') == tid_c and store.get(tid_c)['data']['handoff_state']['state'] == 'adopted')
    root_d = make_repo(base, 'repo-expired')
    attach.set_switch(store, 'on', root_d)
    tid_d, pay_d, _, _ = tell(store, root_d, base, 'd1')
    age(store, tid_d, 13 * 3600)
    attach.handle(store, 'session-start', {'cwd': str(root_d), 'session_id': 'd2', 'source': 'startup'}, 'claude')
    check('13 hours later, with the old session still open, a new session does not take it',
          (store.session_task('d2') or {}).get('task_id') != tid_d, store.session_task('d2'))
    check('invitation_open: a stampless record from older ledgers stays open (compatibility)',
          continuity.invitation_open({'state': 'told'}) and not continuity.invitation_open({'state': 'compacted'}))

    print('③ auto mode: the next request starts the fresh context by itself (fake terminal)')
    root_e = make_repo(base, 'repo-auto')
    attach.set_switch(store, 'on', root_e)
    typed = []
    real_detect, real_spawn = typist.detect, typist.spawn
    fake = {'kind': 'cmux', 'cli': '/bin/echo', 'target': 'PANEL-1'}
    try:
        typist.detect = lambda env=None: fake
        typist.spawn = lambda terminal, steps, log, home=None, task=None: typed.append((terminal, steps, task))
        tid_e, pay_e, _, told_e = tell(store, root_e, base, 'e1')
        out = attach.handle(store, 'prompt', dict(pay_e, prompt='Also add CSV export for the report'), 'claude')
        check('tell mode (default): the request goes through, nothing typed (control)', out is None or 'decision' not in out)
        check('…and nothing was typed', not typed)
        continuity.set_handoff_mode(store, 'auto')
        root_f = make_repo(base, 'repo-auto-2')
        attach.set_switch(store, 'on', root_f)
        tid_f, pay_f, _, told_f = tell(store, root_f, base, 'f1')
        check('auto mode: the message says the next request hands off by itself',
              told_f and 'starts a fresh context by itself' in told_f.get('systemMessage', ''), told_f)
        question = attach.handle(store, 'prompt', dict(pay_f, prompt='What did the last test print?'), 'claude')
        check('a question is answered in place, not handed off (control)', (question or {}).get('decision') != 'block' and not typed)
        end_turn(store, pay_f)  # the answer ends the turn; the next request arrives at an idle input line
        request = 'Add CSV export for the report\nwith one row per item'
        out = attach.handle(store, 'prompt', dict(pay_f, prompt=request), 'claude')
        check('a request is blocked here and carried over', out and out.get('decision') == 'block' and 'carried over' in out['reason'], out)
        keys = [s[1] for s in typed[-1][1] if s[0] in ('key', 'text')] if typed else []
        presses = keys.index('/clear') if '/clear' in keys else -1
        check('the typist empties the input first (one ctrl+u per line of the restored request, at least), then '
              '/clear, Enter, the carry line, Enter', presses >= 2 and set(keys[:presses]) == {'ctrl+u'}
              and keys[presses:] == ['/clear', 'enter', typist.CARRY_LINE, 'enter'], keys)
        check('…and the carry line waits until the task was taken over', typed and ('adopted', 25.0) in typed[-1][1]
              and typed[-1][2] == tid_f, typed[-1][1] if typed else None)
        data = store.get(tid_f)['data']
        check('the request is in the ledger (carried_prompt) and the state is auto',
              data['carried_prompt']['text'] == request and data['handoff_state']['state'] == 'auto' and 'handoff_auto' in kinds(store, tid_f))
        typist.detect = lambda env=None: dict(fake, target='PANEL-2')
        attach.handle(store, 'session-start', {'cwd': str(root_f), 'session_id': 'f9', 'source': 'clear'}, 'claude')
        check('a /clear in another pane does not take the auto handoff (observed: another session took it)',
              (store.session_task('f9') or {}).get('task_id') != tid_f and store.get(tid_f)['data']['handoff_state']['state'] == 'auto')
        typist.detect = lambda env=None: fake
        start = attach.handle(store, 'session-start', {'cwd': str(root_f), 'session_id': 'f2', 'source': 'clear'}, 'claude')
        brief = start['hookSpecificOutput']['additionalContext'] if start else ''
        check('the fresh session takes the task over and its brief carries the request',
              (store.session_task('f2') or {}).get('task_id') == tid_f and 'CARRIED OVER' in brief and 'one row per item' in brief, brief[-400:])
        line = attach.handle(store, 'prompt', {'cwd': str(root_f), 'session_id': 'f2', 'prompt': typist.CARRY_LINE}, 'claude')
        check('the typed carry line is not a new requirement', requirements.classify(typist.CARRY_LINE) == 'trivial'
              and not store.get(tid_f)['data'].get('requirements_pending'), line)
        check('…and the carried request reached exactly that session', store.get(tid_f)['data']['carried_prompt'].get('served_to') == 'f2')
        later = continuity.handoff_lines(dict(store.get(tid_f)['data'], context_live={'session': 'f3'}), 'cosmos-orbit', tid_f)
        check('a later session does not receive the carried request again', not any(l.startswith('CARRIED') for l in later), later)
        # 2026-11-05: the session that received the request at 11:01 (and did it) was told to do it 'now' again in
        # its 12:20 brief. The request belongs to the turn the carry line starts, not to every brief after it.
        pay_f2 = {'cwd': str(root_f), 'session_id': 'f2'}
        mine = continuity.handoff_lines(store.get(tid_f)['data'], 'cosmos-orbit', tid_f)
        check('during the turn the carry line started, the request is still in its brief (control)',
              any(l.startswith('CARRIED') for l in mine), mine)
        end_turn(store, pay_f2)
        attach.handle(store, 'prompt', dict(pay_f2, prompt='이어서 진행해'), 'claude')  # the 12:20 go-ahead
        done_f = store.get(tid_f)
        mine = continuity.handoff_lines(done_f['data'], 'cosmos-orbit', tid_f)
        check("the receiving session's next request no longer carries it (the 12:20 brief)",
              not any(l.startswith('CARRIED') for l in mine), mine)
        undone = json.loads(json.dumps(done_f))
        undone['data']['carried_prompt'].pop('consumed_at', None)
        check('…dropping the line alone does not change the digest, so it sends no brief by itself',
              attach.brief_digest(store, done_f) == attach.brief_digest(store, undone)
              and 'consumed_at' in done_f['data']['carried_prompt'], done_f['data']['carried_prompt'])
        attach.handle(store, 'pre-compact', dict(pay_f2, trigger='auto'), 'claude')  # the whole brief goes again
        resent = attach.handle(store, 'prompt', dict(pay_f2, prompt='이어서 진행해'), 'claude')
        resent = (resent or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
        check('…the whole brief sent again after a compaction does not carry it either',
              'GOAL:' in resent and 'CARRIED OVER' not in resent, resent[-400:])
        count = len(typed)
        root_g = make_repo(base, 'repo-codex')
        attach.set_switch(store, 'on', root_g)
        tid_g, pay_g, _, _ = tell(store, root_g, base, 'g1')
        codex = continuity.auto_handoff(store, store.get(tid_g), 'g1', 'Add PDF export', 'requirement', 'codex')
        check('Codex: no block (its prompt hook is not relied on to block), nothing typed (control)', codex is None and len(typed) == count)
        typist.detect = lambda env=None: None
        root_h = make_repo(base, 'repo-noterm')
        attach.set_switch(store, 'on', root_h)
        tid_h, pay_h, _, told_h = tell(store, root_h, base, 'h1')
        out = attach.handle(store, 'prompt', dict(pay_h, prompt='Add PDF export'), 'claude')
        check('no terminal to type into: the request goes through and the message asks the user (control)',
              (out or {}).get('decision') != 'block' and 'cannot clear this session itself' in (told_h or {}).get('systemMessage', ''), told_h)
    finally:
        typist.detect, typist.spawn = real_detect, real_spawn
        continuity.set_handoff_mode(store, 'tell')

    print('③-a a checkpoint written just before the point is the handoff\'s: the next request hands off (2026-11-05 12:20)')
    # Observed: the session wrote a checkpoint at 12:18 (341k, point 350k); at 12:20 the user's go-ahead crossed the
    # point (355k) and was only "recommended": no handoff, a checkpoint asked for again, and the user had to send
    # another message.
    typed = []
    try:
        typist.detect = lambda env=None: fake
        typist.spawn = lambda terminal, steps, log, home=None, task=None: typed.append((terminal, steps, task))
        continuity.set_handoff_mode(store, 'auto')

        def near_point(name, checkpoint_at, compact_to=None):
            """A session that wrote its checkpoint at `checkpoint_at` tokens, then (optionally) was compacted, then
            crossed the 200k point with a go-ahead. Returns (task id, reply to the go-ahead)."""
            root_ = make_repo(base, name)
            attach.set_switch(store, 'on', root_)
            path_ = base / (name + '.jsonl')
            turn(path_, 50_000)
            pay_ = {'cwd': str(root_), 'session_id': name, 'transcript_path': str(path_)}
            attach.handle(store, 'session-start', dict(pay_, source='startup'), 'claude')
            attach.handle(store, 'prompt', dict(pay_, prompt='Refactor app.py into a module with tests and keep the output identical'), 'claude')
            tid_ = attach.current_task(store, root_)['id']
            turn(path_, checkpoint_at)
            attach.handle(store, 'prompt', dict(pay_, prompt='keep going with the remaining tests'), 'claude')
            continuity.record_checkpoint(store, tid_, 'Write tests/test_mod.py', notes='mod.py extracted')
            end_turn(store, pay_)
            if compact_to:
                turn(path_, compact_to)
                attach.handle(store, 'prompt', dict(pay_, prompt='이어서 진행해'), 'claude')
                end_turn(store, pay_)
            turn(path_, 205_000)
            return tid_, attach.handle(store, 'prompt', dict(pay_, prompt='이어서 진행해'), 'claude')

        count = len(typed)
        tid_r, out = near_point('repo-early-checkpoint', 190_000)
        check('a checkpoint at 95% of the point: the go-ahead that crosses it hands off at once (the 12:20 defect)',
              (out or {}).get('decision') == 'block' and len(typed) == count + 1
              and store.get(tid_r)['data']['handoff_state'].get('state') == 'auto', (out, store.get(tid_r)['data']['handoff_state']))
        count = len(typed)
        tid_r, out = near_point('repo-old-checkpoint', 100_000)
        check('control: a checkpoint at half the point is stale; a fresh one is asked for first',
              (out or {}).get('decision') != 'block' and len(typed) == count
              and store.get(tid_r)['data']['handoff_state'].get('state') == 'recommended', store.get(tid_r)['data']['handoff_state'])
        count = len(typed)
        tid_r, out = near_point('repo-checkpoint-before-compaction', 190_000, compact_to=40_000)
        check('control: a checkpoint from before a compaction describes a context that is gone; a fresh one is asked for',
              (out or {}).get('decision') != 'block' and len(typed) == count
              and store.get(tid_r)['data']['handoff_state'].get('state') == 'recommended', store.get(tid_r)['data']['handoff_state'])
        continuity.set_handoff_mode(store, 'tell')
        count = len(typed)
        tid_r, out = near_point('repo-early-checkpoint-tell', 190_000)
        check('tell mode: nothing is typed, the handoff is ready (checkpointed) and the Stop hook tells the user (control)',
              (out or {}).get('decision') != 'block' and len(typed) == count
              and store.get(tid_r)['data']['handoff_state'].get('state') == 'checkpointed', store.get(tid_r)['data']['handoff_state'])
    finally:
        typist.detect, typist.spawn = real_detect, real_spawn
        continuity.set_handoff_mode(store, 'tell')

    print('③-b a message sent while the session is working never starts the handoff (2026-11-05 10:56)')
    # Observed: a go-ahead started a turn; a request typed while that turn ran was blocked, /clear went into the host's
    # input queue, the typist gave up after 25 s, and the queued /clear ran 4 minutes later when the turn ended.
    typed = []
    try:
        typist.detect = lambda env=None: fake
        typist.spawn = lambda terminal, steps, log, home=None, task=None: typed.append((terminal, steps, task))
        continuity.set_handoff_mode(store, 'auto')
        root_m = make_repo(base, 'repo-midturn')
        attach.set_switch(store, 'on', root_m)
        tid_m, pay_m, _, _ = tell(store, root_m, base, 'm1')
        attach.handle(store, 'prompt', dict(pay_m, prompt='What did the last test print?'), 'claude')  # a turn starts
        mid = attach.handle(store, 'prompt', dict(pay_m, prompt='Keep the flow exact so that no session is ever lost'), 'claude')
        state = store.get(tid_m)['data']['handoff_state']
        check('a request typed during a turn is not blocked and nothing is typed (the incident)',
              (mid or {}).get('decision') != 'block' and not typed and state.get('state') in ('checkpointed', 'told'),
              (mid, typed, state.get('state')))
        check('…the ledger says why the handoff waited', 'handoff_deferred' in kinds(store, tid_m), kinds(store, tid_m)[-6:])
        real_reply = attach._stop_reply
        try:
            attach._stop_reply = lambda *a: {'decision': 'block', 'reason': 'Orbit: a fixed check failed. Fix it now.'}
            attach.handle(store, 'stop', dict(pay_m, stop_hook_active=False, last_assistant_message='Done.'), 'claude')
        finally:
            attach._stop_reply = real_reply
        inside = attach.handle(store, 'prompt', dict(pay_m, prompt='Also add CSV export while you are at it'), 'claude')
        check('a request typed after the Stop hook sent the turn back is still during the turn: not blocked',
              (inside or {}).get('decision') != 'block' and not typed, inside)
        end_turn(store, pay_m)
        after = attach.handle(store, 'prompt', dict(pay_m, prompt='Add PDF export for the report'), 'claude')
        check('after the turn ended, the next request hands off (control)',
              (after or {}).get('decision') == 'block' and len(typed) == 1
              and store.get(tid_m)['data']['handoff_state']['state'] == 'auto', (after, len(typed)))
        attach.handle(store, 'prompt', dict(pay_m, prompt='What did the export write?'), 'claude')  # turn left open
        attach.handle(store, 'session-start', dict(pay_m, source='resume'), 'claude')               # process restarted
        check('a session that restarts (resume) is at its input line: a turn left open is closed',
              not store.get(tid_m)['data']['supervision']['m1'].get('turn_open'))
    finally:
        typist.detect, typist.spawn = real_detect, real_spawn
        continuity.set_handoff_mode(store, 'tell')

    print('③-c after a failed automatic /clear: the request is done exactly once, by whichever session runs next')
    typed = []
    try:
        typist.detect = lambda env=None: fake
        typist.spawn = lambda terminal, steps, log, home=None, task=None: typed.append((terminal, steps, task))
        continuity.set_handoff_mode(store, 'auto')
        # (c) the old session runs another turn: the held request comes back to it, and not to a later fresh session
        root_n = make_repo(base, 'repo-held')
        attach.set_switch(store, 'on', root_n)
        tid_n, pay_n, _, _ = tell(store, root_n, base, 'n1')
        attach.handle(store, 'prompt', dict(pay_n, prompt='Add CSV export with one row per item'), 'claude')
        continuity.auto_failed(store, tid_n, 'the fresh session did not take the task over within 25s')
        nxt = attach.handle(store, 'prompt', dict(pay_n, prompt='What is left to do?'), 'claude')
        brief = (nxt or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
        check('the old session runs another turn: its brief holds the request for it (HELD REQUEST)',
              'HELD REQUEST' in brief and 'one row per item' in brief, brief[-500:])
        check('…served to the old session once', store.get(tid_n)['data']['carried_prompt'].get('served_to') == 'n1')
        end_turn(store, pay_n)
        attach.handle(store, 'prompt', dict(pay_n, prompt='이어서 진행해'), 'claude')
        check("…and that session's next request no longer holds it (the turn that did it is over)",
              not any(l.startswith('HELD') for l in continuity.handoff_lines(store.get(tid_n)['data'], 'cosmos-orbit', tid_n)))
        end_turn(store, pay_n)
        count = len(typed)
        start = attach.handle(store, 'session-start', {'cwd': str(root_n), 'session_id': 'n2', 'source': 'clear'}, 'claude')
        brief = start['hookSpecificOutput']['additionalContext'] if start else ''
        check('…a fresh session after /clear takes the task but is not told to do it again (the 11:01 defect)',
              (store.session_task('n2') or {}).get('task_id') == tid_n and 'CARRIED OVER' not in brief
              and len(typed) == count, (brief[-300:], len(typed) - count))
        # (b) the user clears by hand after the failure: the fresh session takes it and starts by itself
        root_p = make_repo(base, 'repo-failed-clear')
        attach.set_switch(store, 'on', root_p)
        tid_p, pay_p, _, _ = tell(store, root_p, base, 'p1')
        attach.handle(store, 'prompt', dict(pay_p, prompt='Add PDF export with page numbers'), 'claude')
        continuity.auto_failed(store, tid_p, 'the fresh session did not take the task over within 25s')
        count = len(typed)
        start = attach.handle(store, 'session-start', {'cwd': str(root_p), 'session_id': 'p2', 'source': 'clear'}, 'claude')
        brief = start['hookSpecificOutput']['additionalContext'] if start else ''
        state = store.get(tid_p)['data']['handoff_state']
        check('a /clear after the failure: the fresh session takes the task, its brief carries the request',
              (store.session_task('p2') or {}).get('task_id') == tid_p and 'CARRIED OVER' in brief
              and state.get('adopted_from') == 'auto_failed', (brief[-300:], state))
        check('…and Orbit types the carry line once its input is empty (no typist was waiting: the 10-minute stall)',
              len(typed) == count + 1 and typed[-1][1] == typist.carry_steps() and typed[-1][2] == tid_p,
              typed[count:])
        attach.handle(store, 'session-start', {'cwd': str(root_p), 'session_id': 'p2', 'source': 'resume'}, 'claude')
        check('…once: another SessionStart of that session types nothing more', len(typed) == count + 1, len(typed) - count)
        # control: an automatic handoff that worked leaves the carry line to its own typist
        root_q = make_repo(base, 'repo-auto-ok')
        attach.set_switch(store, 'on', root_q)
        tid_q, pay_q, _, _ = tell(store, root_q, base, 'q1')
        attach.handle(store, 'prompt', dict(pay_q, prompt='Add XLSX export with one sheet per month'), 'claude')
        count = len(typed)
        attach.handle(store, 'session-start', {'cwd': str(root_q), 'session_id': 'q2', 'source': 'clear'}, 'claude')
        check('control: after a working automatic /clear nothing extra is typed (its typist types the carry line)',
              (store.session_task('q2') or {}).get('task_id') == tid_q and len(typed) == count, len(typed) - count)
    finally:
        typist.detect, typist.spawn = real_detect, real_spawn
        continuity.set_handoff_mode(store, 'tell')
    tmux_t = {'kind': 'tmux', 'cli': 'tmux', 'target': '%3'}
    seen = []
    ran = typist.run(tmux_t, typist.carry_steps(), runner=lambda argv: 0, sleep=lambda s: None,
                     deliver=lambda terminal, wait, line: seen.append(line) or ('woken', [['typed', line]]))
    check('the carry step types the carry line through wake.deliver (empty input only)',
          seen == [typist.CARRY_LINE] and ran == [['typed', typist.CARRY_LINE]], (seen, ran))
    try:
        typist.run(tmux_t, typist.carry_steps(), runner=lambda argv: 0, sleep=lambda s: None,
                   deliver=lambda terminal, wait, line: ('expired_typing', []))
        error = None
    except RuntimeError as failure:
        error = str(failure)
    check('…the user kept typing until the wait ran out: nothing typed, the failure is reported (control)',
          error and 'expired_typing' in error, error)

    print('③-d a session that took a task at its start and sat idle does not fold next-day work into it (2026-11-05)')
    # Observed: the fresh session took the harness task at 11-04 14:54 (after a failed automatic /clear) and sat idle;
    # at 11-05 10:38 the user used that pane for unrelated product work, which Orbit recorded as corrections of the
    # harness task; the stop guard demanded harness claims and the 11:49 handoff carried the harness goal.
    def bound_hours_ago(session, hours):
        from datetime import datetime, timedelta, timezone
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with store.db:
            store.db.execute('UPDATE session_tasks SET at=? WHERE session=?', (stamp, session))
    other = 'Build the content studio web page: series, episodes and video production steps'
    for name, hours, line, takes in (('idle-day', 20, other, False), ('idle-carry', 20, typist.CARRY_LINE, True),
                                     ('idle-soon', 2, other, True)):
        root_s = make_repo(base, 'repo-' + name)
        attach.set_switch(store, 'on', root_s)
        tid_s, pay_s, _, _ = tell(store, root_s, base, name + '-1')
        fresh = name + '-2'
        attach.handle(store, 'session-start', {'cwd': str(root_s), 'session_id': fresh, 'source': 'clear'}, 'claude')
        took = (store.session_task(fresh) or {}).get('task_id') == tid_s
        bound_hours_ago(fresh, hours)
        before = len(store.get(tid_s)['data']['prompts'])
        attach.handle(store, 'prompt', {'cwd': str(root_s), 'session_id': fresh, 'prompt': line}, 'claude')
        after = store.get(tid_s)['data']
        now_bound = (store.session_task(fresh) or {}).get('task_id')
        if takes:
            check('%s: %dh, %s → the request stays with the task it took (control)' % (name, hours, 'carry line' if line == typist.CARRY_LINE else 'a request'),
                  took and now_bound == tid_s and len(after['prompts']) == before + 1, (took, now_bound, len(after['prompts']) - before))
        else:
            new = store.get(now_bound) if now_bound else None
            check('idle-day: 20h idle after taking the task, an unrelated request is not recorded on it (the incident)',
                  took and len(after['prompts']) == before and now_bound and now_bound != tid_s, (took, now_bound, len(after['prompts']) - before))
            check('…it starts its own task with that request', new is not None and new['prompt'].startswith('Build the content studio'),
                  new and new['prompt'][:60])
            check('…the old task is released, not offered again (no open invitation)',
                  after['handoff_state'].get('state') == 'released' and not continuity.invitation_open(after['handoff_state'])
                  and 'adoption_released' in kinds(store, tid_s), after['handoff_state'])
    root_w = make_repo(base, 'repo-worked')
    attach.set_switch(store, 'on', root_w)
    tid_w, pay_w, _, _ = tell(store, root_w, base, 'worked-1')
    attach.handle(store, 'session-start', {'cwd': str(root_w), 'session_id': 'worked-2', 'source': 'clear'}, 'claude')
    attach.handle(store, 'prompt', {'cwd': str(root_w), 'session_id': 'worked-2', 'prompt': 'Continue with the remaining tests in tests/test_mod.py'}, 'claude')
    bound_hours_ago('worked-2', 20)
    attach.handle(store, 'prompt', {'cwd': str(root_w), 'session_id': 'worked-2', 'prompt': 'Also add a CSV export to the report module'}, 'claude')
    check('control: a session that worked on the task keeps it a day later (only an idle takeover is released)',
          (store.session_task('worked-2') or {}).get('task_id') == tid_w
          and store.get(tid_w)['data']['prompts'][-1]['text'].startswith('Also add a CSV'), store.session_task('worked-2'))

    print('④ the terminal commands')
    cmux = {'kind': 'cmux', 'cli': 'cmux', 'target': 'P'}
    tmux = {'kind': 'tmux', 'cli': 'tmux', 'target': '%3'}
    check('cmux: send-panel text and raw ctrl+u (0x15; its key event never reached Claude Code), send-key-panel enter', typist.command(cmux, text='/clear') == ['cmux', 'send-panel', '--panel', 'P', '--', '/clear']
          and typist.command(cmux, key='enter') == ['cmux', 'send-key-panel', '--panel', 'P', 'enter']
          and typist.command(cmux, key='ctrl+u') == ['cmux', 'send-panel', '--panel', 'P', '--', '\x15'])
    check('tmux: send-keys -l text, send-keys Enter and C-u', typist.command(tmux, text='/clear') == ['tmux', 'send-keys', '-t', '%3', '-l', '/clear']
          and typist.command(tmux, key='enter') == ['tmux', 'send-keys', '-t', '%3', 'Enter']
          and typist.command(tmux, key='ctrl+u') == ['tmux', 'send-keys', '-t', '%3', 'C-u'])
    check('ctrl+u presses cover every line of the request (wrapped lines too)', typist.clear_presses('one line') == 3
          and typist.clear_presses('a\nb\nc') == 5 and typist.clear_presses('x' * 130) == 5)
    check('Codex types /new', [v for k, v in typist.handoff_steps('codex') if k == 'text'][0] == '/new')
    check('detect: nothing outside cmux or tmux (control)', typist.detect({}) is None)
    ran = []
    try:
        typist.run(tmux, typist.handoff_steps('claude', 2), runner=lambda argv: ran.append(argv) or (1 if argv[-1] == '/clear' else 0),
                   sleep=lambda s: None, adopted=lambda: True)
        check('a failed key stops the typing', False)
    except RuntimeError:
        check('a failed key stops the typing (the carry line is never typed after a failed /clear)', not any(typist.CARRY_LINE in a for a in ran))
    ran, ticks = [], iter(range(0, 1000, 5))
    try:
        typist.run(tmux, typist.handoff_steps('claude', 2), runner=lambda argv: ran.append(argv) or 0, sleep=lambda s: None,
                   adopted=lambda: False, clock=lambda: next(ticks))
        check('no takeover → the carry line is not typed', False)
    except RuntimeError as error:
        check('no takeover within the wait → the carry line is not typed into the old session (the observed second defect)',
              not any(typist.CARRY_LINE in a for a in ran) and 'did not take the task over' in str(error), ran)
    ran = []
    typist.run(tmux, typist.handoff_steps('claude', 2), runner=lambda argv: ran.append(argv) or 0, sleep=lambda s: None, adopted=lambda: True)
    check('takeover seen → the carry line is typed (control)', any(typist.CARRY_LINE in a for a in ran))

    print('④-b a failed automatic handoff falls back to telling, and a manual /clear still carries the request')
    root_k = make_repo(base, 'repo-failed')
    attach.set_switch(store, 'on', root_k)
    tid_k, pay_k, _, _ = tell(store, root_k, base, 'k1')
    with store.lock('task:' + tid_k):
        task = store.get(tid_k)
        task['data']['handoff_state'].update(state='auto', auto_at=now())
        task['data']['carried_prompt'] = {'text': 'Add CSV export', 'at': now(), 'from_session': 'k1'}
        store.save(task, task['status'], 'test_setup', {})
    continuity.auto_failed(store, tid_k, 'the fresh session did not take the task over')
    data = store.get(tid_k)['data']
    lines = continuity.handoff_lines(data, 'cosmos-orbit', tid_k)
    check('state auto_failed and the brief asks for a manual /clear', data['handoff_state']['state'] == 'auto_failed'
          and any('did not take effect' in l for l in lines) and 'handoff_auto_failed' in kinds(store, tid_k), lines)
    start = attach.handle(store, 'session-start', {'cwd': str(root_k), 'session_id': 'k2', 'source': 'clear'}, 'claude')
    brief = start['hookSpecificOutput']['additionalContext'] if start else ''
    check('…the manual /clear session takes it over and gets the carried request', (store.session_task('k2') or {}).get('task_id') == tid_k
          and 'Add CSV export' in brief, brief[-300:])

    print('⑤ the CLI switch')
    cli = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(store.home)]
    shown = json.loads(subprocess.run(cli + ['context', 'auto', str(root)], capture_output=True, text=True).stdout)
    check('context auto sets the mode and the view shows it', shown['changed'] == {'handoff_mode': 'auto'} and shown['handoff_mode'] == 'auto', shown)
    shown = json.loads(subprocess.run(cli + ['context', 'tell', str(root)], capture_output=True, text=True).stdout)
    check('context tell sets it back', shown['handoff_mode'] == 'tell' and shown['here']['handoff_at'] == 200_000, shown)

    print('⑥ real typing into a throwaway tmux session')
    if not shutil.which('tmux'):
        print('  skip  tmux is not installed')
    else:
        for label, steps, expected in (
                ('control — the first real run: /clear typed after the restored request joins it',
                 [('text', '/clear'), ('key', 'enter')], ['restored blocked request/clear']),
                ('ctrl+u first: the pane receives /clear and the carry line, each on its own',
                 typist.handoff_steps('claude', 2, settle=0.2), ['/clear', typist.CARRY_LINE])):
            name = 'orbit-verify-%d-%d' % (os.getpid(), len(expected[0]))
            sink = base / ('typed-%d.txt' % len(expected[0]))
            subprocess.run(['tmux', 'new-session', '-d', '-s', name, 'cat > %s' % sink], check=True)
            try:
                pane = subprocess.run(['tmux', 'list-panes', '-t', name, '-F', '#{pane_id}'], capture_output=True, text=True).stdout.strip()
                time.sleep(0.3)
                subprocess.run(['tmux', 'send-keys', '-t', pane, '-l', 'restored blocked request'], check=True)  # like the host
                typist.run({'kind': 'tmux', 'cli': shutil.which('tmux'), 'target': pane}, steps, adopted=lambda: True)
                time.sleep(0.5)
                lines = sink.read_text().splitlines() if sink.exists() else []
                check(label, lines == expected, lines)
            finally:
                subprocess.run(['tmux', 'kill-session', '-t', name], capture_output=True)

    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
