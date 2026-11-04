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
