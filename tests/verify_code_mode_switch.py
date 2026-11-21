#!/usr/bin/env python3
"""Model-free check of the Code Mode switch asked in words ("코드모드 꺼줘" / "켜줘").

The user, 2026-11-21: "코드모드 꺼줘 라고 하는 요청을 하면 또는 반대의 요청을 하면 세션의 컨텍스트를 이어서 전달을 하도록
하고(핸드오프처럼) 모드를 스위칭할 수 있도록". Claude Code decides Code Mode when a session starts, and Code Mode keeps
Claude in Chrome out, so a browser check needed a new session by hand, without this conversation.

① the words: the ways of asking are read, a question about it, work on such a feature and words about something else
   are not; ② through the real prompt hook: no turn runs, this session quits in its pane and Claude Code starts there
   again with Code Mode the other way, the attached task handed over and its request carried; ③ the conversation goes
   with it: the capsule says why, and the new session's first context has the previous turns; ④ `route run --code-mode`:
   off starts with the browser and Code Mode off in both the environment and the flag settings, on the other way,
   one --settings with a key route's helper, the permission mode carried; ⑤ controls: already that way, a Codex session,
   no terminal to type into (the command to run by hand), a route on another service asked to turn it on;
   ⑥ a request asked together with the switch is carried; ⑦ a fresh session outside any repository, with no task yet.
Exit 0 = pass.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID',
             'CODEX_THREAD_ID', 'COSMOS_ORBIT_ROUTE', 'COSMOS_ORBIT_ROUTE_FROM', 'COSMOS_ORBIT_CAPSULE'):
    os.environ.pop(name, None)
from types import SimpleNamespace as _Namespace  # noqa: E402
from harness import attach, routes, typist  # noqa: E402
from harness.store import Store  # noqa: E402
from harness import evolve as _evolve, recovery as _recovery, supervision as _supervision  # noqa: E402
try:
    from harness import codemode  # noqa: E402
except ImportError:
    codemode = None
patch.object(_recovery, 'ensure_worker', return_value=None).start()
patch.object(_evolve, 'maybe_start', return_value=None).start()
patch.object(_supervision, 'subprocess', _Namespace(
    Popen=lambda *a, **k: _Namespace(pid=None), run=subprocess.run, DEVNULL=subprocess.DEVNULL,
    SubprocessError=subprocess.SubprocessError)).start()
patch.object(attach, 'orbit_command', return_value='cosmos-orbit').start()
patch.object(routes.shutil, 'which', side_effect=lambda name: '/usr/bin/' + name if name in ('claude', 'codex') else None).start()
patch.object(routes, '_host_argv', return_value=[]).start()
patch.object(routes, '_codex_config', return_value={}).start()
patch.object(routes, 'host_pid', return_value=4242).start()

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:400]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')


def turn(role, text, cwd):
    return {'type': role, 'cwd': cwd, 'message': {'role': role, 'content': text if role == 'user' else
                                                  [{'type': 'text', 'text': text}]}}


def main():
    if codemode is None:
        check('harness.codemode exists', False, 'module missing')
        print('\nFAIL')
        return 1
    base = Path(tempfile.mkdtemp(prefix='orbit-codemode-'))
    repo = base / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    store = Store(base / 'home')
    attach.set_switch(store, 'on', repo)
    os.environ[routes.CODE_MODE_ENV] = '1'                # this session started in Code Mode
    transcript = base / 'session.jsonl'
    write(transcript, [turn('user', 'Build the sign-in page with email and password', str(repo)),
                       turn('assistant', 'The sign-in page is built; next I check it in the browser.', str(repo)),
                       {'type': 'user', 'cwd': str(repo), 'permissionMode': 'bypassPermissions',
                        'message': {'role': 'user', 'content': 'check it in the browser'}}])
    spawned = []
    terminal = {'kind': 'cmux', 'cli': '/fake/cmux', 'target': 'PANE-1'}

    def prompt(session, words, cwd=repo, host='claude', term=terminal):
        with patch.object(typist, 'detect', return_value=term), \
                patch.object(typist, 'spawn', side_effect=lambda t, steps, log, home=None, task=None, **kw:
                             spawned.append((t, steps, task)) or 1):
            return attach.handle(store, 'prompt', {'cwd': str(cwd), 'session_id': session, 'prompt': words,
                                                   'transcript_path': str(transcript), 'permission_mode': 'bypassPermissions'},
                                 host) or {}

    print('① the words')
    for words, want in (('코드모드 꺼줘', 'off'), ('코드 모드 좀 꺼줘', 'off'), ('코드모드를 꺼', 'off'), ('Code Mode 끄자', 'off'),
                        ('code mode off', 'off'), ('turn off code mode', 'off'), ('코드모드 다시 켜줘', 'on'),
                        ('코드모드 켜', 'on'), ('turn code mode on', 'on'),
                        ('코드모드 끄고 브라우저로 로그인 화면 확인해줘', 'off')):
        check('asks %s: %s' % (want, words), codemode.asks(words) == want, codemode.asks(words))
    for words in ('코드모드 끌 수 있어?', '코드모드 끄는 기능 만들어줘', '코드모드 껐다 켜줘', '이어서 해줘', '불 꺼줘',
                  'why is code mode on', '코드모드가 뭐야'):
        check('control: not a switch: ' + words, codemode.asks(words) is None, codemode.asks(words))

    print('② through the real prompt hook')
    prompt('S1', 'Build the sign-in page with email and password fields and a remember-me box')
    task = store.get(store.session_task('S1')['task_id'])
    prompts_before = len(task['data'].get('prompts') or [])
    reply = prompt('S1', '코드모드 꺼줘')
    reason = reply.get('reason') or ''
    check('no turn runs: the prompt is answered by the hook, in plain Korean', reply.get('decision') == 'block'
          and 'Code Mode를 끕니다' in reason and '아무것도 입력하지 마세요' in reason, reason)
    steps = spawned[-1][1] if spawned else []
    texts = [value for kind, value in steps if kind == 'text']
    line = texts[-1] if texts else ''
    check('this session quits in its pane, then Claude Code starts there again with Code Mode off',
          texts and texts[0] == '/exit' and ('gone', [4242, 30.0]) in steps and
          re.fullmatch(r'cosmos-orbit route run claude --carry --capsule [0-9a-f]{12} --code-mode off', line), steps)
    data = store.get(task['id'])['data']
    handoff = data.get('handoff_state') or {}
    check('the task waits for the session that starts in this pane', handoff.get('state') == 'auto' and
          handoff.get('panel') == 'PANE-1' and handoff.get('session') == 'S1', handoff)
    check('the switch is not recorded as a change of what the task must deliver',
          len(data.get('prompts') or []) == prompts_before and not any('코드모드' in str(c) for c in data.get('corrections') or []),
          (len(data.get('prompts') or []), prompts_before))

    print('③ the conversation goes with it')
    capsule_id = re.search(r'--capsule ([0-9a-f]{12})', line).group(1) if line else ''
    capsule = routes._json(store, routes.CAPSULE_KEY + capsule_id)
    check('the capsule keeps the last turns and the Code Mode asked for', capsule.get('code_mode') == 'off' and
          any('sign-in page is built' in t for _, t in capsule.get('turns') or []) and capsule.get('task') == task['id'],
          capsule)
    check('… and the permission mode of the session left', (capsule.get('mode') or {}).get('mode') == 'bypassPermissions',
          capsule.get('mode'))
    with patch.dict(os.environ, {routes.CAPSULE_ENV: capsule_id, routes.CODE_MODE_ENV: '0'}), \
            patch.object(typist, 'detect', return_value=terminal):
        started = attach.handle(store, 'session-start', {'cwd': str(repo), 'session_id': 'S2', 'source': 'startup',
                                                         'transcript_path': str(base / 'fresh.jsonl')}, 'claude') or {}
    brief = (started.get('hookSpecificOutput') or {}).get('additionalContext') or ''
    check('the new session is told why and gets the previous turns', 'Code Mode switch' in brief and 'Code Mode off'
          in brief and 'Claude in Chrome' in brief and 'sign-in page is built' in brief, brief[:600])
    bound = store.session_task('S2')
    check('the new session takes the task over', bound and bound['task_id'] == task['id'], bound)

    print('④ route run --code-mode')
    ran = []
    capture = lambda program, argv, env: ran.append((argv, env))  # noqa: E731
    fresh = lambda: Store(base / 'home')  # noqa: E731 - a run hands the terminal to the host: it closes its store
    routes.run(fresh(), 'claude', carry=True, capsule=capsule_id, code_mode='off', execvpe=capture,
               environ={'PATH': '/bin', routes.CODE_MODE_ENV: '1'}, out=open(os.devnull, 'w'))
    argv, env = ran[-1]
    flags = json.loads(argv[argv.index('--settings') + 1]) if '--settings' in argv else {}
    check('off: Code Mode 0 in the environment and in the flag settings, with the browser', env.get(routes.CODE_MODE_ENV)
          == '0' and flags.get('env', {}).get(routes.CODE_MODE_ENV) == '0' and '--chrome' in argv, argv)
    check('… in the permission mode carried, the carry line last', '--dangerously-skip-permissions' in argv and
          argv[-1] == typist.CARRY_LINE, argv)
    routes.run(fresh(), 'claude', code_mode='on', execvpe=capture, environ={'PATH': '/bin', routes.CODE_MODE_ENV: '0'},
               out=open(os.devnull, 'w'))
    argv, env = ran[-1]
    flags = json.loads(argv[argv.index('--settings') + 1]) if '--settings' in argv else {}
    check('on: Code Mode 1 in both, no browser flag', env.get(routes.CODE_MODE_ENV) == '1' and
          flags.get('env', {}).get(routes.CODE_MODE_ENV) == '1' and '--chrome' not in argv, argv)
    routes.run(fresh(), 'claude', execvpe=capture, environ={'PATH': '/bin'}, out=open(os.devnull, 'w'))
    check('control: without --code-mode nothing about it is set', '--settings' not in ran[-1][0] and
          routes.CODE_MODE_ENV not in ran[-1][1], ran[-1][0])
    store = fresh()
    routes.add(store, 'gate', 'https://gateway.example', 'some-model', key='env:GATE_TEST_KEY', auth='x-api-key')
    routes.run(fresh(), 'gate', code_mode='off', execvpe=capture, environ={'PATH': '/bin', 'GATE_TEST_KEY': 'k-gate'},
               out=open(os.devnull, 'w'))
    argv = ran[-1][0]
    flags = json.loads(argv[argv.index('--settings') + 1]) if '--settings' in argv else {}
    check('one --settings carries both a key route\'s helper and Code Mode', argv.count('--settings') == 1 and
          flags.get('apiKeyHelper') and flags.get('env', {}).get(routes.CODE_MODE_ENV) == '0', argv)
    help_text = subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), 'route', '--help'],
                               capture_output=True, text=True).stdout
    check('the command line takes --code-mode on|off', '--code-mode {on,off}' in help_text, help_text[-400:])

    print('⑤ controls')
    count = len(spawned)
    with patch.dict(os.environ, {routes.CODE_MODE_ENV: '0'}):
        reply = prompt('S3', '코드모드 꺼줘')
    check('already off: said so, nothing restarted', '이미 꺼져' in (reply.get('reason') or '') and len(spawned) == count, reply)
    reply = prompt('S4', '코드모드 꺼줘', host='codex')
    check('a Codex session: said so, nothing restarted', 'Codex' in (reply.get('reason') or '') and len(spawned) == count,
          reply)
    reply = prompt('S5', '코드모드 끌 수 있어?')
    check('a question about it goes to the session', reply.get('decision') != 'block' and len(spawned) == count, reply)
    reply = prompt('S6', '코드모드 꺼줘', term=None)
    check('no terminal to type into: the command to run by hand, after /exit', '/exit' in (reply.get('reason') or '') and
          '--code-mode off' in (reply.get('reason') or '') and len(spawned) == count, reply)
    with patch.dict(os.environ, {routes.CODE_MODE_ENV: '0'}):
        reply = prompt('S7', '코드모드 다시 켜줘')
    check('the other way: Code Mode on', spawned and len(spawned) == count + 1 and
          [v for k, v in spawned[-1][1] if k == 'text'][-1].endswith('--code-mode on') and 'Code Mode를 켭니다' in
          (reply.get('reason') or ''), reply)
    count = len(spawned)
    with patch.dict(os.environ, {routes.ROUTE_ENV: 'gate', routes.CODE_MODE_ENV: '1'}):
        reply = prompt('S8', '코드모드 켜줘')
    check('a route on another service asked to turn it on: said why, nothing restarted', '켤 수 없습니다' in
          (reply.get('reason') or '') and len(spawned) == count, reply)

    print('⑥ a request asked together with the switch')
    prompt('S9', 'Build the settings page with a dark mode toggle and save button please')
    task9 = store.get(store.session_task('S9')['task_id'])
    words = '코드모드 끄고 브라우저로 설정 화면 확인해줘'
    reply = prompt('S9', words)
    carried = store.get(task9['id'])['data'].get('carried_prompt') or {}
    check('the request goes to the new session: carried to the task, said so', carried.get('text') == words and
          '함께 말씀하신 일' in (reply.get('reason') or ''), (carried, reply.get('reason')))

    print('⑦ outside any repository, with no task yet')
    plain = base / 'folder-of-repos'
    plain.mkdir()
    count = len(spawned)
    reply = prompt('S10', '코드모드 꺼줘', cwd=plain)
    check('control: a folder Orbit is off for stays silent', reply.get('decision') != 'block' and len(spawned) == count,
          reply)
    attach.set_switch(store, 'on', plain)
    reply = prompt('S11', '코드모드 꺼줘', cwd=plain)
    check('a fresh session there is switched too, its conversation carried', reply.get('decision') == 'block' and
          len(spawned) == count + 1 and spawned[-1][2] is None, reply)

    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
