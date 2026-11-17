#!/usr/bin/env python3
"""Model-free check of routes: what a session that hits its usage limit offers, and the switch the user picks.

Observed 2026-11-17 on this Mac: 92 Claude Code transcript lines of `error: rate_limit` (status 429) — "You've hit your
5-hour limit · resets 2:50pm (Asia/Seoul)", "… weekly limit · resets Oct 19 at 6pm (Asia/Seoul)", "… session limit …",
"You're out of usage credits. Switch to another model to continue." Each time the session could do nothing until the
reset, and Orbit only waited: a limit was seen only in Orbit's own unattended runs (recovery.failure), the attached
session's next prompt failed again, and switching the model meant quitting and restarting by hand.

① reads the limit from transcripts shaped like those lines (and Codex's token counts), ② the prompt in a limited
session: blocked with the usable routes numbered, ③ a number picks a route: the pane is switched and the request is
carried to the task (with controls: a session with no limit, a limit already reset), ④ `route run` starts the host
with the route's endpoint and key (never in argv), ⑤ the way back and the end day, ⑥ hooks and the CLI, ⑦ end to end:
the real `claude` through a route to a local stand-in endpoint. Exit 0 = pass. Fails on code without routes.
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID',
             'CODEX_THREAD_ID', 'COSMOS_ORBIT_ROUTE', 'COSMOS_ORBIT_ROUTE_FROM'):
    os.environ.pop(name, None)
from types import SimpleNamespace as _Namespace  # noqa: E402
from unittest.mock import patch  # noqa: E402
from harness import attach, routes, typist  # noqa: E402
from harness.store import Store  # noqa: E402
from harness import evolve as _evolve, recovery as _recovery, supervision as _supervision  # noqa: E402
patch.object(_recovery, 'ensure_worker', return_value=None).start()
patch.object(_evolve, 'maybe_start', return_value=None).start()
patch.object(_supervision, 'subprocess', _Namespace(
    Popen=lambda *a, **k: _Namespace(pid=None), run=subprocess.run, DEVNULL=subprocess.DEVNULL,
    SubprocessError=subprocess.SubprocessError)).start()
patch.object(attach, 'orbit_command', return_value='cosmos-orbit').start()
patch.object(routes.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name if name in ('claude', 'codex') else None).start()

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:500]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def git(root, *args):
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True, capture_output=True)


def make_repo(base, name):
    root = base / name
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'app.py').write_text('print("hello")\n')
    git(root, 'add', '.')
    git(root, 'commit', '-q', '-m', 'files')
    return root


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def answer(at, text='Done: the tests pass.'):
    return {'type': 'assistant', 'uuid': 'a-%s' % at, 'timestamp': iso(at), 'isSidechain': False,
            'message': {'model': 'claude-opus-5-5', 'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}


def limit_line(at, text, resets=None, kind='five_hour', error='rate_limit'):
    """The synthetic line Claude Code writes when a turn stops at a usage limit (shape copied from this Mac)."""
    return {'type': 'assistant', 'uuid': 'l-%s' % at, 'timestamp': iso(at), 'isSidechain': False,
            'isApiErrorMessage': True, 'error': error, 'apiErrorStatus': 429,
            'quotaLimits': ({'status': 'rejected', 'resetsAt': resets, 'rateLimitType': kind,
                             'overageStatus': 'rejected', 'overageDisabledReason': 'out_of_credits'} if resets else None),
            'message': {'model': '<synthetic>', 'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}


def write(path, records):
    with open(path, 'w', encoding='utf-8') as handle:
        for record in records:
            handle.write(json.dumps(record) + '\n')


NOW = time.time()
KST = datetime.timezone(datetime.timedelta(hours=9))

print('① the limit, read from transcripts shaped like this Mac\'s')
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / 't.jsonl'
    write(path, [answer(NOW - 600), limit_line(NOW - 60, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", NOW + 3600)])
    found = routes.claude_limit(path)
    check('a 5-hour limit with quotaLimits: its kind and its reset time', found and found['kind'] == '5-hour'
          and found['resets_at'] == NOW + 3600, found)
    seen = datetime.datetime(2026, 10, 15, 1, 55, 36, tzinfo=datetime.timezone.utc).timestamp()
    write(path, [limit_line(seen, "You've hit your weekly limit · resets Oct 19 at 6pm (Asia/Seoul)")])
    found = routes.claude_limit(path)
    check('a weekly limit with a date and no quotaLimits: the reset read from the text (10-19 18:00 KST)',
          found and found['kind'] == 'weekly' and found['resets_at'] == datetime.datetime(2026, 10, 19, 18, 0, tzinfo=KST).timestamp(), found)
    seen = datetime.datetime(2026, 10, 20, 5, 8, 17, tzinfo=datetime.timezone.utc).timestamp()   # 14:08 KST
    write(path, [limit_line(seen, "You've hit your session limit · resets 2:40pm (Asia/Seoul)")])
    found = routes.claude_limit(path)
    check('a session limit: 2:40pm the same day (14:40 KST)', found and found['resets_at']
          == datetime.datetime(2026, 10, 20, 14, 40, tzinfo=KST).timestamp(), found)
    write(path, [limit_line(NOW - 30, "You're out of usage credits. Switch to another model to continue.")])
    found = routes.claude_limit(path)
    check('out of credits: a limit with no reset time', found and found['kind'] == 'credits' and found['resets_at'] is None, found)
    write(path, [limit_line(NOW - 600, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", NOW + 3600), answer(NOW - 30)])
    check('control: a real answer after the limit line — no limit holds', routes.claude_limit(path) is None)
    sub = dict(limit_line(NOW - 30, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", NOW + 3600), isSidechain=True)
    write(path, [answer(NOW - 600), sub])
    check('a subagent\'s limit line is not the session\'s', routes.claude_limit(path) is None)
    codex = Path(tmp) / 'rollout.jsonl'

    def count(reached, resets):
        return {'timestamp': iso(NOW - 20), 'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {
            'limit_id': 'codex', 'primary': {'used_percent': 100.0, 'window_minutes': 300, 'resets_at': resets},
            'secondary': None, 'rate_limit_reached_type': reached}}}
    write(codex, [count('workspace_member_credits_depleted', NOW + 1800)])
    found = routes.codex_limit(codex)
    check('Codex: the token count says a limit was reached, with its reset', found and found['resets_at'] == NOW + 1800, found)
    write(codex, [count(None, NOW + 1800)])
    check('control: Codex reached nothing', routes.codex_limit(codex) is None)
    check('active: until the reset, then not', routes.active({'resets_at': NOW + 5}, NOW)
          and not routes.active({'resets_at': NOW - 5}, NOW))
    check('active: an unknown reset holds an hour after it was seen', routes.active({'seen_at': NOW - 600}, NOW)
          and not routes.active({'seen_at': NOW - 4000}, NOW))

print('② a prompt in a limited session: blocked, the usable routes numbered')
tmp = tempfile.TemporaryDirectory()
base = Path(tmp.name)
store = Store(base / 'home')
repo = make_repo(base, 'repo')
attach.set_switch(store, 'on', repo)
os.environ['ROUTES_TEST_KEY'] = 'secret-test-key-value'
routes.add(store, 'kimi', 'https://ollama.com', 'kimi-k3.1-pro', 'env:ROUTES_TEST_KEY',
           'Kimi K3.1 Pro on Ollama cloud', (datetime.date.today() + datetime.timedelta(days=29)).isoformat())
routes.add(store, 'trial', 'https://example.test', 'old-model', 'env:ROUTES_TEST_KEY', 'an ended trial',
           (datetime.date.today() - datetime.timedelta(days=1)).isoformat())
routes.add(store, 'nokey', 'https://example.test', 'm', 'env:ROUTES_TEST_MISSING', 'a route without its key')
transcript = base / 'session.jsonl'
write(transcript, [answer(NOW - 900)])
session = 's-limited'
REQUEST = 'refactor the parser and keep the old behaviour for empty input'
first = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': session, 'prompt': 'add a parser for the config file',
                                         'transcript_path': str(transcript)}, 'claude')
task = attach.current_task(store, repo)
check('the session works on a task', bool(task) and not (first or {}).get('decision'), first)
write(transcript, [answer(NOW - 900), limit_line(NOW - 60, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", NOW + 5400)])
reply = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': session, 'prompt': REQUEST,
                                         'transcript_path': str(transcript)}, 'claude')
reason = (reply or {}).get('reason') or ''
print('\n'.join('      | ' + line for line in reason.splitlines()))
check('the prompt is blocked: it would only fail', (reply or {}).get('decision') == 'block', reply)
check('it says which limit and when it resets', '5-hour limit' in reason and 'resets' in reason, reason)
check('the usable routes are numbered, built-in first: 1 codex, 2 kimi (with its end day)',
      '  1  codex' in reason and '  2  kimi' in reason and 'days left' in reason, reason)
check('the limited route itself is not offered', '  claude' not in reason.split('\n', 2)[-1].split('  -  ')[0] and
      not any(line.strip().startswith(('1  claude', '2  claude', '3  claude')) for line in reason.splitlines()), reason)
check('the unusable ones say why: ended, no key', 'trial' in reason and 'ended' in reason and 'nokey' in reason and 'no key' in reason, reason)
check('the request typed is held, to be carried over', 'carried over' in reason, reason)
check('the key never appears', 'secret-test-key-value' not in reason)
check('the limit is recorded on the route (claude)', routes.active(routes.route_limit(store, 'claude')))

print('③ a number picks a route: the pane switches, the request is carried to the task')
spawned = []
terminal = {'kind': 'tmux', 'cli': '/usr/bin/true', 'target': '%9'}
with patch.object(typist, 'detect', return_value=terminal), \
        patch.object(typist, 'spawn', side_effect=lambda term, steps, log, home=None, task=None: spawned.append((term, steps, task)) or 1), \
        patch.object(routes, 'host_pid', return_value=4242):
    picked = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': session, 'prompt': '2',
                                              'transcript_path': str(transcript)}, 'claude')
reason = (picked or {}).get('reason') or ''
check('typing 2 switches to kimi (the reply says so)', (picked or {}).get('decision') == 'block' and 'switching this pane to kimi' in reason, picked)
data = store.get(task['id'])['data']
check('the held request is carried to the task', (data.get('carried_prompt') or {}).get('text') == REQUEST, data.get('carried_prompt'))
state = data.get('handoff_state') or {}
check('the task waits for the session that starts in this pane', state.get('state') == 'auto' and state.get('panel') == '%9'
      and state.get('session') == session and state.get('route') == 'kimi', state)
steps = spawned[0][1] if spawned else []
texts = [value for kind, value in steps if kind == 'text']
check('typed: the input emptied, /exit, then the route started once the host is gone',
      ('key', 'ctrl+u') in steps and texts[0] == '/exit' and ('gone', [4242, 30.0]) in steps
      and texts[-1] == 'cosmos-orbit route run kimi --carry --from claude'
      and steps.index(('gone', [4242, 30.0])) < steps.index(('text', texts[-1])), steps)
check('nothing typed holds the key', all('secret-test-key-value' not in str(value) for _, value in steps))
check('the offer is used up: typing 2 again is an ordinary prompt', not (routes._json(store, routes.SESSION_KEY + session).get('offer')))
# The route starts in the same pane: a new process, so SessionStart says `startup` (not `clear`). It takes the task
# over and its first prompt is the carry line `route run --carry` passes.
fresh = 's-on-kimi'
with patch.object(typist, 'detect', return_value=terminal):
    started = attach.handle(store, 'session-start', {'cwd': str(repo), 'session_id': fresh, 'source': 'startup',
                                                      'transcript_path': str(base / 'fresh.jsonl')}, 'claude')
    brief = ((started or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
    carried = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': fresh, 'prompt': typist.CARRY_LINE,
                                               'transcript_path': str(base / 'fresh.jsonl')}, 'claude')
bound = store.session_task(fresh)
check('the new session in the pane takes the task over', bound and bound['task_id'] == task['id'], bound)
check('its brief carries the request typed before the switch', REQUEST in brief, brief[:600])
check('its first prompt (the carry line) is not blocked', not (carried or {}).get('decision'), carried)
with patch.object(typist, 'detect', return_value=None):
    write(transcript, [limit_line(NOW - 60, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", NOW + 5400, )])
    store.preference(routes.SESSION_KEY + 's-plain', json.dumps({}))
    attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': 's-plain', 'prompt': 'go on', 'transcript_path': str(transcript)}, 'claude')
    manual = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': 's-plain', 'prompt': 'kimi',
                                              'transcript_path': str(transcript)}, 'claude')
check('no pane Orbit can type into: the reply gives the command to run', 'route run kimi' in ((manual or {}).get('reason') or '')
      and '/exit' in ((manual or {}).get('reason') or ''), manual)
codex_steps = routes.switch_steps('codex', 5, 77, 'cosmos-orbit route run claude --carry')
check('Codex: nothing to empty (its blocked prompt does not come back), /exit then the route',
      ('key', 'ctrl+u') not in codex_steps and ('text', '/exit') in codex_steps and ('gone', [77, 30.0]) in codex_steps)
# controls
plain = base / 'plain.jsonl'
write(plain, [answer(NOW - 30)])
out = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': 's-fine', 'prompt': 'add a test for the parser',
                                       'transcript_path': str(plain)}, 'claude')
check('control: a session that is not limited is not blocked', not (out or {}).get('decision'), out)
reset = base / 'reset.jsonl'
write(reset, [limit_line(NOW - 7200, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", NOW - 60)])
out = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': 's-reset', 'prompt': 'try again now',
                                       'transcript_path': str(reset)}, 'claude')
check('control: a limit that has reset is not blocked', not (out or {}).get('decision'), out)
credits = base / 'credits.jsonl'
write(credits, [limit_line(NOW - 30, "You're out of usage credits. Switch to another model to continue.")])
one = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': 's-credits', 'prompt': 'continue the work',
                                       'transcript_path': str(credits)}, 'claude')
two = attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': 's-credits', 'prompt': 'continue the work',
                                       'transcript_path': str(credits)}, 'claude')
check('no reset time known: the offer is shown once, then the user may simply try again',
      (one or {}).get('decision') == 'block' and not (two or {}).get('decision'), (one, two))

print('④ route run: the host with the route\'s endpoint and key; the key never in argv')
ran = []
exec_capture = lambda program, argv, env: ran.append((program, argv, env))  # noqa: E731
routes.run(store, 'kimi', carry=True, came_from='claude', execvpe=exec_capture, environ={'PATH': '/bin', 'ROUTES_TEST_KEY': 'secret-test-key-value'},
           out=open(os.devnull, 'w'))
program, argv, env = ran[-1]
check('claude --model kimi-k3.1-pro … with the carry line last', argv[:3] == ['claude', '--model', 'kimi-k3.1-pro']
      and argv[-1] == typist.CARRY_LINE, argv)
check('the endpoint and key in the environment, the API key emptied', env.get('ANTHROPIC_BASE_URL') == 'https://ollama.com'
      and env.get('ANTHROPIC_AUTH_TOKEN') == 'secret-test-key-value' and env.get('ANTHROPIC_API_KEY') == '', env)
check('every model Claude Code asks for is the route\'s', all(env.get(v) == 'kimi-k3.1-pro' for v in routes.MODEL_VARS))
check('the session knows its route and the one it left', env.get(routes.ROUTE_ENV) == 'kimi' and env.get(routes.FROM_ENV) == 'claude')
check('the key is not in argv', 'secret-test-key-value' not in ' '.join(argv))
store = Store(base / 'home')
try:
    routes.run(store, 'trial', execvpe=exec_capture, environ={'ROUTES_TEST_KEY': 'k'}, out=open(os.devnull, 'w'))
    refused = False
except ValueError as error:
    refused = 'ended' in str(error)
check('a route past its last day is refused', refused)
store = Store(base / 'home')
routes.run(store, 'claude', execvpe=exec_capture, environ=dict(env), out=open(os.devnull, 'w'))
_, argv, env2 = ran[-1]
check('back to claude from inside a kimi session: none of kimi\'s endpoint leaks', argv == ['claude'] and not any(
    v in env2 for v in routes.ENDPOINT_VARS) and env2.get(routes.ROUTE_ENV) == 'claude', (argv, sorted(k for k in env2 if 'ANTHROPIC' in k)))
store = Store(base / 'home')

print('⑤ the way back, and the end day')
store.preference(routes.LIMIT_KEY + 'claude', json.dumps({'id': 'L1', 'kind': '5-hour', 'resets_at': NOW - 60}))
kimi_env = {routes.ROUTE_ENV: 'kimi', routes.FROM_ENV: 'claude'}
note = routes.prompt_note(store, {'session_id': 's-kimi'}, 'claude', environ=kimi_env)
check('on kimi, once claude\'s limit reset: the model is told to say so, once', note and 'route claude is usable again' in note, note)
check('… only once', routes.prompt_note(store, {'session_id': 's-kimi'}, 'claude', environ=kimi_env) is None)
check('control: a session on its own account is told nothing', routes.prompt_note(store, {'session_id': 's-home'}, 'claude', environ={}) is None)
check('typing claude then switches back (the offer holds it)', routes._pick('claude', routes._json(store, routes.SESSION_KEY + 's-kimi')['offer']) == 'claude')
routes.add(store, 'ending', 'https://ollama.com', 'm', 'env:ROUTES_TEST_KEY', 'ends soon', (datetime.date.today() + datetime.timedelta(days=2)).isoformat())
note = routes.prompt_note(store, {'session_id': 's-ending'}, 'claude', environ={routes.ROUTE_ENV: 'ending'})
check('a route that ends within three days says so', note and 'ends' in note and '2 days left' in note, note)

print('⑥ hooks and the command line')
check('Claude Code gets the StopFailure hook (a turn ended on an API error)',
      ('StopFailure', 'stop-failure', 10) in attach.HOST_EVENTS['claude'] and 'StopFailure' in attach.hook_entries('claude'))
store.preference(routes.LIMIT_KEY + 'claude', '')
stop = base / 'stop.jsonl'
write(stop, [limit_line(NOW - 5, "You've hit your weekly limit · resets Oct 19 at 6pm (Asia/Seoul)", NOW + 86400, 'seven_day')])
told = []
with patch('harness.attention.deliver', side_effect=lambda store_, task_, body: told.append(body) or ['tmux']):
    attach.handle(store, 'stop-failure', {'cwd': str(repo), 'session_id': session, 'transcript_path': str(stop),
                                           'hook_event_name': 'StopFailure', 'error': 'rate_limit'}, 'claude')
check('StopFailure records the limit on the route', routes.active(routes.route_limit(store, 'claude')))
check('… and announces it in the task\'s panes with the routes', told and 'weekly limit' in told[0] and 'kimi' in told[0], told)
home = base / 'cli-home'
cli = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(home)]
done = subprocess.run(cli + ['hook', 'stop-failure', '--host', 'claude'], input=json.dumps({'session_id': 'x', 'cwd': str(repo)}),
                      capture_output=True, text=True, timeout=60)
check('the CLI takes the stop-failure hook (exit 0, nothing printed)', done.returncode == 0 and not done.stdout.strip(), done.stderr[-300:])
added = subprocess.run(cli + ['route', 'add', 'kimi', '--base-url', 'https://ollama.com', '--model', 'kimi-k3.1-pro',
                              '--key', 'env:ROUTES_TEST_KEY', '--until', '2099-01-01'], capture_output=True, text=True, timeout=60)
listed = subprocess.run(cli + ['route', 'list'], capture_output=True, text=True, timeout=60)
check('route add, then route list shows it with its end day', added.returncode == 0 and 'kimi' in listed.stdout
      and 'until 2099-01-01' in listed.stdout, listed.stdout + added.stderr)
check('routes.json is private (0600)', oct((home / 'routes.json').stat().st_mode & 0o777) == '0o600')
bad = subprocess.run(cli + ['route', 'add', 'claude', '--base-url', 'https://x.test', '--model', 'm', '--key', 'env:X'],
                     capture_output=True, text=True, timeout=60)
check('a built-in route cannot be redefined', bad.returncode != 0)
sequence, clock = [True, True, False], [0.0]
ran_steps = typist.run(terminal, [('gone', [4242, 5.0])], runner=lambda argv: 0, sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                       clock=lambda: clock[0], alive=lambda pid: sequence.pop(0))
check('the typist waits until the host is gone', sequence == [])
try:
    typist.run(terminal, [('gone', [4242, 1.0]), ('text', 'never')], runner=lambda argv: 0,
               sleep=lambda s: clock.__setitem__(0, clock[0] + s), clock=lambda: clock[0], alive=lambda pid: True)
    stuck = False
except RuntimeError as error:
    stuck = 'did not quit' in str(error)
check('… and types nothing more when it never goes', stuck)

print('⑦ end to end: the real claude through a route, to a local stand-in endpoint')
claude = __import__('shutil').which('claude')
if not claude:
    print('  skip (claude is not installed)')
else:
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    log = base / 'stub.log'
    token = 'stub-token-%d' % os.getpid()
    server = subprocess.Popen([sys.executable, str(ROOT / 'tests' / 'stub_anthropic.py'), str(port), str(log), token])
    try:
        time.sleep(1.0)
        subprocess.run(cli + ['route', 'add', 'stub', '--base-url', 'http://127.0.0.1:%d' % port, '--model', 'stub-model',
                              '--key', 'env:ROUTES_STUB_KEY'], capture_output=True, timeout=60)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC_', 'COSMOS_ORBIT_ROUTE'))}
        env['ROUTES_STUB_KEY'] = token
        result = subprocess.run(cli + ['route', 'run', 'stub', '--', '-p', 'Reply with anything.', '--setting-sources', '',
                                       '--strict-mcp-config'], capture_output=True, text=True, timeout=180, env=env, cwd=str(base))
        seen = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        messages = [r for r in seen if r['path'].startswith('/v1/messages')]
        check('claude answered with the stand-in\'s text', 'ROUTE-STUB-ANSWER' in result.stdout, (result.returncode, result.stdout[-200:], result.stderr[-300:]))
        check('every request went to the route\'s endpoint, for the route\'s model, with its key',
              messages and all(r['model'] == 'stub-model' and r['token_ok'] for r in messages), seen)
        check('the key is in none of the output', token not in result.stdout + result.stderr)
    finally:
        server.kill()

tmp.cleanup()
print('\n%s' % ('PASS' if not FAILED else 'FAIL: ' + ', '.join(FAILED)))
sys.exit(1 if FAILED else 0)
