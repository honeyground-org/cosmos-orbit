#!/usr/bin/env python3
"""The review prompt stays bounded when the task's folder is shared with thousands of files it never touched.

Observed 2026-11-18: an attached task's record listed 28,657 changed paths (25,823 of them another project's
untracked build output, 30 its own). Both path lists went into the review prompt whole — 6.3 million characters —
and every review of that task ended in 2 seconds with "Prompt is too long". Nothing was ever reviewed.

Synthetic data only; the reviewer is a fake that keeps the prompt it was given. Exit 0 = pass.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH'):
    os.environ.pop(name, None)
for _marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):  # these runs stand for sessions a person opened,
    os.environ.pop(_marker, None)                                    # not ones an agent delegated (attach.delegated)
from harness import attach, evidence, providers  # noqa: E402
from harness.engine import Engine  # noqa: E402
from harness.store import Store  # noqa: E402

# Nothing in this suite starts a process (see verify_requirements for why).
from types import SimpleNamespace as _Namespace  # noqa: E402
from unittest.mock import patch as _no_process  # noqa: E402
from harness import evolve as _evolve, recovery as _recovery, supervision as _supervision  # noqa: E402
_no_process.object(_recovery, 'ensure_worker', return_value=None).start()
_no_process.object(_evolve, 'maybe_start', return_value=None).start()
_no_process.object(_supervision, 'subprocess', _Namespace(
    Popen=lambda *a, **k: _Namespace(pid=None), run=__import__('subprocess').run,
    DEVNULL=__import__('subprocess').DEVNULL, SubprocessError=__import__('subprocess').SubprocessError)).start()

FAILED = []
ROWS, CHARS = 300, 30000          # the budget of one path list, stated here on purpose: the test does not read it from the code
PROMPT_LIMIT = 200000             # characters; the folder below made a prompt of over 700,000 before the bound
OTHERS = 6000                     # files of the project next door


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


class KeepingReviewer:
    """Finds every item met and keeps the prompt it was sent."""
    prompts = []

    def __init__(self, name, home, model=None, timeout=None):
        self.name, self.home = name, Path(home)

    def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
        KeepingReviewer.prompts.append(prompt)
        payload = json.loads(prompt[prompt.index('\n{') + 1:])
        log = self.home / 'runs' / task_id / 'review.events.jsonl'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({'type': 'item.completed', 'item': {'id': 'r', 'type': 'command_execution', 'command': 'cat', 'exit_code': 0}}) + '\n')
        answer = {'items': [{'id': d['id'], 'status': 'met', 'evidence': ['observed ' + d['id']]} for d in payload['done']],
                  'extra': [], 'missing': [], 'fix': [], 'question': '', 'reopen': []}
        return answer, {'call_id': 'r%d' % len(KeepingReviewer.prompts), 'session': 'rev', 'provider': self.name,
                        'requested_model': None, 'reported_models': ['fake'],
                        'context': {'tokens': 1, 'source': 'fake', 'window': None}, 'seconds': 0.1,
                        'usage': {}, 'log': str(log), 'invocation': ''}


def make_repo(base, name):
    root = base / name
    root.mkdir()
    (root / 'verify.py').write_text('print("ok")\n')
    (root / 'app.py').write_text('TOTAL = 1\n')
    for args in (['init', '-q'], ['add', '.'], ['commit', '-q', '-m', 'init']):
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True, capture_output=True)
    return root


def reviewed(engine, store, tid):
    """Claim and review; the payload the reviewer was sent, and the prompt's length."""
    attach.claim(store, tid, summary='claim')
    task = attach.run_verification(engine, tid)
    prompt = KeepingReviewer.prompts[-1]
    return task, json.loads(prompt[prompt.index('\n{') + 1:]), len(prompt)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-bounded-review-'))
    store = Store(base / 'home')
    engine = Engine(store)
    original = providers.Provider
    providers.Provider = KeepingReviewer
    try:
        run(base, store, engine)
    finally:
        providers.Provider = original
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'all checks passed') + ' · ' + str(base))
    return 1 if FAILED else 0


def run(base, store, engine):
    print('① a folder shared with a project the task never touched')
    root = make_repo(base, 'shared')
    other = root / 'other-project' / 'build' / 'intermediates'
    other.mkdir(parents=True)
    for index in range(OTHERS):                                # about 100 characters a path: 600,000 in all
        (other / ('compiled-asset-%05d-%s.bin' % (index, 'x' * 40))).write_text('o')
    for index in range(400):                                   # loose files at the top level, first in name order
        (root / ('aaa-%03d.tmp' % index)).write_text('t')
    (root / 'app.py').write_text('TOTAL = 2\n')                 # the task's change to a tracked file
    (root / 'proof').mkdir()
    (root / 'proof' / 'check.py').write_text('print("total ok")\n')     # its evidence command
    (root / 'proof' / 'output.txt').write_text('2\n')                   # beside the command, named nowhere
    (root / 'zzz-notes').mkdir()
    (root / 'zzz-notes' / 'own.txt').write_text('why 2\n')              # its fingerprinted file, last in name order
    tid = attach.create(store, root, 'Fix the total', 'claude')['id']
    attach.set_done(store, tid, ['the total is right'])
    attach.record_evidence(store, tid, 'D1', command='["python3","-m","py_compile","proof/check.py"]')
    attach.record_evidence(store, tid, 'D1', note='the reasoning', file='zzz-notes/own.txt')
    task, payload, size = reviewed(engine, store, tid)
    recorded = task['data']['development']
    changes = payload['observations']['workspace_changes']
    shown, loose = changes['changed_paths'], changes['untracked_paths']
    check('control: the task record really lists the whole folder', len(recorded['changed_paths']) > OTHERS
          and sum(len(p) for p in recorded['changed_paths']) > 500000, len(recorded['changed_paths']))
    check('the review prompt stays under %d characters' % PROMPT_LIMIT, size < PROMPT_LIMIT, size)
    check('each path list is within %d rows and %d characters' % (ROWS, CHARS),
          all(len(rows) <= ROWS and sum(len(p) for p in rows) <= CHARS for rows in (shown, loose)),
          (len(shown), sum(len(p) for p in shown), len(loose), sum(len(p) for p in loose)))
    check('the change to a tracked file comes first', shown[:1] == ['app.py'], shown[:3])
    check('the files the evidence names follow, wherever they sort by name',
          shown[1:3] == ['proof/check.py', 'zzz-notes/own.txt'], shown[:5])
    check('…then the file beside the evidence command', shown[3:4] == ['proof/output.txt'], shown[:6])
    check('…then the rest in name order: an argument that is not a file ("-m") promotes nothing',
          shown[4:6] == ['aaa-000.tmp', 'aaa-001.tmp'], shown[:8])
    check('the untracked list is ordered the same way', loose[:4] == ['proof/check.py', 'zzz-notes/own.txt', 'proof/output.txt', 'aaa-000.tmp'], loose[:6])
    cut = (changes.get('omitted') or {}).get('changed_paths') or {}
    check('what was left out is counted: shown + omitted = recorded',
          cut.get('of') == len(recorded['changed_paths']) and len(shown) + cut.get('omitted', 0) == len(recorded['changed_paths']), cut)
    check('…and by top-level folder, the project next door whole',
          (cut.get('by_top_level_folder') or {}).get('other-project') == OTHERS
          and sum((cut.get('by_top_level_folder') or {}).values()) == cut.get('omitted'), cut.get('by_top_level_folder'))
    check('the reviewer is told the lists are bounded', 'bounded' in changes['limits'] and 'omitted' in changes['limits'], changes['limits'])
    excerpts = [row.get('path') for row in payload['observations']['files']]
    check('the excerpted files are the task\'s own, the tracked change first',
          excerpts[:4] == ['app.py', 'proof/check.py', 'zzz-notes/own.txt', 'proof/output.txt']
          and 'TOTAL = 2' in payload['observations']['files'][0].get('text', ''), excerpts[:6])
    check('the review ran and the task is verified', task['status'] == 'verified', (task['status'], task['data'].get('reason')))

    print('② control: a folder of its own is listed whole, as recorded')
    root = make_repo(base, 'alone')
    (root / 'app.py').write_text('TOTAL = 2\n')
    (root / 'b-notes.txt').write_text('n\n')
    (root / 'a-notes.txt').write_text('n\n')
    tid = attach.create(store, root, 'Fix the total', 'claude')['id']
    attach.set_done(store, tid, ['the total is right'])
    attach.record_evidence(store, tid, 'D1', note='the reasoning', file='b-notes.txt')
    task, payload, size = reviewed(engine, store, tid)
    recorded = task['data']['development']
    changes = payload['observations']['workspace_changes']
    check('the lists are the recorded lists, in the recorded order',
          changes['changed_paths'] == recorded['changed_paths'] == ['a-notes.txt', 'app.py', 'b-notes.txt']
          and changes['untracked_paths'] == recorded['untracked_paths'], changes['changed_paths'])
    check('nothing is reported as omitted', 'omitted' not in changes and 'bounded' not in changes['limits'], changes)
    check('the excerpts keep the recorded order', [row.get('path') for row in payload['observations']['files']] == recorded['changed_paths'])
    check('the review ran and the task is verified', task['status'] == 'verified', task['status'])

    print('③ the budget is characters as well as rows')
    long_paths = ['deep/%s/%03d.txt' % ('d' * 380, index) for index in range(200)]     # 200 rows, 78,000 characters
    shown, cut = evidence.bounded_paths(long_paths, lambda path: 3)
    check('a list under the row budget is still cut at the character budget',
          cut is not None and len(shown) < 200 and sum(len(p) for p in shown) <= CHARS and len(shown) + cut['omitted'] == 200,
          (len(shown), cut))
    shown, cut = evidence.bounded_paths(['a/%03d' % index for index in range(ROWS)], lambda path: 3)
    check('control: a list exactly at the row budget is whole', cut is None and len(shown) == ROWS, (len(shown), cut))
    shown, cut = evidence.bounded_paths(['f%03d/x' % index for index in range(ROWS + 40)], lambda path: 3)   # a folder a path
    check('more than fifteen folders left out: the remainder is summed as other folders',
          cut is not None and sum(cut['by_top_level_folder'].values()) == cut['omitted'] == 40
          and len(cut['by_top_level_folder']) <= 16, cut)


if __name__ == '__main__':
    sys.exit(main())
