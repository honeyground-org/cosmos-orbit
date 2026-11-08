#!/usr/bin/env python3
"""Model-free check of waking a session for a message from another session (§harness.wake).

The screens are the ones measured on 2026-11-04 (Claude Code 2.1 and Codex in cmux, read as plain text); each rule
has a control. Nothing is typed into a real terminal except in section ⑥, which types into a throwaway tmux session
running a stand-in that draws an input box the way Claude Code does.
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
for name in ('ORBIT', 'COSMOS_ORBIT', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID', 'CMUX_SURFACE_ID', 'CMUX_BUNDLED_CLI_PATH'):
    os.environ.pop(name, None)  # never type into the terminal running this check

for _marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):  # these runs stand for sessions a person opened,
    os.environ.pop(_marker, None)                                    # not ones an agent delegated (attach.delegated)
from harness import attach, continuity, requirements, wake  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []
RULE = '─' * 60


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def claude(input_lines=('❯',), footer='  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent'):
    return '\n'.join(['  and then I will publish the page and send you the link.',
                      '                         ✔ Update installed · Restart to update',
                      '─' * 40 + ' collection-unification ─'] + list(input_lines) + [RULE, footer])


SCREENS = {
    'claude idle (measured)': (claude(), 'empty'),
    'claude idle, a no-break space in the input': (claude(('❯\xa0',)), 'empty'),
    'claude busy (measured footer)': (claude(footer='  ⏵⏵ bypass permissions on · 2 shells · esc to interrupt · ← 1 agent'), 'busy'),
    'claude, the user typing': (claude(('❯ hello, can you also',)), 'typing'),
    'claude fresh session placeholder (measured)': (claude(('❯\xa0Try "fix lint errors"',)), 'empty'),
    'claude placeholder naming a file': (claude(('❯\xa0Try "write a test for app.py"',)), 'empty'),
    'claude, typed text after the same no-break space (measured)': (claude(('❯\xa0half typed',)), 'typing'),
    'claude, the user typing the word Try': (claude(('❯\xa0Try this instead',)), 'typing'),
    'claude, a second line typed':(claude(('❯', '  the second line')), 'typing'),
    'codex idle (measured placeholder)': ('\n\n› Ask Codex to do anything\n\n  GPT-6-Astra medium · ~/development/projects\n  ? for shortcuts', 'empty'),
    'codex idle, follow-up placeholder': ('› Ask a follow-up question\n\n  model · path', 'empty'),
    'codex, the user typing': ('› fix the failing test first\n\n  model · path', 'typing'),
    'codex busy': ('• Working (12s • esc to interrupt)\n\n› Ask Codex to do anything\n\n  model · path', 'busy'),
    'a shell prompt': ('sean@host ~/projects % ', 'unknown'),
    'a shell prompt using the same sign, no input box': ('~/projects main\n❯ ', 'unknown'),
    'claude box without its closing rule': ('─' * 40 + '\n❯ \n  footer', 'unknown'),
    'a permission dialog': ('Do you want to proceed?\n❯ 1. Yes\n  2. No', 'unknown'),
}


class Fake:
    """A terminal whose screens come from a list; records every command."""

    def __init__(self, screens, after=None):
        self.screens, self.after, self.ran = list(screens), after, []

    def __call__(self, argv):
        self.ran.append(argv)
        if 'read-screen' in argv or 'capture-pane' in argv:
            if self.after is not None and any(wake.WAKE_LINE in a for a in self.ran[:-1]):
                return 0, self.after
            return 0, self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]
        return 0, ''

    def typed(self):
        return [a for a in self.ran if 'send-panel' in a or 'send-key-panel' in a or 'send-keys' in a]

    def notified(self):
        return [a for a in self.ran if 'notify' in a or 'display-message' in a]

    def cleared(self):
        return [a for a in self.ran if '\x15' in a or 'C-u' in a]


CMUX = {'kind': 'cmux', 'cli': '/x/cmux', 'target': 'PANEL-1'}


def run(fake, **kw):
    clock = [0.0]
    return wake.deliver(CMUX, 'codex', runner=fake, sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                        clock=lambda: clock[0], **kw)


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'app.py').write_text('print("hello")\n')
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '-m', 'f'], check=True)
    return root


def in_pane(panel, surface, fn):
    """Run `fn` as a hook running in a cmux pane (a stand-in cli that is never called here)."""
    saved = {k: os.environ.get(k) for k in ('CMUX_PANEL_ID', 'CMUX_SURFACE_ID', 'CMUX_BUNDLED_CLI_PATH')}
    os.environ.update(CMUX_PANEL_ID=panel, CMUX_SURFACE_ID=surface, CMUX_BUNDLED_CLI_PATH=shutil.which('true') or '/usr/bin/true')
    try:
        return fn()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-wake-'))
    store = Store(base / 'home')

    print('① reading the screen: busy, empty, the user typing, or not an input box')
    for name, (screen, expected) in SCREENS.items():
        got = wake.read_state(screen)
        check('%s → %s' % (name, expected), got == expected, got)

    print('② the waker types one line only into an empty input')
    fake = Fake([claude(footer='esc to interrupt'), claude(footer='esc to interrupt'), claude()],
                after=claude(footer='esc to interrupt'))
    outcome, typed = run(fake)
    check('busy, busy, then empty: exactly the wake line and Enter, once', outcome == 'woken'
          and fake.typed() == [['/x/cmux', 'send-panel', '--panel', 'PANEL-1', '--', wake.WAKE_LINE],
                               ['/x/cmux', 'send-key-panel', '--panel', 'PANEL-1', 'enter']], fake.ran)
    check('…and nothing is typed while it is busy (control: two busy screens came first)',
          fake.ran.index(fake.typed()[0]) == 3, fake.ran)
    fake = Fake([claude(footer='esc to interrupt')])
    check('busy with --when-busy skip: nothing typed (the channel delivers during the turn)',
          run(fake, when_busy='skip')[0] == 'busy_delivers_itself' and not fake.typed())
    fake = Fake([claude(('❯ hello, can you also',)), claude(('❯ hello, can you also',)), claude()], after=claude(footer='esc to interrupt'))
    outcome, _ = run(fake)
    check('the user typing: one notification, then typed once the input is empty', outcome == 'woken'
          and len(fake.notified()) == 1 and len(fake.typed()) == 2, fake.ran)
    check('★the user\'s text is never erased★ (no ctrl+u, ever)', not fake.cleared(), fake.ran)
    fake = Fake([claude(('❯ hello, can you also',))])
    outcome, _ = run(fake, wait=10)
    check('the user keeps typing: it gives up without typing, notified once', outcome == 'expired_typing'
          and not fake.typed() and len(fake.notified()) == 1 and not fake.cleared(), fake.ran)
    fake = Fake([SCREENS['a shell prompt using the same sign, no input box'][0]])
    check('not an input box (a shell): nothing typed', run(fake, wait=10)[0] == 'expired_unknown' and not fake.typed())
    fake = Fake([claude()])
    check('the hourly limit reached: nothing typed, notified', run(fake, allowed=lambda: False)[0] == 'throttled'
          and not fake.typed() and len(fake.notified()) == 1)
    fake = Fake([claude()], after=claude(('❯ ' + wake.WAKE_LINE,)))
    check('typed but still in the input afterwards: reported, not called woken', run(fake)[0] == 'not_submitted')
    class Pasted(Fake):
        # Codex 0.166 (measured 2026-11-05): an Enter right after the text leaves it in the input; the next submits.
        def __call__(self, argv):
            self.ran.append(argv)
            if 'read-screen' in argv or 'capture-pane' in argv:
                enters = sum(1 for a in self.ran if a[-1] == 'enter')
                if any(wake.WAKE_LINE in a for a in self.ran):
                    return 0, ('› ' + wake.WAKE_LINE + '\n\n  model · path') if enters < 2 else '• Working (1s • esc to interrupt)'
                return 0, '› Ask Codex to do anything\n\n  model · path'
            return 0, ''
    fake = Pasted([])
    outcome, typed = run(fake)
    check('Codex keeps the line after the first Enter (a paste): one more Enter submits it, nothing else is typed',
          outcome == 'woken' and [a[-1] for a in fake.typed()] == [wake.WAKE_LINE, 'enter', 'enter']
          and not fake.cleared(), fake.typed())
    fake = Fake(['› Ask Codex to do anything\n\n  model · path'], after='• Working (1s • esc to interrupt)')
    check('codex with its placeholder: woken', run(fake)[0] == 'woken' and len(fake.typed()) == 2)

    print('③ every hook records where its session can be woken')
    root = make_repo(base, 'repo')
    attach.set_switch(store, 'on', root)
    pay = {'cwd': str(root), 'session_id': 's1'}
    in_pane('PANEL-A', 'SURF-A', lambda: attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude'))
    row = store.db.execute('SELECT * FROM terminals WHERE session=?', ('s1',)).fetchone()
    check('session start: the pane, the surface and idle are recorded', row and row['kind'] == 'cmux' and row['target'] == 'PANEL-A'
          and row['surface'] == 'SURF-A' and row['state'] == 'idle', dict(row) if row else None)
    in_pane('PANEL-A', 'SURF-A', lambda: attach.handle(store, 'prompt', dict(pay, prompt='Refactor app.py into a module and keep the output'), 'claude'))
    check('prompt: busy', store.db.execute('SELECT state FROM terminals WHERE session=?', ('s1',)).fetchone()['state'] == 'busy')
    in_pane('PANEL-A', 'SURF-A', lambda: attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Done.'), 'claude'))
    check('stop: idle', store.db.execute('SELECT state FROM terminals WHERE session=?', ('s1',)).fetchone()['state'] == 'idle')
    terminal, reason = wake.resolve(store, session='s1')
    check('the session resolves to its pane', terminal == {'kind': 'cmux', 'cli': shutil.which('true') or '/usr/bin/true', 'target': 'PANEL-A'}, (terminal, reason))
    check('…and so does its surface', wake.resolve(store, surface='SURF-A')[0] == terminal)
    check('…written in lower case, as a mailbox may keep it', wake.resolve(store, surface='surf-a')[0] == terminal)
    attach.handle(store, 'session-start', {'cwd': str(root), 'session_id': 's-plain', 'source': 'startup'}, 'claude')
    check('outside cmux and tmux: recorded without a terminal → no_terminal', wake.resolve(store, session='s-plain') == (None, 'no_terminal'))
    check('an unknown session → unknown_session', wake.resolve(store, session='nobody') == (None, 'unknown_session'))
    time.sleep(0.01)
    in_pane('PANEL-A', 'SURF-A', lambda: attach.handle(store, 'session-start', {'cwd': str(root), 'session_id': 's2', 'source': 'clear'}, 'claude'))
    check('a newer session in the same pane: the old one is not woken there (replaced)', wake.resolve(store, session='s1') == (None, 'replaced'))
    check('…the newer one is (control)', wake.resolve(store, session='s2')[0] is not None)
    attach.handle(store, 'session-end', {'cwd': str(root), 'session_id': 's2'}, 'claude')
    check('session end forgets it', wake.resolve(store, session='s2') == (None, 'unknown_session'))
    off = make_repo(base, 'repo-off')
    attach.set_switch(store, 'off', off)
    in_pane('PANEL-B', 'SURF-B', lambda: attach.handle(store, 'session-start', {'cwd': str(off), 'session_id': 's-off', 'source': 'startup'}, 'claude'))
    check('Orbit switched off in that project: nothing recorded (control)', wake.resolve(store, session='s-off') == (None, 'unknown_session'))

    print('④ the wake line is Orbit\'s, not the user\'s')
    check('it is classified trivial (never a goal, a correction or a requirement)', requirements.classify(wake.WAKE_LINE) == 'trivial')
    check('it is plain ASCII on one line (typed literally)', wake.WAKE_LINE.isascii() and '\n' not in wake.WAKE_LINE)
    tid = attach.current_task(store, root)['id']
    before = len(store.get(tid)['data'].get('prompts') or [])
    out = in_pane('PANEL-A', 'SURF-A', lambda: attach.handle(store, 'prompt', dict(pay, session_id='s1', prompt=wake.WAKE_LINE), 'claude'))
    text = (out or {}).get('hookSpecificOutput', {}).get('additionalContext', '')
    check('the prompt hook says it was typed by Orbit and the message is another agent\'s words',
          'typed by Orbit, not by the user' in text and "not the user's instruction" in text, text[:200])
    check('…and it is not recorded as a prompt of the user', len(store.get(tid)['data'].get('prompts') or []) == before)
    in_pane('PANEL-A', 'SURF-A', lambda: attach.handle(store, 'prompt', dict(pay, session_id='s1', prompt='also keep the CLI flags'), 'claude'))
    check('a real prompt is recorded (control)', len(store.get(tid)['data'].get('prompts') or []) == before + 1)
    # A checkpointed handoff in auto mode hands off on the next request — but never on the wake line: the blocked
    # prompt would drop the message its channel delivers with it.
    continuity.set_handoff_mode(store, 'auto')
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['handoff_state'] = {'state': 'checkpointed', 'session': 's1', 'limit': 1000}
        store.save(task, task['status'], 'test_setup', {})
    spawned = []
    handed = continuity.auto_handoff(store, store.get(tid), 's1', 'go on', 'trivial', 'claude', terminal=CMUX,
                                     spawn=lambda *a, **k: spawned.append(a))
    check('control: a go-ahead in that state hands off', bool(handed) and spawned)
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['handoff_state'] = {'state': 'checkpointed', 'session': 's1', 'limit': 1000}
        store.save(task, task['status'], 'test_setup', {})
    out = in_pane('PANEL-A', 'SURF-A', lambda: attach.handle(store, 'prompt', dict(pay, session_id='s1', prompt=wake.WAKE_LINE), 'claude'))
    check('the wake line in that state does not hand off (the prompt goes through)', out and out.get('decision') != 'block'
          and store.get(tid)['data']['handoff_state']['state'] == 'checkpointed', out)

    print('⑤ the command: one waker per terminal, only in auto mode')
    in_pane('PANEL-C', 'SURF-C', lambda: attach.handle(store, 'session-start', {'cwd': str(root), 'session_id': 'c1', 'source': 'startup'}, 'codex'))
    starts = []
    out = wake.wake(store, ['c1', 'c1', 'nobody'], ['SURF-C'], 'claude', spawn=lambda *a: starts.append(a) or 4242)
    check('auto mode: one waker for the pane; the same pane twice and an unknown session start nothing more',
          len(starts) == 1 and [r['outcome'] for r in out['wake']] == ['waker_started', 'same_terminal', 'unknown_session', 'same_terminal'], out)
    notes = []
    real_run = wake._run
    wake._run = lambda argv: notes.append(argv) or (0, '')
    try:
        out = wake.wake(store, ['c1'], (), 'claude', spawn=lambda *a: starts.append(a) or 1, mode='tell')
    finally:
        wake._run = real_run
    check('tell mode: notified only, no waker (the same switch as the automatic handoff)',
          out['wake'][0]['outcome'] == 'notified' and len(starts) == 1 and notes and 'notify' in notes[0], out)
    home = base / 'bounds'
    check('the first waker for a terminal claims it', wake.claim_waker(home, 'P-1', pid=os.getpid()))
    holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])
    (home / 'wake' / 'P-2.pid').write_text(str(holder.pid))
    check('a live waker on that terminal → no second one (its run delivers every waiting message)', not wake.claim_waker(home, 'P-2'))
    holder.kill()
    holder.wait()
    check('…a dead waker\'s claim is taken over (control)', wake.claim_waker(home, 'P-2'))
    with_errors = []
    try:
        wake.wake(store, ['c1'], (), 'x', when_busy='later')
    except ValueError as error:
        with_errors.append(str(error))
    check('--when-busy accepts only wait or skip', with_errors)

    print('⑥ bounds: a gap between wakes and an hourly limit (two agents acknowledging each other)')
    t = 1_000_000.0
    check('no history: allowed at once', wake.allowed(home, 'P-9', at=t) == (True, 0.0))
    wake.record_wake(home, 'P-9', at=t)
    ok, pause = wake.allowed(home, 'P-9', at=t + 5)
    check('5 s after a wake: allowed after the rest of the gap', ok and abs(pause - (wake.MIN_GAP - 5)) < 0.01, (ok, pause))
    for i in range(1, wake.MAX_PER_HOUR):
        wake.record_wake(home, 'P-9', at=t + i * 60)
    check('%d wakes within the hour: no more' % wake.MAX_PER_HOUR, wake.allowed(home, 'P-9', at=t + 1200)[0] is False)
    check('…an hour after the first ones, allowed again (control)', wake.allowed(home, 'P-9', at=t + 3600 + wake.MAX_PER_HOUR * 60)[0])
    check('another terminal is not limited by it (control)', wake.allowed(home, 'P-8', at=t + 1200)[0])

    print('⑦ real typing into a throwaway tmux session (a stand-in draws the input box)')
    if not shutil.which('tmux'):
        print('  skip  tmux is not installed')
    else:
        agent = base / 'agent.py'
        agent.write_text(
            'import sys, time\n'
            'R = "\\u2500" * 50\n'
            'sys.stdout.write("an earlier answer\\n" + R + "\\n\\u276f \\n" + R + "\\n  footer\\x1b[2A\\r\\x1b[2C")\n'
            'sys.stdout.flush()\n'
            'line = sys.stdin.readline()\n'
            'open(sys.argv[1], "w").write(line)\n'
            'sys.stdout.write("\\n\\n\\n\\n  working \\u00b7 esc to interrupt\\n")\n'
            'sys.stdout.flush()\n'
            'time.sleep(30)\n')
        wake.POLL = 0.3
        for label, pretyped in (('idle and empty', ''), ('the user typing (control)', 'half a sentence')):
            name = 'orbit-wake-%d' % os.getpid()
            sink = base / ('sink-%d' % len(pretyped))
            tmux = shutil.which('tmux')
            subprocess.run([tmux, 'new-session', '-d', '-s', name, '-x', '120', '-y', '20', '%s %s %s' % (sys.executable, agent, sink)], check=True)
            try:
                pane = subprocess.run([tmux, 'list-panes', '-t', name, '-F', '#{pane_id}'], capture_output=True, text=True).stdout.strip()
                time.sleep(0.8)
                if pretyped:
                    subprocess.run([tmux, 'send-keys', '-t', pane, '-l', pretyped], check=True)
                    time.sleep(0.3)
                terminal = {'kind': 'tmux', 'cli': tmux, 'target': pane}
                outcome, _ = wake.deliver(terminal, 'codex', wait=2.0)
                time.sleep(0.5)
                got = sink.read_text().strip() if sink.exists() else ''
                screen = subprocess.run([tmux, 'capture-pane', '-p', '-t', pane], capture_output=True, text=True).stdout
                if pretyped:
                    check(label + ': nothing typed, and the user\'s text is still there', outcome == 'expired_typing' and not got
                          and pretyped in screen, (outcome, got, screen[-200:]))
                else:
                    check(label + ': the stand-in received exactly the wake line', outcome == 'woken' and got == wake.WAKE_LINE, (outcome, got))
            finally:
                subprocess.run([tmux, 'kill-session', '-t', name], capture_output=True)

    shutil.rmtree(base, ignore_errors=True)
    print('\n%s' % ('PASS' if not FAILED else 'FAIL: %d — %s' % (len(FAILED), '; '.join(FAILED))))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
