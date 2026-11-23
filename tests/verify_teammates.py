#!/usr/bin/env python3
"""Model-free check of the teammates' size (§Teammates): measured, told to the lead, told to the teammate.

Why (measured 2026-11-23): Orbit handed the attached Claude Code session to a fresh context at 300k, but the named
subagents it ran (teammates) went on at 450-610k, at most 700-800k: they are not attached sessions. Replayed here with
synthetic transcripts laid out as Claude Code 2.1.330 writes them (<session>/subagents/agent-<id>.jsonl, isSidechain,
.meta.json, teams/<team>/config.json):

  ① measurement: each running teammate's latest request; the session's own transcript, a teammate that finished, left
    its team, was replaced or (no team) went quiet, and an anonymous subagent are not counted; no usage is unknown
  ② the lead: one line in the prompt brief and at its next shell call only when a teammate is past the point (name,
    size, point, what to do); none below it; once per crossing, again after 100k
  ③ the teammate: told at its own shell calls (agent_id), once per crossing, again after 100k or after a compaction;
    silent where it must be
  ④ the point: the host's handoff point, `cosmos-orbit context teammates 250k|off|reset`, and the measured list

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
from harness import attach, teammates  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []
TEAM = 'session-t1'


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def run(base, home, args, stdin=None, **extra):
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(base / 'claude'))
    env.update({k: v for k, v in extra.items() if v is not None})
    return subprocess.run(CLI + ['--home', str(home)] + list(args), input=stdin, capture_output=True, text=True,
                          cwd=str(base), env=env, timeout=120)


def hook(base, home, payload, **extra):
    out = run(base, home, ['hook', 'pre-tool', '--host', 'claude'], stdin=json.dumps(payload), **extra).stdout
    return json.loads(out) if out.strip() else None


def context(base, home, *args, **extra):
    out = run(base, home, ['context'] + list(args), **extra)
    try:
        return json.loads(out.stdout)
    except ValueError:
        return {'error': out.stderr[-300:]}


def note(out):
    spec = (out or {}).get('hookSpecificOutput') or {}
    return spec.get('additionalContext') or spec.get('permissionDecisionReason') or ''


def say(path, tokens, sidechain=True, agent=None, model='claude-test'):
    """One assistant request: its size as input + cache creation + cache read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = {'input_tokens': 5, 'cache_creation_input_tokens': 995, 'cache_read_input_tokens': tokens - 1000}
    if model == '<synthetic>':
        usage = {'input_tokens': 0, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 0}
    event = {'type': 'assistant', 'isSidechain': sidechain, 'message': {'model': model, 'role': 'assistant', 'usage': usage}}
    if agent:
        event['agentId'] = agent
    with open(path, 'a') as stream:
        stream.write(json.dumps(event) + '\n')


def mate(sub, name, tokens=None, team=TEAM, finished=False, hexid='0123456789abcdef', meta=True):
    """A teammate's transcript (agent-a<name>-<16 hex>.jsonl) and, when meta, its .meta.json. Returns (agent id, path)."""
    agent = 'a%s-%s' % (name, hexid)
    path = sub / ('agent-%s.jsonl' % agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as stream:
        stream.write(json.dumps({'type': 'user', 'isSidechain': True, 'agentId': agent,
                                 'message': {'role': 'user', 'content': 'work'}}) + '\n')
    if tokens:
        say(path, tokens, agent=agent)
    if meta:
        data = {'agentType': name, 'name': name, 'taskKind': 'in_process_teammate'}
        if team:
            data['teamName'] = team
        if finished:
            data['finished'] = True
        (sub / ('agent-%s.meta.json' % agent)).write_text(json.dumps(data))
    return agent, path


def team(base, names):
    folder = base / 'claude' / 'teams' / TEAM
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'config.json').write_text(json.dumps({'name': TEAM, 'members': [{'name': 'team-lead'}] +
                                                    [{'name': n} for n in names]}))


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'app.py').write_text('print("hello")\n')
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '-m', 'f'], check=True)
    return root


def lead(base, store, session):
    """A lead session with a task: its transcript at 120k, Orbit on in its repository. Returns (payload, subagents dir)."""
    root = make_repo(base, 'repo-' + session)
    attach.set_switch(store, 'on', root)
    transcript = base / 'claude' / 'projects' / '-tmp-repo' / (session + '.jsonl')
    say(transcript, 120_000, sidechain=False)
    pay = {'cwd': str(root), 'session_id': session, 'transcript_path': str(transcript)}
    attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    attach.handle(store, 'prompt', dict(pay, prompt='Split the work across three teammates and keep the tests green'), 'claude')
    return pay, teammates.subagents_dir(transcript)


def prompt(store, pay, text='how are the teammates doing?'):
    out = attach.handle(store, 'prompt', dict(pay, prompt=text), 'claude')
    return note(out)


def bash(pay, command='ls', **more):
    return dict({'session_id': pay['session_id'], 'transcript_path': pay['transcript_path'], 'cwd': pay['cwd'],
                 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'toolu_1',
                 'tool_input': {'command': command, 'description': 'x'}}, **more)


def teammate_lines(text):
    return [line for line in text.splitlines() if 'TEAMMATES past the handoff point' in line]


def measurement(base, store):
    print('① measurement')
    transcript = base / 'claude' / 'projects' / '-tmp-m' / 'M1.jsonl'
    say(transcript, 400_000, sidechain=False)                       # the session's own size: not a teammate's
    sub = teammates.subagents_dir(transcript)
    team(base, ['small', 'big', 'renamed', 'blank'])
    mate(sub, 'small', 200_000)
    mate(sub, 'big', 320_000)
    mate(sub, 'done', 500_000, team=None, finished=True)            # an ended subagent: finished
    mate(sub, 'gone', 550_000)                                      # left its team
    _, old = mate(sub, 'renamed', 650_000, hexid='aaaaaaaaaaaaaaaa')  # replaced by a newer one of the same name
    os.utime(old, (time.time() - 600, time.time() - 600))
    mate(sub, 'renamed', 90_000, hexid='bbbbbbbbbbbbbbbb')
    _, blank = mate(sub, 'blank')                                   # a request yet to come: unknown
    say(blank, 1, agent='ablank-0123456789abcdef', model='<synthetic>')  # Claude Code's own line, usage 0
    anonymous = sub / 'agent-a1234567890abcdef1.jsonl'               # an anonymous subagent: not a teammate
    say(anonymous, 700_000, agent='a1234567890abcdef1')
    rows = {(r['name'], r['agent_id'][-4:]): r for r in teammates.agents(transcript)}
    over = teammates.over(transcript, 300_000)
    check('past 300k: only the running teammate "big", with its size', [(r['name'], r['tokens']) for r in over]
          == [('big', 320_000)], over)
    check('control: the session\'s own transcript (400k, not a sidechain) is not a teammate',
          all(r['transcript'] != str(transcript) for r in rows.values()), list(rows))
    check('control: an anonymous subagent (agent-a<hex>, no name) is not listed', 'a1234567890abcdef1' not in
          [r['agent_id'] for r in rows.values()], list(rows))
    check('ended: "finished" in .meta.json', rows[('done', 'cdef')]['running'] is False, rows[('done', 'cdef')])
    check('ended: no longer a member of its team', rows[('gone', 'cdef')]['running'] is False
          and 'no longer a member' in rows[('gone', 'cdef')]['why'], rows[('gone', 'cdef')])
    check('ended: replaced by a newer transcript of the same name (650k not counted, 90k counted)',
          rows[('renamed', 'aaaa')]['running'] is False and rows[('renamed', 'bbbb')]['tokens'] == 90_000, rows)
    check('no usage (only a user line and a usage-0 line of Claude Code\'s own): running, size unknown (None)',
          rows[('blank', 'cdef')]['running'] is True and rows[('blank', 'cdef')]['tokens'] is None, rows[('blank', 'cdef')])
    check('below the point: measured, not past it', rows[('small', 'cdef')]['tokens'] == 200_000)
    lone = base / 'claude' / 'projects' / '-tmp-n' / 'N1.jsonl'      # no team at all: recency decides
    say(lone, 50_000, sidechain=False)
    nsub = teammates.subagents_dir(lone)
    mate(nsub, 'fresh', 330_000, team=None)
    _, quiet = mate(nsub, 'quiet', 340_000, team=None)
    os.utime(quiet, (time.time() - 3600, time.time() - 3600))
    rows = {r['name']: r for r in teammates.agents(lone)}
    check('no team to ask: written in the last 30 min counts, quiet for an hour does not',
          rows['fresh']['running'] and not rows['quiet']['running'], rows)
    check('a session with no subagents folder: nothing, no error', teammates.agents(base / 'nowhere.jsonl') == [])


def lead_line(base, store):
    print('② the lead')
    home = store.home
    pay, sub = lead(base, store, 'L1')
    team(base, ['alpha', 'beta'])
    a_id, a_path = mate(sub, 'alpha', 250_000)
    mate(sub, 'beta', 120_000)
    check('below the point: no teammate line in the brief (0 lines)', teammate_lines(prompt(store, pay)) == [])
    check('below the point: silent at the lead\'s shell call', hook(base, home, bash(pay)) is None)
    say(a_path, 320_000, agent=a_id)
    lines = teammate_lines(prompt(store, pay))
    check('past it: exactly one line, naming the teammate, its size and the point', len(lines) == 1
          and 'alpha 320k' in lines[0] and '300k' in lines[0] and 'beta' not in lines[0], lines)
    check('… with what to do: a handoff note, and a new teammate given only that note',
          'handoff note' in lines[0] and 'new teammate given only that note' in lines[0] and len(lines[0]) <= 700, lines)
    check('told once: the next prompt at that size carries no line', teammate_lines(prompt(store, pay)) == [])
    check('… nor the next shell call', hook(base, home, bash(pay)) is None)
    say(a_path, 360_000, agent=a_id)
    check('40k later: still nothing', teammate_lines(prompt(store, pay)) == [])
    say(a_path, 421_000, agent=a_id)
    out = hook(base, home, bash(pay))
    check('100k later: told again, at the lead\'s shell call (additionalContext)', 'alpha 421k' in note(out)
          and 'permissionDecision' not in (out or {}).get('hookSpecificOutput', {}), out)
    tid = attach.current_task(store, Path(pay['cwd']))['id']
    told = [e['payload'] for e in store.events(tid) if e['kind'] == 'teammates_over']
    check('the ledger holds what the lead was told (320k, 421k)', [t['teammates'][0]['tokens'] for t in told]
          == [320_000, 421_000], told)
    team(base, ['beta'])                                             # alpha left the team
    say(a_path, 600_000, agent=a_id)
    check('a teammate that left the team is not reported, whatever its size', teammate_lines(prompt(store, pay)) == [])
    attach.set_switch(store, 'off', Path(pay['cwd']))
    team(base, ['alpha', 'beta'])
    check('Orbit off for the lead\'s project: no line', hook(base, home, bash(pay)) is None)
    attach.set_switch(store, 'on', Path(pay['cwd']))


def teammate_notice(base, store):
    print('③ the teammate')
    home = store.home
    pay, sub = lead(base, store, 'T1')
    team(base, ['gamma'])
    g_id, g_path = mate(sub, 'gamma', 290_000, hexid='fedcba9876543210')
    call = bash(pay, 'npm run build', agent_id=g_id, agent_type='gamma', cwd=pay['cwd'])
    check('290k: silent', hook(base, home, call) is None)
    say(g_path, 330_000, agent=g_id)
    out = hook(base, home, call)
    text = note(out)
    check('330k: the notice, as advice (not a refusal)', bool(text) and 'permissionDecision' not in out['hookSpecificOutput'], out)
    check('… with its size, the point and the handoff note to write (done · code · branch · PRs · remaining) for the lead',
          all(w in text for w in ('330k', '300k', 'handoff note', 'code locations', 'branch', 'open PRs', 'what remains',
                                  'send its path to the lead', 'replace you')), text)
    check('… and it is the teammate\'s notice, not the lead\'s line', 'TEAMMATES' not in text, text)
    say(g_path, 380_000, agent=g_id)
    check('380k: told for this stretch already: silent', hook(base, home, call) is None)
    say(g_path, 431_000, agent=g_id)
    check('431k (100k after the first): told again', '431k' in note(hook(base, home, call)))
    say(g_path, 120_000, agent=g_id)                                 # compacted
    check('compacted to 120k: silent', hook(base, home, call) is None)
    say(g_path, 305_000, agent=g_id)
    check('past the point again after the compaction: told again', '305k' in note(hook(base, home, call)))
    tid = attach.current_task(store, Path(pay['cwd']))['id']
    check('the ledger holds the notices (330k, 431k, 305k)', [e['payload']['tokens'] for e in store.events(tid)
                                                               if e['kind'] == 'teammate_notice'] == [330_000, 431_000, 305_000])
    anon = sub / 'agent-a99999999999999999.jsonl'
    say(anon, 500_000, agent='a99999999999999999')
    _, b_path = mate(sub, 'blank2', hexid='1111111111111111')
    say(g_path, 520_000, agent=g_id)
    for label, payload, extra in (
            ('an anonymous subagent\'s call', dict(call, agent_id='a99999999999999999', agent_type='Explore'), {}),
            ('a teammate whose size is unknown', dict(call, agent_id='ablank2-1111111111111111'), {}),
            ('an agent id that is not a file name', dict(call, agent_id='../../etc/passwd'), {}),
            ('ORBIT=off in the terminal', call, {'ORBIT': 'off'}),
            ('a Codex host', call, {'_host': 'codex'})):
        if extra.pop('_host', None):
            result = run(base, home, ['hook', 'pre-tool', '--host', 'codex'], stdin=json.dumps(payload)).stdout
            out = json.loads(result) if result.strip() else None
        else:
            out = hook(base, home, payload, **extra)
        check('silent: %s' % label, out is None, out)
    missing = base / 'no-home'
    check('no ledger in the home: silent, and the home is not created',
          hook(base, missing, call) is None and not missing.exists())
    check('control: the same teammate call still speaks (520k)', '520k' in note(hook(base, home, call)))


def settings(base, store):
    print('④ the point')
    home = store.home
    before = context(base, home)['here']['handoff_at']
    view = context(base, home, 'teammates')
    check('built in: the host\'s handoff point (Claude Code 300k)', view['teammates']['at'] == 300_000
          and 'handoff point' in view['teammates']['source'], view.get('teammates'))
    check('… shown by cosmos-orbit context too', context(base, home)['teammates']['at'] == 300_000)
    pay, sub = lead(base, store, 'P1')
    team(base, ['delta'])
    d_id, d_path = mate(sub, 'delta', 260_000, hexid='2222222222222222')
    call = bash(pay, agent_id=d_id, agent_type='delta')
    check('260k at the 300k point: silent', hook(base, home, call) is None)
    changed = context(base, home, 'teammates', '250k')
    check('context teammates 250k: stored, applies', changed['teammates']['at'] == 250_000
          and changed['teammates']['source'] == 'handoff.teammates', changed.get('teammates'))
    check('… the teammate at 260k is told now', '250k' in note(hook(base, home, call)))
    check('… and the lead', 'delta 260k' in '\n'.join(teammate_lines(prompt(store, pay))))
    check('the handoff point of a session is not changed by it', context(base, home)['here']['handoff_at'] == before,
          (before, context(base, home).get('here')))
    context(base, home, 'teammates', 'off')
    say(d_path, 400_000, agent=d_id)
    check('off: the teammate is not told', hook(base, home, call) is None)
    check('off: the lead is not told', teammate_lines(prompt(store, pay)) == [])
    back = context(base, home, 'teammates', 'reset')
    check('reset: the handoff point again', back['teammates']['at'] == 300_000, back.get('teammates'))
    check('a point below 20k is refused', 'error' in context(base, home, 'teammates', '5k')
          or context(base, home, 'teammates')['teammates']['at'] == 300_000)
    shown = context(base, home, 'teammates', '--session', 'P1')
    rows = {r['name']: r for r in shown.get('measured') or []} if isinstance(shown.get('measured'), list) else {}
    check('the measured list for a session: name, size, running, past', rows.get('delta', {}).get('tokens') == 400_000
          and rows['delta']['running'] and rows['delta']['past'], shown.get('measured'))
    _, b = mate(sub, 'echo', hexid='3333333333333333')
    team(base, ['delta', 'echo'])
    shown = context(base, home, 'teammates', '--session', 'P1')
    rows = {r['name']: r for r in shown.get('measured') or []}
    check('… a running teammate without a request yet: "unknown", not past', rows.get('echo', {}).get('tokens') == 'unknown'
          and rows['echo']['past'] is False, rows.get('echo'))


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-teammates-'))
    store = Store(base / 'home')
    for section in (measurement, lead_line, teammate_notice, settings):
        try:
            section(base, store)
        except Exception:  # noqa: BLE001 - a section that cannot finish fails as one check; the next still runs
            import traceback
            check('%s: ran to the end' % section.__name__, False, traceback.format_exc()[-600:])
    store.close()
    shutil.rmtree(base, ignore_errors=True)
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
