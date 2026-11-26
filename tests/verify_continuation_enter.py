#!/usr/bin/env python3
"""The continuation line reaches a Codex session: typed, then sent with an Enter it does not take for a paste.

Measured 2026-11-26 15:32 (Codex 0.166, cmux): the supervisor typed "Orbit: continue the unfinished task recorded in
your ledger." and pressed Enter at once. Codex takes text and an Enter that follows it at once as a paste and keeps the
line in its input (first measured 2026-11-05, which is why wake.deliver waits PASTE_GAP and looks again). The ledger
said continued; the Codex transcript had no new message after it; the session sat idle until the user pressed Enter.
The automatic round (patrol) typed the same way.

The fake Codex below keeps a line whose Enter comes within half a second of its text, and sends it on a later Enter.

① the supervisor's continuation is sent: the line leaves the input and the outcome is 'continued'
② a line that still sits there after two Enters is 'not_submitted', said where it sits, never erased; the supervisor
  leaves (the user's Enter starts the next turn, and its Stop a supervisor)
③ the automatic round sends it the same way
④ controls: a wake line (wake.deliver) is sent as before; a Claude Code input that sends at once is pressed once
"""
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['ORBIT_DESKTOP_NOTIFY'] = 'off'
for name in ('ORBIT', 'COSMOS_ORBIT', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID', 'CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID',
             'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
    os.environ.pop(name, None)                  # never a real terminal: every terminal below is a fake one

from harness import attach, patrol, supervision, wake  # noqa: E402
from harness.store import Store  # noqa: E402
from verify_living_checklist import check, FAILED, make_repo  # noqa: E402

LATER = 10 * 60                                 # seconds after the stop: past patrol.IDLE_BEFORE
NOT_SENT = getattr(supervision, 'NOT_SENT', 'press Enter to send it')  # absent before the fix: those checks fail, not crash


class Codex:
    """A fake Codex pane in tmux. An Enter within PASTE_WINDOW seconds of the text is part of the paste: the line
    stays in the input. A later Enter sends it. `sends=False`: a pane where no Enter sends (the line always stays)."""
    PASTE_WINDOW = 0.5

    def __init__(self, target, sends=True):
        self.terminal = {'kind': 'tmux', 'cli': 'tmux', 'target': target}
        self.sends, self.input, self.texted = sends, '', None
        self.sent_lines, self.argv, self.notices = [], [], []

    def screen(self):
        return ('• Done for now; the export handles empty input.\n\n› %s\n\n  GPT-6-Astra low · /repo\n'
                '  ? for shortcuts\n' % (self.input or 'Ask Codex to do anything'))

    def __call__(self, argv):
        self.argv.append(argv)
        if argv[1] == 'capture-pane':
            return 0, self.screen()
        if argv[1] == 'send-keys' and '-l' in argv:
            self.input, self.texted = self.input + argv[-1], time.monotonic()
        elif argv[1] == 'send-keys' and argv[-1] == 'Enter':
            if self.sends and self.input and time.monotonic() - self.texted >= self.PASTE_WINDOW:
                self.sent_lines.append(self.input)
                self.input = ''
        elif argv[1] == 'display-message':
            self.notices.append(argv[-1])
        return 0, ''

    def keys(self):
        return [a[-1] if a[-1] == 'Enter' else 'text' for a in self.argv if a[1] == 'send-keys']


def only(pane):
    """A runner for one pane: a round looks at every open task, and the other tasks' panes are not on this screen."""
    def run(argv):
        if '-t' in argv and argv[argv.index('-t') + 1] == pane.terminal['target']:
            return pane(argv)
        return 1, "can't find pane"
    return run


class Claude(Codex):
    """A Claude Code input: any Enter sends."""
    PASTE_WINDOW = 0.0

    def screen(self):
        rule = '─' * 40
        return '● Done for now.\n\n%s\n❯\xa0%s\n%s\n  ? for shortcuts\n' % (rule, self.input, rule)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-continuation-enter-'))
    store = Store(base / 'home')
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        run(base, store)
    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


def stopped(store, root, session, goal, pane, host='codex'):
    """A session of an unfinished task whose turn just stopped, idle at its input line."""
    task = attach.create(store, root, goal, host, session, done=['D1 the export handles every list, including an empty one'])
    wake.note(store, session, host, 'stop', pane.terminal)
    supervision.record_stop(store, store.get(task['id']), session)
    return task['id']


def kinds(store, tid, kind):
    return [e['payload'] for e in store.events(tid) if e['kind'] == kind]


class Looped(Exception):
    pass


def run(base, store):
    root = make_repo(base, 'continuation')
    attach.set_switch(store, 'on', root)
    supervision.configure(store, 'auto')

    print("① the supervisor's continuation reaches a Codex session")
    pane = Codex('%81')
    tid = stopped(store, root, 'S-C', 'Make the Parquet export robust for empty input', pane)
    outcome = supervision.tick(store, tid, 'S-C', pane)
    check('the line is sent: it left the input and Codex received it (was: kept in the input as a paste, reported '
          'as continued)', pane.sent_lines == [supervision.RESUME_LINE] and not pane.input, (outcome, pane.sent_lines, pane.input))
    check('…and the outcome is continued', outcome == 'continued', outcome)
    check('…the text once, then one Enter (control: no second Enter once it is sent)', pane.keys() == ['text', 'Enter'],
          pane.keys())
    again = supervision.tick(store, tid, 'S-C', pane)
    check('…once for that stop (control: the next look types nothing)', again == 'awaiting_stop' and pane.keys() == ['text', 'Enter'],
          (again, pane.keys()))

    print('② a line that stays in the input is not reported as continued')
    pane = Codex('%82', sends=False)
    tid = stopped(store, root, 'S-K', 'Make the Avro export robust for empty input', pane)
    outcome = supervision.tick(store, tid, 'S-K', pane)
    check("two Enters, then the outcome is 'not_submitted' (was: continued while the line sat there)",
          outcome == 'not_submitted' and pane.keys() == ['text', 'Enter', 'Enter'], (outcome, pane.keys()))
    check('…the line is still there, nothing erased it (control: the input is the user\'s to send or clear)',
          pane.input == supervision.RESUME_LINE and not any('C-u' in a for a in pane.argv), (pane.input, pane.argv))
    check('…the pane is told to press Enter', pane.notices == [NOT_SENT], pane.notices)
    check('…and the ledger records it (continuation_not_submitted, by the supervisor)',
          [e.get('by') for e in kinds(store, tid, 'continuation_not_submitted')] == ['supervisor'],
          kinds(store, tid, 'continuation_not_submitted'))
    looks = []

    def fake_sleep(seconds):
        raise Looped()
    with patch.object(supervision, 'tick', side_effect=lambda *a, **k: looks.append(1) or 'not_submitted'):
        try:
            supervision.main(str(store.home), tid, 'S-K', sleep=fake_sleep)
            left = True
        except Looped:
            left = False
    check('…and the supervisor leaves on it (the user\'s Enter starts the turn, whose Stop starts one again)',
          left and looks == [1], (left, looks))

    print('③ the automatic round sends it the same way')
    pane = Codex('%83')
    tid = stopped(store, root, 'S-P', 'Make the ORC export robust for empty input', pane)
    out = [o for t, _, o in patrol.sweep(store, only(pane), at=time.time() + LATER, desktop=False) if t == tid]
    check('the round\'s line is sent (was: kept in the input as a paste)', out == ['continued']
          and pane.sent_lines == [supervision.RESUME_LINE] and pane.keys() == ['text', 'Enter'], (out, pane.sent_lines, pane.keys()))
    pane = Codex('%84', sends=False)
    tid = stopped(store, root, 'S-Q', 'Make the CSV export robust for empty input', pane)
    out = [o for t, _, o in patrol.sweep(store, only(pane), at=time.time() + LATER, desktop=False) if t == tid]
    check("…one that stays is 'not_submitted', told and recorded (by the round)",
          out == ['not_submitted'] and pane.notices == [NOT_SENT]
          and [e.get('by') for e in kinds(store, tid, 'continuation_not_submitted')] == ['patrol'], (out, pane.notices))

    print('④ controls')
    pane = Codex('%85')
    outcome, typed = wake.deliver(pane.terminal, runner=pane, wait=5)
    check('a wake line is sent as before (wake.deliver, the same helper)',
          outcome == 'woken' and pane.sent_lines == [wake.WAKE_LINE] and len(typed) == 2, (outcome, pane.sent_lines, typed))
    pane = Codex('%86', sends=False)
    outcome, typed = wake.deliver(pane.terminal, runner=pane, wait=5)
    check("…one that stays is still 'not_submitted' there", outcome == 'not_submitted' and len(typed) == 3, (outcome, typed))
    pane = Claude('%87')
    tid = stopped(store, root, 'S-L', 'Make the JSON export robust for empty input', pane, host='claude')
    outcome = supervision.tick(store, tid, 'S-L', pane)
    check('a Claude Code input: continued with one Enter', outcome == 'continued' and pane.keys() == ['text', 'Enter']
          and pane.sent_lines == [supervision.RESUME_LINE], (outcome, pane.keys(), pane.sent_lines))


if __name__ == '__main__':
    sys.exit(main())
