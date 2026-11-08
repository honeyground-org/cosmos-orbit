#!/usr/bin/env python3
"""Model-free check that the list follows a conversation without friction, measured on this Mac's own sessions.

N1 a `codex exec` run a Claude Code session launched is that session's sub-task, not a task of its own.
N2 items the last review found met no longer take one of the 12 places.
N3 the executor confirms or extends the list in the same command that claims.
N4 the claim names the items only a note backs (the reviewer cannot see this machine).
Every rule has a control.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH'):
    os.environ.pop(name, None)

for _marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):  # these runs stand for sessions a person opened,
    os.environ.pop(_marker, None)                                    # not ones an agent delegated (attach.delegated)
from harness import attach, requirements  # noqa: E402
from harness.store import Store  # noqa: E402
from verify_living_checklist import check, FAILED, make_repo, user_says  # noqa: E402

CLAUDE_ENV = {'CLAUDECODE': '1', 'CLAUDE_CODE_SESSION_ID': 'parent-session'}


def transcript(base, name, source, originator):
    path = base / (name + '.jsonl')
    path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': name, 'source': source, 'originator': originator}}) + '\n')
    return str(path)


def met_verdict(task, ids, status='met'):
    task['data'].setdefault('verdicts', []).append(
        {'items': [{'id': i, 'status': status, 'evidence': ['seen']} for i in ids], 'turn': 1})


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-natural-'))
    store = Store(base / 'home')
    run(base, store)
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'all checks passed') + ' · ' + str(base))
    return 1 if FAILED else 0


def run(base, store):
    print('N1 a codex exec run from a Claude Code session is not a task of its own')
    exec_t = transcript(base, 'exec', 'exec', 'codex_exec')
    tui_t = transcript(base, 'tui', 'cli', 'codex-tui')
    check('codex exec + Claude marks → delegated', attach.delegated('codex', {'transcript_path': exec_t}, CLAUDE_ENV))
    check('control: a Codex pane a person opened (source cli) attaches even with inherited marks',
          attach.delegated('codex', {'transcript_path': tui_t}, CLAUDE_ENV) is None)
    check('control: codex exec without a Claude session around it attaches',
          attach.delegated('codex', {'transcript_path': exec_t}, {}) is None)
    check('control: no readable transcript → unsure → attaches',
          attach.delegated('codex', {'transcript_path': str(base / 'missing.jsonl')}, CLAUDE_ENV) is None
          and attach.delegated('codex', {}, CLAUDE_ENV) is None)
    check('control: Claude Code hooks are never judged by this rule',
          attach.delegated('claude', {'transcript_path': exec_t}, CLAUDE_ENV) is None)

    root = make_repo(base, 'repo-n1')
    attach.set_switch(store, 'on', root)
    payload = {'cwd': str(root), 'session_id': 'astra-1', 'transcript_path': exec_t, 'source': 'startup',
               'prompt': 'Review the plan and report'}
    saved = {k: os.environ.get(k) for k in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'ORBIT')}
    try:
        os.environ.update(CLAUDE_ENV)
        attach.handle(store, 'session-start', payload, 'codex')
        attach.handle(store, 'prompt', payload, 'codex')
        check('the delegated run made no task', attach.current_task(store, root) is None)
        check('…and says so once in hooks.log with the way back', 'not attached (a codex exec run' in
              (store.home / 'hooks.log').read_text() and 'ORBIT=on' in (store.home / 'hooks.log').read_text())
        os.environ['ORBIT'] = 'on'
        attach.handle(store, 'session-start', dict(payload, session_id='astra-2'), 'codex')
        attach.handle(store, 'prompt', dict(payload, session_id='astra-2'), 'codex')
        check('control: an explicit ORBIT=on still attaches it', attach.current_task(store, root) is not None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    root_b = make_repo(base, 'repo-n1b')
    attach.set_switch(store, 'on', root_b)
    attach.handle(store, 'session-start', dict(payload, cwd=str(root_b), session_id='person-1'), 'codex')
    attach.handle(store, 'prompt', dict(payload, cwd=str(root_b), session_id='person-1'), 'codex')
    check('control: the same run without Claude marks attaches', attach.current_task(store, root_b) is not None)

    print('N2 items the last review found met leave the 12 places')
    root = make_repo(base, 'repo-n2')
    tid = attach.create(store, root, 'Ship the report', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, tid, ['item %d works' % n for n in range(1, 13)])
    try:
        attach.set_done(store, tid, ['item 13 works'], add=True)
        refused = ''
    except ValueError as error:
        refused = str(error)
    check('control: 12 open items with no review → a 13th is refused, naming each item', 'D1 open' in refused, refused[:200])
    with store.lock('task:' + tid):
        task = store.get(tid)
        met_verdict(task, ['D%d' % n for n in range(1, 7)])
        store.save(task, task['status'], 'test_setup', {})
    task = attach.set_done(store, tid, ['item 13 works'], add=True)
    ids = [d['id'] for d in task['data']['done'] if d['id'].startswith('D')]
    check('6 met → the 13th is added and keeps a new ID', ids[-1] == 'D13' and len(ids) == 13, ids)
    check('…met items stay open (only a clean review closes them)', 'D1' in ids and not task['data'].get('done_closed'))
    with store.lock('task:' + tid):
        task = store.get(tid)
        met_verdict(task, ['D%d' % n for n in range(1, 14)], status='unclear')
        store.save(task, task['status'], 'test_setup', {})
    try:
        attach.set_done(store, tid, ['item 14 works'], add=True)
        later = ''
    except ValueError as error:
        later = str(error)
    check('control: a later review that found them unclear counts them again', 'At most 12' in later, later[:120])

    with store.lock('task:' + tid):
        task = store.get(tid)
        met_verdict(task, ['D%d' % n for n in range(1, 14)])
        task['data']['verdicts'][-1]['superseded'] = 'own_files'
        store.save(task, task['status'], 'test_setup', {})
    try:
        attach.set_done(store, tid, ['item 14 works'], add=True)
        set_aside = ''
    except ValueError as error:
        set_aside = str(error)
    check('control: a verdict set aside (superseded) vouches for nothing', 'At most 12' in set_aside, set_aside[:120])

    print('N2b items backed by a passing command Orbit ran leave the 12 places (no review needed yet)')
    # 2026-11-08: every claim in a session ended in a question, so no review ran; eight items already backed by
    # passing commands kept two new requests out of the list.
    root = make_repo(base, 'repo-n2b')
    tid = attach.create(store, root, 'Ship the report', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, tid, ['item %d works' % n for n in range(1, 13)])

    def add(text):
        try:
            attach.set_done(store, tid, [text], add=True)
            return ''
        except ValueError as error:
            return str(error)
    for n in range(1, 9):
        attach.record_evidence(store, tid, 'D%d' % n, note='by hand')
    check('control: notes alone free no place (the reviewer cannot check them)', 'At most 12' in add('item 13 works'))
    for n in range(1, 8):
        attach.record_evidence(store, tid, 'D%d' % n, command='["true"]')
    (root / 'report.txt').write_text('ok\n')
    attach.record_evidence(store, tid, 'D8', file='report.txt')
    refused = add('item 13 works')
    check('7 passing commands + 1 file → a 13th item is added', refused == '', refused[:200])
    check('…and a 14th', add('item 14 works') == '')
    ids = [d['id'] for d in store.get(tid)['data']['done'] if d['id'].startswith('D')]
    check('…proven items stay open for the review', 'D1' in ids and len(ids) == 14, ids)
    for n in range(1, 7):                   # 6 failing + D9..D14 unproven = 12; a 13th to prove is refused
        attach.record_evidence(store, tid, 'D%d' % n, command='["false"]')
    over = add('item 15 works')
    check('control: an item whose latest command failed counts again', 'At most 12' in over and 'D1 open' in over
          and 'D7 proven' in over, over[:300])
    for n in range(1, 15):
        attach.record_evidence(store, tid, 'D%d' % n, command='["true"]')
    for n in range(15, 25):
        check_text = add('item %d works' % n)
        if check_text:
            break
    check('24 open items in all, every one proven, are accepted', not check_text, check_text[:200])
    total = add('item 25 works')
    check('control: a 25th is refused even when every item is proven (24 in all)', 'and 24 open in all' in total, total[:200])

    print('N3 confirm or extend the list in the claim itself')
    root = make_repo(base, 'repo-n3')
    tid = attach.create(store, root, 'Build the export', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, tid, ['CSV export downloads'])
    attach.record_evidence(store, tid, 'D1', command='["python3","verify.py"]')
    user_says(store, tid, 'Also make sure the header row is bold')
    cli = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(store.home)]
    # every claim here reports remaining work: no review is queued, so no background worker takes the task lock
    hold = ['--remaining', 'the review follows later']
    plain = subprocess.run(cli + ['claim', tid, '--summary', 'done'], capture_output=True, text=True)  # refused: nothing queued
    check('control: a plain claim with a pending change is refused and shows the one-step form',
          plain.returncode != 0 and '--keep' in plain.stderr and 'claim ' + tid in plain.stderr, plain.stderr[-300:])
    empty = subprocess.run(cli + ['claim', tid, '--summary', 'done', '--keep', ' '] + hold, capture_output=True, text=True)
    check('control: --keep without a reason is refused and nothing moves',
          empty.returncode != 0 and store.get(tid)['data'].get('requirements_pending'))
    added = subprocess.run(cli + ['claim', tid, '--summary', 'done', '--add', 'Header row is bold'] + hold,
                           capture_output=True, text=True)
    task = store.get(tid)
    ids = [d['id'] for d in task['data']['done'] if d['id'].startswith('D')]
    check('claim --add revises then claims in one command', added.returncode == 0 and ids == ['D1', 'D2']
          and not task['data'].get('requirements_pending') and task['data'].get('claimed_at'), added.stderr[-300:])
    check('…the user\'s own words stay with the revision', 'header row is bold' in json.dumps(task['data']['done_history'][-1]),
          task['data']['done_history'][-1])
    user_says(store, tid, 'Thanks, and keep the column order as it is')
    kept = subprocess.run(cli + ['claim', tid, '--summary', 'done', '--keep', 'D1 already keeps the column order'] + hold,
                          capture_output=True, text=True)
    task = store.get(tid)
    row = task['data']['done_history'][-1]
    check('claim --keep records why and claims', kept.returncode == 0 and not task['data'].get('requirements_pending')
          and row.get('because') == 'D1 already keeps the column order', (kept.stderr[-300:], row.get('because')))

    user_says(store, tid, 'Also add a totals row')
    rev = store.get(tid)['data']['done_rev']
    with store.lock('task:' + tid):        # another process holds the task: the one-step claim must change nothing
        busy = subprocess.run(cli + ['claim', tid, '--summary', 'done', '--add', 'Totals row is shown'] + hold,
                              capture_output=True, text=True)
    task = store.get(tid)
    check('a one-step claim that cannot take the lock leaves list and claim untouched',
          busy.returncode != 0 and task['data']['done_rev'] == rev and task['data'].get('requirements_pending'),
          (busy.returncode, task['data']['done_rev'], rev))
    try:
        attach.claim(store, tid, 'done', settle={'add': ['item %d' % n for n in range(20)]})
        capped = ''
    except ValueError as error:
        capped = str(error)
    task = store.get(tid)
    check('a revision the claim cannot make (over the limit) refuses the claim too',
          'At most 12' in capped and task['data']['done_rev'] == rev and task['data'].get('requirements_pending'))
    full = attach.claim(store, tid, 'done', settle={'add': ['Totals row is shown']})
    check('a finished one-step claim queues the independent review', full['data'].get('verification_pending')
          and not full['data'].get('requirements_pending') and full['data']['done_rev'] == rev + 1)
    with store.lock('task:' + tid):        # leave the queued review out of the rest of this suite
        task = store.get(tid)
        task['data']['verification_pending'] = False
        store.save(task, 'working', 'test_setup', {})

    print('N4 the claim names items only a note backs')
    check('D2 has no evidence: not named (nothing to warn about yet)', 'Only a note backs' not in kept.stderr)
    attach.record_evidence(store, tid, 'D2', note='looked at it, the header is bold')
    noted = subprocess.run(cli + ['claim', tid, '--summary', 'done'] + hold, capture_output=True, text=True)
    check('note-only D2 is named with the command to fix it', 'Only a note backs D2' in noted.stderr
          and 'evidence ' + tid + ' D2 --command' in noted.stderr, noted.stderr[-300:])
    check('control: D1, backed by a command Orbit ran, is not named', 'D1' not in noted.stderr.split('Only a note backs')[-1].split(':')[0])
    check('…and the claim is not blocked by it', noted.returncode == 0, noted.stderr[-200:])
    check('the warning is in the claim\'s stdout JSON too (a stderr-only line was filtered away on 2026-11-08)',
          'Only a note backs D2' in (json.loads(noted.stdout).get('warning') or ''), noted.stdout[:200])

    print('N5 evidence the reviewer can check: hints on stdout, --cwd, a failing command named')
    root = Path(store.get(tid)['workspace'])
    note_only = subprocess.run(cli + ['evidence', tid, 'D2', '--note', 'checked by hand'], capture_output=True, text=True)
    check('a note-only record says on stdout that the sandboxed reviewer cannot check it',
          'sandboxed' in ' '.join(json.loads(note_only.stdout).get('warning') or []), note_only.stdout[-300:])
    other = base / 'other-repo'
    other.mkdir(exist_ok=True)
    (other / 'marker.txt').write_text('here\n')
    elsewhere = subprocess.run(cli + ['evidence', tid, 'D2', '--command', '["cat","marker.txt"]', '--cwd', str(other)],
                               capture_output=True, text=True)
    row = json.loads(elsewhere.stdout)['recorded']
    check('--cwd runs the command in another folder and records where', row['exit_code'] == 0
          and 'here' in row['output_tail'] and row['cwd'] == str(other.resolve()) and 'warning' not in json.loads(elsewhere.stdout),
          elsewhere.stdout[-300:])
    wrong = subprocess.run(cli + ['evidence', tid, 'D2', '--command', '["cat","marker.txt"]'], capture_output=True, text=True)
    out = json.loads(wrong.stdout)
    check('control: without --cwd it runs in the workspace, fails, and the failure is named',
          out['recorded']['exit_code'] != 0 and 'cwd' not in out['recorded']
          and any('exited' in w for w in out.get('warning') or []), wrong.stdout[-300:])
    outside = subprocess.run(cli + ['evidence', tid, 'D2', '--file', str(other / 'marker.txt')], capture_output=True, text=True)
    check('a file outside the workspace is refused with the command that reads it instead',
          outside.returncode != 0 and '"cat"' in outside.stderr, outside.stderr[-300:])
    missing = subprocess.run(cli + ['evidence', tid, 'D2', '--command', '["true"]', '--cwd', str(base / 'nowhere')],
                             capture_output=True, text=True)
    check('--cwd that does not exist is refused', missing.returncode != 0 and '--cwd' in missing.stderr, missing.stderr[-200:])
    check('the brief prefers a command and says why', 'cannot check a note alone' in attach.brief_text(store, store.get(tid)))


if __name__ == '__main__':
    sys.exit(main())
