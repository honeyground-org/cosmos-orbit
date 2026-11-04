#!/usr/bin/env python3
"""Model-free check of context measurement and mid-task handoff, with a control for every rule.

Attached sessions: transcripts are written by the test in the Claude Code and Codex formats; the hooks run
in-process. Unattended work: a fake executor reports growing context, and the real loop must restart the
owned session from a ledger packet before the next turn. Exit 0 = pass.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH'):
    os.environ.pop(name, None)

from harness import attach, context, continuity, providers  # noqa: E402
from harness.engine import Engine  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def git(root, *args):
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True, capture_output=True)


def make_repo(base, name='repo'):
    root = base / name
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'app.py').write_text('print("hello")\n')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'files')
    return root


def claude_turn(path, tokens, sidechain=False, model='claude-test'):
    row = {'type': 'assistant', 'isSidechain': sidechain,
           'message': {'id': 'm%d' % tokens, 'model': model,
                       'usage': {'input_tokens': 5, 'cache_creation_input_tokens': 995, 'cache_read_input_tokens': tokens - 1000}}}
    with open(path, 'a') as stream:
        stream.write(json.dumps(row) + '\n')


def codex_turn(path, tokens, window=272000):
    row = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'last_token_usage': {'total_tokens': tokens},
                                                                            'model_context_window': window}}}
    with open(path, 'a') as stream:
        stream.write(json.dumps(row) + '\n')


def events(store, task_id):
    return [row['kind'] for row in store.events(task_id)]


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-continuity-'))
    store = Store(base / 'home')
    root = make_repo(base)
    attach.set_switch(store, 'on', root)

    print('① settings: parsing and precedence (environment → project → global → built-in), all in the settings file')
    check('200k · 1.5m · on · off', [continuity.parse_tokens(v) for v in ('200k', '1.5m', 'on', 'off', '250000')]
          == [200000, 1500000, continuity.DEFAULT_HANDOFF_AT, 0, 250000])
    try:
        continuity.parse_tokens('5k')
        check('a point below 20k is refused', False)
    except ValueError:
        check('a point below 20k is refused', True)
    settings = store.home / 'settings.json'
    check('built-in 350k, no settings file yet', continuity.resolve_threshold(store, root) == (350000, 'built-in')
          and continuity.DEFAULT_HANDOFF_AT == 350000 and not settings.exists())
    check('unattended work uses the same built-in point', context.DEFAULT_LIMIT == continuity.DEFAULT_HANDOFF_AT)
    continuity.set_threshold(store, '300k', global_default=True)
    check('global 300k', continuity.resolve_threshold(store, root) == (300000, 'default'))
    on_disk = json.loads(settings.read_text())
    check('the command wrote the settings file', on_disk.get('handoff', {}).get('at') == 300000, on_disk)
    check('…and nothing in the ledger database (one place)', store.db.execute(
        "SELECT count(*) FROM preferences WHERE key LIKE 'handoff%'").fetchone()[0] == 0)
    continuity.set_threshold(store, '200k', root)
    check('project beats global', continuity.resolve_threshold(store, root) == (200000, 'project'))
    os.environ['ORBIT_HANDOFF_AT'] = 'off'
    check('terminal session beats project', continuity.resolve_threshold(store, root) == (0, 'environment'))
    os.environ.pop('ORBIT_HANDOFF_AT')
    check('settings view carries the handoff point and the file', attach.settings_view(store, root)['handoff']['here']['handoff_at'] == 200000
          and attach.settings_view(store, root)['handoff']['settings_file']['path'] == str(settings))

    on_disk = json.loads(settings.read_text())
    on_disk['handoff']['projects'][str(root.resolve())] = '250k'
    on_disk['note'] = 'kept by Orbit'
    settings.write_text(json.dumps(on_disk))
    check('a hand edit applies at once ("250k" in the file)', continuity.resolve_threshold(store, root) == (250000, 'project'))
    continuity.set_handoff_mode(store, 'auto')
    on_disk = json.loads(settings.read_text())
    check('the mode lives in the same file, and keys Orbit does not own survive a write',
          on_disk['handoff'].get('mode') == 'auto' and on_disk.get('note') == 'kept by Orbit' and continuity.handoff_mode(store) == 'auto', on_disk)
    continuity.set_handoff_mode(store, 'tell')
    continuity.clear_threshold(store, root)
    check('reset removes the project point: the global one applies again', continuity.resolve_threshold(store, root) == (300000, 'default'))
    continuity.clear_threshold(store, global_default=True)
    check('reset --global: the built-in point applies again', continuity.resolve_threshold(store, root) == (350000, 'built-in'))

    on_disk['handoff']['at'] = 'lots'
    settings.write_text(json.dumps(on_disk))
    view = continuity.threshold_view(store, root)
    check('an unreadable value is ignored and named in the view', view['here']['source'] != 'default'
          and any('lots' in line for line in view['settings_file']['problems']), view['settings_file'])
    settings.write_text('{"handoff": {"at": 300000,')
    view = continuity.threshold_view(store, root)
    check('broken JSON: the hooks fall back to the built-in point and the view says why',
          view['here'] == {'handoff_at': 350000, 'source': 'built-in', 'off': False} and view['settings_file']['problems'], view)
    try:
        continuity.set_threshold(store, '400k', global_default=True)
        refused = False
    except ValueError:
        refused = True
    check('…and a command never overwrites the broken file (the user fixes it)', refused
          and settings.read_text() == '{"handoff": {"at": 300000,')

    legacy = Store(base / 'legacy-home')
    legacy.preference('handoff_at', '420000')
    legacy.preference('handoff_mode', 'auto')
    legacy.preference('handoff_at:' + str(root.resolve()), '180000')
    legacy.preference('provider', 'codex')
    check('an earlier version\'s points move into the file once', continuity.resolve_threshold(legacy, root) == (180000, 'project')
          and continuity.handoff_mode(legacy) == 'auto'
          and json.loads((legacy.home / 'settings.json').read_text())['handoff']['at'] == 420000)
    check('…leaving no copy in the database, and other preferences untouched (control)',
          legacy.db.execute("SELECT count(*) FROM preferences WHERE key LIKE 'handoff%'").fetchone()[0] == 0
          and legacy.preference('provider') == 'codex')

    settings.write_text('{}')
    continuity.set_threshold(store, '200k', global_default=True)  # the scenarios below drive sessions past 200k

    print('② measurement: Claude Code transcript and Codex rollout')
    t1 = base / 's1.jsonl'
    claude_turn(t1, 50_000)
    claude_turn(t1, 900_000, sidechain=True)  # a subagent's context is not the session's
    found = continuity.measure_transcript(t1)
    check('claude: latest main-thread turn, sidechain ignored', found and found['tokens'] == 50_000 and found['source'] == 'claude_transcript', found)
    rollout = base / 'rollout.jsonl'
    codex_turn(rollout, 123_456)
    found = continuity.measure_transcript(rollout)
    check('codex: last token_count with window', found and found['tokens'] == 123_456 and found['window'] == 272000, found)
    check('missing transcript → unknown, not zero', continuity.measure({'transcript_path': str(base / 'none.jsonl')}, 'claude') is None)

    print('③ attached session: brief once, handoff point, one checkpoint request, one message to the user')
    pay = {'cwd': str(root), 'session_id': 's1', 'transcript_path': str(t1)}
    out = attach.handle(store, 'prompt', dict(pay, prompt='Refactor app.py into a module with tests and keep the output identical'), 'claude')
    task = attach.current_task(store, root)
    tid = task['id']
    check('first prompt → task + full brief', out and out['hookSpecificOutput']['additionalContext'].startswith('[orbit] task'))
    check('context measured at 50k, session logged', task['data']['context_live']['tokens'] == 50_000 and len(task['data']['session_log']) == 1)
    again = attach.handle(store, 'prompt', dict(pay, prompt='continue with the module split please'), 'claude')
    check('unchanged brief is not repeated in the same session', again is None)
    claude_turn(t1, 120_000)
    check('below the point (120k) → still silent', attach.handle(store, 'prompt', dict(pay, prompt='and add the tests now please'), 'claude') is None)
    check('stop below the point → silent (control)', attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Working on it.'), 'claude') is None)
    claude_turn(t1, 210_000)
    out = attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining tests'), 'claude')
    brief = out['hookSpecificOutput']['additionalContext'] if out else ''
    check('at 210k the brief returns with the handoff line', 'CONTEXT 210k tokens ≥ handoff point 200k' in brief and ' checkpoint ' + tid in brief, brief[-400:])
    check('handoff_recommended recorded', 'handoff_recommended' in events(store, tid))
    stop = attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Tests are half written.'), 'claude')
    check('stop → one checkpoint request to the executor', stop and stop.get('decision') == 'block' and 'checkpoint ' + tid in stop['reason'], stop)
    check('stop again without a checkpoint → silent (asked once)', attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='still going'), 'claude') is None)
    check('stop right after the request (active) without a checkpoint → silent, never a second block', attach.handle(store, 'stop', dict(pay, stop_hook_active=True, last_assistant_message='still going'), 'claude') is None)
    (root / 'mod.py').write_text('def main():\n    print("hello")\n')
    row = continuity.record_checkpoint(store, tid, 'Write tests/test_mod.py for main()', notes='mod.py extracted; app.py still imports nothing', files=['mod.py'])
    check('checkpoint keeps next, notes, files, tokens and changed paths', row['tokens'] == 210_000 and row['files'][0]['path'] == 'mod.py' and 'mod.py' in row['changed'], row)
    check('handoff projection written to the store, not the repo', (store.home / 'runs' / tid / 'handoff.md').is_file() and not (root / 'HANDOFF.md').exists())
    told = attach.handle(store, 'stop', dict(pay, stop_hook_active=True, last_assistant_message='checkpoint saved'), 'claude')
    check('stop after the checkpoint → message to the user with /clear and /new', told and '/clear' in told.get('systemMessage', '') and '/new' in told['systemMessage'], told)
    check('told once', attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='ok'), 'claude') is None)

    print('④ fresh session after /clear: brief with the checkpoint, rotation recorded, no handoff line')
    t2 = base / 's2.jsonl'
    claude_turn(t2, 40_000)
    pay2 = {'cwd': str(root), 'session_id': 's2', 'transcript_path': str(t2)}
    attach.handle(store, 'session-end', {'cwd': str(root), 'session_id': 's1', 'reason': 'clear'}, 'claude')  # /clear ends s1
    start = attach.handle(store, 'session-start', dict(pay2, source='clear'), 'claude')
    brief = start['hookSpecificOutput']['additionalContext'] if start else ''
    check('brief: CONTINUED + CHECKPOINT next step', 'CONTINUED in a fresh context (previous session peaked at 210k' in brief and 'Write tests/test_mod.py' in brief, brief[-500:])
    check('no handoff line in the fresh session', 'CONTEXT ' not in brief and 'HANDOFF READY' not in brief)
    check('a full brief stays within 2400 characters and keeps the RECORD and CLAIM lines', len(brief) <= 2400 and 'RECORD evidence' in brief and 'CLAIM when done' in brief, len(brief))
    task = store.get(tid)
    rotated = [r['payload'] for r in store.events(tid) if r['kind'] == 'session_rotated']
    check('session_rotated: from s1 (peak 210k) to s2, checkpoint present', rotated and rotated[-1]['from_peak'] == 210_000 and rotated[-1]['checkpoint'] is True, rotated)
    check('session log: two sessions, second starts at 40k', [r['session'] for r in task['data']['session_log']] == ['s1', 's2'] and task['data']['session_log'][-1]['start'] == 40_000)
    check('first prompt after session start does not repeat the brief', attach.handle(store, 'prompt', dict(pay2, prompt='go on with the tests'), 'claude') is None)

    print('④-b stop after a block still measures; a claim settles the handoff without a checkpoint request')
    t6 = base / 's6.jsonl'
    claude_turn(t6, 60_000)
    pay6 = {'cwd': str(root), 'session_id': 's6', 'transcript_path': str(t6)}
    attach.handle(store, 'session-end', {'cwd': str(root), 'session_id': 's2'}, 'claude')  # the previous terminal closed
    attach.handle(store, 'session-start', dict(pay6, source='startup'), 'claude')
    claude_turn(t6, 270_000)
    check('stop_hook_active: no decision (control)', attach.handle(store, 'stop', dict(pay6, stop_hook_active=True, last_assistant_message='x'), 'claude') is None)
    task = store.get(tid)
    check('… but the context was measured and the point recorded', task['data']['context_live']['tokens'] == 270_000
          and (task['data'].get('handoff_state') or {}).get('session') == 's6', task['data']['context_live'])
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['verification_pending'] = True  # the executor has just claimed
        store.save(task, task['status'], 'claimed', {'finished': True})
    msg = attach.handle(store, 'stop', dict(pay6, stop_hook_active=False, last_assistant_message='claimed'), 'claude')
    text = (msg or {}).get('systemMessage', '')
    check('claim + handoff point → one merged message, no checkpoint request', msg and 'decision' not in msg and 'verification' in text.lower()
          and '/clear' in text and 'After the verdict' in text, msg)
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['verification_pending'] = False
        store.save(task, task['status'], 'test_reset', {})

    print('⑤ growth guard: a session that starts big is not handed off at once (anti-thrash)')
    t3 = base / 's3.jsonl'
    # `claude --resume` of s6 under a new id: the transcript keeps s6's entries (the lineage that finds its task)
    t3.write_text(json.dumps({'type': 'user', 'sessionId': 's6', 'message': {'role': 'user', 'content': 'earlier'}}) + '\n')
    claude_turn(t3, 190_000)
    pay3 = {'cwd': str(root), 'session_id': 's3', 'transcript_path': str(t3)}
    attach.handle(store, 'session-end', {'cwd': str(root), 'session_id': 's6'}, 'claude')
    attach.handle(store, 'session-start', dict(pay3, source='resume'), 'claude')
    claude_turn(t3, 215_000)
    attach.handle(store, 'prompt', dict(pay3, prompt='next step on the tests please'), 'claude')
    task = store.get(tid)
    check('190k start → limit 250k, 215k does not trigger', task['data']['context_live']['limit'] == 250_000 and (task['data'].get('handoff_state') or {}).get('session') != 's3', task['data']['context_live'])
    claude_turn(t3, 255_000)
    attach.handle(store, 'prompt', dict(pay3, prompt='finish the tests and run them'), 'claude')
    check('255k → handoff recommended for s3', (store.get(tid)['data'].get('handoff_state') or {}).get('session') == 's3')

    print('⑥ switches: off silences the handoff (control), terminal session override')
    continuity.set_threshold(store, 'off', root)
    t4 = base / 's4.jsonl'
    claude_turn(t4, 400_000)
    pay4 = {'cwd': str(root), 'session_id': 's4', 'transcript_path': str(t4)}
    attach.handle(store, 'session-end', {'cwd': str(root), 'session_id': 's3'}, 'claude')
    start = attach.handle(store, 'session-start', dict(pay4, source='startup'), 'claude')
    stop = attach.handle(store, 'stop', dict(pay4, stop_hook_active=False, last_assistant_message='working'), 'claude')
    check('project off: 400k → no CONTEXT line, no request', 'CONTEXT ' not in start['hookSpecificOutput']['additionalContext'] and stop is None)
    os.environ['ORBIT_HANDOFF_AT'] = '300k'
    claude_turn(t4, 470_000)  # this session started at 400k: the growth guard puts its point at 460k
    attach.handle(store, 'prompt', dict(pay4, prompt='one more change to the tests'), 'claude')
    os.environ.pop('ORBIT_HANDOFF_AT')
    task = store.get(tid)
    check('ORBIT_HANDOFF_AT=300k in this terminal re-enables it', task['data']['context_live']['threshold_source'] == 'environment'
          and (task['data'].get('handoff_state') or {}).get('session') == 's4', task['data']['context_live'])
    continuity.set_threshold(store, '200k', root)

    print('⑦ native compaction: automatic checkpoint when none was written, full brief again afterwards')
    t5 = base / 's5.jsonl'
    claude_turn(t5, 60_000)
    pay5 = {'cwd': str(root), 'session_id': 's5', 'transcript_path': str(t5)}
    attach.handle(store, 'session-end', {'cwd': str(root), 'session_id': 's4'}, 'claude')
    attach.handle(store, 'session-start', dict(pay5, source='startup'), 'claude')
    claude_turn(t5, 150_000)
    before = len(store.get(tid)['data'].get('checkpoints') or [])
    attach.handle(store, 'pre-compact', dict(pay5, trigger='auto'), 'claude')
    task = store.get(tid)
    auto = task['data']['checkpoints'][-1]
    check('pre-compact → auto checkpoint by orbit', len(task['data']['checkpoints']) == before + 1 and auto['kind'] == 'auto' and auto['by'] == 'orbit', auto)
    check('compacted recorded', 'compacted' in events(store, tid))
    claude_turn(t5, 30_000)
    out = attach.handle(store, 'prompt', dict(pay5, prompt='continue after the compaction'), 'claude')
    check('after compaction the next prompt gets the full brief again', out is not None)
    attach.handle(store, 'pre-compact', dict(pay5, trigger='auto'), 'claude')
    check('second compaction in the same session: no duplicate auto checkpoint', len(store.get(tid)['data']['checkpoints']) == before + 1)

    print('⑧ Codex host: measurement from the rollout through the hook')
    root2 = make_repo(base, 'repo2')
    attach.set_switch(store, 'on', root2)
    roll = base / 'codex-rollout.jsonl'
    codex_turn(roll, 70_000)
    payc = {'cwd': str(root2), 'session_id': 'c1', 'transcript_path': str(roll)}
    attach.handle(store, 'prompt', dict(payc, prompt='Count words separated by any whitespace in words.py'), 'codex')
    codex_turn(roll, 205_000)
    out = attach.handle(store, 'prompt', dict(payc, prompt='continue with the tab and newline cases'), 'codex')
    task2 = attach.current_task(store, root2)
    check('codex: 205k from the rollout → handoff line', out and 'CONTEXT 205k' in out['hookSpecificOutput']['additionalContext']
          and task2['data']['context_live']['window'] == 272000, task2['data'].get('context_live'))

    print('⑨ unattended loop: the next turn starts a fresh session from a slim ledger packet')
    from harness.development import create
    root3 = make_repo(base, 'repo3')
    managed = create(store, root3, 'claude', 'Add a greeting option to app.py', [], ['--greeting prints the greeting'], 4)
    managed['data']['options'].update(context_budget=200_000, auto_handoff=True)
    managed['data']['memory'] = {'mode': 'off'}
    store.save(managed, 'received', 'options_recorded', managed['data']['options'])
    calls = []

    class FakeExecutor:
        def __init__(self, name, home, model=None, timeout=None):
            self.name, self.home = name, Path(home)

        def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
            calls.append({'session': session, 'handoff': 'Historical handoff, not authority:' in prompt, 'prompt': prompt})
            n = len(calls)
            log = self.home / 'runs' / task_id / ('fake%d.events.jsonl' % n)
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(json.dumps({'type': 'item.completed', 'item': {'id': 'x%d' % n, 'type': 'command_execution', 'command': 'ls', 'exit_code': 0}}) + '\n')
            (Path(workspace) / ('step%d.txt' % n)).write_text(str(n))  # progress, so the loop does not call it stalled
            tokens = {1: 120_000, 2: 205_000, 3: 60_000}.get(n, 70_000)
            answer = {'summary': 'turn %d' % n, 'finished': False, 'remaining': ['more work'], 'question': '' if n < 3 else 'stop here for the test',
                      'done_proposed': []}
            metadata = {'call_id': 'c%d' % n, 'session': 'exec-%d' % (1 if n < 3 else n), 'provider': self.name, 'requested_model': None,
                        'reported_models': ['fake'], 'context': {'tokens': tokens, 'source': 'fake', 'window': None},
                        'seconds': 0.1, 'usage': {}, 'log': str(log), 'invocation': ''}
            return answer, metadata

    original = providers.Provider
    providers.Provider = FakeExecutor
    try:
        Engine(store).run(managed['id'])
    finally:
        providers.Provider = original
    managed = store.get(managed['id'])
    kinds = events(store, managed['id'])
    check('three executor turns ran', len(calls) == 3, [c['session'] for c in calls])
    check('turn 2 resumed the owned session (120k < 200k, control)', calls[1]['session'] == 'exec-1' and not calls[1]['handoff'], [(c['session'], c['handoff']) for c in calls])
    check('turn 3 started fresh with the ledger packet (205k ≥ 200k)', calls[2]['session'] is None and calls[2]['handoff'], calls[2]['session'])
    check('automatic_checkpoint recorded once', kinds.count('automatic_checkpoint') == 1, kinds)
    packet = managed['data']['handoffs']['claude']
    check('packet is slim: no reviewer traces, no contract dump', 'observations' not in json.dumps(packet.get('verdict') or {})
          and 'contract' not in packet and packet['development'] and set(packet['development']) == {'source', 'base', 'checks', 'changed_paths'}, sorted(packet))
    check('packet carries goal, done and the executor remaining work', packet['goal'] and packet['done'] and packet['executor_result']['remaining'] == ['more work'])
    check('packet linked to the fresh session', packet.get('to_session') == 'exec-3')
    task = managed
    task['data']['context_measurements']['claude'] = {'tokens': 230_000, 'session': 'exec-3'}
    task['data']['context_start']['claude'] = 200_000
    check('anti-thrash: fresh session that started at 200k is not handed off at 230k', not continuity.rollover_due(store, task, 'claude'))
    task['data']['options']['auto_handoff'] = False
    task['data']['context_start']['claude'] = 10_000
    check('auto_handoff false → never (control)', not continuity.rollover_due(store, task, 'claude'))

    print('⑩ status and dashboard projections')
    from harness.observer import snapshot, terminal
    snap = snapshot(store, tid)['tasks'][0]
    check('snapshot: context_live, checkpoints, session log', snap['context_live']['tokens'] and len(snap['checkpoints']) >= 2 and len(snap['session_log']) >= 4, snap['context_live'])
    check('short event list skips context_measured noise', all(e['kind'] != 'context_measured' for e in snap['events']))
    check('terminal twin prints the context line', 'context: ' in terminal(snapshot(store), 'en'))
    md = attach.handoff_markdown(store, store.get(tid))
    check('handoff markdown: latest checkpoint and sessions', '## Latest checkpoint' in md and '## Sessions' in md)
    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
