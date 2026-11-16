#!/usr/bin/env python3
"""Model-free check of Orbit's round (patrol.py) and of telling the user a task waits for them (attention.py).

The user, 2026-11-16: "올빗의 경우 사용자한테 답을 기다리는 것에는 알림을 더 추가" and "사용자 입력 기다리는 것을 제외하고 그냥
멈춰서 업무를 다한것처럼 멈춰 있는 상황 그리고 최종 목표를 다 완료하지 않았는데 가만히 있는 상황에 자동으로 개입".
Measured before (two weeks, 68 attached tasks): 12 of 17 questions were never shown; the supervisor left after 15
unchanged minutes 30 times and about ten unfinished tasks sat at their input line until the 12-hour close.

Every rule has a control: nothing is typed into a busy screen, the user's typing, a recent stop, a supervised session,
a question, or a task without completion items; a notice is never sent twice for one due moment.
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
os.environ['ORBIT_DESKTOP_NOTIFY'] = 'off'      # a desktop notification is always a fake one below
for name in ('ORBIT', 'COSMOS_ORBIT', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID', 'CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID',
             'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
    os.environ.pop(name, None)                  # never a real terminal: every terminal below is a fake one

from harness import attach, attention, lifecycle, manager, patrol, supervision, wake  # noqa: E402
from harness.store import Store  # noqa: E402
from verify_living_checklist import check, FAILED, make_repo  # noqa: E402

TMUX = {'kind': 'tmux', 'cli': 'tmux', 'target': '%7'}
EMPTY = '› Ask a follow-up question\n\n'
BUSY = '• Working (12s • esc to interrupt)\n\n› Ask a follow-up question\n'
TYPING = '› let me write the next step myself\n'
LATER = 10 * 60                                  # seconds after the stop: past patrol.IDLE_BEFORE


class Pane:
    """A fake terminal: what its screen shows, and every argv sent to it."""

    def __init__(self, screen=EMPTY, notify_ok=True):
        self.screen, self.sent, self.notify_ok = screen, [], notify_ok

    def __call__(self, argv):
        self.sent.append(argv)
        if argv[1] in ('capture-pane', 'read-screen'):
            return 0, self.screen
        if argv[1] in ('display-message', 'notify'):
            return (0, '') if self.notify_ok else (1, 'gone')
        return 0, ''

    def typed(self):
        return [a for a in self.sent if a[1] in ('send-keys', 'send-panel', 'send-key-panel')]

    def notices(self):
        return [a for a in self.sent if a[1] in ('display-message', 'notify')]


def desk(log):
    return lambda body: (log.append(body), ['desk', body])[1]


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-patrol-'))
    store = Store(base / 'home')
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        run(base, store)
    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


def open_task(store, root, session, goal, done=('D1 the export handles every list, including an empty one',),
              terminal=TMUX, event='stop'):
    task = attach.create(store, root, goal, 'claude', session, done=list(done) if done else None)
    wake.note(store, session, 'claude', event, terminal)
    return task['id']


def mine(results, tid):
    return [outcome for task_id, _, outcome in results if task_id == tid]


def run(base, store):
    root = make_repo(base, 'patrol')
    attach.set_switch(store, 'on', root)
    supervision.configure(store, 'auto')
    t0 = time.time()

    print('① a question the session asked itself (claim --question) is told: at once, reminders, before the close')
    tid = open_task(store, root, 'S-Q', 'Move the export to the new storage bucket')
    attach.claim(store, tid, summary='need the bucket', question='Which bucket name should the export use?')
    check('the task waits for the user (needs_input, the manager says waiting_for_user)',
          store.get(tid)['status'] == 'needs_input' and manager.diagnose(store.get(tid))['reason'] == 'waiting_for_user',
          store.get(tid)['status'])
    updated = store.get(tid)['updated']
    pane, shown = Pane(), []
    out = mine(patrol.sweep(store, pane, at=t0 + 5, desktop=desk(shown)), tid)
    check('the first round tells it: the terminal and the desktop (tmux posts no banner), with the question',
          out == ['told_asked'] and len(pane.notices()) == 1 and len(shown) == 1 and 'Which bucket' in shown[0]
          and tid in shown[0], (out, pane.sent, shown))
    check('…nothing is typed for a question (control)', not pane.typed(), pane.typed())
    out = mine(patrol.sweep(store, pane, at=t0 + 65, desktop=desk(shown)), tid)
    check('…once: the next round says nothing (control)', out == ['waiting'] and len(shown) == 1, (out, shown))
    out = mine(patrol.sweep(store, pane, at=t0 + 30 * 60 + 5, desktop=desk(shown)), tid)
    check('30 minutes unanswered: a reminder', out == ['told_reminder'] and len(shown) == 2 and 'still waiting' in shown[1]
          and '30 min' in shown[1], (out, shown))
    out = mine(patrol.sweep(store, pane, at=t0 + 31 * 60, desktop=desk(shown)), tid)
    check('…not again a minute later (control)', out == ['waiting'] and len(shown) == 2, (out, shown))
    out = mine(patrol.sweep(store, pane, at=t0 + 2 * 3600 + 5, desktop=desk(shown)), tid)
    check('2 hours unanswered: the second reminder', out == ['told_reminder'] and '2 h' in shown[-1], (out, shown))
    out = mine(patrol.sweep(store, pane, at=t0 + 5 * 3600, desktop=desk(shown)), tid)
    check('…then quiet until the close nears (control)', out == ['waiting'] and len(shown) == 3, (out, shown))
    out = mine(patrol.sweep(store, pane, at=t0 + 11 * 3600 + 60, desktop=desk(shown)), tid)
    check('an hour before the 12-hour close: one last notice', out == ['told_closing'] and 'closes in about 1 h' in shown[-1],
          (out, shown[-1:]))
    out = mine(patrol.sweep(store, pane, at=t0 + 11 * 3600 + 600, desktop=desk(shown)), tid)
    check('…and nothing after it (control)', out == ['waiting'] and len(shown) == 4, (out, shown))
    later = store.get(tid)
    check('telling is not activity: the task record is untouched, so it still closes after 12 hours',
          later['updated'] == updated and lifecycle.due(store, later, attention.datetime.datetime.fromtimestamp(
              t0 + 12 * 3600 + 120, attention.datetime.timezone.utc)) == 'idle', (later['updated'], updated))

    print('② one notice per question, whoever tells first; cmux posts its own banner')
    tid = open_task(store, root, 'S-R', 'Rename the export columns to the new schema')
    attach.claim(store, tid, summary='need the schema', question='Where is the new schema file?')
    pane, shown = Pane(), []
    with patch.object(wake, '_run', pane):
        told = supervision.tell(store, store.get(tid), 'S-R', pane)
    out = mine(patrol.sweep(store, pane, at=t0 + 5, desktop=desk(shown)), tid)
    check('a question supervision.tell already showed is not shown again by the round',
          told and out == ['told_already_told'] and len(pane.notices()) == 1 and not shown, (told, out, pane.sent, shown))
    out = mine(patrol.sweep(store, pane, at=t0 + 30 * 60 + 5, desktop=desk(shown)), tid)
    check('…its reminder still comes', out == ['told_reminder'], out)
    tid = open_task(store, root, 'S-R2', 'Rename the export files to the new pattern')
    attach.claim(store, tid, summary='need the pattern', question='Which file name pattern?')
    pane = Pane()
    out = mine(patrol.sweep(store, pane, at=t0 + 5, desktop=False), tid)
    check('the other order: the round told it, then supervision.tell does not (control)',
          out == ['told_asked'] and supervision.tell(store, store.get(tid), 'S-R2', pane) is False
          and len(pane.notices()) == 1, (out, pane.sent))
    cmux = {'kind': 'cmux', 'cli': 'cmux', 'target': 'SURF-1'}
    tid = open_task(store, root, 'S-C', 'Rename the export folder to the new layout', terminal=cmux)
    attach.claim(store, tid, summary='need the layout', question='Which folder layout?')
    pane, shown = Pane(), []
    out = mine(patrol.sweep(store, pane, at=t0 + 5, desktop=desk(shown)), tid)
    check('in cmux the notification is its native banner: no second desktop notification',
          out == ['told_asked'] and len(pane.notices()) == 1 and not shown, (out, pane.sent, shown))
    tid = open_task(store, root, 'S-G', 'Rename the export bucket to the new region', terminal=cmux)
    attach.claim(store, tid, summary='need the region', question='Which region?')
    pane, shown = Pane(notify_ok=False), []
    out = mine(patrol.sweep(store, pane, at=t0 + 5, desktop=desk(shown)), tid)
    check('…but when that pane is gone, the desktop gets it', out == ['told_asked'] and len(shown) == 1, (out, shown))

    print('③ a session that stopped with work left, idle, with no supervisor: the round sends it back')
    tid = open_task(store, root, 'S-W', 'Make the Parquet export robust for empty input')
    check('the manager reads it as unfinished work (interrupted)', manager.diagnose(store.get(tid))['reason'] == 'interrupted')
    pane = Pane(EMPTY)
    out = mine(patrol.sweep(store, pane, at=time.time() + 60, desktop=False), tid)
    check('a stop a minute ago is left to the supervisor, the Stop guard and the user (control)',
          out == ['recent'] and not pane.typed(), (out, pane.sent))
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER, desktop=False), tid)
    typed = [' '.join(a) for a in pane.typed()]
    check('idle past the wait with the input empty: the continuation is typed, then Enter',
          out == ['continued'] and len(typed) == 2 and supervision.RESUME_LINE in typed[0] and typed[1].endswith('Enter'),
          (out, typed))
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER + 60, desktop=False), tid)
    check('…the session did not move: once more', out == ['continued'] and len(pane.typed()) == 4, (out, len(pane.typed())))
    shown = []
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER + 120, desktop=desk(shown)), tid)
    check('…still not moving after %d continuations: the user is told instead, nothing more is typed' % patrol.MAX_NUDGES,
          out == ['gave_up'] and len(pane.typed()) == 4 and len(pane.notices()) == 1 and 'without moving' in pane.notices()[0][-1]
          and len(shown) == 1, (out, pane.sent[-3:], shown))
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER + 180, desktop=desk(shown)), tid)
    check('…told once (control)', out == ['gave_up'] and len(pane.notices()) == 1 and len(shown) == 1, (out, shown))
    wake.note(store, 'S-W', 'claude', 'stop', TMUX)          # the session moved: a turn ran and stopped again
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER, desktop=False), tid)
    check('a new idle stretch (the session ran a turn since): continued again', out == ['continued'], out)

    print('④ the incident shape: the supervisor typed for this stop, then left; nothing started it again')
    tid = open_task(store, root, 'S-I', 'Make the Avro export robust for empty input')
    supervision.record_stop(store, store.get(tid), 'S-I')
    with store.lock('task:' + tid):
        task = store.get(tid)
        mark = task['data']['supervision']['S-I']
        mark['sent_for'] = mark['stop_at']                  # the continuation was typed for that stop
        store.save(task, task['status'], 'test_marked')
    wake.note(store, 'S-I', 'claude', 'stop', TMUX)
    pane = Pane(EMPTY)
    with patch.object(wake, '_run', pane):
        outcome = supervision.tick(store, tid, 'S-I', pane)
    check('the supervisor reads it as awaiting_stop and types nothing (it would leave after 15 minutes)',
          outcome == 'awaiting_stop' and not pane.typed(), outcome)
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER, desktop=False), tid)
    check('…the round sends it back', out == ['continued'] and len(pane.typed()) == 2, (out, pane.sent))

    print('⑤ controls: never typed into')
    for session, screen, want in (('S-B', BUSY, 'busy'), ('S-T', TYPING, 'typing')):
        tid = open_task(store, root, session, 'Make the %s export robust for empty input' % session)
        pane = Pane(screen)
        out = mine(patrol.sweep(store, pane, at=time.time() + LATER, desktop=False), tid)
        check('a screen that is %s' % want, out == [want] and not pane.typed(), (out, pane.sent))
    tid = open_task(store, root, 'S-S', 'Make the ORC export robust for empty input')
    pane = Pane(EMPTY)
    with store.lock('supervisor:%s:S-S' % tid):
        out = mine(patrol.sweep(store, pane, at=time.time() + LATER, desktop=False), tid)
    check('a session its supervisor holds', out == ['supervised'] and not pane.typed(), (out, pane.sent))
    tid = open_task(store, root, 'S-U', 'Investigate why the export is slow', done=None)
    pane = Pane(EMPTY)
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER, desktop=False), tid)
    check('a task without completion items (a question answered, exploration): not looked at',
          manager.diagnose(store.get(tid))['reason'] == 'undefined' and out == [] and not pane.typed(), (out, pane.sent))
    tid = open_task(store, root, 'S-P', 'Make the Feather export robust for empty input')
    store.set_paused(tid, True)
    pane = Pane(EMPTY)
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER, desktop=False), tid)
    check('a task the user paused', out == [] and not pane.typed(), (out, pane.sent))
    tid = open_task(store, root, 'S-N', 'Make the Thrift export robust for empty input', terminal={})
    shown = []
    out = mine(patrol.sweep(store, Pane(EMPTY), at=time.time() + LATER, desktop=desk(shown)), tid)
    out2 = mine(patrol.sweep(store, Pane(EMPTY), at=time.time() + LATER + 60, desktop=desk(shown)), tid)
    check('a terminal Orbit cannot type into: the user is told once, on the desktop',
          out == ['told'] and out2 == ['no_terminal'] and len(shown) == 1 and 'work left' in shown[0], (out, out2, shown))
    supervision.configure(store, 'tell')
    tid = open_task(store, root, 'S-M', 'Make the Excel export robust for empty input')
    pane = Pane(EMPTY)
    out = mine(patrol.sweep(store, pane, at=time.time() + LATER, desktop=False), tid)
    check('automatic continuation off (tell): told once, nothing typed',
          out == ['notify_only'] and not pane.typed() and len(pane.notices()) == 1
          and 'continuation is off' in pane.notices()[0][-1], (out, pane.sent))
    supervision.configure(store, 'auto')

    print('⑥ the round runs in the dashboard process, one process at a time, and can be switched off')
    rounds = []
    with patch.object(patrol, 'sweep', side_effect=lambda s: rounds.append(1) or []):
        with Store(base / 'home').lock('patrol'):
            patrol.run_forever(base / 'home', sleep=lambda _s: None, rounds=1)
        held = len(rounds)
        patrol.run_forever(base / 'home', sleep=lambda _s: None, rounds=1)
    check('another process on its round: this one skips (control), then runs when free', held == 0 and len(rounds) == 1,
          (held, rounds))
    os.environ['ORBIT_PATROL'] = 'off'
    try:
        check('ORBIT_PATROL=off starts no background round', patrol.start_background(base / 'home') is None)
    finally:
        os.environ.pop('ORBIT_PATROL', None)
    source = (ROOT / 'harness' / 'observer.py').read_text()
    check('the dashboard starts the round when it serves', 'start_background(home)' in source.split('def serve(')[1])


if __name__ == '__main__':
    raise SystemExit(main())
