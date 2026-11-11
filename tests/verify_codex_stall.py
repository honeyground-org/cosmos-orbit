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

for _marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):  # these runs stand for sessions a person opened,
    os.environ.pop(_marker, None)                                    # not ones an agent delegated (attach.delegated)
from harness import attach, evolve, manager, providers, recovery, supervision, verifier, wake  # noqa: E402
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
    # A Stop hook while a review is pending starts a background worker; in a test store that worker ran the real
    # provider (2026-11-06: two real `codex exec` reviews from this suite) and raced the in-process review for its
    # lock. Nothing here may start a process: requests are counted instead.
    started = []
    try:
        with patch.object(recovery, 'ensure_worker', side_effect=lambda *a, **k: started.append('worker')), \
                patch.object(evolve, 'maybe_start', side_effect=lambda *a, **k: started.append('evolve')):
            run(base, store, engine)
    finally:
        providers.Provider = original
    leaked = [d for d in (base / 'home' / 'runs').glob('*/*.invocation.json')]
    check('no real model call and no background process from this suite (%d worker starts were stubbed)'
          % started.count('worker'), not leaked, leaked)
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
    tid, task = stopped_in_dead_pane('S-LOC4', 'Build the Protobuf export and make it robust', verdict('met'))
    sent = []
    told = supervision.tell(store, task, 'S-LOC4', cmux_runner({'S-MINE': codex_screen(tid) + '\n• Working (3s • esc to interrupt)\n'}, sent))
    check('…also while that turn is still finishing (measured: the verdict came before the claiming turn ended); a '
          'notification types nothing', told and len(sent) == 1 and sent[0][sent[0].index('--surface') + 1] == 'S-MINE',
          (told, sent))
    tid, task = stopped_in_dead_pane('S-LOC5', 'Build the Thrift export and make it robust', verdict('met'))
    sent, inner = [], []
    plain = cmux_runner({'S-MINE': codex_screen(tid)}, sent)
    def racing(argv):
        if argv[1] == 'notify' and not inner:     # the other process tells the same outcome at this very moment
            inner.append(supervision.tell(store, store.get(tid), 'S-LOC5', plain))
        return plain(argv)
    first = supervision.tell(store, task, 'S-LOC5', racing)
    check('…two processes telling one outcome at the same moment show it once (measured: the worker and a supervisor '
          'both did, one second apart)', first and inner == [False] and len(sent) == 1, (first, inner, sent))

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

    print('⑤ a shared folder: what moved is told apart by its paths, not by whether the session called any tool')
    from harness import development

    def old_fingerprint(folder):                         # the algorithm before snapshot(), copied: trees stay comparable
        import hashlib
        names = set(development.git(folder, 'ls-files', '-z').split(b'\0'))
        names.update(development.git(folder, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0'))
        digest = hashlib.sha256()
        for raw in sorted(names - {b''}):
            item = Path(folder) / os.fsdecode(raw)
            digest.update(raw + b'\0')
            if item.is_symlink():
                digest.update(b'link:' + os.fsencode(os.readlink(item)))
            elif item.is_file():
                digest.update(str(item.stat().st_mode).encode())
                digest.update(item.read_bytes())
            else:
                digest.update(b'missing')
        digest.update(development.git(folder, 'rev-parse', 'HEAD'))
        return digest.hexdigest()
    (root / 'gone-link').symlink_to('nowhere')
    check('the fingerprint is the same value as before (trees recorded earlier stay comparable)',
          development.fingerprint(root) == old_fingerprint(root))
    (root / 'gone-link').unlink()
    if hasattr(development, 'snapshot'):
        before = development.snapshot(root)
        other('notes-moved.md')()
        after = development.snapshot(root)
        moved = development.moved_entries(before[1], after[1])
    else:
        moved = 'no snapshot(): a moved tree cannot tell which paths moved'
    check('…and the snapshot tells which paths moved', moved == ['notes-moved.md'], moved)

    real_check = development.check

    def then(write):
        def checks_then_write(*args, **kwargs):
            done_checks = real_check(*args, **kwargs)
            write()                                      # lands after the checks recorded the tree, before the review
            return done_checks
        return checks_then_write

    print('  incident 2026-11-09 07:31:12Z: refused 0.6 s after its checks, a cache file of another project had moved')
    tid = claimed(store, root, 'S-P1', 'Build the Lisp export and make it robust')
    with patch.object(development, 'check', side_effect=then(other('notes-p1.md'))):
        task = review(engine, tid, verdict('met'))
    check('another session writes a file between the checks and the review: reviewed and verified '
          '(was: refused at once, "Verifier requires all current fixed checks to pass", and the user was asked)',
          task['status'] == 'verified' and (last_of(task).get('changed_before_review') or {}).get('by') == 'other_sessions',
          (task['status'], task['data'].get('reason_code'), task['data'].get('reason'), last_of(task).get('changed_before_review')))
    check('…the verdict names what moved', (last_of(task).get('changed_before_review') or {}).get('paths') == ['notes-p1.md'],
          last_of(task).get('changed_before_review'))

    own_log = base / 'S-P2.jsonl'
    append(own_log, {'timestamp': stamp(-30), 'type': 'session_meta', 'payload': {'id': 'S-P2'}})
    tid = claimed(store, root, 'S-P2', 'Build the Lua export and make it robust')
    store.preference(verifier.TRANSCRIPT_KEY + 'S-P2', str(own_log))

    def own_write_after_checks():
        (root / 'export.py').write_text('def export(rows):\n    return tuple(rows)\n')
        append(own_log, codex_item('tool', 'apply_patch export.py'))
    with patch.object(development, 'check', side_effect=then(own_write_after_checks)):
        task = review(engine, tid, verdict('met'))
    check("control: the task's own session edits a file after the checks: not reviewed, sent back to claim again",
          task['status'] == 'working' and task['data'].get('reason_code') == 'changed_during_review'
          and 'before the review began' in (task['data'].get('reason') or ''),
          (task['status'], task['data'].get('reason_code'), task['data'].get('reason')))
    check('…no review ran and nothing was asked of the user', not task['data'].get('verdicts') and task['status'] != 'needs_input',
          (task['status'], len(task['data'].get('verdicts') or [])))

    print('  incident 2026-11-09 turn 11: the session waited for the verdict with a tool, another session wrote a file')

    def waiting_turn(session, goal, calls, writes):
        log = base / ('%s.jsonl' % session)
        append(log, {'timestamp': stamp(-30), 'type': 'session_meta', 'payload': {'id': session}},
               claude_item('tool', 'cosmos-orbit claim %s' % session))
        tid_ = claimed(store, root, session, goal)
        wake.note(store, session, 'claude', 'prompt', TERMINAL)

        def during():
            for write in writes:
                write()
            append(log, *[call() for call in calls])
            stop_hook(session, log)
        return review(engine, tid_, verdict('met'), during=during)

    def bash(command, cwd=None):
        def item():
            event = claude_item('tool', command)
            if cwd:
                event['cwd'] = str(cwd)
            return event
        return item

    def build(folder):
        def write():
            (root / folder).mkdir(exist_ok=True)
            (root / folder / 'out.txt').write_text('built by make\n')
        return write

    task = waiting_turn('S-Q1', 'Build the Erlang export and make it robust',
                        [bash('for i in $(seq 1 90); do cosmos-orbit status S-Q1; sleep 10; done', cwd=root)],
                        [other('notes-q1.md')])
    check('the session polls the review with a tool that names none of what moved: verified, filed under other sessions '
          '(was: superseded as its own change, "claim again")',
          task['status'] == 'verified' and last_of(task).get('changed_during_review') == 'other_sessions',
          (task['status'], task['data'].get('reason_code'), last_of(task).get('superseded'), last_of(task).get('changed_during_review')))
    def play(log, steps):
        """Write a session's transcript and the shared folder in this order. A step: ('call', id, command[, cwd[,
        background]]) and ('result', id) for Claude Code · ('tool', id, name) another Claude Code tool · ('notify', id,
        status) Claude Code's notice that a background job ended, as a user message (the session idle) · ('queued', id,
        status) the same notice queued while a turn runs · ('codex', id, exec script) and ('codex_out', id, text) for
        Codex · ('write', fn) · ('pause', seconds)."""
        import time
        for step in steps:
            if step[0] in ('notify', 'queued'):
                notice = ('<task-notification>\n<task-id>b%s</task-id>\n<tool-use-id>%s</tool-use-id>\n<output-file>/tmp/'
                          'b%s.output</output-file>\n<status>%s</status>\n<summary>Background command "a step" %s (exit '
                          'code 0)</summary>\n</task-notification>' % (step[1], step[1], step[1], step[2], step[2]))
                if step[0] == 'notify':
                    append(log, {'timestamp': stamp(), 'type': 'user', 'message': {'role': 'user', 'content': notice}})
                else:
                    append(log, {'timestamp': stamp(), 'type': 'attachment', 'attachment': {
                        'type': 'queued_command', 'prompt': notice, 'commandMode': 'task-notification'}})
            elif step[0] == 'tool':
                append(log, {'timestamp': stamp(), 'type': 'assistant', 'message': {'content': [
                    {'type': 'tool_use', 'id': step[1], 'name': step[2], 'input': {'query': 'export'}}]}})
            elif step[0] == 'call':
                data = {'command': step[2], 'description': 'a step'}
                if len(step) > 4 and step[4]:
                    data['run_in_background'] = True
                event = {'timestamp': stamp(), 'type': 'assistant',
                         'message': {'content': [{'type': 'tool_use', 'id': step[1], 'name': 'Bash', 'input': data}]}}
                if len(step) > 3 and step[3]:
                    event['cwd'] = str(step[3])
                append(log, event)
            elif step[0] == 'result':
                append(log, {'timestamp': stamp(), 'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': step[1], 'content': 'ok'}]}})
            elif step[0] == 'codex':
                append(log, {'timestamp': stamp(), 'type': 'response_item', 'payload': {
                    'type': 'custom_tool_call', 'name': 'exec', 'call_id': step[1], 'input': step[2]}})
            elif step[0] == 'codex_out':
                append(log, {'timestamp': stamp(), 'type': 'response_item', 'payload': {
                    'type': 'custom_tool_call_output', 'call_id': step[1], 'output': [{'type': 'input_text', 'text': step[2]}]}})
            elif step[0] == 'write':
                step[1]()
            else:
                time.sleep(step[1])

    def timeline(session, goal, steps, claim_line=None, before=()):
        """A session that claimed (its claiming call still runs: it waits for the verdict), then did `steps` while the
        review ran. `before`: steps before the claim."""
        log = base / ('%s.jsonl' % session)
        append(log, {'timestamp': stamp(-30), 'type': 'session_meta', 'payload': {'id': session}})
        play(log, before)
        append(log, claude_item('tool', claim_line or 'cosmos-orbit claim %s' % session))
        tid_ = claimed(store, root, session, goal)
        wake.note(store, session, 'claude', 'prompt', TERMINAL)

        def during():
            play(log, steps)
            stop_hook(session, log)
        return review(engine, tid_, verdict('met'), during=during)

    gap = verifier.RUN_SLACK + .3 if hasattr(verifier, 'RUN_SLACK') else .3
    task = timeline('S-Q2', 'Build the Elixir export and make it robust',
                    [('call', 'q2', 'cp notes-q2.md.bak /tmp/keep'), ('result', 'q2'), ('pause', gap),
                     ('write', other('notes-q2.md'))])
    check('…a longer name that contains the moved one does not name it ("notes-q2.md.bak" is not "notes-q2.md")',
          task['status'] == 'verified', (task['status'], last_of(task).get('superseded')))
    task = waiting_turn('S-Q3', 'Build the Haskell export and make it robust',
                        [bash('cd build-q3 && make')], [build('build-q3')])
    check('control: a call names the folder at the top of what moved (cd build-q3 && make wrote build-q3/out.txt): '
          'its own change, the pass is superseded',
          task['status'] == 'working' and last_of(task).get('superseded') == 'own_files',
          (task['status'], last_of(task).get('superseded'), last_of(task).get('changed_during_review')))
    task = waiting_turn('S-Q4', 'Build the OCaml export and make it robust',
                        [bash('make', cwd=root / 'build-q4')], [build('build-q4')])
    check('control: a call that runs inside the folder that moved (cwd build-q4, "make"): its own change',
          task['status'] == 'working' and last_of(task).get('superseded') == 'own_files',
          (task['status'], last_of(task).get('superseded'), last_of(task).get('changed_during_review')))
    task = waiting_turn('S-Q5', 'Build the Scala export and make it robust',
                        [bash("sed -i '' 's/rows/list(rows)/' %s" % (root / 'export.py'))],
                        [lambda: (root / 'export.py').write_text('def export(rows):\n    return list(rows)\n'),
                         other('notes-q5.md')])
    check('control: a call names the moved file by its absolute path, another session also wrote one: its own change',
          task['status'] == 'working' and last_of(task).get('superseded') == 'own_files',
          (task['status'], last_of(task).get('superseded')))

    print('⑥ a command that names nothing: what changed while it ran is its own (the risk names alone left open)')
    R, W, B = (False, False), (True, False), (True, True)
    table = [
        ('git status --short', R), ('git -C sub diff HEAD -- a.py', R), ('cat a.txt 2>/dev/null | head -5 >&2', R),
        ('for i in $(seq 1 90); do cosmos-orbit status S; sleep 10; done', R),
        ('until cosmos-orbit status X | grep -q verified; do sleep 10; done', R),
        ("cosmos-orbit claim X --summary \"$(cat <<'EOF'\nran `make` in dist/\nEOF\n)\"", R),
        ("cosmos-orbit claim X --summary 'ran `make` and $(make)'", R), ('sed -n 1,5p f', R), ('find . -name "*.py"', R),
        ('make', W), ('cd build && make', W), ('python3 tools/gen.py', W), ('rm -f *.log', W), ('echo hi > out.txt', W),
        ("sed -i '' s/a/b/ f", W), ('find . -name "*.pyc" -delete', W), ('git checkout main', W), ('echo "$(make)"', W),
        ('diff <(make) <(ls)', W), ('bash -lc "make"', W), ('ls\nmake', W), ('timeout 5 make', W), ('xargs -I {} rm {}', W),
        ('npm run dev &', B), ('nohup make', B),
        ('for i in $(seq 1 60); do cosmos-orbit status S 2>/dev/null | grep -q -E "^x" || break; sleep 5; done', R),
        ('while true; do cosmos-orbit status S | grep -q verified && break; sleep 10; done', R),
        ('for f in *.py; do grep -q x "$f" || continue; echo "$f"; done', R),
        ('cosmos-orbit status S | grep -q verified || exit 1', R), ('grep -q x f || return 0', R), ('shift', R),
        ('getopts ab opt', R), ('umask', R), ('ulimit -n', R), ('shopt -s nullglob', R), ('hash -r', R),
        ("trap 'rm -f out.txt' EXIT", W), ('source ./env.sh', W), ('. ./env.sh', W), ('make || exit 1', W),
        # 2026-11-11: jobs the command's own shell waits for with a bare `wait` are not left running
        ('for t in tests/verify_*.py; do ( python3 $t > $LOG/$(basename $t).log 2>&1; echo "$? $t" ) & done; wait', W),
        ('make & make test & wait', W), ('sleep 1 & sleep 2 & wait; echo done', R), ('{ make & }; wait', W),
        ('make & wait $!', B), ('make & wait; npm run dev &', B), ('(make &); wait', B), ('nohup make & wait', B),
        ('make &', B), ('echo "$(make &)"; wait', B)]
    try:
        from harness import attribution
        wrong = [(line, attribution.shell_effect(line), want) for line, want in table if attribution.shell_effect(line) != want]
    except ImportError:
        wrong = 'no classifier: every call is judged by the names in it alone'
    check('the command classifier tells reads, writes and jobs left running (%d command lines)' % len(table), not wrong, wrong)

    def dist(folder, name='app.bin'):
        def write():
            (root / folder).mkdir(exist_ok=True)
            (root / folder / name).write_text('built\n')
        return write

    def own(task):
        return task['status'] == 'working' and last_of(task).get('superseded') == 'own_files'

    def others(task):
        return task['status'] == 'verified' and last_of(task).get('changed_during_review') == 'other_sessions'

    def seen(task):
        return (task['status'], last_of(task).get('superseded'), last_of(task).get('changed_during_review'))

    task = timeline('S-R1', 'Build the Kotlin export and make it robust',
                    [('call', 'r1', 'make', root), ('write', dist('dist-r1')), ('result', 'r1')])
    check('the session runs `make` at the root during the review and it writes dist-r1/app.bin, a folder the command '
          'never names: its own change, the pass is superseded (was: filed under other sessions, the pass kept)',
          own(task), seen(task))
    task = timeline('S-R2', 'Build the Swift export and make it robust',
                    [('call', 'r2', 'npm run build', root, True), ('result', 'r2'), ('pause', gap),
                     ('write', dist('dist-r2', 'bundle.js'))])
    check('a background job the session started during the review writes after its call returned: its own change',
          own(task), seen(task))
    (root / 'debug-r3.log').write_text('an old log, in the tree the checks record\n')
    task = timeline('S-R3', 'Build the Dart export and make it robust',
                    [('call', 'r3', 'rm -f *.log'), ('write', lambda: (root / 'debug-r3.log').unlink()), ('result', 'r3')])
    check('a glob deletes a file it never names (rm -f *.log): its own change', own(task), seen(task))
    task = timeline('S-R4', 'Codex: build the Nim export and make it robust',
                    [('codex', 'r4', 'text(await tools.exec_command({cmd:"make", "workdir":"%s"}));' % root),
                     ('write', dist('dist-r4')), ('codex_out', 'r4', '{"exit_code":0,"output":"done"}')])
    check('Codex: exec_command runs `make` at the root and writes dist-r4/app.bin: its own change', own(task), seen(task))

    task = timeline('S-R5', 'Build the Rust export and make it robust',
                    [('call', 'r5', 'python3 tools/gen.py'), ('result', 'r5'), ('pause', gap), ('write', other('notes-r5.md'))])
    check('control: a command that may write had finished before another session wrote a file: verified, other sessions',
          others(task), seen(task))
    task = timeline('S-R6', 'Build the Go export and make it robust',
                    [('call', 'r6', 'git diff notes-r6.md; cat notes-r6.md'), ('write', other('notes-r6.md')),
                     ('result', 'r6')])
    check('control: a command that only reads names the file another session writes while it runs: verified (was: its '
          'own change, because the name appeared)', others(task), seen(task))
    task = timeline('S-R7', 'Codex: build the Crystal export and make it robust',
                    [('codex', 'r7', 'text(await tools.exec_command({cmd:"cosmos-orbit status S-R7 && sleep 30", '
                                     'yield_time_ms:1000}));'), ('codex_out', 'r7', '{"session_id":73440}'),
                     ('write', other('notes-r7.md'))])
    check('control: Codex polls the review and its poll is still running when another session writes: verified',
          others(task), seen(task))
    task = timeline('S-R8', 'Build the Zig export and make it robust', [('write', dist('dist-r8', 'notes.md'))],
                    claim_line="cosmos-orbit claim S-R8 --summary 'ran `make` in dist-r8/ and $(make) twice'")
    check('control: the claiming call itself still runs during the review; its summary names `make` and dist-r8/ in '
          'quotes; another session writes there: verified (the summary is text, not a command)', others(task), seen(task))
    def again(session, tid_, steps):
        """The same session claims again after a review sent it back; `steps` happen during the next review."""
        log = base / ('%s.jsonl' % session)
        if store.get(tid_)['status'] != 'working':
            return store.get(tid_)                       # nothing sent it back: there is no second claim
        append(log, claude_item('tool', 'cosmos-orbit claim %s' % session))
        attach.claim(store, tid_, summary='export done')

        def during():
            play(log, steps)
            stop_hook(session, log)
        return review(engine, tid_, verdict('met'), during=during)

    task = timeline('S-R9', 'Build the Julia export and make it robust', [('write', other('notes-r9.md'))],
                    before=[('call', 'r9', 'npm run dev', root, True), ('result', 'r9'), ('pause', gap)])
    check('a job the session left running from before the claim (no notice that it ended) changes a file during the '
          'review: held to it once, the pass is superseded (was: left out entirely, its changes never reviewed)',
          own(task), seen(task))
    check('…and the job is recorded as already held to what it changed once',
          task['data'].get('background_excused') == ['r9'], task['data'].get('background_excused'))
    task = again('S-R9', task['id'], [('write', other('notes-r9b.md'))])
    check('…the session claims again, the same job is still running and another file changes: verified, judged by '
          'names only (else the task is sent back to claim again and again)', others(task), seen(task))
    task = timeline('S-R15', 'Build the Pascal export and make it robust', [('write', other('notes-r15.md'))],
                    before=[('call', 'r15', 'npm run watch -- --log watch-r15.log', root, True), ('result', 'r15'),
                            ('pause', gap)])
    task = again('S-R15', task['id'], [('write', other('watch-r15.log'))])
    check('a job already held once still counts for a path its command names exactly (watch-r15.log): its own change',
          own(task), seen(task))

    print('⑦ when a background job ended is read from Claude Code\'s notice; MCP tools are told apart by their names')
    task = timeline('S-R10', 'Build the Ada export and make it robust',
                    [('call', 'r10', 'npm run build', root, True), ('result', 'r10'), ('notify', 'r10', 'completed'),
                     ('pause', gap), ('write', other('notes-r10.md'))])
    check('a background build started during the review ended (its notice came) before another session wrote a file: '
          'verified (was: counted as running until now, its own change)', others(task), seen(task))
    task = timeline('S-R11', 'Build the COBOL export and make it robust',
                    [('call', 'r11', 'npm run build', root, True), ('result', 'r11'), ('queued', 'r11', 'failed'),
                     ('pause', gap), ('write', other('notes-r11.md'))])
    check('…the same when the notice was queued while a turn ran (an attachment, status failed)', others(task), seen(task))
    task = timeline('S-R12', 'Build the Prolog export and make it robust', [('write', other('notes-r12.md'))],
                    before=[('call', 'r12', 'npm run build', root, True), ('result', 'r12'),
                            ('notify', 'r12', 'completed'), ('pause', gap)])
    check('control: a job left running before the claim had ended (its notice came) before the review: verified at the '
          'first review', others(task), seen(task))
    task = timeline('S-R16', 'Build the Smalltalk export and make it robust',
                    [('call', 'r16', 'npm run build', root, True), ('result', 'r16'), ('notify', 'r16', 'running'),
                     ('pause', gap), ('write', dist('dist-r16'))])
    check('control: a notice with a status that is not an end leaves the job running: what changed is its own',
          own(task), seen(task))
    task = timeline('S-R13', 'Build the Groovy export and make it robust',
                    [('tool', 'r13', 'mcp__brain__recall'), ('write', other('notes-r13.md')), ('result', 'r13')])
    check('the session looks something up with an MCP tool (mcp__brain__recall) while another session writes a file: '
          'verified (was: any MCP tool may write, its own change)', others(task), seen(task))
    task = timeline('S-R14', 'Build the Perl export and make it robust',
                    [('tool', 'r14', 'mcp__brain__remember'), ('write', other('notes-r14.md')), ('result', 'r14')])
    check('control: an MCP tool that writes (mcp__brain__remember) runs while a file changes: its own change',
          own(task), seen(task))
    mcp = [('claude', {'name': 'mcp__brain__recall'}, False),
           ('claude', {'name': 'mcp__codebase-memory-mcp__search_graph'}, False),
           ('claude', {'name': 'mcp__codebase-memory-mcp__get_code_snippet'}, False),
           ('claude', {'name': 'mcp__claude_ai_Slack__slack_read_channel'}, False),
           ('claude', {'name': 'mcp__claude_ai_Notion__notion-search'}, False),
           ('claude', {'name': 'mcp__stylmatch__list_feature_requests'}, False),
           ('claude', {'name': 'mcp__brain__brain_capabilities'}, False),
           ('claude', {'name': 'mcp__example__getUserStatus'}, False),
           ('claude', {'name': 'mcp__brain__remember'}, True), ('claude', {'name': 'mcp__brain__reindex'}, True),
           ('claude', {'name': 'mcp__claude_ai_Slack__slack_send_message'}, True),
           ('claude', {'name': 'mcp__playwright__browser_take_screenshot'}, True),
           ('claude', {'name': 'mcp__codebase-memory-mcp__index_repository'}, True),
           ('claude', {'name': 'mcp__codex_apps__slack_slack_get_file_upload_url'}, True),
           ('claude', {'name': 'mcp__claude_ai_Figma__authenticate'}, True),
           ('claude', {'name': 'mcp__playwright__browser_click'}, True),
           ('codex', {'type': 'custom_tool_call', 'name': 'exec',
                      'input': 'text(await tools.mcp__brain__recall({query:"export"}));'}, False),
           ('codex', {'type': 'custom_tool_call', 'name': 'exec',
                      'input': 'text(await tools.mcp__codebase_memory_mcp__search_graph({name_pattern:"x"}));'}, False),
           ('codex', {'type': 'custom_tool_call', 'name': 'exec', 'input': 'text(await tools.mcp__brain__reindex({}));'}, True),
           ('codex', {'type': 'function_call', 'name': 'mcp__brain__recall', 'arguments': '{}'}, False)]
    from harness import attribution
    wrong = [(call.get('name'), call.get('input'), want) for provider, call, want in mcp
             if attribution.call_effect(provider, dict(call, input=call.get('input', {})))[0] != want]
    check('the MCP classifier: a read verb and no write verb reads, a write verb or an unknown name may write '
          '(%d tools, Claude Code and Codex)' % len(mcp), not wrong, wrong)
    task = timeline('S-R17', 'Build the Fortran 77 export and make it robust',
                    [('call', 'r17', 'for i in $(seq 1 60); do cosmos-orbit status S-R17 2>/dev/null | grep -q -E '
                                     '\'^  "status": "(checking|verifying)"\' || break; sleep 5; done', root),
                     ('write', other('notes-r17.md')), ('result', 'r17')])
    check('incident 2026-11-10 08:34Z: the session waits for its review in a loop that leaves with `|| break` while '
          'another session writes a file: verified (was: `break` counted as a write, its own change)', others(task), seen(task))
    check('a Workflow works on after its call returns, like a subagent',
          attribution.call_effect('claude', {'name': 'Workflow', 'input': {}})[:2] == (True, True),
          attribution.call_effect('claude', {'name': 'Workflow', 'input': {}}))

    own_log = base / 'S-P3.jsonl'
    append(own_log, {'timestamp': stamp(-30), 'type': 'session_meta', 'payload': {'id': 'S-P3'}})
    tid = claimed(store, root, 'S-P3', 'Build the Fortran export and make it robust')
    store.preference(verifier.TRANSCRIPT_KEY + 'S-P3', str(own_log))
    with patch.object(development, 'check', side_effect=then(lambda: play(own_log, [
            ('call', 'p3', 'make', root), ('write', dist('dist-p3')), ('result', 'p3')]))):
        task = review(engine, tid, verdict('met'))
    check("before the review: the task's own `make` wrote dist-p3/app.bin after the checks, naming nothing: not "
          "reviewed, sent back to claim again (was: reviewed as another session's change)",
          task['status'] == 'working' and task['data'].get('reason_code') == 'changed_during_review'
          and not task['data'].get('verdicts'), (task['status'], task['data'].get('reason_code'), len(task['data'].get('verdicts') or [])))

    lingering_log = base / 'S-P4.jsonl'
    append(lingering_log, {'timestamp': stamp(-30), 'type': 'session_meta', 'payload': {'id': 'S-P4'}})
    play(lingering_log, [('call', 'p4', 'npm run dev', root, True), ('result', 'p4'), ('pause', gap)])
    append(lingering_log, claude_item('tool', 'cosmos-orbit claim S-P4'))
    tid = claimed(store, root, 'S-P4', 'Build the Tcl export and make it robust')
    store.preference(verifier.TRANSCRIPT_KEY + 'S-P4', str(lingering_log))
    with patch.object(development, 'check', side_effect=then(other('notes-p4.md'))):
        task = review(engine, tid, verdict('met'))
    check('before the review: a job left running from before the claim, a file changes after the checks: sent back once '
          'and the job recorded (the caller reloads the task, so the record travels with the error)',
          task['data'].get('reason_code') == 'changed_during_review' and task['data'].get('background_excused') == ['p4'],
          (task['status'], task['data'].get('reason_code'), task['data'].get('background_excused')))
    if task['status'] == 'working':                      # else nothing sent it back: there is no second claim
        append(lingering_log, claude_item('tool', 'cosmos-orbit claim S-P4'))
        attach.claim(store, tid, summary='export done')
        with patch.object(development, 'check', side_effect=then(other('notes-p4b.md'))):
            task = review(engine, tid, verdict('met'))
    check('…claimed again, the same job still running, another file changes after the checks: reviewed and verified',
          task['status'] == 'verified' and (last_of(task).get('changed_before_review') or {}).get('by') == 'other_sessions',
          (task['status'], task['data'].get('reason_code'), last_of(task).get('changed_before_review')))


if __name__ == '__main__':
    sys.exit(main())
