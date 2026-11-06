#!/usr/bin/env python3
"""A Codex session that stopped mid-task is carried to the end (2026-11-05). Model-free, with controls.

The incident, from the ledger: a finished review was thrown away as "changed during verification" although nobody
had changed the request (another session had edited a file in the shared folder) — 18 times, 16 of them Codex tasks;
the executor, told to claim again, did so up to 57 times without a verdict. And a Stop that came while the review
held the task lock was lost: the supervisor took the session for busy, left after 15 minutes, and the session sat
at its input line with nobody telling it (or the user) what the review had found.

① a review's verdict survives other sessions' edits; a pass does not cover the task's own session's edits
② a Stop recorded outside the lock still counts: the supervisor types the continuation; the Stop starts it
③ a decided outcome is shown in the waiting session's terminal; unfinished work gets a supervisor
④ the instruction a stopped session receives says where each item stands
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID', 'CMUX_SURFACE_ID', 'CMUX_WORKSPACE_ID',
             'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):
    os.environ.pop(name, None)               # never a real terminal: every terminal below is a fake one

from harness import attach, manager, providers, supervision, verifier, wake  # noqa: E402
from harness.engine import Engine  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []
TERMINAL = {'kind': 'tmux', 'cli': 'tmux', 'target': '%71'}
EMPTY_CODEX = '› Ask a follow-up question\n\n'


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', '-C', str(root), 'init', '-q'], check=True)
    (root / 'export.py').write_text('def export(rows):\n    return rows\n')
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '-m', 'init'], check=True)
    return root


def verdict(d1='met'):
    return {'items': [{'id': 'G0', 'status': 'met' if d1 == 'met' else 'unmet', 'evidence': ['goal checked']},
                      {'id': 'D1', 'status': d1, 'evidence': ['ran export on an empty list: ' + ('ok' if d1 == 'met' else 'crashed')]}],
            'extra': [], 'missing': [], 'fix': [] if d1 == 'met' else ['handle an empty list'], 'question': ''}


class FakeReviewer:
    """Stands in for providers.Provider. `during` runs while the review holds the task lock."""
    answer = None
    during = None

    def __init__(self, name, home, model=None, timeout=None):
        self.name, self.home = name, Path(home)

    def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
        if FakeReviewer.during:
            FakeReviewer.during()
        log = self.home / 'runs' / task_id / 'fake.events.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({'type': 'item.completed', 'item': {'id': 'c1', 'type': 'command_execution',
                                                                      'command': 'python3 export.py', 'exit_code': 0}}) + '\n')
        metadata = {'call_id': 'fake', 'session': 'fake-reviewer', 'provider': self.name, 'requested_model': None,
                    'reported_models': ['fake'], 'context': {'tokens': 1, 'source': 'fake', 'window': None},
                    'seconds': 0.1, 'usage': {'input_tokens': 1, 'output_tokens': 1}, 'log': str(log), 'invocation': ''}
        return json.loads(json.dumps(FakeReviewer.answer)), metadata


def last_of(task):
    return ((task['data'].get('verdicts') or [{}])[-1]) or {}


def claimed(store, root, session, goal):
    task = attach.create(store, root, goal, 'codex', session, done=['D1 the export handles every list, including an empty one'])
    attach.record_evidence(store, task['id'], 'D1', note='ran the export on a sample list')
    attach.claim(store, task['id'], summary='export done')
    return task['id']


def review(engine, tid, answer, during=None):
    FakeReviewer.answer, FakeReviewer.during = answer, during
    try:
        return attach.run_verification(engine, tid)
    finally:
        FakeReviewer.during = None


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-codex-stall-'))
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
    supervision.configure(store, 'auto')                # as on the user's machine (explicit auto, 23 continuations typed)
    other = lambda name: (lambda: (root / name).write_text('another session wrote this during the review\n'))

    print('① the verdict survives other sessions\' edits in a shared folder (16 of 18 discarded reviews were Codex)')
    tid = claimed(store, root, 'S-A', 'Build the CSV export and make it robust')
    task = review(engine, tid, verdict('unmet'), during=other('notes-a.md'))
    last = (task['data'].get('verdicts') or [{}])[-1]
    check('another session edits a file during the review: the unmet verdict is recorded, not thrown away',
          len(task['data'].get('verdicts') or []) == 1 and not last.get('superseded'), task['data'].get('verdicts'))
    check('…and delivered as unmet (not "the request changed")',
          task['status'] == 'working' and task['data'].get('reason_code') == 'unmet', (task['status'], task['data'].get('reason_code')))
    check('…the manager sends the session back with what the review found (not "claim again")',
          manager.diagnose(task)['reason'] == 'review_unmet', manager.diagnose(task))
    check('…the verdict says the folder moved under it, by other sessions', last.get('changed_during_review') == 'other_sessions', last)

    tid = claimed(store, root, 'S-B', 'Build the TSV export and make it robust')
    task = review(engine, tid, verdict('met'), during=other('notes-b.md'))
    check('another session edits a file during a passing review: verified', task['status'] == 'verified', (task['status'], task['data'].get('reason')))

    tid = claimed(store, root, 'S-C', 'Build the JSON export and make it robust')
    wake.note(store, 'S-C', 'codex', 'stop', TERMINAL)

    def own_session_worked():
        (root / 'export.py').write_text('def export(rows):\n    return list(rows)\n')
        wake.note(store, 'S-C', 'codex', 'stop', TERMINAL)   # its Stop hook fired: a turn ran during the review
    task = review(engine, tid, verdict('met'), during=own_session_worked)
    check("the task's own session changed files during a passing review: not verified (control)",
          task['status'] == 'working' and task['data'].get('reason_code') == 'changed_during_review',
          (task['status'], task['data'].get('reason_code')))
    check('…the pass is kept in the ledger, marked superseded', ((task['data'].get('verdicts') or [{}])[-1] or {}).get('superseded') == 'own_files')
    check('…and the session is told why, to claim again', 'claim again' in manager.diagnose(task)['next'], manager.diagnose(task))

    tid = claimed(store, root, 'S-D', 'Build the XML export and make it robust')
    wake.note(store, 'S-D', 'codex', 'stop', TERMINAL)
    task = review(engine, tid, verdict('unmet'), during=lambda: (other('notes-d.md')(), wake.note(store, 'S-D', 'codex', 'stop', TERMINAL)))
    check("…while an unmet verdict holds even when the task's own session worked (findings are never dropped)",
          task['data'].get('reason_code') == 'unmet' and last_of(task).get('changed_during_review') == 'own_session',
          (task['data'].get('reason_code'), last_of(task).get('changed_during_review')))

    tid = claimed(store, root, 'S-E', 'Build the YAML export and make it robust')
    task = review(engine, tid, verdict('met'))
    check('nothing moved: verified (control)', task['status'] == 'verified' and not last_of(task).get('changed_during_review'))

    print("①-b the claiming turn ends while the review runs: that Stop is not work (real cmux Codex pane, 2026-11-06)")
    def stamp(offset=0.0):
        moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset)
        return moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    def codex_item(kind, text=''):
        if kind == 'end':
            return {'timestamp': stamp(), 'type': 'event_msg', 'payload': {'type': 'task_complete'}}
        return {'timestamp': stamp(), 'type': 'response_item',
                'payload': {'type': 'custom_tool_call', 'name': 'exec', 'input': text}}

    def claude_item(kind, text=''):
        part = ({'type': 'tool_use', 'name': 'Bash', 'input': {'command': text}} if kind == 'tool'
                else {'type': 'text', 'text': 'Claimed; the review is running.'})
        return {'timestamp': stamp(), 'type': 'assistant', 'message': {'content': [part]}}

    def append(path, *items):
        with open(path, 'a', encoding='utf-8') as stream:
            for item in items:
                stream.write(json.dumps(item) + '\n')

    def stop_hook(session, path):
        """The session's real Stop hook, run while the review holds the task lock."""
        with patch.object(attach, '_pane', return_value=TERMINAL), patch.object(supervision, 'ensure'):
            attach.handle(store, 'stop', {'cwd': str(root), 'session_id': session, 'stop_hook_active': False,
                                          'transcript_path': str(path), 'last_assistant_message': 'Claimed.'}, 'codex')

    def claiming_turn(session, goal, item, during_items, own_edit=None):
        """A turn that ran `claim` as its last tool, then (during the review) wrote `during_items` and stopped."""
        path = base / ('%s.jsonl' % session)
        append(path, {'timestamp': stamp(-30), 'type': 'session_meta', 'payload': {'id': session}},
               item('tool', 'cosmos-orbit claim %s' % session))
        tid_ = claimed(store, root, session, goal)
        wake.note(store, session, 'codex', 'prompt', TERMINAL)  # the claiming turn's prompt, before the review
        def during():
            other('notes-%s.md' % session.lower())()
            if own_edit:
                own_edit()
            append(path, *[item(kind, text) for kind, text in during_items])
            stop_hook(session, path)
        return review(engine, tid_, verdict('met'), during=during)

    task = claiming_turn('S-CL1', 'Build the Avro export and make it robust', codex_item, [('end', '')])
    check('Codex: the claiming turn stops during a passing review, no tool after it, another session wrote a file: '
          'verified (was: filed under its own changes, superseded, "claim again")',
          task['status'] == 'verified' and last_of(task).get('changed_during_review') == 'other_sessions',
          (task['status'], task['data'].get('reason_code'), last_of(task).get('changed_during_review'), last_of(task).get('superseded')))
    check('…the Stop hook remembered where the transcript is (written outside the lock)',
          store.preference(verifier.TRANSCRIPT_KEY + 'S-CL1') == str(base / 'S-CL1.jsonl'))
    task = claiming_turn('S-CL2', 'Build the ORC export and make it robust', claude_item, [('text', '')])
    check('Claude Code transcript, same turn: verified', task['status'] == 'verified'
          and last_of(task).get('changed_during_review') == 'other_sessions', (task['status'], last_of(task)))

    def own_patch():
        (root / 'export.py').write_text('def export(rows):\n    return sorted(rows)\n')
    task = claiming_turn('S-CL3', 'Build the Feather export and make it robust', codex_item,
                         [('tool', 'apply_patch export.py'), ('end', '')], own_edit=own_patch)
    check('…control: a tool called after the review began (the session edited after claiming): the pass is superseded',
          task['status'] == 'working' and last_of(task).get('superseded') == 'own_files',
          (task['status'], last_of(task).get('superseded')))
    tid = claimed(store, root, 'S-CL4', 'Build the Arrow export and make it robust')
    task = review(engine, tid, verdict('met'), during=lambda: (other('notes-cl4.md')(), stop_hook('S-CL4', base / 'gone.jsonl')))
    check('…control: the transcript cannot be read: any hook counts as before (a pass is never kept on a guess)',
          task['status'] == 'working' and last_of(task).get('superseded') == 'own_files',
          (task['status'], last_of(task).get('superseded')))
    late = base / 'late.jsonl'
    append(late, {'timestamp': stamp(5), 'type': 'event_msg', 'payload': {'type': 'task_complete'}})
    check('…a transcript tail that starts after the review began tells nothing (None), and no file tells nothing',
          verifier.tool_calls_since(str(late), stamp()) is None and verifier.tool_calls_since(None, stamp()) is None)

    print('② a Stop that came while the review held the lock still ends the turn')
    tid = claimed(store, root, 'S-F', 'Build the Parquet export and make it robust')
    wake.note(store, 'S-F', 'codex', 'prompt', TERMINAL)
    supervision.begin_turn(store, store.get(tid), 'S-F')
    launched = []
    with patch.object(attach, '_pane', return_value=TERMINAL), \
            patch.object(supervision, 'ensure', side_effect=lambda s, t, sess: launched.append(sess)):
        with store.lock('task:' + tid):                 # the review holds it (minutes, in the incident)
            attach.handle(store, 'stop', {'cwd': str(root), 'session_id': 'S-F', 'stop_hook_active': False,
                                          'last_assistant_message': 'Claimed; the review is running.'}, 'codex')
    check('the Stop during the review still starts the supervisor', launched == ['S-F'], launched)
    review(engine, tid, verdict('unmet'))
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, EMPTY_CODEX
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        outcome = supervision.tick(store, tid, 'S-F', runner)
    typed = [c for c in calls if 'send-keys' in c]
    check('the review came back unmet while the session sat idle: the supervisor types the continuation (was host_turn_active)',
          outcome == 'continued' and any(supervision.RESUME_LINE in ' '.join(c) for c in typed), (outcome, calls))
    calls.clear()
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        again = supervision.tick(store, tid, 'S-F', runner)
    check('…once (control: the same stop is not typed into twice)', again == 'awaiting_stop' and not calls, (again, calls))

    # The hooks named a cmux surface that does not exist (Codex's daemon; measured 2026-11-06 in a real cmux pane:
    # the review came back unmet, the supervisor read 'unreadable', the session sat with nothing typed).
    dead = {'kind': 'cmux', 'cli': '/cmux', 'target': 'DEAD-SURFACE'}

    def stopped_in_dead_pane(session, goal, answer):
        tid_ = claimed(store, root, session, goal)
        wake.note(store, session, 'codex', 'prompt', dead)
        supervision.begin_turn(store, store.get(tid_), session)
        with patch.object(attach, '_pane', return_value=dead), patch.object(supervision, 'ensure'):
            with store.lock('task:' + tid_):
                attach.handle(store, 'stop', {'cwd': str(root), 'session_id': session, 'stop_hook_active': False,
                                              'last_assistant_message': 'Claimed.'}, 'codex')
        return tid_, review(engine, tid_, answer)

    def codex_screen(tid_, typed=''):
        return ('• Done; the claim is submitted.\n↳ Hook · Orbit: Independent review is running. Task %s; inspect\n'
                '`cosmos-orbit status %s`.\n  Worked for 41s • 10:34 AM\n\n› %s\n\n  GPT-6-Astra low · /repo\n'
                '  ? for shortcuts\n' % (tid_, tid_, typed or 'Ask Codex to do anything'))

    def cmux_runner(screens, sent):
        tree = {'windows': [{'workspaces': [{'panes': [{'surfaces': [{'id': pane, 'type': 'terminal'}]} for pane in screens]}]}]}
        def run(argv):
            if argv[1:] == ['--json', '--id-format', 'both', 'tree', '--all']:
                return 0, json.dumps(tree)
            if argv[1] == 'read-screen':
                return (0, screens[argv[3]]) if argv[3] in screens else (1, 'Error: not_found: Surface not found')
            if argv[1] in ('send-panel', 'send-key-panel', 'notify'):
                if argv[1] == 'notify' and argv[argv.index('--surface') + 1] not in screens:
                    return 1, 'Error: not_found'
                sent.append(argv)
                return 0, 'OK'
            return 1, ''
        return run

    tid, _ = stopped_in_dead_pane('S-LOC', 'Build the HDF5 export and make it robust', verdict('unmet'))
    sent = []
    screens = {'S-OTHER': EMPTY_CODEX, 'S-MINE': codex_screen(tid), 'S-WORKING': codex_screen('0' * 12) + '\nesc to interrupt\n'}
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        outcome = supervision.tick(store, tid, 'S-LOC', cmux_runner(screens, sent))
    check('the pane its hooks named does not exist: the one pane whose screen names the task, input empty, gets the '
          'continuation (was: "unreadable", the session sat idle)',
          outcome == 'continued' and sent and all(a[a.index('--panel') + 1] == 'S-MINE' for a in sent)
          and any(supervision.RESUME_LINE in ' '.join(a) for a in sent), (outcome, sent))
    tid, _ = stopped_in_dead_pane('S-LOC2', 'Build the NetCDF export and make it robust', verdict('unmet'))
    sent = []
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        two = supervision.tick(store, tid, 'S-LOC2', cmux_runner({'S-A': codex_screen(tid), 'S-B': codex_screen(tid)}, sent))
    check('…two panes name the task: nothing is typed (control: never a guess)', two == 'unreadable' and not sent, (two, sent))
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        typing = supervision.tick(store, tid, 'S-LOC2', cmux_runner({'S-A': codex_screen(tid, 'my own words')}, sent))
    check('…the one pane naming it holds the user\'s typing: nothing is typed (control)', typing == 'unreadable' and not sent,
          (typing, sent))
    tid, task = stopped_in_dead_pane('S-LOC3', 'Build the Pickle export and make it robust', verdict('met'))
    sent = []
    told = supervision.tell(store, task, 'S-LOC3', cmux_runner({'S-MINE': codex_screen(tid)}, sent))
    check('…a verified outcome is shown in the pane that names the task when the named one is gone',
          told and len(sent) == 1 and sent[0][1] == 'notify' and sent[0][sent[0].index('--surface') + 1] == 'S-MINE', (told, sent))

    tid = claimed(store, root, 'S-G', 'Build the Avro export and make it robust')
    wake.note(store, 'S-G', 'codex', 'prompt', TERMINAL)
    supervision.begin_turn(store, store.get(tid), 'S-G')
    with patch.object(attach, '_pane', return_value=TERMINAL), patch.object(supervision, 'ensure'):
        with store.lock('task:' + tid):
            attach.handle(store, 'stop', {'cwd': str(root), 'session_id': 'S-G', 'stop_hook_active': False,
                                          'last_assistant_message': 'Claimed.'}, 'codex')
    review(engine, tid, verdict('unmet'))
    wake.note(store, 'S-G', 'codex', 'prompt', TERMINAL)     # the user typed: a turn really runs now
    calls.clear()
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        busy = supervision.tick(store, tid, 'S-G', runner)
    check('a turn that started after that Stop is never typed into (control)', busy == 'host_turn_active' and not calls, (busy, calls))

    tid = claimed(store, root, 'S-R', 'Build the Excel export and make it robust')
    review(engine, tid, verdict('unmet'))
    wake.note(store, 'S-R', 'codex', 'prompt', TERMINAL)
    supervision.begin_turn(store, store.get(tid), 'S-R')
    order = []
    with patch.object(attach, '_pane', return_value=TERMINAL), \
            patch.object(supervision, 'ensure', side_effect=lambda s, t, sess: order.append(
                ('ensure', bool((store.get(t['id'])['data'].get('supervision') or {}).get(sess, {}).get('turn_open'))))):
        reply = attach.handle(store, 'stop', {'cwd': str(root), 'session_id': 'S-R', 'stop_hook_active': False,
                                              'last_assistant_message': 'Done for now.'}, 'codex')
    calls.clear()
    with patch.object(wake, 'allowed', return_value=(True, 0)):
        racing = supervision.tick(store, tid, 'S-R', runner)
    check('a Stop the hook sends back (block) starts the supervisor only after the turn is marked as continued',
          (reply or {}).get('decision') == 'block' and order == [('ensure', True)], (reply and reply.get('decision'), order))
    check('…so the supervisor never types into the continued turn (the 07:17:45 double instruction)',
          racing == 'host_turn_active' and not calls, (racing, calls))

    print('③ the outcome reaches the session that waits for it')
    deliver = getattr(supervision, 'deliver', None)
    shown = []

    def notifier(argv):
        shown.append(argv)
        return 0, ''
    tid = claimed(store, root, 'S-H', 'Build the ORC export and make it robust')
    wake.note(store, 'S-H', 'codex', 'stop', TERMINAL)
    task = review(engine, tid, verdict('met'))
    if deliver:
        deliver(store, task, runner=notifier, launch=lambda *a: shown.append(('launch',) + a[2:]))
    check('verified while the session sat idle: its terminal is told the task is complete',
          len(shown) == 1 and 'display-message' in shown[0] and 'verified' in ' '.join(shown[0]) and tid in ' '.join(shown[0]), shown)
    if deliver:
        deliver(store, store.get(tid), runner=notifier)
    check('…once (control)', len(shown) == 1, shown)
    shown.clear()
    tid = claimed(store, root, 'S-I', 'Build the Feather export and make it robust')
    wake.note(store, 'S-I', 'codex', 'stop', TERMINAL)
    task = review(engine, tid, verdict('unmet'))
    if deliver:
        deliver(store, task, runner=notifier, launch=lambda s, t, sess: shown.append(('launch', sess)))
    check('unmet: a supervisor is started for the waiting session (it types the continuation at the idle line)',
          shown == [('launch', 'S-I')], shown)
    shown.clear()
    tid = claimed(store, root, 'S-J', 'Build the Arrow export and make it robust')
    wake.note(store, 'S-J', 'codex', 'stop', TERMINAL)
    ask = dict(verdict('met'), question='Which delimiter should the export use?')
    task = review(engine, tid, ask)
    if deliver:
        deliver(store, task, runner=notifier)
    check('the review needs the user: the terminal says a decision is needed, with the question',
          task['status'] == 'needs_input' and len(shown) == 1 and 'decision' in ' '.join(shown[0]) and 'delimiter' in ' '.join(shown[0]),
          (task['status'], shown))

    print('④ a session sent back to work is told where each item stands')
    task = attach.create(store, root, 'Ship the three exporters with tests', 'codex', 'S-K',
                         done=['D1 CSV exporter', 'D2 TSV exporter', 'D3 JSON exporter'])
    attach.record_evidence(store, task['id'], 'D1', note='csv tests pass')
    text = manager.instruction(store, store.get(task['id']), manager.diagnose(store.get(task['id'])))
    check('the instruction carries the progress line: D1 evidenced, D2 and D3 nothing yet',
          'PROGRESS' in text and 'D1 ✓' in text and 'D2 ·' in text and 'D3 ·' in text, text[:400])


if __name__ == '__main__':
    sys.exit(main())
