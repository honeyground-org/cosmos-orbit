#!/usr/bin/env python3
"""Model-free check of the subagent limit (§Subagents): at most 5 at once per Claude Code session, the next refused with
the reason, every start, refusal and end recorded, and the session told when it starts what a subagent costs.

Why (measured 2026-11-24, Claude Code 2.1.331, three Agent calls in one message with every hook input saved): the
PreToolUse hooks of parallel Agent calls run within 80 ms of each other; every Agent call returns at once and the
subagent works on; Claude Code writes `<session>/subagents/agent-<id>.meta.json` at launch with the call's toolUseId and
`"finished": true` once it returned; a refusal reaches the model as the deny reason; a subagent that only replied "OK"
used 26,314 tokens. Replayed here through the real hook command line, with the folder laid out as Claude Code writes it:

  ① six Agent calls in one message, their six hooks at once: five start (counted 1..5, each once), the sixth is refused
  ② the folder decides what runs: launched ones hold their places; one that finished frees its place; an agent started
    without an Agent call (a workflow) is counted; a teammate counts while its team lists it, not after; a call whose
    subagent never appeared is dropped after 120 s; an unfinished one silent for an hour is gone; SessionEnd ends all
  ③ the limit: `context subagents 2` refuses the third, `off` answers nothing, `reset` is 5 again, a bad value is refused
  ④ silent where it must be: a shell call, Codex, a folder where Orbit is off, the same call answered twice
  ⑤ hooks install: Claude Code's PreToolUse takes Bash|Agent|Task, Codex's stays Bash; an old Bash-only entry is
    reported outdated with the fix
  ⑥ the session is told at SessionStart (after the rest of the brief), not where the limit is off, not in Codex
  ⑦ the record: subagent_started, subagent_refused and subagent_ended on the session's task
  ⑧ after a lead's /clear handoff (measured 2026-11-26): its old team is gone and its teammates go on in the new
    folder; one whose turn ended waits and is not counted (one waiting on a tool call is); a teammate's .meta.json has
    no toolUseId, so its place is matched by name and counted once; a refusal names every subagent it counted

Nothing starts a model or touches the live Orbit home or host settings: the store and the Claude Code folder are
temporary.
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
for _name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_OFFLOAD', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIDTURN', 'ORBIT_HANDOFF_CEILING',
              'HARNESS_HOME', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID', 'CMUX_SURFACE_ID', 'CMUX_BUNDLED_CLI_PATH', 'CLAUDECODE',
              'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
    os.environ.pop(_name, None)
CLI = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit')]
from harness import attach  # noqa: E402
from harness.store import Store  # noqa: E402
from harness.teammates import subagents_dir  # noqa: E402
try:
    from harness import subagents  # noqa: E402
except ImportError:                 # main before this change: the checks below fail, they do not crash
    subagents = None

FAILED = []
TEAM = 'session-s1'


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def env_for(base):
    return dict(os.environ, CLAUDE_CONFIG_DIR=str(base / 'claude'), CODEX_HOME=str(base / 'codex'))


def run(base, home, args, stdin=None):
    return subprocess.run(CLI + ['--home', str(home)] + list(args), input=stdin, capture_output=True, text=True,
                          cwd=str(base), env=env_for(base), timeout=120)


def hook(base, home, payload, host='claude'):
    out = run(base, home, ['hook', 'pre-tool', '--host', host], stdin=json.dumps(payload)).stdout
    return json.loads(out) if out.strip() else None


def hooks_at_once(base, home, payloads):
    """The hooks of one message's parallel Agent calls, started together as Claude Code does."""
    procs = [subprocess.Popen(CLI + ['--home', str(home), 'hook', 'pre-tool', '--host', 'claude'], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(base), env=env_for(base))
             for _ in payloads]
    for proc, payload in zip(procs, payloads):
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
    outs = []
    for proc in procs:
        proc.wait(timeout=120)
        text = proc.stdout.read()
        outs.append(json.loads(text) if text.strip() else None)
    return outs


def context(base, home, *args):
    out = run(base, home, ['context'] + list(args))
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {'error': (out.stderr or out.stdout)[-300:], 'code': out.returncode}


def spec(out):
    return (out or {}).get('hookSpecificOutput') or {}


def refused(out):
    return spec(out).get('permissionDecision') == 'deny'


def started(out):
    return bool(out) and not refused(out) and 'subagent' in (spec(out).get('additionalContext') or '')


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'app.py').write_text('print("hello")\n')
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '-m', 'f'], check=True)
    return root


def session(base, store, sid, on=True):
    """A Claude Code session in a repository (Orbit on unless on=False), with a task. Returns (payload, start reply)."""
    root = make_repo(base, 'repo-' + sid)
    attach.set_switch(store, 'on' if on else 'off', root)
    transcript = base / 'claude' / 'projects' / ('-tmp-' + sid) / (sid + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': 'hi'}}) + '\n')
    pay = {'cwd': str(root), 'session_id': sid, 'transcript_path': str(transcript)}
    reply = attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    attach.handle(store, 'prompt', dict(pay, prompt='Survey the three modules and fix the failing test'), 'claude')
    return pay, reply


def call(pay, use, description, name=None, by=None):
    tool_input = {'description': description, 'subagent_type': 'general-purpose', 'prompt': 'do ' + description}
    if name:
        tool_input['name'] = name
    payload = {'session_id': pay['session_id'], 'transcript_path': pay['transcript_path'], 'cwd': pay['cwd'],
               'hook_event_name': 'PreToolUse', 'tool_name': 'Agent', 'tool_use_id': use, 'tool_input': tool_input}
    if by:
        payload['agent_id'], payload['agent_type'] = by, 'general-purpose'
    return payload


def launch(pay, use, hexid, description='x', finished=False, name=None, team=None, age=0):
    """What Claude Code writes when the call's subagent starts: agent-<id>.jsonl and its .meta.json (2.1.331 fields)."""
    sub = subagents_dir(pay['transcript_path'])
    sub.mkdir(parents=True, exist_ok=True)
    agent = ('a%s-%s' % (name, hexid)) if name else ('a' + hexid)
    meta = {'agentType': name or 'general-purpose', 'isBuiltIn': not name, 'description': description, 'toolUseId': use,
            'spawnDepth': 1, 'requestShape': 'background'}
    if name:
        meta.update(name=name, teamName=team or TEAM, taskKind='in_process_teammate')
    if finished:
        meta['finished'] = True
    transcript, meta_path = sub / ('agent-%s.jsonl' % agent), sub / ('agent-%s.meta.json' % agent)
    transcript.write_text(json.dumps({'type': 'user', 'isSidechain': True, 'agentId': agent,
                                      'message': {'role': 'user', 'content': 'go'}}) + '\n')
    meta_path.write_text(json.dumps(meta))
    if age:
        for path in (transcript, meta_path):
            os.utime(path, (time.time() - age, time.time() - age))
    return agent, meta_path


def finish(meta_path):
    meta = json.loads(meta_path.read_text())
    meta['finished'] = True
    meta_path.write_text(json.dumps(meta))


def team(base, names):
    folder = base / 'claude' / 'teams' / TEAM
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'config.json').write_text(json.dumps({'name': TEAM, 'members': [{'name': 'team-lead'}] +
                                                    [{'name': n} for n in names]}))


def rows(store, sid):
    return [dict(r) for r in store.db.execute('SELECT * FROM subagents WHERE session=? ORDER BY started', (sid,))] \
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name='subagents'").fetchone() else []


def kinds(store, task_id):
    return [e['kind'] for e in store.events(task_id)]


def parallel(base, store):
    print('① six Agent calls in one message, six hooks at once')
    home = store.home
    pay, _ = session(base, store, 'S1')
    outs = hooks_at_once(base, home, [call(pay, 'toolu_p%d' % i, 'survey part %d' % i) for i in range(6)])
    allowed = [o for o in outs if started(o)]
    denied = [o for o in outs if refused(o)]
    check('five start and one is refused', len(allowed) == 5 and len(denied) == 1, [spec(o) for o in outs])
    counts = sorted(int(spec(o)['additionalContext'].split('subagent ')[1].split(' ')[0]) for o in allowed) if allowed else []
    check('each start counted once, 1..5 (the count and the place are one transaction)', counts == [1, 2, 3, 4, 5], counts)
    reason = spec(denied[0]).get('permissionDecisionReason', '') if denied else ''
    check('the refusal says why: 5 already run, at most 5, wait or do it here', '5 subagents already run' in reason
          and 'at most 5' in reason and 'in this conversation' in reason, reason)
    check('the start line says what a subagent costs and to keep the rest here',
          bool(allowed) and '26k tokens' in spec(allowed[0])['additionalContext']
          and 'do the rest in this conversation' in spec(allowed[0])['additionalContext'], allowed[:1])
    states = sorted(r['state'] for r in rows(store, 'S1'))
    check('the store: five places taken, one refusal', states == ['refused'] + ['starting'] * 5, states)
    return pay


def folder(base, store, pay):
    print('② the folder decides what runs')
    home = store.home
    metas = {}
    taken = [r['call'] for r in rows(store, 'S1') if r['state'] == 'starting'] or ['toolu_p%d' % i for i in range(5)]
    for i in range(6):                  # (main before this change took no places: launch five so the rest still runs)
        use = 'toolu_p%d' % i
        if use in taken:
            metas[use] = launch(pay, use, '%016x' % (i + 1), 'survey part %d' % i)[1]
    out = hook(base, home, call(pay, 'toolu_7', 'one more'))
    check('five launched (.meta.json with their toolUseId): the next call is refused', refused(out), spec(out))
    linked = [r for r in rows(store, 'S1') if r['state'] == 'running']
    check('their rows now carry the agent id and transcript', len(linked) == 5 and all(r['agent'] and r['transcript']
                                                                                        for r in linked), linked)
    first = sorted(metas)[0]
    finish(metas[first])
    out = hook(base, home, call(pay, 'toolu_8', 'after one finished'))
    check('one says "finished": its place is free, the next starts as 5 of 5', started(out)
          and 'subagent 5 of at most 5' in spec(out).get('additionalContext', ''), spec(out))
    ended = [r for r in rows(store, 'S1') if r['call'] == first]
    check('the finished one is ended, with why', bool(ended) and ended[0]['state'] == 'ended'
          and ended[0]['why'] == 'finished', ended)
    launch(pay, 'toolu_8', 'f' * 16, 'after one finished')
    for use in sorted(metas)[1:]:
        finish(metas[use])
    wf = launch(pay, 'toolu_workflow_x', 'e' * 16, 'a workflow agent')[1]   # no Agent call of the session made it
    out = hook(base, home, call(pay, 'toolu_9', 'beside a workflow'))
    check('an agent started without an Agent call (a workflow) is counted: 2 running, this is 3 of 5',
          started(out) and 'subagent 3 of at most 5' in spec(out).get('additionalContext', ''), spec(out))
    launch(pay, 'toolu_9', 'd' * 16, 'beside a workflow')
    finish(wf)
    team(base, ['researcher'])
    out = hook(base, home, call(pay, 'toolu_t', 'a teammate', name='researcher'))
    check('a teammate (started with a name) starts', started(out), spec(out))
    _, mate_meta = launch(pay, 'toolu_t', '0123456789abcdef', 'a teammate', name='researcher')
    out = hook(base, home, call(pay, 'toolu_10', 'counting the teammate'))
    check('the teammate counts while its team lists it (it never says finished): 4 of 5',
          'subagent 4 of at most 5' in spec(out).get('additionalContext', ''), spec(out))
    launch(pay, 'toolu_10', 'c' * 16, 'counting the teammate', finished=True)
    team(base, [])
    out = hook(base, home, call(pay, 'toolu_11', 'after the teammate left'))
    check('it left its team: not counted (2 running: f…, d…; this is 3 of 5)',
          'subagent 3 of at most 5' in spec(out).get('additionalContext', ''), spec(out))
    if subagents:
        later = time.time() + subagents.RESERVE_SECONDS + 5
        result = subagents.gate(home, 'claude', call(pay, 'toolu_12', 'two minutes on'), now=later)
        lost = [r for r in rows(store, 'S1') if r['call'] == 'toolu_11']
        check('a call whose subagent never appeared is dropped after 120 s (lost), not counted',
              bool(lost) and lost[0]['state'] == 'lost' and 'subagent 3 of at most 5' in
              spec(result).get('additionalContext', ''), (lost, spec(result)))
    else:
        check('a call whose subagent never appeared is dropped after 120 s (lost), not counted', False, 'no subagents module')
    for use, hexid in (('toolu_12', 'b' * 16),):
        launch(pay, use, hexid, 'two minutes on')
    quiet = launch(pay, 'toolu_quiet', '9' * 16, 'silent for an hour', age=3700)[1]
    out = hook(base, home, call(pay, 'toolu_13', 'beside a silent one'))
    check('an unfinished subagent silent for an hour is gone: 3 running (f…, d…, b…), this is 4 of 5',
          'subagent 4 of at most 5' in spec(out).get('additionalContext', ''), spec(out))
    attach.handle(store, 'session-end', dict(pay, reason='other'), 'claude')
    live = [r for r in rows(store, 'S1') if r['state'] in ('starting', 'running')]
    check('SessionEnd: every subagent of the session ended with it', not live and any(
        r['why'] == 'the session ended' for r in rows(store, 'S1')), live)
    return quiet


def limits(base, store):
    print('③ the limit')
    home = store.home
    pay, _ = session(base, store, 'S2')
    shown = context(base, home, 'subagents', '2')
    check('context subagents 2: stored', shown.get('max') == 2 and shown.get('source') == 'settings.json subagents.max', shown)
    outs = [hook(base, home, call(pay, 'toolu_l%d' % i, 'limited %d' % i)) for i in range(3)]
    check('at most 2: two start, the third is refused saying at most 2', started(outs[0]) and started(outs[1])
          and refused(outs[2]) and 'at most 2' in spec(outs[2]).get('permissionDecisionReason', ''), [spec(o) for o in outs])
    shown = context(base, home, 'subagents', 'off')
    out = hook(base, home, call(pay, 'toolu_l9', 'with the limit off'))
    check('off: the Agent call gets no answer at all', shown.get('max') == 0 and out is None, (shown, out))
    shown = context(base, home, 'subagents', 'reset')
    check('reset: 5 again, built in', shown.get('max') == 5 and shown.get('source') == 'built in', shown)
    bad = context(base, home, 'subagents', '0')
    check('a bad value is refused (0; off is the word for none)', 'error' in bad or bad.get('max') != 0, bad)
    view = context(base, home, 'subagents', '--session', 'S2')
    check('without a value: the limit and the session\'s subagents (2 starting, 1 refused)',
          view.get('max') == 5 and len(view.get('running') or []) == 2 and view.get('refused') == 1, view)


def silent(base, store):
    print('④ silent where it must be')
    home = store.home
    pay, _ = session(base, store, 'S3')
    shell = dict(call(pay, 'toolu_s1', 'x'), tool_name='Bash', tool_input={'command': 'ls', 'description': 'list'})
    check('a shell call: no subagent answer', hook(base, home, shell) is None)
    check('Codex: no answer to an Agent-named call', hook(base, home, call(pay, 'toolu_s2', 'codex'), host='codex') is None)
    off, _ = session(base, store, 'S4', on=False)
    check('a folder where Orbit is off: no answer', hook(base, home, call(off, 'toolu_s3', 'off here')) is None)
    first = hook(base, home, call(pay, 'toolu_s4', 'once'))
    again = hook(base, home, call(pay, 'toolu_s4', 'once'))
    check('the same call answered once (a repeated hook adds nothing)', started(first) and again is None, (first, again))
    by_sub = hook(base, home, call(pay, 'toolu_s5', 'nested', by='a' + '7' * 16))
    check('an Agent call made by a subagent counts in the same session (2 of 5)',
          'subagent 2 of at most 5' in spec(by_sub).get('additionalContext', ''), spec(by_sub))


def install(base):
    print('⑤ hooks install')
    home = base / 'home-install'
    out = run(base, home, ['hooks', 'install', '--host', 'claude'])
    out2 = run(base, home, ['hooks', 'install', '--host', 'codex'])
    claude = json.loads((base / 'claude' / 'settings.json').read_text())['hooks']
    codex = json.loads((base / 'codex' / 'hooks.json').read_text())['hooks']
    mine = [e for e in claude.get('PreToolUse', []) if attach._is_ours(e)]
    check('Claude Code: PreToolUse takes Bash|Agent|Task', [e['matcher'] for e in mine] == ['Bash|Agent|Task'],
          (mine, out.stderr[-200:]))
    theirs = [e for e in codex.get('PreToolUse', []) if attach._is_ours(e)]
    check('control: Codex keeps Bash', [e['matcher'] for e in theirs] == ['Bash'], (theirs, out2.stderr[-200:]))
    settings = json.loads((base / 'claude' / 'settings.json').read_text())
    for entry in settings['hooks']['PreToolUse']:
        if attach._is_ours(entry):
            entry['matcher'] = 'Bash'                                       # as installed before this change
    (base / 'claude' / 'settings.json').write_text(json.dumps(settings))
    status = json.loads(run(base, home, ['hooks', 'status', '--host', 'claude']).stdout or '{}').get('claude', {})
    check('an old Bash-only entry: not installed, outdated PreToolUse, fix = hooks install',
          status.get('installed') is False and status.get('outdated') == ['PreToolUse']
          and status.get('fix') == 'cosmos-orbit hooks install', status)


def told(base, store):
    print('⑥ the session is told when it starts')
    home = store.home
    _, reply = session(base, store, 'S5')
    text = spec(reply).get('additionalContext') or ''
    line = [x for x in text.splitlines() if x.startswith('[orbit] Subagents:')]
    check('SessionStart carries the line: new conversation, 26k tokens, work here, at most 5', len(line) == 1
          and '26k tokens' in line[0] and 'at most 5' in line[0] and 'in this conversation' in line[0], text[-400:])
    binding = store.session_task('S5')
    check('telling binds nothing: S5 is bound by its prompt\'s task, not by the line', bool(binding and binding['task_id']))
    pay5 = {'cwd': str(base / 'repo-S5'), 'session_id': 'S5',
            'transcript_path': str(base / 'claude' / 'projects' / '-tmp-S5' / 'S5.jsonl')}
    again = spec(attach.handle(store, 'session-start', dict(pay5, source='resume'), 'claude')).get('additionalContext') or ''
    check('with a task: the brief still opens the context, the line comes last', again.startswith('[orbit] task')
          and again.splitlines()[-1].startswith('[orbit] Subagents:'), again[:120] + ' … ' + again[-120:])
    codex = attach.handle(store, 'session-start', {'cwd': str(base / 'repo-S5'), 'session_id': 'C5', 'source': 'startup'},
                          'codex')
    check('control: Codex is not told', '[orbit] Subagents' not in (spec(codex).get('additionalContext') or ''))
    context(base, home, 'subagents', 'off')
    _, reply = session(base, store, 'S6')
    check('control: not told where the limit is off', '[orbit] Subagents' not in (spec(reply).get('additionalContext') or ''))
    context(base, home, 'subagents', 'reset')
    _, reply = session(base, store, 'S7', on=False)
    check('control: not told where Orbit is off', '[orbit] Subagents' not in (spec(reply or {}).get('additionalContext') or ''))


def record(store):
    print('⑦ the record on the session\'s task')
    binding = store.session_task('S1')
    seen = kinds(store, binding['task_id']) if binding else []
    check('subagent_started ×5+, subagent_refused, subagent_ended on the task',
          seen.count('subagent_started') >= 5 and 'subagent_refused' in seen and 'subagent_ended' in seen,
          sorted(set(k for k in seen if k.startswith('subagent'))))
    ended = [e['payload'] for e in store.events(binding['task_id']) if e['kind'] == 'subagent_ended'] if binding else []
    check('an end says which, why and when', bool(ended) and all(p.get('why') and p.get('ended') for p in ended), ended[:2])


def turn(path, tool=False):
    """The model's last line in a teammate's transcript: an answer that ends its turn, or a tool call it waits on; then a
    line that is not a message (Claude Code writes such lines after an answer)."""
    content = ([{'type': 'tool_use', 'id': 'toolu_w', 'name': 'Bash', 'input': {'command': 'make'}}] if tool
               else [{'type': 'text', 'text': 'Done: merged, and the result is sent to team-lead.'}])
    with open(path, 'a') as stream:
        stream.write(json.dumps({'type': 'assistant', 'isSidechain': True, 'message': {
            'role': 'assistant', 'content': content, 'stop_reason': 'tool_use' if tool else 'end_turn'}}) + '\n')
        stream.write(json.dumps({'type': 'attachment', 'isSidechain': True}) + '\n')


def teammate(pay, name, hexid, team_name, age=0, last=None):
    """A teammate as Claude Code 2.1.332 writes it: .meta.json with its name and team and no toolUseId. last: None (a
    message to it is the last line: working), 'answer' (its turn ended) or 'tool' (waiting on a tool call)."""
    sub = subagents_dir(pay['transcript_path'])
    sub.mkdir(parents=True, exist_ok=True)
    agent = 'a%s-%s' % (name, hexid)
    transcript, meta = sub / ('agent-%s.jsonl' % agent), sub / ('agent-%s.meta.json' % agent)
    transcript.write_text(json.dumps({'type': 'user', 'isSidechain': True, 'agentId': agent,
                                      'message': {'role': 'user', 'content': 'go'}}) + '\n')
    if last:
        turn(transcript, tool=last == 'tool')
    meta.write_text(json.dumps({'agentType': name, 'description': 'teammate ' + name, 'name': name,
                                'taskKind': 'in_process_teammate', 'teamName': team_name}))
    if age:
        for path in (transcript, meta):
            os.utime(path, (time.time() - age, time.time() - age))
    return agent


def handoff(base, store):
    print('⑧ after a /clear handoff (measured 2026-11-26): the old team gone, teammate places, every name in a refusal')
    home = store.home
    pay, _ = session(base, store, 'S8')
    for i, name in enumerate(('rec16', 'rich4', 'ledger24', 'loop1', 'shopcat5', 'noti3')):
        teammate(pay, name, '%016x' % (0xa0 + i), 'session-gone', age=300 + 240 * i, last='answer')
    out = hook(base, home, call(pay, 'repl_n1', 'notice fix and merge', name='noti4'))
    check('six teammates of a team that is gone, each turn ended 5-25 min ago, wait: none counted, this is 1 of 5',
          started(out) and 'subagent 1 of at most 5' in spec(out).get('additionalContext', ''), spec(out))
    team(base, ['noti4'])
    noti4 = teammate(pay, 'noti4', '7ef008d0dca64565', TEAM)
    out = hook(base, home, call(pay, 'repl_n2', 'feedback loop', name='loop2'))
    check("a teammate's .meta.json has no toolUseId: its place is matched by its name, counted once (this is 2 of 5)",
          started(out) and 'subagent 2 of at most 5' in spec(out).get('additionalContext', ''), spec(out))
    teammate(pay, 'mid1', '%016x' % 0xb1, 'session-gone', age=60, last='tool')
    out = hook(base, home, call(pay, 'repl_n3', 'beside a busy one'))
    check('control: a teammate of the gone team still waiting on a tool call (1 min ago) counts: noti4, mid1 and loop2 '
          '(its place still being taken): this is 4 of 5', started(out)
          and 'subagent 4 of at most 5' in spec(out).get('additionalContext', ''), spec(out))
    teammate(pay, 'mid2', '%016x' % 0xb2, 'session-gone', age=30, last='tool')
    teammate(pay, 'mid3', '%016x' % 0xb3, 'session-gone', age=10)
    out = hook(base, home, call(pay, 'repl_n4', 'one too many'))
    reason = spec(out).get('permissionDecisionReason', '')
    names = ('noti4', 'mid1', 'mid2', 'mid3', 'loop2', 'beside a busy one')
    check('six counted, the seventh refused: the refusal names all six it counted', refused(out)
          and '6 subagents already run' in reason and all(n in reason for n in names), reason)
    quiet = teammate(pay, 'mid4', '%016x' % 0xb4, 'session-gone', age=2400, last='tool')
    view = context(base, home, 'subagents', '--session', 'S8')
    labels = [r.get('label') for r in view.get('running') or []]
    check('control: one of the gone team silent for 40 min (its last line a tool call) is not counted', 'mid4' not in labels
          and 'mid1' in labels, labels)
    if subagents:
        later = time.time() + subagents.RESERVE_SECONDS + 5
        subagents.gate(home, 'claude', call(pay, 'repl_n5', 'two minutes on'), now=later)
        row = [r for r in rows(store, 'S8') if r['call'] == 'repl_n1']
        check("noti4's row carries its agent id and runs (not lost after 120 s while it ran)", bool(row)
              and row[0]['state'] == 'running' and row[0]['agent'] == noti4, row)
    else:
        check("noti4's row carries its agent id and runs (not lost after 120 s while it ran)", False, 'no subagents module')
    return quiet


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-subagents-'))
    try:
        store = Store(base / 'home')
        pay = parallel(base, store)
        folder(base, store, pay)
        limits(base, store)
        silent(base, store)
        install(base)
        told(base, store)
        record(store)
        handoff(base, store)
        store.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print('\n%s — %d failed' % ('FAIL' if FAILED else 'all checks passed', len(FAILED)))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
