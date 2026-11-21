"""Code Mode on or off, asked in words, in the same pane with this conversation's context.

The user, 2026-11-21: "코드모드 꺼줘 라고 하는 요청을 하면 또는 반대의 요청을 하면 세션의 컨텍스트를 이어서 전달을 하도록
하고(핸드오프처럼) 모드를 스위칭할 수 있도록". Claude Code decides Code Mode once, when a session starts (2.1.330:
`settleCodeModeOptInAtStartup`; nothing switches it in a running session), and Code Mode keeps Claude in Chrome and
computer use out, so checking a page in the browser needs a session started without it. A session started in Code Mode
also resumes only with Code Mode on, so the conversation cannot be resumed on the other side.

So a prompt that asks for it ("코드모드 꺼줘", "코드 모드 다시 켜줘", "turn off code mode") runs no turn: the hook ends
this session in its pane and starts Claude Code there again with Code Mode the other way, carrying this conversation
the way a route switch does (§routes.switch: the capsule of its last turns and files, the attached task and the
permission mode). Measured 2026-11-21 on 2.1.330: `CLAUDE_CODE_EXPERIMENTAL_CODE_MODE=0 claude` and `claude --settings
'{"env":{"CLAUDE_CODE_EXPERIMENTAL_CODE_MODE":"0"}}'` both start without Code Mode while settings.json turns it on.
"""
import json
import os
import re

from . import routes

WORDS = r'(코드\s*모드|code\s*-?\s*mode)'
OFF = re.compile(WORDS + r'\S{0,2}\s*(좀\s*)?(이제\s*)?(좀\s*)?(꺼|끄|끈|끌|해제|비활성|off\b|disable)', re.I)
ON = re.compile(WORDS + r'\S{0,2}\s*(좀\s*)?(다시\s*)?(좀\s*)?(켜|켠|켤|활성|on\b|enable)', re.I)
EN_OFF = re.compile(r'\b(turn|switch)\s+(code\s*-?\s*mode\s+)?off(\s+code\s*-?\s*mode)?\b|\bdisable\s+code\s*-?\s*mode\b|'
                    r'\bwithout\s+code\s*-?\s*mode\b', re.I)
EN_ON = re.compile(r'\b(turn|switch)\s+(code\s*-?\s*mode\s+)?on(\s+code\s*-?\s*mode)?\b|\benable\s+code\s*-?\s*mode\b',
                   re.I)
# A question about Code Mode, or work on a feature that switches it, is not the switch itself.
NOT = re.compile(r'기능|개발|구현|만들|설계|수\s*있|어떻게|왜|뭐야|인가|될까|되나|[?？]|'
                 r'\b(feature|implement\w*|build|why|how|can\s+(i|you|we))\b', re.I)
LENGTH = 120
JOINERS = re.compile(r'^[\s,.·]*(고서|하고|해서|고|서|해|줘|주고|주세요|줄래|그리고|그다음|그\s*다음|and|then)*[\s,.·]*', re.I)
HANGUL = re.compile(r'[가-힣]')


def asks(text):
    """'on' or 'off' when a prompt asks to turn Code Mode that way, else None. One line, not a question or work on
    such a feature; both ways at once ("껐다 켜줘") is neither."""
    text = (text or '').strip()
    if not text or len(text) > LENGTH or '\n' in text or NOT.search(text):
        return None
    if not re.search(WORDS, text, re.I):
        return None
    off = bool(OFF.search(text) or EN_OFF.search(text))
    on = bool(ON.search(text) or EN_ON.search(text))
    if off == on:
        return None
    return 'off' if off else 'on'


def held(text):
    """The request asked together with the switch ("코드모드 끄고 로그인 화면을 브라우저로 확인해줘"), or None."""
    found = (OFF.search(text) or ON.search(text) or EN_OFF.search(text) or EN_ON.search(text))
    if not found:
        return None
    rest = (text[:found.start()] + ' ' + text[found.end():]).strip()
    rest = re.sub(r'^\S{0,3}(고|서)\b', '', rest).strip()          # the end of the verb: 꺼"고", 끄"고서"
    rest = JOINERS.sub('', rest).strip()
    return text if len(re.findall(r'[\w가-힣]', rest)) >= 4 else None


def current(environ=None):
    """'on' or 'off': Code Mode in the session this hook runs under, read from the environment it started with."""
    environ = os.environ if environ is None else environ
    route = environ.get(routes.ROUTE_ENV)
    if route and route not in routes.BUILTIN:
        return 'off'                       # another service's endpoint: Code Mode needs Claude's own account
    value = (environ.get(routes.CODE_MODE_ENV) or '').strip().lower()
    return 'on' if value in ('1', 'true', 'on', 'yes') else 'off'


def _task(store, session):
    from .attach import CLOSED, NO_TASK
    row = store.session_task(session)
    if not row or not row['task_id'] or row['task_id'] == NO_TASK:
        return None
    try:
        task = store.get(row['task_id'])
    except ValueError:
        return None
    return task if task['status'] not in CLOSED else None


def _say(korean, ko, en):
    return {'decision': 'block', 'reason': 'Orbit: ' + (ko if korean else en)}


def on_prompt(store, payload, host, environ=None, terminal=None, spawn=None, pid=None, at=None):
    """The prompt hook's part: a block reply (the switch started, or why not), or None to let the prompt on."""
    text = (payload.get('prompt') or '').strip()
    want = asks(text)
    session = payload.get('session_id')
    if not want or not session:
        return None
    korean = bool(HANGUL.search(text))
    environ = os.environ if environ is None else environ
    if host != 'claude':
        return _say(korean, 'Code Mode는 Claude Code에만 있습니다. 이 세션은 Codex라 바꿀 것이 없습니다.',
                    'Code Mode is a Claude Code mode; this is a Codex session, so there is nothing to switch.')
    have = current(environ)
    if have == want:
        return _say(korean, 'Code Mode는 이미 %s 있습니다. 바꿀 것이 없습니다.%s' % (
                        '꺼져' if want == 'off' else '켜져', ' 브라우저 도구를 그대로 쓰면 됩니다.' if want == 'off' else ''),
                    'Code Mode is already %s here: nothing to switch.' % want)
    route = routes.current_route(host, environ)
    if want == 'on' and route not in routes.BUILTIN:
        return _say(korean, 'Code Mode는 Claude 계정으로 돌 때만 켜집니다. 지금은 %s 경로라 켤 수 없습니다.' % route,
                    'Code Mode runs only on Claude\'s own account; this session runs on route %s.' % route)
    task = _task(store, session)
    state = routes._json(store, routes.SESSION_KEY + session)
    state = dict(state, offer=[], route=route, host=host, asked=text, held=held(text), code_mode=want,
                 at=routes.now(), blocked_text=text)
    mode = routes.session_mode(payload, host, pid, environ=environ) if routes.mode_carried(store) else None
    reply = routes.switch(store, session, host, task, route, state, text, environ=environ, at=at, terminal=terminal,
                          spawn=spawn, pid=pid, transcript=payload.get('transcript_path'), cwd=payload.get('cwd'),
                          mode=mode, code_mode=want)
    after = routes._json(store, routes.SESSION_KEY + session)
    if not after.get('run_line') or after.get('code_mode') != want:
        return reply                       # the switch did not start (the route is not usable): its own words
    run_line = after['run_line']
    way = '끕니다' if want == 'off' else '켭니다'
    gets = ('브라우저(Claude in Chrome)를 쓸 수 있는' if want == 'off' else 'Code Mode가 켜진')
    carried = []
    if task:
        carried.append('Orbit 작업 %s도 그대로 이어집니다.' % task['id'] if korean else
                       'Orbit task %s goes on there.' % task['id'])
    if state['held']:
        carried.append('함께 말씀하신 일은 새 세션이 바로 이어서 합니다.' if korean else
                       'The new session runs what you asked together with the switch.')
    back = ('"코드모드 켜줘"' if want == 'off' else '"코드모드 꺼줘"')
    if after.get('typed'):
        if korean:
            words = ' '.join(['Code Mode를 %s. 지금 세션을 이 칸에서 닫고, 지금까지의 대화를 넘겨받은 새 세션(%s 세션)을 '
                              '바로 엽니다. 아무것도 입력하지 마세요.' % (way, gets)] + carried +
                             ['되돌리려면 새 세션에서 %s라고 하세요.' % back])
        else:
            words = ' '.join(['turning Code Mode %s: this session ends in this pane and a new one starts here with '
                              'this conversation\'s context. Type nothing.' % want] + carried +
                             ['To switch back, say "turn %s code mode" there.' % ('on' if want == 'off' else 'off')])
    elif korean:
        words = ('Code Mode를 %s. 이 터미널에는 Orbit이 대신 입력할 수 없어서 직접 해 주세요: /exit 로 이 세션을 닫고 '
                 '같은 칸에서 아래를 실행하면 지금까지의 대화를 넘겨받은 새 세션이 열립니다.\n  %s\n%s'
                 % (way, run_line, ' '.join(carried)))
    else:
        words = ('to turn Code Mode %s, quit this session with /exit and run this in the same terminal; the new session '
                 'gets this conversation\'s context:\n  %s\n%s' % (want, run_line, ' '.join(carried)))
    return {'decision': 'block', 'reason': 'Orbit: ' + words.strip()}
