#!/usr/bin/env python3
"""Model-free check of idle teammates (§Teammates): the lead told who sits idle and why, and the Stop guard letting the
lead's turn end while teammates work or hold reports it has not read.

Why (measured 2026-11-25 on one lead and its teammates, Claude Code 2.1.33x): a teammate ends each turn idle and only a
message wakes it; what it sends the lead ("Message sent to team-lead's inbox") reaches the lead only when the lead's own
turn ends. At 18:47 the lead tried to stop, Orbit's manager guard sent it back ("stopped before the work was
finished"), and it waited inside its turn: five teammates' reports (two handoff requests, two waiting for the user, one
for permission) sat for three hours. Nothing told the lead who was idle or why. Replayed here with synthetic
transcripts shaped as Claude Code writes them (an end_turn answer, then the SubagentStop attachment 0.1 s later; a wake
is a <teammate-message> user line; the lead reads a teammate's message as a user line "Another Claude session sent a
message: <teammate-message teammate_id=…>"):

  ① the lead's line: none while every teammate works; one line when a teammate goes idle (name, how long, its state
    line, what to do); not again for the same idle state; again when the state changes; no state line → the answer's
    last line; at most 700 characters; a teammate that left its team, Orbit off, a usage-limit end: nothing
  ② a handoff: a teammate idle since before the lead's conversation changed is still seen (the task's earlier
    transcript, and the folder of the session that made the team)
  ③ the Stop guard: a teammate working, or idle with a report the lead has not read, lets the turn end (one line for
    the user) instead of "stopped before the work was finished"; an idle teammate asking for a handoff (or done) sends
    the turn back once with what to do; with every report read, the guard decides as before; the supervisor waits
    for the same teammates instead of typing a continuation

Controls: the same guard blocks with no teammates, and with idle teammates whose reports were read. On main (without
this change) the positive checks fail. Nothing starts a model or touches the live Orbit home or host settings.
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
              'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME'):
    os.environ.pop(_name, None)
CLI = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit')]
from harness import attach, supervision, teammates  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []
TEAM = 'session-i1'


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:400]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def iso(ago=0.0):
    moment = datetime.datetime.fromtimestamp(time.time() - ago, datetime.timezone.utc)
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + '%03dZ' % (moment.microsecond // 1000)


def line(path, event):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')


def usage(tokens=120_000):
    return {'input_tokens': 5, 'cache_creation_input_tokens': 995, 'cache_read_input_tokens': tokens - 1000}


def begin(sub, name, team=TEAM, hexid='0123456789abcdef', meta=True):
    """A teammate's transcript as Claude Code starts it: the lead's instruction. Returns (agent id, path)."""
    agent = 'a%s-%s' % (name, hexid)
    path = sub / ('agent-%s.jsonl' % agent)
    line(path, {'type': 'user', 'isSidechain': True, 'agentId': agent, 'timestamp': iso(3600),
                'message': {'role': 'user', 'content': '<teammate-message teammate_id="team-lead">work</teammate-message>'}})
    if meta:
        data = {'agentType': name, 'name': name, 'taskKind': 'in_process_teammate'}
        if team:
            data['teamName'] = team
        (sub / ('agent-%s.meta.json' % agent)).write_text(json.dumps(data))
    return agent, path


def working(path, agent, ago=0.0):
    """Mid-turn: a request, a tool call, its result."""
    line(path, {'type': 'api-request', 'agentId': agent, 'timestamp': iso(ago + 2)})
    line(path, {'type': 'assistant', 'isSidechain': True, 'agentId': agent, 'timestamp': iso(ago + 1),
                'message': {'id': 'msg_w%d' % int(time.time() * 1000), 'model': 'claude-test', 'role': 'assistant',
                            'stop_reason': 'tool_use', 'usage': usage(),
                            'content': [{'type': 'tool_use', 'id': 'toolu_w', 'name': 'Bash', 'input': {'command': 'ls'}}]}})
    line(path, {'type': 'user', 'isSidechain': True, 'agentId': agent, 'timestamp': iso(ago),
                'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_w', 'content': 'ok'}]}})


def answer(path, agent, text, ago=0.0, split=False):
    """The turn's last answer (stop_reason end_turn) and, 0.1 s later, the SubagentStop hook's attachment. split: the
    answer written over two lines of the same message id (one per content block)."""
    message_id = 'msg_%d' % int(time.time() * 1e6)
    blocks = [text] if not split else text.split('\n', 1)
    for i, block in enumerate(blocks):
        line(path, {'type': 'assistant', 'isSidechain': True, 'agentId': agent, 'timestamp': iso(ago),
                    'message': {'id': message_id, 'model': 'claude-test', 'role': 'assistant',
                                'stop_reason': 'end_turn' if i == len(blocks) - 1 else None, 'usage': usage(),
                                'content': [{'type': 'text', 'text': block}]}})
    line(path, {'type': 'attachment', 'isSidechain': True, 'agentId': agent, 'timestamp': iso(max(0, ago - 0.1)),
                'attachment': {'type': 'hook_success', 'hookEvent': 'SubagentStop'}})


def woken(path, agent):
    """A message from the lead wakes it: the message, the SubagentStart attachment, a request."""
    line(path, {'type': 'user', 'isSidechain': True, 'agentId': agent, 'timestamp': iso(),
                'message': {'role': 'user', 'content': '<teammate-message teammate_id="team-lead">go on</teammate-message>'}})
    line(path, {'type': 'attachment', 'isSidechain': True, 'agentId': agent, 'timestamp': iso(),
                'attachment': {'type': 'hook_success', 'hookEvent': 'SubagentStart'}})
    line(path, {'type': 'api-request', 'agentId': agent, 'timestamp': iso()})


def limit_end(path, agent):
    """Claude Code's own line when the account hit its usage limit (subagents.py): not an answer."""
    line(path, {'type': 'assistant', 'isSidechain': True, 'agentId': agent, 'timestamp': iso(), 'isApiErrorMessage': True,
                'message': {'id': 'msg_limit', 'model': '<synthetic>', 'role': 'assistant', 'stop_reason': 'stop_sequence',
                            'usage': {'input_tokens': 0}, 'content': [{'type': 'text', 'text': "You've hit your limit"}]}})


def delivered(lead_transcript, *names, ago=0.0):
    """The lead read these teammates' messages: one user line, as Claude Code writes it between the lead's turns."""
    body = ''.join('<teammate-message teammate_id="%s" color="blue">{"type":"idle_notification","from":"%s"}'
                   '</teammate-message>\n' % (n, n) for n in names)
    line(lead_transcript, {'type': 'user', 'isSidechain': False, 'timestamp': iso(ago),
                           'message': {'role': 'user', 'content': 'Another Claude session sent a message:\n' + body}})


def team(base, names, name=TEAM, lead_session=None):
    folder = base / 'claude' / 'teams' / name
    folder.mkdir(parents=True, exist_ok=True)
    data = {'name': name, 'members': [{'name': 'team-lead'}] + [{'name': n} for n in names]}
    if lead_session:
        data['leadSessionId'] = lead_session
    (folder / 'config.json').write_text(json.dumps(data))


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'app.py').write_text('print("hello")\n')
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '-m', 'f'], check=True)
    return root


def transcript_of(base, session):
    path = base / 'claude' / 'projects' / '-tmp-repo' / (session + '.jsonl')
    line(path, {'type': 'assistant', 'isSidechain': False, 'timestamp': iso(4000),
                'message': {'model': 'claude-test', 'role': 'assistant', 'usage': usage(120_000)}})
    return path


def lead(base, store, session, root=None):
    """A lead session with a task, Orbit on in its repository. Returns (payload, its subagents folder)."""
    root = root or make_repo(base, 'repo-' + session)
    attach.set_switch(store, 'on', root)
    transcript = transcript_of(base, session)
    pay = {'cwd': str(root), 'session_id': session, 'transcript_path': str(transcript)}
    attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    attach.handle(store, 'prompt', dict(pay, prompt='Split the work across teammates and keep the tests green'), 'claude')
    return pay, teammates.subagents_dir(transcript)


def note(out):
    spec = (out or {}).get('hookSpecificOutput') or {}
    return spec.get('additionalContext') or ''


def idle_lines(text):
    return [l for l in (text or '').splitlines() if l.startswith('[orbit] TEAMMATES idle:')]


def prompt(store, pay, text='how are the teammates doing?'):
    return idle_lines(note(attach.handle(store, 'prompt', dict(pay, prompt=text), 'claude')))


def shell(base, home, pay):
    payload = {'session_id': pay['session_id'], 'transcript_path': pay['transcript_path'], 'cwd': pay['cwd'],
               'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'toolu_1',
               'tool_input': {'command': 'ls', 'description': 'x'}}
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(base / 'claude'))
    out = subprocess.run(CLI + ['--home', str(home), 'hook', 'pre-tool', '--host', 'claude'], input=json.dumps(payload),
                         capture_output=True, text=True, cwd=str(base), env=env, timeout=120).stdout
    return idle_lines(note(json.loads(out) if out.strip() else None))


def the_line(base, store):
    print('① the lead\'s line')
    pay, sub = lead(base, store, 'I1')
    team(base, ['alpha', 'beta', 'gamma', 'delta'])
    a_id, a_path = begin(sub, 'alpha')
    b_id, b_path = begin(sub, 'beta', hexid='1111111111111111')
    g_id, g_path = begin(sub, 'gamma', hexid='2222222222222222')
    d_id, d_path = begin(sub, 'delta', hexid='3333333333333333')
    for agent, path in ((a_id, a_path), (b_id, b_path), (g_id, g_path), (d_id, d_path)):
        working(path, agent)
    check('control: every teammate working — no idle line at the prompt', prompt(store, pay) == [])
    check('control: … nor at the lead\'s shell call', shell(base, store.home, pay) == [])
    answer(b_path, b_id, 'Wrote the note.\n상태: 교대요청(/tmp/notes/handoff-beta.md)', ago=12 * 60 + 5)
    lines = prompt(store, pay)
    check('a teammate idle with a handoff request: one line — its name, 12m, its state line', len(lines) == 1
          and 'beta 12m — 상태: 교대요청(/tmp/notes/handoff-beta.md)' in lines[0], lines)
    check('… what to do: a new teammate given only that handoff note', bool(lines) and 'handoff note' in lines[0]
          and 'new teammate' in lines[0], lines)
    check('… and only the idle one (alpha, gamma, delta work)', bool(lines) and all(n not in lines[0] for n in
                                                                                     ('alpha', 'gamma', 'delta')), lines)
    check('the same idle state again: no second line at the prompt', prompt(store, pay) == [])
    check('… nor at the shell call', shell(base, store.home, pay) == [])
    answer(g_path, g_id, 'Pushed the branch and opened the PR for review.', ago=3 * 3600 + 5)
    lines = shell(base, store.home, pay)
    check('a newly idle teammate with no state line: told at the shell call, with its last line', len(lines) == 1
          and 'gamma 3h — last line: Pushed the branch and opened the PR for review. handoff requested' in lines[0], lines)
    check('… the handoff request listed first', bool(lines) and lines[0].index('beta') < lines[0].index('gamma'), lines)
    woken(b_path, b_id)
    working(b_path, b_id)
    answer(b_path, b_id, 'Merged.\n**상태:** 끝', ago=30)
    lines = prompt(store, pay)
    check('the state changed (woken, then done): told again, with "done → read its report, then shut it down"',
          len(lines) == 1 and 'beta 0m — 상태: 끝' in lines[0] and 'done → read its report' in lines[0]
          and 'handoff note' not in lines[0], lines)
    answer(d_path, d_id, 'First block of the answer.\n상태: 사용자대기(#12 · merge)', ago=60, split=True)
    lines = prompt(store, pay)
    check('an answer written over two lines of one message: its state line read, "waiting for the user"',
          len(lines) == 1 and 'delta 1m — 상태: 사용자대기(#12 · merge)' in lines[0] and 'waiting for the user' in lines[0],
          lines)
    woken(a_path, a_id)
    limit_end(a_path, a_id)
    check('a usage-limit end (Claude Code\'s own line) is not idle here', 'alpha' not in ' '.join(prompt(store, pay)))
    tid = attach.current_task(store, Path(pay['cwd']))['id']
    told = [e['payload'] for e in store.events(tid) if e['kind'] == 'teammates_idle']
    check('the ledger holds what the lead was told (4 lines)', len(told) == 4 and told[-1]['teammates'][0]['kind']
          in ('done', 'waiting'), told)
    many = []
    for i in range(8):
        agent, path = begin(sub, 'echo%d' % i, hexid='%016x' % (i + 16))
        answer(path, agent, 'State: handoff requested (/tmp/a/very/long/folder/name/that/keeps/going/and/going/'
                            'handoff-echo%d.md) after a long day of work on many things' % i, ago=120)
        many.append('echo%d' % i)
    team(base, ['alpha', 'beta', 'gamma', 'delta'] + many)
    lines = prompt(store, pay)
    check('eleven idle teammates with long states: one line, at most 700 characters, the guide kept', len(lines) == 1
          and len(lines[0]) <= 700 and 'handoff note' in lines[0], (len(lines[0]) if lines else None, lines))
    team(base, ['alpha', 'beta', 'gamma'])                            # delta and the echoes left the team
    answer(g_path, g_id, 'Still here.\n상태: 진행중', ago=5)
    lines = prompt(store, pay)
    check('a teammate that left its team is not listed', len(lines) == 1 and 'delta' not in lines[0]
          and 'echo' not in lines[0], lines)
    attach.set_switch(store, 'off', Path(pay['cwd']))
    answer(g_path, g_id, 'Again.\n상태: 진행중(2)', ago=1)
    check('Orbit off for the lead\'s project: no line', prompt(store, pay) == [] and shell(base, store.home, pay) == [])
    attach.set_switch(store, 'on', Path(pay['cwd']))


def after_handoff(base, store):
    print('② after a handoff')
    pay, sub = lead(base, store, 'H1')
    team(base, ['kilo', 'lima'], name='session-h1', lead_session='H1')
    k_id, k_path = begin(sub, 'kilo', team='session-h1')
    answer(k_path, k_id, 'Note written.\n상태: 교대요청(/tmp/notes/handoff-kilo.md)', ago=11 * 60)
    tid = attach.current_task(store, Path(pay['cwd']))['id']
    fresh = transcript_of(base, 'H2')                                # the lead's new conversation, same task
    store.end_session('H1')                                          # the handoff ended the earlier conversation
    store.bind_session('H2', tid, pay['cwd'])
    pay2 = dict(pay, session_id='H2', transcript_path=str(fresh))
    start = attach.handle(store, 'session-start', dict(pay2, source='clear'), 'claude')
    lines = idle_lines(note(start))
    check('the new conversation\'s session start: the teammate idle in the earlier conversation\'s folder is listed',
          len(lines) == 1 and 'kilo 11m — 상태: 교대요청(/tmp/notes/handoff-kilo.md)' in lines[0], note(start)[-500:])
    beside = transcript_of(base, 'H4')                               # another live conversation on the same task
    store.bind_session('H4', tid, pay['cwd'])
    store.preference('transcript:H4', str(beside))
    team(base, ['kilo', 'lima', 'mono'], name='session-h4')
    o_id, o_path = begin(teammates.subagents_dir(beside), 'mono', team='session-h4')
    answer(o_path, o_id, 'Mine.\n상태: 끝', ago=60)
    check('control: a conversation beside it on the same task (not ended) is not followed — its teammate is not listed',
          'mono' not in ' '.join(prompt(store, pay2)))
    l_id, l_path = begin(teammates.subagents_dir(fresh), 'lima', team='session-h1')   # written after the handoff
    working(l_path, l_id)
    other = transcript_of(base, 'H3')                                # a lead without the task: the team's own folder
    begin(teammates.subagents_dir(other), 'lima', team='session-h1', hexid='9999999999999999')
    rows = {r['name']: r for r in teammates.turns([str(other)])}
    check('no task to follow: the folder of the session that made the team (leadSessionId) is read too',
          rows.get('kilo', {}).get('idle') is True and rows['kilo'].get('kind') == 'handoff', rows)


def stop_guard(base, store):
    print('③ the Stop guard')
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    root = make_repo(base, 'repo-stop')
    attach.set_switch(store, 'on', root)
    session = 'S1'
    transcript = transcript_of(base, session)
    task = attach.create(store, root, 'Build the importer in two stages with teammates', 'claude', session,
                         done=['First stage built', 'Second stage built'])
    tid = task['id']
    attach.record_evidence(store, tid, 'D1', note='stage one ran on the sample file')
    sub = teammates.subagents_dir(transcript)
    team(base, ['mike', 'nova'], name='session-s1')
    m_id, m_path = begin(sub, 'mike', team='session-s1')

    def stop(active=False):
        fake = SimpleNamespace(Popen=MagicMock(), run=subprocess.run, DEVNULL=subprocess.DEVNULL,
                               SubprocessError=subprocess.SubprocessError)
        with patch.object(supervision, 'subprocess', fake):
            out = attach.handle(store, 'stop', {'cwd': str(root), 'session_id': session, 'stop_hook_active': active,
                                                'transcript_path': str(transcript),
                                                'last_assistant_message': 'Waiting for the teammates.'}, 'claude')
        supervision.begin_turn(store, store.get(tid), session)
        return out or {}

    team(base, [], name='session-s1')
    out = stop()
    check('control: no teammate running — the guard sends the turn back ("stopped before the work was finished")',
          out.get('decision') == 'block' and 'stopped before the work was finished' in out.get('reason', ''), out)
    team(base, ['mike', 'nova'], name='session-s1')
    working(m_path, m_id)
    out = stop()
    check('a teammate working: the turn may end (no block)', out.get('decision') != 'block', out)
    check('… one line for the user: "teammates working: mike"', 'teammates working: mike' in out.get('systemMessage', ''), out)
    check('the supervisor waits for the same teammate instead of typing a continuation',
          teammates.waits_on(store, session) == ['mike'])
    answer(m_path, m_id, 'Stage two halfway.\n상태: 진행중', ago=40)
    delivered(transcript, 'mike', ago=30)
    out = stop()
    check('control: idle and its report read by the lead — the guard decides as before (sent back)',
          out.get('decision') == 'block' and 'stopped before the work was finished' in out.get('reason', ''), out)
    check('… and the supervisor waits for nobody', teammates.waits_on(store, session) == [])
    woken(m_path, m_id)
    answer(m_path, m_id, 'Stage two done locally.\n상태: 진행중(테스트 중)', ago=5)
    out = stop()
    check('idle with a report the lead has not read: the turn may end, so it arrives',
          out.get('decision') != 'block' and 'idle with a report not yet read: mike' in out.get('systemMessage', ''), out)
    n_id, n_path = begin(sub, 'nova', team='session-s1', hexid='4444444444444444')
    answer(n_path, n_id, 'Handoff note written.\n상태: 교대요청(/tmp/notes/handoff-nova.md)', ago=3)
    out = stop()
    check('an idle teammate asking for a handoff: the turn is sent back once, with what to do and the note\'s path',
          out.get('decision') == 'block' and 'nova asked for a handoff' in out.get('reason', '')
          and '/tmp/notes/handoff-nova.md' in out.get('reason', '') and 'new teammate' in out.get('reason', ''), out)
    out = stop(active=True)
    check('… once: the next Stop is not sent back for it, and the unread reports let the turn end',
          out.get('decision') != 'block' and 'nova' in out.get('systemMessage', ''), out)
    out = stop()
    check('… nor the one after', out.get('decision') != 'block', out)
    delivered(transcript, 'mike', 'nova')
    out = stop()
    check('every report read, nobody working: the guard decides as before (sent back for D2)',
          out.get('decision') == 'block' and 'stopped before the work was finished' in out.get('reason', ''), out)
    events = [e['payload'] for e in store.events(tid) if e['kind'] == 'teammates_stop']
    check('the ledger holds each teammate decision at a Stop (allow, allow, block, allow, allow)',
          [e['decision'] for e in events] == ['allow', 'allow', 'block', 'allow', 'allow'], events)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-idle-teammates-'))
    os.environ['CLAUDE_CONFIG_DIR'] = str(base / 'claude')
    store = Store(base / 'home')
    for section in (the_line, after_handoff, stop_guard):
        try:
            section(base, store)
        except Exception:  # noqa: BLE001 - a section that cannot finish fails as one check; the next still runs
            import traceback
            check('%s: ran to the end' % section.__name__, False, traceback.format_exc()[-700:])
    store.close()
    shutil.rmtree(base, ignore_errors=True)
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
