#!/usr/bin/env python3
"""Model-free check that the list follows a conversation without friction, measured on this Mac's own sessions.

N1 a `codex exec` run a Claude Code session launched is that session's sub-task, not a task of its own.
N2 the guide of 12 items to prove (24 open in all) is advice: a revision past it is saved and the executor is told;
   items the last review found met, or that a passing command Orbit ran backs, do not count toward it.
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

    print('N2 the guide of 12 refuses nothing; items the last review found met do not count toward it')
    root = make_repo(base, 'repo-n2')
    tid = attach.create(store, root, 'Ship the report', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, tid, ['item %d works' % n for n in range(1, 13)])

    def add(text, task_id=None):
        """Add one item. Returns (open D ids, the revision's over_guide or None, the advice or '')."""
        task = attach.set_done(store, task_id or tid, [text], add=True)
        row = task['data']['done_history'][-1]
        return ([d['id'] for d in task['data']['done'] if d['id'].startswith('D')], row.get('over_guide'),
                requirements.guide_notice(row, task_id or tid) or '')
    ids, over, notice = add('item 13 works')
    check('12 items to prove, no review → a 13th is saved and keeps a new ID (2026-11-20: it was refused)',
          ids[-1] == 'D13' and len(ids) == 13, ids)
    check('…the revision records that it passed the guide, and the advice says so with the way to shorten the list',
          over == {'to_prove': 13, 'open': 13} and '13 items still to prove (guide 12)' in notice
          and 'nothing is refused' in notice and 'done %s --replace' % tid in notice, (over, notice[:200]))
    with store.lock('task:' + tid):
        task = store.get(tid)
        met_verdict(task, ['D%d' % n for n in range(1, 7)])
        store.save(task, task['status'], 'test_setup', {})
    ids, over, notice = add('item 14 works')
    check('6 met → 8 still to prove: the next item brings no advice', len(ids) == 14 and over is None and notice == '', (over, notice))
    check('…met items stay open (only a clean review closes them)', 'D1' in ids and not store.get(tid)['data'].get('done_closed'))
    with store.lock('task:' + tid):
        task = store.get(tid)
        met_verdict(task, ['D%d' % n for n in range(1, 15)], status='unclear')
        store.save(task, task['status'], 'test_setup', {})
    ids, over, notice = add('item 15 works')
    check('control: a later review that found them unclear counts them again',
          over == {'to_prove': 15, 'open': 15} and len(ids) == 15, over)

    with store.lock('task:' + tid):
        task = store.get(tid)
        met_verdict(task, ['D%d' % n for n in range(1, 16)])
        task['data']['verdicts'][-1]['superseded'] = 'own_files'
        store.save(task, task['status'], 'test_setup', {})
    ids, over, notice = add('item 16 works')
    check('control: a verdict set aside (superseded) vouches for nothing',
          over == {'to_prove': 16, 'open': 16} and len(ids) == 16, over)
    task = attach.set_done(store, tid, [], keep=True)
    check('control: a revision that adds nothing brings no advice, however long the list is',
          requirements.guide_notice(task['data']['done_history'][-1], tid) is None, task['data']['done_history'][-1])

    print('N2b items backed by a passing command Orbit ran do not count toward the guide (no review needed yet)')
    # 2026-11-08: every claim in a session ended in a question, so no review ran; eight items already backed by
    # passing commands kept two new requests out of the list (the 12 was a limit then).
    root = make_repo(base, 'repo-n2b')
    tid = attach.create(store, root, 'Ship the report', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, tid, ['item %d works' % n for n in range(1, 13)])
    for n in range(1, 9):
        attach.record_evidence(store, tid, 'D%d' % n, note='by hand')
    ids, over, notice = add('item 13 works')
    check('control: notes alone count as still to prove (the reviewer cannot check them)',
          over == {'to_prove': 13, 'open': 13} and len(ids) == 13, over)
    for n in range(1, 8):
        attach.record_evidence(store, tid, 'D%d' % n, command='["true"]')
    (root / 'report.txt').write_text('ok\n')
    attach.record_evidence(store, tid, 'D8', file='report.txt')
    ids, over, notice = add('item 14 works')
    check('7 passing commands + 1 file → 6 still to prove: a 14th item brings no advice', over is None and notice == '', over)
    check('…proven items stay open for the review', 'D1' in ids and len(ids) == 14, ids)
    for n in range(1, 7):                   # 6 failing + D9..D14 unproven = 12; one more makes 13 to prove
        attach.record_evidence(store, tid, 'D%d' % n, command='["false"]')
    ids, over, notice = add('item 15 works')
    check('control: an item whose latest command failed counts again', over == {'to_prove': 13, 'open': 15}, over)
    for n in range(1, 16):
        attach.record_evidence(store, tid, 'D%d' % n, command='["true"]')
    quiet = []
    for n in range(16, 25):
        ids, over, notice = add('item %d works' % n)
        quiet.append(over)
    check('24 open items in all, at most 9 of them still to prove: no advice', len(ids) == 24 and not any(quiet), quiet)
    ids, over, notice = add('item 25 works')
    check('a 25th is saved too; the advice names the guide of 24 open in all (10 to prove is within the 12)',
          len(ids) == 25 and over == {'to_prove': 10, 'open': 25} and '25 open in all (guide 24)' in notice, (over, notice[:200]))

    print('N2c the advice reaches the executor on stdout, and the command still succeeds')
    orbit = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(store.home)]
    grown = subprocess.run(orbit + ['done', tid, '--add', 'item 26 works'], capture_output=True, text=True)
    out = json.loads(grown.stdout) if grown.returncode == 0 else {}
    check('done --add past the guide exits 0, saves the item and carries the advice as `warning`',
          grown.returncode == 0 and out.get('done_rev') == store.get(tid)['data']['done_rev']
          and any(d['text'] == 'item 26 works' for d in store.get(tid)['data']['done'])
          and 'past its guide' in (out.get('warning') or '') and 'past its guide' in grown.stderr, grown.stderr[-300:])
    root_c = make_repo(base, 'repo-n2c')
    small = attach.create(store, root_c, 'Ship the note', 'claude', checks=['["python3","verify.py"]'])['id']
    attach.set_done(store, small, ['the note exists'])
    short = subprocess.run(orbit + ['done', small, '--add', 'the note is signed'], capture_output=True, text=True)
    check('control: under the guide the same command prints no advice',
          short.returncode == 0 and 'warning' not in json.loads(short.stdout) and 'guide' not in short.stderr, short.stderr[-200:])
    many = subprocess.run(orbit + ['claim', small, '--summary', 'done', '--remaining', 'the review follows later']
                          + [arg for n in range(3, 15) for arg in ('--add', 'part %d holds' % n)], capture_output=True, text=True)
    out = json.loads(many.stdout) if many.returncode == 0 else {}
    open_small = [d['id'] for d in store.get(small)['data']['done'] if d['id'].startswith('D')]
    check('claim --add past the guide goes through (14 open) and carries the advice',
          many.returncode == 0 and len(open_small) == 14 and store.get(small)['data'].get('claimed_at')
          and '14 items still to prove (guide 12)' in (out.get('warning') or ''), (many.stderr[-300:], open_small))
    wide = attach.create(store, make_repo(base, 'repo-n2d'), 'Ship the atlas', 'claude',
                         done=['page %d renders' % n for n in range(1, 21)])
    check('a list given whole at the start is not limited to twelve either (20 items)',
          len([d for d in wide['data']['done'] if d['id'].startswith('D')]) == 20, len(wide['data']['done']))

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
        attach.claim(store, tid, 'done', settle={'add': ['x' * 2001]})
        invalid = ''
    except ValueError as error:
        invalid = str(error)
    task = store.get(tid)
    check('a revision the claim cannot make (an item over 2000 characters) refuses the claim too',
          'at most 2000 characters' in invalid and task['data']['done_rev'] == rev and task['data'].get('requirements_pending'),
          invalid[:120])
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
