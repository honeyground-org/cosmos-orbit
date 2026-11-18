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
the real `claude` through a route to a local stand-in endpoint.

Then what the user asked of it on 2026-11-17 ("사용자마다 각자의 키를 넣을 수 있어야해 … 사용 안할 수도 있고 사용할 수도
있고 사용시 다양한 모델을 고를수도 … 다른 서비스를 사용할 수도" · "범용으로 호환성이 있도록 … 모든 온오프등은 컨트롤이
되도록"): ⑧ the switches, the whole offer and each route, built-in or added (and a first-version routes.json still
works), ⑨ each user's own key put into their keychain by `route key`, never printed and never in argv, ⑩ a route with
several models, each offered and any one picked, ⑪ any service: an OpenAI Responses-compatible route runs Codex, an
Anthropic-compatible one may send its key as x-api-key — end to end with the real `codex` and `claude`, and no request
carries any credential but the route's (with ANTHROPIC_API_KEY alone Claude Code sent the user's own login instead).
⑫ ⑬ a switch the user asks for and the context carried. ⑭ (2026-11-18: "실제 날짜 모르겠어" — when a free period ends is not
known): a route whose service refuses for payment is seen when it happens, in Claude Code and in Codex, and is not
offered again until `route on NAME`; a 401 for a wrong key, a network error and Claude Code's own rate limit are not.
Exit 0 = pass. Fails on code without routes, and ⑧-⑪ on the first version of routes.
"""
import datetime
import json
import os
import re
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
      and re.fullmatch(r'cosmos-orbit route run kimi --carry --capsule [0-9a-f]{12} --from claude', texts[-1] or '')
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

print('⑧ the switches: the whole offer, and each route (built-in or added), on and off')
s8 = Store(base / 'home-switch')
routes.add(s8, 'kimi', 'https://ollama.com', 'kimi-k3.1-pro', 'env:ROUTES_TEST_KEY', 'Kimi on Ollama cloud')
limited = base / 'limited8.jsonl'
write(limited, [limit_line(NOW - 60, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", NOW + 5400)])


def offer8(sid, prompt='go on'):
    return routes.on_prompt(s8, {'session_id': sid, 'prompt': prompt, 'transcript_path': str(limited)}, 'claude', None)


on = offer8('s8-on')
check('control: on by default, a limited session gets the offer', routes.enabled(s8) and (on or {}).get('decision') == 'block', on)
routes.set_enabled(s8, False)
s8.preference(routes.LIMIT_KEY + 'claude', '')
check('route off: a limited session is left alone (no block, no offer)', offer8('s8-off') is None)
check('… the way-back note is off too', routes.prompt_note(s8, {'session_id': 's8-k'}, 'claude',
                                                          environ={routes.ROUTE_ENV: 'kimi', routes.FROM_ENV: 'claude'}) is None)
check('… StopFailure records and announces nothing', routes.on_stop_failure(
    s8, {'session_id': 's8-off', 'transcript_path': str(limited)}, 'claude', None) is None
      and routes.route_limit(s8, 'claude') is None)
routes.set_enabled(s8, True)
check('route on: the offer is back', (offer8('s8-back') or {}).get('decision') == 'block')
routes.set_enabled(s8, False, 'codex')
reason8 = (offer8('s8-nocodex') or {}).get('reason') or ''
check('route off codex: a built-in route leaves the offer, and says how to turn it on',
      '1  kimi' in reason8 and '  -  codex      off (route on codex)' in reason8, reason8)
try:
    routes.run(s8, 'codex', execvpe=exec_capture, environ={}, out=open(os.devnull, 'w'))
    refused = False
except ValueError as error:
    refused = 'off' in str(error)
check('… and route run refuses it while it is off', refused)
routes.set_enabled(s8, True, 'codex')
routes.set_enabled(s8, False, 'kimi')
reason8 = (offer8('s8-nokimi') or {}).get('reason') or ''
check('route off kimi: an added route leaves the offer, its settings kept', '1  codex' in reason8 and 'kimi       off' in reason8
      and routes.load(s8)['kimi']['model'] == 'kimi-k3.1-pro', reason8)
routes.set_enabled(s8, True, 'kimi')
try:
    routes.set_enabled(s8, False, 'nosuch')
    unknown = False
except ValueError:
    unknown = True
check('a switch for a route that does not exist is refused', unknown)
home8 = base / 'cli-switch'
cli8 = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(home8)]
subprocess.run(cli8 + ['route', 'off'], capture_output=True, timeout=60)
off_list = subprocess.run(cli8 + ['route', 'list'], capture_output=True, text=True, timeout=60).stdout
subprocess.run(cli8 + ['route', 'on'], capture_output=True, timeout=60)
subprocess.run(cli8 + ['route', 'off', 'codex'], capture_output=True, timeout=60)
on_list = subprocess.run(cli8 + ['route', 'list'], capture_output=True, text=True, timeout=60).stdout
check('the CLI: route off / on / off NAME, and route list shows each switch', off_list.startswith('ROUTES: off')
      and on_list.startswith('ROUTES: on') and any(l.split()[:2] == ['codex', 'off'] for l in on_list.splitlines()),
      off_list + on_list)
old = base / 'home-old'
old.mkdir()
(old / 'routes.json').write_text(json.dumps({'routes': {'legacy': {
    'host': 'claude', 'base_url': 'https://ollama.com', 'model': 'kimi-k3.1-pro', 'key': 'env:ROUTES_TEST_KEY',
    'label': 'written by the first version', 'until': None}}}))
s_old = Store(old)
routes.run(s_old, 'legacy', execvpe=exec_capture, environ={'ROUTES_TEST_KEY': 'k-legacy-value'}, out=open(os.devnull, 'w'))
_, argv_old, env_old = ran[-1]
check('a routes.json from the first version still works: on, one model, Claude Code with bearer',
      routes.enabled(s_old) and routes.route_on(routes.all_routes(s_old)['legacy']) and argv_old == ['claude', '--model', 'kimi-k3.1-pro']
      and env_old.get('ANTHROPIC_AUTH_TOKEN') == 'k-legacy-value', (argv_old, sorted(env_old)))

print('⑨ each user\'s own key: route key puts it in their keychain, never printed')
s9 = Store(base / 'home-key')
added9 = routes.add(s9, 'envonly', 'https://ollama.com', 'm', 'env:ROUTES_TEST_MISSING')
mine = 'ktest-%d' % (os.getpid() % 100000)
added9 = routes.add(s9, mine, 'https://ollama.com', 'kimi-k3.1-pro')
check('a route added without --key reads the user\'s own env var, then their keychain',
      added9['key'] == 'env:COSMOS_ORBIT_%s_KEY,keychain:cosmos-orbit-%s' % (mine.upper().replace('-', '_'), mine), added9['key'])
try:
    routes.set_key(s9, 'envonly', 'abcdefgh12345')
    env_refused = ''
except ValueError as error:
    env_refused = str(error)
check('a route that reads only an env var says to export it (nothing written)', 'export ROUTES_TEST_MISSING=' in env_refused, env_refused)
try:
    routes.set_key(s9, mine, 'has a space in it')
    bad_refused = False
except ValueError:
    bad_refused = True
check('something that is not a key (spaces) is refused', bad_refused)
if sys.platform != 'darwin':
    print('  skip the keychain checks (not macOS)')
else:
    service = 'cosmos-orbit-' + mine
    secret = 'orbit-test-key.%d-AbC_xyz' % os.getpid()
    home9 = base / 'home-key'
    cli9 = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(home9)]
    try:
        stored = subprocess.run(cli9 + ['route', 'key', mine], input=secret + '\n', capture_output=True, text=True, timeout=60)
        back = subprocess.run(['security', 'find-generic-password', '-s', service, '-w'], capture_output=True, text=True).stdout.strip()
        check('route key NAME (piped): stored in the keychain, the value nowhere in the output',
              stored.returncode == 0 and back == secret and secret not in stored.stdout + stored.stderr
              and 'keychain:' + service in stored.stdout, (stored.returncode, stored.stdout[-300:], stored.stderr[-300:]))
        calls9 = []

        def recording(argv, **kw):
            calls9.append((argv, kw.get('input') or ''))
            return subprocess.run(argv, **kw)
        routes.set_key(s9, mine, secret, runner=recording)
        check('the key never reaches argv: security -i reads it from stdin', calls9 and calls9[0][0] == ['security', '-i']
              and all(secret not in ' '.join(argv) for argv, _ in calls9) and secret in calls9[0][1], [c[0] for c in calls9])
        listed9 = subprocess.run(cli9 + ['route', 'list'], capture_output=True, text=True, timeout=60).stdout
        check('route list: the route is usable now that the key is there', any(l.split()[:2] == [mine, 'ok'] for l in listed9.splitlines()), listed9)
        routes.run(s9, mine, execvpe=exec_capture, environ={'PATH': '/bin'}, out=open(os.devnull, 'w'))
        _, argv9, env9 = ran[-1]
        check('route run reads it from the keychain into the environment, not argv',
              env9.get('ANTHROPIC_AUTH_TOKEN') == secret and secret not in ' '.join(argv9))
        removed = subprocess.run(cli9 + ['route', 'key', mine, '--remove'], capture_output=True, text=True, timeout=60)
        gone = subprocess.run(['security', 'find-generic-password', '-s', service], capture_output=True).returncode != 0
        ok9, why9 = routes.availability(s9, routes.all_routes(s9)[mine])
        check('route key NAME --remove deletes it; the route then says no key', removed.returncode == 0 and gone and not ok9
              and 'no key' in why9, (removed.stdout, removed.stderr, why9))
    finally:
        subprocess.run(['security', 'delete-generic-password', '-s', service], capture_output=True)

def run_fresh(home, name, **kw):
    """route run replaces the process and closes the ledger first: each call here opens it again."""
    routes.run(Store(home), name, execvpe=exec_capture, out=open(os.devnull, 'w'), **kw)
    return ran[-1]


print('⑩ a route with several models: each offered, any one picked')
s10 = Store(base / 'home-models')
routes.add(s10, 'ollama', 'https://ollama.com', 'kimi-k3.1-pro, GLM-5', 'env:ROUTES_TEST_KEY', 'Ollama cloud')
check('the first model is the default, the rest kept in order', routes.models(routes.load(s10)['ollama']) == ['kimi-k3.1-pro', 'GLM-5'])
first10 = routes.on_prompt(s10, {'session_id': 's10', 'prompt': 'go on', 'transcript_path': str(limited)}, 'claude', None)
reason10 = (first10 or {}).get('reason') or ''
print('\n'.join('      | ' + line for line in reason10.splitlines()))
check('the offer lists the route on its default model, then each other model as NAME:MODEL',
      '2  ollama     Ollama cloud · model kimi-k3.1-pro' in reason10 and '3  ollama:GLM-5 the same route on model GLM-5' in reason10, reason10)
spawned10 = []
with patch.object(typist, 'detect', return_value=terminal), \
        patch.object(typist, 'spawn', side_effect=lambda term, steps, log, home=None, task=None: spawned10.append(steps) or 1), \
        patch.object(routes, 'host_pid', return_value=4242):
    picked10 = routes.on_prompt(s10, {'session_id': 's10', 'prompt': 'ollama:glm-5', 'transcript_path': str(limited)}, 'claude', None)
texts10 = [v for k, v in (spawned10[0] if spawned10 else []) if k == 'text']
check('typing ollama:glm-5 (any case) picks that model: the pane runs route run ollama --model GLM-5',
      texts10 and re.fullmatch(r'cosmos-orbit route run ollama --model GLM-5 --carry --capsule [0-9a-f]{12} --from claude',
                               texts10[-1])
      and 'model GLM-5' in ((picked10 or {}).get('reason') or ''), (texts10, picked10))
run_fresh(base / 'home-models', 'ollama', model='GLM-5', environ={'ROUTES_TEST_KEY': 'k10-value'})
_, argv10, env10 = ran[-1]
check('route run --model: that model for the session and every model Claude Code asks for',
      argv10 == ['claude', '--model', 'GLM-5'] and all(env10.get(v) == 'GLM-5' for v in routes.MODEL_VARS), argv10)
run_fresh(base / 'home-models', 'ollama', model='qwen3-coder:480b', environ={'ROUTES_TEST_KEY': 'k10-value'})
check('a model the route does not list is still started (the service decides what it serves)', ran[-1][1] == ['claude', '--model', 'qwen3-coder:480b'])
run_fresh(base / 'home-models', 'claude', model='opus', environ={})
claude_argv = ran[-1][1]
run_fresh(base / 'home-models', 'codex', model='gpt-6.1-sol', environ={})
codex_argv = ran[-1][1]
check('the built-in routes take a model too (claude --model, codex -c model=)', claude_argv == ['claude', '--model', 'opus']
      and codex_argv == ['codex', '-c', 'model="gpt-6.1-sol"'], (claude_argv, codex_argv))

print('⑪ any service: OpenAI Responses-compatible routes run Codex; x-api-key for Anthropic-compatible ones')
s11 = Store(base / 'home-api')
for bad_args in (dict(api='gemini'), dict(api='openai', auth='x-api-key'), dict(auth='basic')):
    try:
        routes.add(s11, 'bad', 'https://x.test', 'm', 'env:X', **bad_args)
        bad_ok = False
    except ValueError:
        bad_ok = True
    check('refused: %s' % bad_args, bad_ok)
routes.add(s11, 'oai', 'https://api.example.test/v1', 'model-one,model-two', 'env:ROUTES_TEST_KEY', 'a Responses API', api='openai')
check('an openai route runs on Codex', routes.load(s11)['oai']['host'] == 'codex')
run_fresh(base / 'home-api', 'oai', model='model-two', carry=True, environ={'ROUTES_TEST_KEY': 'k11-value', 'PATH': '/bin'})
_, argv11, env11 = ran[-1]
expected11 = ['codex', '-c', 'model_providers.orbit-oai.name="oai"', '-c', 'model_providers.orbit-oai.base_url="https://api.example.test/v1"',
              '-c', 'model_providers.orbit-oai.env_key="%s"' % routes.KEY_ENV, '-c', 'model_providers.orbit-oai.wire_api="responses"',
              '-c', 'model_provider="orbit-oai"', '-c', 'model="model-two"', typist.CARRY_LINE]
check('codex with a provider defined for this run, the picked model, the carry line last', argv11 == expected11, argv11)
check('its key in the environment Codex reads (env_key), never in argv, no Anthropic endpoint',
      env11.get(routes.KEY_ENV) == 'k11-value' and 'k11-value' not in ' '.join(argv11) and 'ANTHROPIC_BASE_URL' not in env11)
run_fresh(base / 'home-api', 'claude', environ=dict(env11, **{routes.ROUTE_ENV: 'oai'}))
check('back to claude from inside it: its key does not leak', routes.KEY_ENV not in ran[-1][2])
routes.add(s11, 'xk', 'https://api.example.test', 'm', 'env:ROUTES_TEST_KEY', auth='x-api-key')
run_fresh(base / 'home-api', 'xk', environ={'ROUTES_TEST_KEY': 'k-xk-value', 'ANTHROPIC_AUTH_TOKEN': 'stale'})
_, argv_xk, env_xk = ran[-1]
check('--auth x-api-key: an apiKeyHelper that prints the key from the environment; neither Anthropic key variable set',
      env_xk.get(routes.KEY_ENV) == 'k-xk-value' and 'ANTHROPIC_AUTH_TOKEN' not in env_xk and 'ANTHROPIC_API_KEY' not in env_xk
      and argv_xk == ['claude', '--settings', json.dumps({'apiKeyHelper': 'printenv ' + routes.KEY_ENV}), '--model', 'm']
      and 'k-xk-value' not in ' '.join(argv_xk), (argv_xk, sorted(k for k in env_xk if 'ANTHROPIC' in k)))
codex_bin = __import__('shutil').which('codex')
if not codex_bin:
    print('  skip the Codex end to end (codex is not installed)')
else:
    import socket
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    log11 = base / 'stub-openai.log'
    token11 = 'stub-openai-token-%d' % os.getpid()
    server = subprocess.Popen([sys.executable, str(ROOT / 'tests' / 'stub_openai.py'), str(port), str(log11), token11])
    codex_home = base / 'codex-home'     # Codex on its own: none of this user's Codex config or hooks
    codex_home.mkdir()
    work = make_repo(base, 'codex-work')
    try:
        time.sleep(1.0)
        subprocess.run(cli + ['route', 'add', 'stubo', '--api', 'openai', '--base-url', 'http://127.0.0.1:%d/v1' % port,
                              '--model', 'stub-model-a,stub-model-b', '--key', 'env:ROUTES_STUB_KEY'], capture_output=True, timeout=60)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC_', 'COSMOS_ORBIT_', 'OPENAI_', 'CODEX_'))}
        env.update(ROUTES_STUB_KEY=token11, CODEX_HOME=str(codex_home))
        result = subprocess.run(cli + ['route', 'run', 'stubo', '--model', 'stub-model-b', '--', 'exec', '--skip-git-repo-check',
                                       'Reply with anything.'], capture_output=True, text=True, timeout=180, env=env, cwd=str(work),
                                stdin=subprocess.DEVNULL)   # codex exec reads a piped stdin as more prompt: closed here
        seen = [json.loads(line) for line in log11.read_text().splitlines()] if log11.exists() else []
        posts = [r for r in seen if r['method'] == 'POST']
        check('codex answered with the stand-in\'s text', 'ROUTE-STUB-ANSWER' in result.stdout, (result.returncode, result.stdout[-200:], result.stderr[-400:]))
        check('every request went to the route\'s /v1/responses, for the picked model, with its key',
              posts and all(r['path'] == '/v1/responses' and r['model'] == 'stub-model-b' and r['token_ok'] for r in posts), seen)
        check('the key is in none of the output', token11 not in result.stdout + result.stderr)
    finally:
        server.kill()
if claude:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    log_xk = base / 'stub-xk.log'
    token_xk = 'stub-xk-token-%d' % os.getpid()
    server = subprocess.Popen([sys.executable, str(ROOT / 'tests' / 'stub_anthropic.py'), str(port), str(log_xk), token_xk])
    try:
        time.sleep(1.0)
        subprocess.run(cli + ['route', 'add', 'stubx', '--base-url', 'http://127.0.0.1:%d' % port, '--model', 'stub-model',
                              '--key', 'env:ROUTES_STUB_KEY', '--auth', 'x-api-key'], capture_output=True, timeout=60)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC_', 'COSMOS_ORBIT_ROUTE'))}
        env['ROUTES_STUB_KEY'] = token_xk
        result = subprocess.run(cli + ['route', 'run', 'stubx', '--', '-p', 'Reply with anything.', '--setting-sources', '',
                                       '--strict-mcp-config'], capture_output=True, text=True, timeout=180, env=env, cwd=str(base))
        seen = [json.loads(line) for line in log_xk.read_text().splitlines()] if log_xk.exists() else []
        messages = [r for r in seen if r['path'].startswith('/v1/messages')]
        check('x-api-key end to end: claude answered, and every request sent the route\'s key as x-api-key',
              'ROUTE-STUB-ANSWER' in result.stdout and messages and all(r['x_api_key_ok'] for r in messages),
              (result.returncode, result.stdout[-200:], result.stderr[-300:], seen))
        check('… and no other credential: any Authorization header holds the route\'s key, never the user\'s login',
              messages and all(r['authorization_kind'] is None or r['authorization_is_expected'] for r in messages), seen)
    finally:
        server.kill()


def said(at, text):
    return {'type': 'user', 'uuid': 'u-%s' % at, 'timestamp': iso(at), 'isSidechain': False,
            'message': {'role': 'user', 'content': text}}


def edited(at, path):
    return {'type': 'assistant', 'uuid': 'e-%s' % at, 'timestamp': iso(at), 'isSidechain': False,
            'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 't%s' % at, 'name': 'Edit',
                                                          'input': {'file_path': path, 'old_string': 'a', 'new_string': 'b'}}]}}


def tool_result(at):
    return {'type': 'user', 'uuid': 'r-%s' % at, 'timestamp': iso(at), 'isSidechain': False,
            'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't', 'content': 'ok'}]}}


def codex_item(at, item):
    return {'timestamp': iso(at), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': item}}


print('⑫ asked, not limited: a prompt that asks to switch gets the offer; every other prompt goes on')
s12 = Store(base / 'home-ask')
repo12 = make_repo(base, 'ask-repo')
attach.set_switch(s12, 'on', repo12)
routes.add(s12, 'kimi', 'https://ollama.com', 'kimi-k3.1-pro,glm-5', 'env:ROUTES_TEST_KEY', 'Kimi K3.1 Pro on Ollama cloud')
ASKS = ['kimi로 바꿔줘', '모델 바꾸고 싶어', '올라마 모델 사용하고 싶어', '다른 모델로 전환해줘', 'Kimi 모델로 바꿔',
        'glm-5 써줘', 'codex로 전환해줘', 'switch to kimi', 'use the kimi model', 'change model', '다른 모델 있어?']
WORK = ['리밋에 걸렸을 경우 말고도 그냥 사용을 하다가도 모델을 변경하고 싶다거나 올라마 모델 사용하고 싶다고 구체적으로 말하거나 '
        '했을때 제안을 해줘야해. 이 추가 모델 기능으로 변경하는 것을 제안을 주고 스위칭이 될 수 있어야 해.',
        'Organization 모델을 변경해줘', 'UserProfile 모델 변경', '모델 변경 기능을 개발하자', 'claude 설정 바꿔줘',
        'kimi 경로 테스트 결과 알려줘', '올라마 무료야?', 'codex 리뷰 받아줘', 'switch the branch to main', 'use the new schema',
        'kimi로 바꿔줘\n그리고 테스트도 돌려줘', '변경사항을 모두 커밋 후 푸시 해.']
t12 = base / 'ask.jsonl'
write(t12, [said(NOW - 900, 'add a parser for the config file'), edited(NOW - 890, str(repo12 / 'parser.py')),
            tool_result(NOW - 889), answer(NOW - 880, 'The parser reads PELICAN-7 keys now; empty input keeps the old behaviour.')])
offered = {t: routes.on_prompt(s12, {'session_id': 'm-%d' % i, 'prompt': t, 'transcript_path': str(t12)}, 'claude', None)
           for i, t in enumerate(ASKS)}
check('each way of asking gets the offer (Korean, English, a route, a model, a model\'s stem, Ollama, "another model")',
      all((r or {}).get('decision') == 'block' and 'you asked to switch' in r['reason'] for r in offered.values()),
      {t: (r or {}).get('reason', '')[:80] for t, r in offered.items()})
passed = {t: routes.on_prompt(s12, {'session_id': 'w-%d' % i, 'prompt': t, 'transcript_path': str(t12)}, 'claude', None)
          for i, t in enumerate(WORK)}
check('work that mentions a model or a route goes on: a class\'s model, a feature to build, a question, several lines',
      all(r is None for r in passed.values()), {t[:40]: r for t, r in passed.items() if r})
reason12 = offered['kimi로 바꿔줘']['reason']
print('\n'.join('      | ' + line for line in reason12.splitlines()))
check('the offer numbers every usable route and model, and points at the one named',
      '  1  codex' in reason12 and '  2  kimi' in reason12 and '  3  kimi:glm-5' in reason12
      and '(what you named: 2, 3)' in reason12 and '  claude' not in reason12, reason12)
check('the same words sent again go to the session', routes.on_prompt(
    s12, {'session_id': 'm-0', 'prompt': 'kimi로 바꿔줘', 'transcript_path': str(t12)}, 'claude', None) is None)
check('… and the offer is gone: 2 is an ordinary prompt now', routes.on_prompt(
    s12, {'session_id': 'm-0', 'prompt': '2', 'transcript_path': str(t12)}, 'claude', None) is None)
check('another prompt after the offer goes on, and ends it', routes.on_prompt(
    s12, {'session_id': 'm-1', 'prompt': 'add a test for the parser', 'transcript_path': str(t12)}, 'claude', None) is None
      and not routes._json(s12, routes.SESSION_KEY + 'm-1').get('offer'))
spawned_back = []
with patch.object(typist, 'detect', return_value=terminal), \
        patch.object(typist, 'spawn', side_effect=lambda term, steps, log, home=None, task=None: spawned_back.append(steps) or 1), \
        patch.object(routes, 'host_pid', return_value=4242):
    back12 = routes.on_prompt(s12, {'session_id': 'm-6', 'prompt': 'codex로 전환해줘1', 'transcript_path': str(t12)}, 'claude', None)
texts_back = [v for k, v in (spawned_back[0] if spawned_back else []) if k == 'text']
check('Claude Code puts the blocked words back in its input: those words with 1 typed after them pick 1 (seen live)',
      texts_back and ' route run codex --carry --capsule ' in texts_back[-1], (texts_back, back12))
stale = dict(routes._json(s12, routes.SESSION_KEY + 'm-2'), at=iso(NOW - routes.ASKED_TTL - 60))
s12.preference(routes.SESSION_KEY + 'm-2', json.dumps(stale))
check('an offer left unanswered past its time picks nothing: 1 goes to the session', routes.on_prompt(
    s12, {'session_id': 'm-2', 'prompt': '1', 'transcript_path': str(t12)}, 'claude', None) is None)
routes.set_enabled(s12, False)
check('route off: asking changes nothing', routes.on_prompt(
    s12, {'session_id': 'm-off', 'prompt': 'kimi로 바꿔줘', 'transcript_path': str(t12)}, 'claude', None) is None)
routes.set_enabled(s12, True)
routes.set_enabled(s12, False, 'kimi')
off12 = routes.on_prompt(s12, {'session_id': 'm-k', 'prompt': '다른 모델로', 'transcript_path': str(t12)}, 'claude', None)
check('route off kimi: it is listed as off, not offered', '  -  kimi' in off12['reason'] and '  2  kimi' not in off12['reason'],
      off12['reason'])
routes.set_enabled(s12, True, 'kimi')

first12 = attach.handle(s12, 'prompt', {'cwd': str(repo12), 'session_id': 's12', 'prompt': 'add a parser for the config file',
                                        'transcript_path': str(t12)}, 'claude')
task12 = attach.current_task(s12, repo12)
asked12 = attach.handle(s12, 'prompt', {'cwd': str(repo12), 'session_id': 's12', 'prompt': 'kimi로 바꿔줘',
                                        'transcript_path': str(t12)}, 'claude')
check('in a session with a task, through the hook: the offer, and the request is not recorded on the task',
      (asked12 or {}).get('decision') == 'block' and 'kimi로 바꿔줘' not in json.dumps(s12.get(task12['id'])['data'],
                                                                                  ensure_ascii=False), asked12)
spawned12 = []
with patch.object(typist, 'detect', return_value=terminal), \
        patch.object(typist, 'spawn', side_effect=lambda term, steps, log, home=None, task=None: spawned12.append(steps) or 1), \
        patch.object(routes, 'host_pid', return_value=4242):
    picked12 = attach.handle(s12, 'prompt', {'cwd': str(repo12), 'session_id': 's12', 'prompt': '2',
                                             'transcript_path': str(t12)}, 'claude')
texts12 = [v for k, v in (spawned12[0] if spawned12 else []) if k == 'text']
run12 = re.fullmatch(r'cosmos-orbit route run kimi --carry --capsule ([0-9a-f]{12}) --from claude', texts12[-1] if texts12 else '')
check('2 switches this pane to kimi, carrying a capsule of this conversation', bool(run12) and texts12[0] == '/exit'
      and 'context' in (picked12 or {}).get('reason', ''), (texts12, picked12))
data12 = s12.get(task12['id'])['data']
check('the task is handed over with a continuation that says the user chose it (not the limit\'s)',
      (data12.get('carried_prompt') or {}).get('text') == routes.ASKED_TEXT, data12.get('carried_prompt'))

print('⑬ the context goes with the switch: to the new session, once, task or not, from either host to either')
cid = run12[1] if run12 else ''
capsule = routes._json(s12, routes.CAPSULE_KEY + cid)
check('the capsule holds the last turns, the files changed, the request that asked and the task',
      capsule.get('reason') == 'asked' and capsule.get('asked') == 'kimi로 바꿔줘' and capsule.get('task') == task12['id']
      and any('PELICAN-7' in text for _, text in capsule.get('turns') or ()) and capsule.get('files') == [str(repo12 / 'parser.py')]
      and capsule.get('turns')[0] == ['user', 'add a parser for the config file'], capsule)
ran.clear()
routes.run(s12, 'kimi', carry=True, capsule=cid, environ={'ROUTES_TEST_KEY': 'k13', routes.CAPSULE_ENV: 'aaaaaaaaaaaa'},
           execvpe=exec_capture, out=open(os.devnull, 'w'))
_, argv13, env13 = ran[-1]
s12 = Store(base / 'home-ask')          # route run closes the store before the host replaces its process
check('route run --capsule hands the capsule to the new session, and starts it on the carry line',
      env13.get(routes.CAPSULE_ENV) == cid and argv13[-1] == typist.CARRY_LINE, (argv13, env13.get(routes.CAPSULE_ENV)))
routes.run(s12, 'kimi', environ={'ROUTES_TEST_KEY': 'k13', routes.CAPSULE_ENV: cid}, execvpe=exec_capture,
           out=open(os.devnull, 'w'))
s12 = Store(base / 'home-ask')
check('a route started from inside that session without --capsule does not inherit it', routes.CAPSULE_ENV not in ran[-1][2])
with patch.dict(os.environ, {routes.CAPSULE_ENV: cid}), patch.object(typist, 'detect', return_value=terminal):
    start13 = attach.handle(s12, 'session-start', {'cwd': str(repo12), 'session_id': 'fresh12', 'source': 'startup',
                                                   'transcript_path': str(base / 'fresh12.jsonl')}, 'claude')
    carry13 = attach.handle(s12, 'prompt', {'cwd': str(repo12), 'session_id': 'fresh12', 'prompt': typist.CARRY_LINE,
                                            'transcript_path': str(base / 'fresh12.jsonl')}, 'claude')
    cleared13 = attach.handle(s12, 'session-start', {'cwd': str(repo12), 'session_id': 'fresh12-b', 'source': 'clear',
                                                     'transcript_path': str(base / 'fresh12b.jsonl')}, 'claude')
context13 = ((start13 or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
print('\n'.join('      | ' + line for line in context13.split('\n[orbit] task')[0].splitlines()[:14]))
check('the new session starts with the capsule: the switch, its words, the turns, the files, and the task\'s brief',
      'Carried over by a route switch' in context13 and 'kimi로 바꿔줘' in context13 and 'PELICAN-7' in context13
      and str(repo12 / 'parser.py') in context13 and task12['id'] in context13 and routes.ASKED_TEXT in context13
      and (s12.session_task('fresh12') or {'task_id': None})['task_id'] == task12['id'], context13[:3000])
carried13 = json.dumps(carry13 or {}, ensure_ascii=False)
check('its first prompt (the carry line) is not blocked, and the capsule is not given twice',
      not (carry13 or {}).get('decision') and 'Carried over by a route switch' not in carried13, carried13[:800])
check('a /clear later in that process does not get it again',
      'Carried over by a route switch' not in json.dumps(cleared13 or {}, ensure_ascii=False))

repo13 = make_repo(base, 'plain-repo')
attach.set_switch(s12, 'on', repo13)
rollout = base / 'rollout-13.jsonl'
write(rollout, [{'timestamp': iso(NOW - 700), 'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [
                    {'type': 'input_text', 'text': '# AGENTS.md instructions for the repo'}]}},
                codex_item(NOW - 690, {'type': 'UserMessage', 'content': [{'type': 'text', 'text': 'make the README say OSPREY-3'}]}),
                codex_item(NOW - 680, {'type': 'FileChange', 'changes': {str(repo13 / 'README.md'): {'type': 'update'}}}),
                codex_item(NOW - 670, {'type': 'AgentMessage', 'content': [{'type': 'Text', 'text': 'The README now says OSPREY-3.'}]})])
asked13 = attach.handle(s12, 'prompt', {'cwd': str(repo13), 'session_id': 'cx13', 'prompt': 'kimi로 바꿔줘',
                                        'transcript_path': str(rollout)}, 'codex')
check('Codex, no task: asking gets the offer (codex itself not offered) and starts no task',
      (asked13 or {}).get('decision') == 'block' and '  1  claude' in asked13['reason'] and '  codex' not in asked13['reason']
      and attach.current_task(s12, repo13) is None, asked13)
spawned13 = []
with patch.object(typist, 'detect', return_value=terminal), \
        patch.object(typist, 'spawn', side_effect=lambda term, steps, log, home=None, task=None: spawned13.append((steps, task)) or 1), \
        patch.object(routes, 'host_pid', return_value=4343):
    picked13 = attach.handle(s12, 'prompt', {'cwd': str(repo13), 'session_id': 'cx13', 'prompt': 'kimi',
                                             'transcript_path': str(rollout)}, 'codex')
steps13 = spawned13[0][0] if spawned13 else []
texts13 = [v for k, v in steps13 if k == 'text']
run13 = re.fullmatch(r'cosmos-orbit route run kimi --carry --capsule ([0-9a-f]{12}) --from codex', texts13[-1] if texts13 else '')
check('Codex → kimi (Claude Code): /exit, a pause before its Enter (Codex reads a quick one as part of a paste), then '
      'the route with the capsule; nothing to empty first in Codex',
      bool(run13) and texts13[0] == '/exit' and ('key', 'ctrl+u') not in steps13
      and steps13[steps13.index(('text', '/exit')) + 1] == ('wait', 1.2)
      and 'gets this conversation\'s context' in (picked13 or {}).get('reason', ''), (steps13, picked13))
with patch.dict(os.environ, {routes.CAPSULE_ENV: run13[1] if run13 else ''}):
    start13b = attach.handle(s12, 'session-start', {'cwd': str(repo13), 'session_id': 'cl13', 'source': 'startup',
                                                    'transcript_path': str(base / 'cl13.jsonl')}, 'claude')
    first13b = attach.handle(s12, 'prompt', {'cwd': str(repo13), 'session_id': 'cl13', 'prompt': typist.CARRY_LINE,
                                             'transcript_path': str(base / 'cl13.jsonl')}, 'claude')
context13b = ((start13b or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
check('the Claude Code session gets the Codex session\'s turns and files in the same form, with no task to brief',
      'route codex (codex)' in context13b and 'OSPREY-3' in context13b and str(repo13 / 'README.md') in context13b
      and 'AGENTS.md' not in context13b and 'Orbit task: none' in context13b and 'tell the user in one line' in context13b,
      context13b)
check('… and its first prompt starts no task and is not blocked', not (first13b or {}).get('decision')
      and attach.current_task(s12, repo13) is None, first13b)
typed13 = []
with patch.object(typist, 'run', side_effect=lambda term, steps, **kw: typed13.append((steps, kw)) or []):
    exit13 = typist._main({'terminal': terminal, 'steps': [['text', '/exit'], ['key', 'enter']], 'home': str(base / 'home-ask'),
                           'task': None})
check('the detached typist of a switch with no task types the steps (it died on an unbound name before /exit, seen live)',
      exit13 == 0 and typed13 and typed13[0][1].get('claim') is None, (exit13, typed13))
s3 = Store(base / 'home')
held3 = routes._json(s3, routes.CAPSULE_KEY + (routes._json(s3, routes.SESSION_KEY + 's-limited').get('capsule') or ''))
check('the limit\'s switch carries its capsule too: the blocked request, to be run', held3.get('reason') == 'limit'
      and held3.get('held') == REQUEST and 'Now: run the request carried over' in routes.capsule_text(s3, held3), held3)

print('⑭ a route whose service refuses for payment: seen when it happens, offered no more, no end day needed')
# Shapes copied from the real hosts on 2026-11-18 (Claude Code 2.1.327, Codex 0.167.1) against tests/stub_*.py with
# STUB_REFUSE=403 and 402: Claude Code calls a 403 `authentication_failed`; Codex ends the turn with the error.
OLLAMA = 'this model requires a subscription, upgrade for access: https://ollama.com/upgrade (ref: x1)'


def refused_line(at, text, status, error='authentication_failed'):
    return dict(limit_line(at, text, error=error), apiErrorStatus=status, uuid='r-%s' % at)


def codex_end(at, message=None, status=None):
    error = {'message': message, 'codex_error_info': {'http_connection_failed': {'http_status_code': status}}} if message else None
    return {'timestamp': iso(at), 'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 't-%s' % at,
                                                                     'last_agent_message': None, 'error': error}}


def no_limit_count(at):
    return {'timestamp': iso(at), 'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {
        'primary': {'used_percent': 3.0, 'resets_at': NOW + 9000}, 'rate_limit_reached_type': None}}}


t14 = base / 'paid.jsonl'
write(t14, [said(NOW - 900, 'add a parser'), answer(NOW - 880),
            refused_line(NOW - 60, 'Failed to authenticate. API Error: 403 ' + OLLAMA, 403)])
found14 = routes.claude_limit(t14)
check('Claude Code: a 403 whose words ask for a subscription is a refusal for payment, with no reset',
      found14 and found14.get('paid') and found14['resets_at'] is None and 'requires a subscription' in found14['text'], found14)
write(t14, [refused_line(NOW - 60, 'API Error: 402 You have run out of usage credits.', 402, error='unknown')])
check('Claude Code: a 402 is one too', (routes.claude_limit(t14) or {}).get('paid'), routes.claude_limit(t14))
write(t14, [refused_line(NOW - 60, 'Failed to authenticate. API Error: 401 invalid x-api-key', 401)])
check('control: a 401 for a wrong key is not', routes.claude_limit(t14) is None, routes.claude_limit(t14))
write(t14, [refused_line(NOW - 600, 'Failed to authenticate. API Error: 403 ' + OLLAMA, 403), answer(NOW - 30)])
check('control: a real answer after it — nothing holds', routes.claude_limit(t14) is None)
write(t14, [limit_line(NOW - 30, "You're out of usage credits. Switch to another model to continue.")])
check('control: Claude Code\'s own rate limit stays a limit (it resets), not a refusal',
      not (routes.claude_limit(t14) or {}).get('paid') and routes.claude_limit(t14)['kind'] == 'credits')
r14 = base / 'paid-rollout.jsonl'
write(r14, [no_limit_count(NOW - 900), codex_end(NOW - 890),
            codex_end(NOW - 60, 'unexpected status 403 Forbidden: %s, url: http://127.0.0.1/v1/responses' % OLLAMA, 403)])
check('Codex: a turn ended on a 403 asking for a subscription is a refusal for payment', (routes.codex_limit(r14) or {}).get('paid'),
      routes.codex_limit(r14))
write(r14, [codex_end(NOW - 60, 'unexpected status 402 Payment Required: You have run out of usage credits.', 402)])
check('Codex: a 402 is one too', (routes.codex_limit(r14) or {}).get('paid'), routes.codex_limit(r14))
write(r14, [no_limit_count(NOW - 900), codex_end(NOW - 60, 'stream disconnected before completion', None)])
check('control: Codex ending on a network error is not', routes.codex_limit(r14) is None, routes.codex_limit(r14))
write(r14, [codex_end(NOW - 600, 'unexpected status 402 Payment Required: no credits', 402), no_limit_count(NOW - 40),
            codex_end(NOW - 30)])
check('control: a Codex turn that answered after it — nothing holds', routes.codex_limit(r14) is None, routes.codex_limit(r14))

s14 = Store(base / 'home-paid')
routes.add(s14, 'kimi', 'https://ollama.com', 'kimi-k3.1-pro', 'env:ROUTES_TEST_KEY', 'Kimi K3.1 Pro on Ollama cloud')
routes.add(s14, 'kimo', 'http://127.0.0.1:1/v1', 'kimi-k3.1-pro', 'env:ROUTES_TEST_KEY', 'Kimi through Codex', api='openai')
env14 = {'ROUTES_TEST_KEY': 'k', routes.ROUTE_ENV: 'kimi', routes.FROM_ENV: 'claude'}
write(t14, [said(NOW - 900, 'add a parser'), answer(NOW - 880),
            refused_line(NOW - 60, 'Failed to authenticate. API Error: 403 ' + OLLAMA, 403)])
blocked14 = routes.on_prompt(s14, {'session_id': 'p14', 'prompt': 'add a test for it', 'transcript_path': str(t14)},
                             'claude', None, environ=env14)
reason14 = (blocked14 or {}).get('reason', '')
print('\n'.join('      | ' + line for line in reason14.splitlines()))
check('the next prompt in that session gets the offer: refused for payment, the routes to switch to, the request held',
      (blocked14 or {}).get('decision') == 'block' and 'was refused: its service asks for payment' in reason14
      and '  1  claude' in reason14 and '  2  codex' in reason14 and 'carried over' in reason14
      and 'route on kimi' in reason14 and 'Or wait' not in reason14, reason14)
check('… shown once: the same refusal does not block the next prompt', routes.on_prompt(
    s14, {'session_id': 'p14', 'prompt': 'add a test for it', 'transcript_path': str(t14)}, 'claude', None, environ=env14) is None)
usable14, why14 = routes.availability(s14, routes.all_routes(s14)['kimi'], NOW + 30 * 86400, environ=env14)
check('kimi is not offered again — a month later still not: waiting does not end a refusal for payment',
      not usable14 and 'refused for payment' in why14 and 'route on kimi' in why14, why14)
other14 = routes.on_prompt(s14, {'session_id': 'o14', 'prompt': '다른 모델로 바꿔줘', 'transcript_path': str(t12)},
                           'claude', None, environ={'ROUTES_TEST_KEY': 'k'})
check('… in another session\'s offer it is listed with why, not numbered', '  -  kimi' in other14['reason']
      and 'refused for payment' in other14['reason'] and re.search(r'  \d  kimi ', other14['reason']) is None, other14['reason'])
check('… and no "usable again" note is given in the session that left it', routes.prompt_note(
    s14, {'session_id': 'back14'}, 'claude', environ={'ROUTES_TEST_KEY': 'k', routes.ROUTE_ENV: 'claude',
                                                       routes.FROM_ENV: 'kimi'}, at=NOW + 86400) is None)
spawned14 = []
with patch.object(typist, 'detect', return_value=terminal), \
        patch.object(typist, 'spawn', side_effect=lambda term, steps, log, home=None, task=None: spawned14.append(steps) or 1), \
        patch.object(routes, 'host_pid', return_value=4444):
    write(t14, [said(NOW - 900, 'add a parser'), answer(NOW - 880),
                refused_line(NOW - 20, 'Failed to authenticate. API Error: 403 ' + OLLAMA, 403)])
    again14 = routes.on_prompt(s14, {'session_id': 'p14', 'prompt': 'write the docs', 'transcript_path': str(t14)},
                               'claude', None, environ=env14)
    picked14 = routes.on_prompt(s14, {'session_id': 'p14', 'prompt': 'write the docs1', 'transcript_path': str(t14)},
                                'claude', None, environ=env14)
texts14 = [v for k, v in (spawned14[0] if spawned14 else []) if k == 'text']
check('a new refusal offers again, and its number switches the pane, carrying the request typed',
      (again14 or {}).get('decision') == 'block' and texts14 and re.fullmatch(
          r'cosmos-orbit route run claude --carry --capsule [0-9a-f]{12} --from kimi', texts14[-1])
      and routes._json(s14, routes.CAPSULE_KEY + routes._json(s14, routes.SESSION_KEY + 'p14').get('capsule', '')).get('held')
      == 'write the docs', (texts14, again14, picked14))
routes.set_enabled(s14, True, 'kimi')
check('route on kimi (it answers again): offered again', routes.availability(
    s14, routes.all_routes(s14)['kimi'], NOW, environ=env14) == (True, ''))
told14 = []
r14b = base / 'paid-codex.jsonl'
write(r14b, [no_limit_count(NOW - 900), codex_end(NOW - 890),
             codex_end(NOW - 50, 'unexpected status 402 Payment Required: You have run out of usage credits.', 402)])
codex14 = routes.on_prompt(s14, {'session_id': 'c14', 'prompt': 'go on', 'transcript_path': str(r14b)}, 'codex', None,
                           environ={'ROUTES_TEST_KEY': 'k', routes.ROUTE_ENV: 'kimo'})
check('Codex on an added route: the same — the offer, and the route marked',
      (codex14 or {}).get('decision') == 'block' and 'was refused: its service asks for payment' in codex14['reason']
      and not routes.availability(s14, routes.all_routes(s14)['kimo'], NOW, environ={'ROUTES_TEST_KEY': 'k'})[0], codex14)
routes.add(s14, 'kimo', 'http://127.0.0.1:1/v1', 'kimi-k3.1-pro', 'env:ROUTES_TEST_KEY', 'Kimi through Codex', api='openai')
check('… adding the route again (a new key, a new plan) clears it', routes.availability(
    s14, routes.all_routes(s14)['kimo'], NOW, environ={'ROUTES_TEST_KEY': 'k'}) == (True, ''))
write(t14, [said(NOW - 900, 'add a parser'), refused_line(NOW - 10, 'Failed to authenticate. API Error: 403 ' + OLLAMA, 403)])
routes.on_stop_failure(s14, {'session_id': 'sf14', 'transcript_path': str(t14), 'error': 'authentication_failed',
                             'last_assistant_message': 'Failed to authenticate. API Error: 403 ' + OLLAMA}, 'claude',
                       {'id': 'task14'}, environ=env14, deliver=lambda store, task, text: told14.append(text))
check('StopFailure (the turn ended on it): kimi marked and the task\'s panes told it was refused for payment',
      told14 and 'kimi was refused: its service asks for payment' in told14[0]
      and (routes.route_limit(s14, 'kimi') or {}).get('paid'), (told14, routes.route_limit(s14, 'kimi')))

import shutil  # noqa: E402
fake = base / 'installed'
site = fake / 'lib' / 'python3.9' / 'site-packages' / 'harness'
shutil.copytree(ROOT / 'harness', site, ignore=shutil.ignore_patterns('__pycache__'))
(fake / 'bin').mkdir()
(fake / 'bin' / 'cosmos-orbit').write_text('#!/bin/sh\n')
same14 = attach._same_code(str(fake / 'bin' / 'cosmos-orbit'))
(site / 'routes.py').write_text((site / 'routes.py').read_text() + '\n# an older routes\n')
check('hooks call the installed Orbit only when every module is this code: one with an older routes.py alone is not '
      '(it was, seen live: the hooks ran the installed routes without this change)',
      same14 and not attach._same_code(str(fake / 'bin' / 'cosmos-orbit')), same14)

tmp.cleanup()
print('\n%s' % ('PASS' if not FAILED else 'FAIL: ' + ', '.join(FAILED)))
sys.exit(1 if FAILED else 0)
