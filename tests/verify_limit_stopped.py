#!/usr/bin/env python3
"""Model-free check: the subagents a usage limit stopped are listed for the lead at its next prompt that goes on, with
the one call that resumes each on the account now in use (§Subagents, stopped at the usage limit).

Why (measured 2026-11-23/24, Claude Code 2.1.331): at the limit every subagent of the session stops with it, its own
transcript ending on the host's synthetic line (error rate_limit). After /login in the session a message resumes it;
after an account switched from outside, or a restart, its name is no longer reachable ("No agent named … is reachable")
but its agent id still resumes it from its transcript with its whole conversation. Left alone, the lead forgot them,
tried the names, or started new agents with handoff notes. Replayed here through the real hook entry point
(attach.handle), with the folder laid out as Claude Code writes it:

  ① the list: a teammate and an anonymous subagent whose transcripts end on the limit line (rate_limit, and
    billing_error) are listed, newest stop first, with their agent ids; not one that ran again after (a resume writes
    after the line), not one idle on a real answer, not another API error, not one whose name was started again after
    the stop, not a stop older than a day; lines that are not messages after the limit line do not hide it
  ② the prompt: a prompt that goes on carries the list and the SendMessage call by agent id; one stop at 3 prompts at
    most; it leaves by itself once the subagent runs again; a new stop of it is told again
  ③ with the routes: while the session itself is stopped at its limit, its prompt is blocked (the offer) and carries no
    list; after /login the same prompt goes on and carries it
  ④ silent: Codex, a subagent's own payload, a folder where Orbit is off, a session with no stopped subagent
  ⑤ shown and recorded: `context subagents --session` lists them; the task records subagents_stopped_told

Nothing starts a model or touches the live Orbit home or host settings: the store and the Claude Code folder are
temporary.
"""
import datetime
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
              'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME', 'COSMOS_ORBIT_ROUTE',
              'COSMOS_ORBIT_ROUTE_FROM'):
    os.environ.pop(_name, None)
CLI = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit')]
from harness import attach  # noqa: E402
from harness.store import Store  # noqa: E402
from harness.teammates import subagents_dir  # noqa: E402
from harness import subagents  # noqa: E402

FAILED = []
NOW = time.time()
HEAD = 'stopped at the usage limit'
LIMIT_TEXT = "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)"


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def limit_line(at, agent=None, error='rate_limit', resets=None):
    """The synthetic line Claude Code writes when a request stops at the limit (2.1.331 fields, copied from this Mac)."""
    line = {'type': 'assistant', 'uuid': 'l-%s-%d' % (agent or 'main', at), 'timestamp': iso(at),
            'isSidechain': bool(agent), 'isApiErrorMessage': True, 'error': error, 'apiError': 'usage_limit_reached',
            'apiErrorStatus': 429, 'quotaLimits': {'status': 'rejected', 'resetsAt': resets} if resets else None,
            'message': {'model': '<synthetic>', 'role': 'assistant', 'stop_reason': 'stop_sequence',
                        'content': [{'type': 'text', 'text': LIMIT_TEXT}]}}
    if agent:
        line['agentId'] = agent
    return line


def answer(at, agent=None, text='Done.'):
    return {'type': 'assistant', 'uuid': 'a-%s-%d' % (agent or 'main', at), 'timestamp': iso(at), 'isSidechain': bool(agent),
            'message': {'model': 'claude-haiku-5-5', 'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}


def asked(at, agent, text='go on'):
    return {'type': 'user', 'uuid': 'u-%s-%d' % (agent, at), 'timestamp': iso(at), 'isSidechain': True, 'agentId': agent,
            'message': {'role': 'user', 'content': text}}


def write(path, records, mode='w'):
    with open(path, mode, encoding='utf-8') as handle:
        for record in records:
            handle.write(json.dumps(record) + '\n')


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'app.py').write_text('print("hello")\n')
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '-m', 'f'], check=True)
    return root


def session(base, store, sid, on=True):
    """A Claude Code session in a repository (Orbit on unless on=False), bound to a task by its first prompt."""
    root = make_repo(base, 'repo-' + sid)
    attach.set_switch(store, 'on' if on else 'off', root)
    transcript = base / 'claude' / 'projects' / ('-tmp-' + sid) / (sid + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    write(transcript, [{'type': 'user', 'message': {'role': 'user', 'content': 'hi'}}])
    pay = {'cwd': str(root), 'session_id': sid, 'transcript_path': str(transcript)}
    attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    attach.handle(store, 'prompt', dict(pay, prompt='Build the three screens with a team of subagents'), 'claude')
    return pay


def agent(pay, agent_id, records, name=None, description='x', written=None):
    """agent-<id>.jsonl and its .meta.json, as Claude Code writes them (a teammate's id is 'a' + name + '-' + 16 hex)."""
    sub = subagents_dir(pay['transcript_path'])
    sub.mkdir(parents=True, exist_ok=True)
    meta = {'agentType': name or 'general-purpose', 'description': description, 'toolUseId': 'toolu_' + agent_id,
            'requestShape': 'background'}
    if name:
        meta.update(name=name, teamName='session-' + pay['session_id'], taskKind='in_process_teammate')
    path = sub / ('agent-%s.jsonl' % agent_id)
    write(path, records)
    (sub / ('agent-%s.meta.json' % agent_id)).write_text(json.dumps(meta))
    if written:
        os.utime(path, (written, written))
    return path


def note(reply):
    text = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if HEAD in line), None)
    return '' if start is None else '\n'.join(lines[start:start + 20])


def prompt(store, pay, text='이어서 진행해. 계정을 바꿨어'):
    return attach.handle(store, 'prompt', dict(pay, prompt=text), 'claude')


def the_list(base, store):
    print('① the list, read from the subagent folder')
    pay = session(base, store, 'S1')
    t = NOW - 600
    mate = 'aui-0123456789abcdef'
    agent(pay, mate, [asked(t, mate), answer(t + 60, mate, 'Working…'), limit_line(t + 120, mate)], name='ui',
          description='Build the records screen')
    anon = 'a5f0c1d2e3b4a5968'
    agent(pay, anon, [asked(t, anon), limit_line(t + 100, anon),
                      {'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': iso(t + 130)}],
          description='Run the screen tests')
    credits = 'a7777777777777777'
    agent(pay, credits, [asked(t, credits), limit_line(t + 90, credits, error='billing_error')], description='Fetch')
    resumed = 'aledger-1111111111111111'
    agent(pay, resumed, [asked(t, resumed), limit_line(t + 110, resumed), asked(t + 300, resumed, 'The usage limit is '
          'lifted: continue from where you stopped.'), answer(t + 360, resumed)], name='ledger')
    idle = 'aphone-2222222222222222'
    agent(pay, idle, [asked(t, idle), answer(t + 100, idle, 'Report: all green.')], name='phone')
    other = 'a3333333333333333'
    agent(pay, other, [asked(t, other), dict(limit_line(t + 100, other), error='server_error')], description='Other')
    old_mate = 'abuild-4444444444444444'
    agent(pay, old_mate, [asked(t, old_mate), limit_line(t + 100, old_mate)], name='build', written=t + 100)
    agent(pay, 'abuild-5555555555555555', [asked(t + 400, 'abuild-5555555555555555')], name='build', written=t + 400)
    ancient = 'a6666666666666666'
    agent(pay, ancient, [asked(NOW - 3 * 86400, ancient), limit_line(NOW - 3 * 86400 + 60, ancient)],
          description='Old', written=NOW - 3 * 86400 + 60)
    rows = subagents.stopped(pay['transcript_path'], NOW)
    ids = [r['agent_id'] for r in rows]
    check('three listed, newest stop first: the teammate, the anonymous one (a queue line after its limit line), the '
          'one stopped for credits', ids == [mate, anon, credits], ids)
    check('the teammate by its name, the anonymous one by its description', rows and rows[0]['name'] == 'ui'
          and rows[1]['name'] is None and rows[1]['description'] == 'Run the screen tests', rows[:2])
    check('control: one that ran again after its limit line is not listed', resumed not in ids)
    check('control: one idle on a real answer is not listed', idle not in ids)
    check('control: another API error (server_error) is not listed', other not in ids)
    check('control: one whose name was started again after the stop is not listed', old_mate not in ids)
    check('control: a stop older than a day is not listed', ancient not in ids)
    return pay, mate, anon, credits


def the_prompt(base, store, pay, mate, anon, credits):
    print('② the prompt that goes on carries the list and the call')
    text = note(prompt(store, pay))
    check('the list: 3 subagents, each with its agent id', text.startswith('[orbit] 3 subagents of this session ' + HEAD)
          and all(('agentId ' + i) in text for i in (mate, anon, credits)), text)
    check('the call: SendMessage to the agentId, not the name, and why', 'SendMessage({to: "<its agentId>"' in text
          and 'Use the agentId, not the name' in text and 'No agent named' in text, text)
    check('the subagent limit is said (at most 5 at a time), and to leave them when the user said so',
          'At most 5 run at a time' in text and 'If the user said to leave them stopped' in text, text)
    second, third, fourth = note(prompt(store, pay, 'go on')), note(prompt(store, pay, 'and?')), note(prompt(store, pay, 'ok'))
    check('the same stops at 3 prompts at most: the 2nd and 3rd carry it, the 4th does not',
          bool(second) and bool(third) and not fourth, (bool(second), bool(third), fourth))
    for agent_id in (mate, anon, credits):
        path = subagents_dir(pay['transcript_path']) / ('agent-%s.jsonl' % agent_id)
        write(path, [asked(NOW - 30, agent_id, 'The usage limit is lifted: continue from where you stopped.'),
                     answer(NOW - 20, agent_id)], mode='a')
    check('it leaves by itself once they run again', subagents.stopped(pay['transcript_path'], NOW) == [])
    path = subagents_dir(pay['transcript_path']) / ('agent-%s.jsonl' % anon)
    write(path, [limit_line(NOW - 10, anon)], mode='a')
    again = note(prompt(store, pay, 'next'))
    check('a new stop of it is told again (1 subagent, the anonymous one)',
          again.startswith('[orbit] 1 subagent of this session ' + HEAD) and anon in again and mate not in again, again)


def with_routes(base, store):
    print('③ with the routes: no list while the session itself is stopped; after /login it comes')
    pay = session(base, store, 'S2')
    mate = 'aengine-89abcdef01234567'
    agent(pay, mate, [asked(NOW - 400, mate), limit_line(NOW - 200, mate)], name='engine')
    write(pay['transcript_path'], [answer(NOW - 260), limit_line(NOW - 190, resets=NOW + 7200)], mode='a')
    blocked = prompt(store, pay, 'continue my work')
    check('the session at its limit: the prompt is blocked with the routes, and carries no list',
          (blocked or {}).get('decision') == 'block' and HEAD not in json.dumps(blocked), blocked)
    command = {'type': 'user', 'userType': 'external', 'uuid': 'login-command', 'timestamp': iso(NOW - 30),
               'message': {'content': '<command-name>/login</command-name>\n<command-message>login</command-message>\n'
                                      '<command-args></command-args>'}}
    success = {'type': 'user', 'userType': 'external', 'uuid': 'login-result', 'parentUuid': 'login-command',
               'timestamp': iso(NOW - 30), 'message': {'content': '<local-command-stdout>Login successful</local-command-stdout>'}}
    write(pay['transcript_path'], [command, success], mode='a')
    went = prompt(store, pay, 'continue my work')
    text = note(went)
    check('after /login the same prompt goes on and carries the list with the teammate\'s agent id',
          not (went or {}).get('decision') and ('agentId ' + mate) in text, (went or {}).get('decision') or text)


def silent(base, store, pay):
    print('④ silent where it must be')
    mate = 'aqa-0f0f0f0f0f0f0f0f'
    agent(pay, mate, [asked(NOW - 100, mate), limit_line(NOW - 50, mate)], name='qa')
    check('Codex: nothing', subagents.stopped_reply(store, dict(pay, prompt='go'), 'codex') is None)
    check('a subagent\'s own payload: nothing', subagents.stopped_reply(store, dict(pay, agent_id=mate), 'claude') is None)
    off = session(base, store, 'S3', on=False)
    agent(off, mate, [asked(NOW - 100, mate), limit_line(NOW - 50, mate)], name='qa')
    check('a folder where Orbit is off: nothing', HEAD not in json.dumps(prompt(store, off) or {}))
    plain = session(base, store, 'S4')
    agent(plain, 'adone-0123012301230123', [asked(NOW - 100, 'adone-0123012301230123'),
                                           answer(NOW - 50, 'adone-0123012301230123')], name='done')
    check('a session whose subagents all answered: nothing', HEAD not in json.dumps(prompt(store, plain) or {}))


def shown(base, store):
    print('⑤ shown and recorded')
    home = store.home
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(base / 'claude'), CODEX_HOME=str(base / 'codex'))
    out = subprocess.run(CLI + ['--home', str(home), 'context', 'subagents', '--session', 'S2'], capture_output=True,
                         text=True, cwd=str(base), env=env, timeout=120)
    try:
        data = json.loads(out.stdout)
    except ValueError:
        data = {'error': (out.stderr or out.stdout)[-300:]}
    listed = [r.get('agent_id') for r in data.get('stopped_at_limit') or []]
    check('context subagents --session S2 lists the stopped teammate by agent id', listed == ['aengine-89abcdef01234567'],
          data)
    binding = store.session_task('S1')
    told = [e['payload'] for e in store.events(binding['task_id']) if e['kind'] == 'subagents_stopped_told'] \
        if binding else []
    check('the task records what it was told: subagents_stopped_told with the agent ids and stop times',
          bool(told) and all(a.get('agent_id') and a.get('stopped') for a in told[0].get('agents') or []), told[:1])


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-limit-stopped-'))
    try:
        store = Store(base / 'home')
        pay, mate, anon, credits = the_list(base, store)
        the_prompt(base, store, pay, mate, anon, credits)
        with_routes(base, store)
        silent(base, store, pay)
        shown(base, store)
        store.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print('\n%s — %d failed' % ('FAIL' if FAILED else 'all checks passed', len(FAILED)))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
