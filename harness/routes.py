"""Routes: where a Claude Code or Codex session can run, and the switch when the one in use hits its limit.

A session that hits its usage limit cannot answer until the reset. Claude Code writes a synthetic assistant line
(`error: rate_limit`, `apiErrorStatus: 429`, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", with
`quotaLimits.resetsAt`); Codex reports `rate_limits.rate_limit_reached_type` in its token counts. The user's next
prompt there is blocked — it would only fail again — and shows the routes usable now, numbered, with their
commands. Typing a route's number or name as the next prompt starts that route in the same pane and carries the
blocked request over. Orbit never switches by itself: the user picks.

A route is a host program (claude or codex). The two built in run on the account the shell already has; a route the
user adds points one of them at any service that speaks its protocol: Claude Code at an Anthropic-compatible
endpoint (`--api anthropic`: Ollama's cloud, a local Ollama, a gateway), Codex at an OpenAI Responses-compatible
one (`--api openai`). A route may serve several models; the first is its default and the others are offered beside
it. Each user's routes and keys are their own: routes.json in their Orbit home, the key in their environment or
their macOS keychain (`route key NAME` puts it there), read only when the route starts: never written to a file of
Orbit's, typed into a pane or printed. A route may end (`until`, a free period, say): after that day it is not
offered. Everything is a switch: `route off` turns the whole offer off (a limited session is then left alone, as
before routes existed), `route off NAME` keeps one route (built-in or added) out of it, `route on` undoes either.

The same offer comes when the user asks for it: a short prompt that asks to change the model or to use a route or a
model by name ("kimi로 바꿔줘", "올라마 모델 쓰고 싶어", "switch to codex") is answered by the numbered routes instead
of a turn; the same words sent again go to the session as they are. Every switch carries the session's context: a
capsule of where it was (its last turns, the files it changed, the request held, the task) is kept in the store and
handed to the new session at its start, whichever host each runs on and whether or not an Orbit task is attached.

A route's end need not be known in advance. When its service refuses for payment (a free period over, credits spent,
a plan it needs: HTTP 402, or the service's words saying so), the session's next prompt gets the same offer, and
the route is not offered again until `route on NAME` says it answers again: waiting does not bring it back.

A switch is meant to be one number or name and nothing else (2026-11-18: "클로드 <-> 코덱스 스위칭도 자연스럽게"). The
session it starts runs in the permission mode of the one left, so its first command is not stopped by an approval
prompt the user never had (`route mode off`: the host's own default instead). A switch whose typing failed says why at
the session's next prompt, with the command to run by hand, and the same pick tries again; a pick is never passed on to
the model or recorded as a change of what the task must deliver. `route run` starts a session only in a terminal: one
started from a shell nobody watches could be neither seen nor steered.
"""
import datetime
import json
import os
import re

import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .store import now

BUILTIN = {
    'claude': {'name': 'claude', 'host': 'claude', 'label': 'Claude Code on your Anthropic account'},
    'codex': {'name': 'codex', 'host': 'codex', 'label': 'Codex on your OpenAI account'},
}
ROUTE_ENV = 'COSMOS_ORBIT_ROUTE'   # set by `route run` for the session it starts: the route a session runs on
FROM_ENV = 'COSMOS_ORBIT_ROUTE_FROM'  # set by a switch: the route the session left, offered back once it resets
LIMIT_ERRORS = ('rate_limit', 'billing_error')
AUTH_KEY = 'route-auth:'          # + built-in route: latest successful native login, not a quota claim
LIMIT_KEY = 'route-limit:'         # + route: the latest limit seen on that route (JSON)
SESSION_KEY = 'route-session:'     # + session: the limit it stopped on, the routes offered, the request held
UNKNOWN_RESET = 3600               # a limit without a reset time is taken to hold this long after it was seen
OFFER_TTL = 12 * 3600              # an offer is answered by a number or a name within this time
SOON_DAYS = 7                      # a route that ends within this many days says so
NAME = re.compile(r'^[a-z][a-z0-9-]{0,23}$')
# Claude Code asks for these models besides the main one (background work, subagents); on an endpoint that only
# serves the route's model, every one of them is that model.
MODEL_VARS = ('ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
              'CLAUDE_CODE_SUBAGENT_MODEL')
KEY_ENV = 'COSMOS_ORBIT_ROUTE_KEY'  # a Codex route: the variable Codex's provider reads its key from (env_key)
ENDPOINT_VARS = ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', KEY_ENV) + MODEL_VARS
APIS = {'anthropic': 'claude', 'openai': 'codex'}   # the protocol an added route's service speaks -> the host for it
AUTHS = ('bearer', 'x-api-key')    # how a Claude Code route sends its key: Authorization: Bearer, or x-api-key
PROVIDER = 'orbit-'                # + route: the model provider a Codex route defines for its run
CONTINUE_TEXT = ('Continue the unfinished task on this route. The previous session stopped (its usage limit, or its '
                 'service refused for payment) and the user chose this route; they asked nothing new.')
ASKED_TEXT = ('Continue the unfinished task on this route. The user switched to it by choice; the context of the session '
              'they left is in this session\'s brief. They asked nothing new.')
MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
CAPSULE_ENV = 'COSMOS_ORBIT_CAPSULE'  # set by `route run --capsule ID`: the context the session it starts receives
CAPSULE_KEY = 'route-capsule:'     # + id: where the session left was (JSON), served once to the session started
ASKED_TTL = 1800                   # an offer the user asked for is answered by the next prompt, within this time
ASK_LENGTH = 120                   # a prompt longer than this, or of several lines, is work, never a switch request
TURNS, TURN_CHARS, FILES = 8, 700, 20   # a capsule keeps this many last turns, each this long at most, and files
# A switch request names no route: "다른 모델로", "모델 바꿔줘", "switch models".
ASK_ANY = re.compile(r'((다른|딴)\s?(ai\s?)?모델|모델\s?(을|를|좀)?\s?(좀\s?)?(바꾸|바꿔|바꿀|변경|전환|교체|스위치)'
                     r'|\b(other|another|different)\s+(ai\s+)?models?\b|\b(switch|change)\s+(the\s+)?models?\b)', re.I)
# Work that mentions a model or a route is not a request to switch: "Organization 모델 변경", "모델 전환 기능 개발".
ASK_NOT = re.compile(r'(기능|개발|구현|코드|함수|클래스|스키마|필드|컬럼|마이그레이션|테이블|버그|테스트|리팩|커밋'
                     r'|\b(feature|implement\w*|develop\w*|code|function|class|schema|field|migration|commit|bug|tests?|'
                     r'refactor\w*)\b|[`/\\{}<>=]|::|\.(py|php|ts|tsx|js|go|rb|json|md)\b)', re.I)
ASK_CLASS = re.compile(r'\b[A-Z][a-z]+[A-Z]\w*\b|\b[A-Z][a-z]{2,}\s?모델')   # a class: "UserProfile", "Organization 모델"
ALIASES = {'클로드': 'claude', '코덱스': 'codex', '키미': 'kimi', '올라마': 'ollama'}
# A service refusing for payment. Seen 2026-11-18, Claude Code 2.1.327 and Codex 0.167.1 against an endpoint refusing
# the way Ollama's cloud does ("this model requires a subscription, upgrade for access: https://ollama.com/upgrade"):
# Claude Code records `error: authentication_failed` with `apiErrorStatus: 403` (402: no error kind of its own) and
# the service's words in the text; Codex ends the turn (`task_complete`) with `error.message` "unexpected status 402
# Payment Required: …" and `codex_error_info.http_connection_failed.http_status_code`. A 401 or 403 without these
# words (a wrong key, say) is not one.
PAID = re.compile(r'requires? an? (paid )?(subscription|plan)|upgrade (for|to get) access|ollama\.com/upgrade'
                  r'|payment required|insufficient[_ ](credits?|balance|funds|quota)|(out of|no) (usage )?credits'
                  r'|add credits|exceeded your current quota', re.I)
USAGE_WORDS = re.compile(r'usage limit|rate limit|hit your', re.I)   # a usage limit: it resets, it is no refusal
OUTCOME_KEY = 'route-switch:'      # + session: what became of the switch it started (JSON, left by the typist)
SWITCH_WAIT = 45                   # a switch types for at most about this long (its host is given 30s to quit)
# The permission modes carried to the session a switch starts. Claude Code names its mode (hook payload
# `permission_mode`, transcript `permissionMode`); Codex has an approval policy and a sandbox (rollout `turn_context`).
CLAUDE_MODES = ('acceptEdits', 'plan', 'dontAsk', 'bypassPermissions')
CODEX_APPROVALS = ('untrusted', 'on-failure', 'on-request', 'never')
CODEX_SANDBOXES = ('read-only', 'workspace-write', 'danger-full-access')
PERMISSION_OPTIONS = ('--dangerously-skip-permissions', '--allow-dangerously-skip-permissions', '--permission-mode',
                      '-a', '--ask-for-approval', '-s', '--sandbox', '--dangerously-bypass-approvals-and-sandbox',
                      '--full-auto', '--yolo')
# `route run NAME -- ARGS` with one of these answers once and ends: its output goes to whoever ran it, no session stays.
ONE_OFF = {'claude': ('-p', '--print', '--version', '-v', '--help', '-h'),
           'codex': ('exec', 'e', 'review', '--version', '-V', '--help', '-h')}


# ---------------------------------------------------------------- the routes

def _path(store):
    return Path(store.home) / 'routes.json'


def _data(store):
    """The whole routes.json: {'enabled': bool, 'builtin': {name: {'enabled': bool}}, 'routes': {name: route}}."""
    try:
        with _path(store).open(encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load(store):
    """The routes the user added: {name: route}."""
    routes = _data(store).get('routes')
    return {name: dict(route, name=name) for name, route in (routes or {}).items() if isinstance(route, dict)}


def _save(store, data):
    path = _path(store)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _write(store, routes):
    data = _data(store)
    data['routes'] = {k: {f: v for f, v in r.items() if f != 'name'} for k, r in routes.items()}
    _save(store, data)


def all_routes(store):
    data = _data(store)
    overrides = data.get('builtin') if isinstance(data.get('builtin'), dict) else {}
    routes = {}
    for name, route in BUILTIN.items():
        routes[name] = dict(route)
        if isinstance(overrides.get(name), dict) and overrides[name].get('enabled') is False:
            routes[name]['enabled'] = False
    routes.update(load(store))
    return routes


def enabled(store):
    """Whether a session stopped at its limit is offered routes at all (on unless the user turned it off)."""
    return _data(store).get('enabled') is not False


def route_on(route):
    return route.get('enabled') is not False


def set_enabled(store, value, name=None):
    """`route on|off` (the whole offer) or `route on|off NAME` (one route, built-in or added)."""
    data = _data(store)
    if not name:
        data['enabled'] = bool(value)
        _save(store, data)
        return {'routes': 'on' if value else 'off'}
    if name in BUILTIN:
        builtin = data.get('builtin') if isinstance(data.get('builtin'), dict) else {}
        builtin[name] = dict(builtin.get(name) or {}, enabled=bool(value))
        data['builtin'] = builtin
    elif name in (data.get('routes') or {}):
        data['routes'][name]['enabled'] = bool(value)
    else:
        raise ValueError('no route named %r (cosmos-orbit route list)' % name)
    _save(store, data)
    if value:
        forget_refusal(store, name)       # `route on NAME`: the user says it answers again
    return {'route': name, 'enabled': bool(value)}


def mode_carried(store):
    """Whether a switch starts the new session in the permission mode of the one left (on unless turned off)."""
    return _data(store).get('carry_mode') is not False


def set_mode(store, value):
    """`route mode on|off`."""
    data = _data(store)
    data['carry_mode'] = bool(value)
    _save(store, data)
    return {'mode': 'carried' if value else 'host default'}


def models(route):
    """The models a route serves, its default first ([] for a built-in route: the host's own choice)."""
    listed = route.get('models') if isinstance(route.get('models'), list) else []
    first = route.get('model')
    return ([first] if first else []) + [m for m in listed if m and m != first]


def default_key(name):
    """Where an added route reads its key unless told otherwise: the user's own environment, then keychain."""
    return 'env:COSMOS_ORBIT_%s_KEY,keychain:cosmos-orbit-%s' % (name.upper().replace('-', '_'), name)


def key_sources(spec):
    """'env:OLLAMA_API_KEY,keychain:cosmos-orbit-ollama' -> [('env', 'OLLAMA_API_KEY'), ('keychain', ...)]."""
    sources = []
    for part in (spec or '').split(','):
        kind, _, value = part.strip().partition(':')
        if kind not in ('env', 'keychain') or not value.strip():
            raise ValueError('a key is env:VARIABLE or keychain:SERVICE (several, comma separated), not %r' % part.strip())
        sources.append((kind, value.strip()))
    if not sources:
        raise ValueError('a route needs --key env:VARIABLE or keychain:SERVICE')
    return sources


def _day(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError('--until is a day, YYYY-MM-DD: %r' % value)


def add(store, name, base_url, model, key=None, label=None, until=None, api=None, auth=None):
    """Add (or replace) a route: `model` is one model or several, comma separated, the first the default."""
    if not NAME.match(name or '') or name in BUILTIN:
        raise ValueError('a route name is lowercase letters, digits and dashes (not claude or codex): %r' % name)
    api = (api or 'anthropic').strip().lower()
    if api not in APIS:
        raise ValueError('--api is anthropic (Claude Code, an Anthropic-compatible endpoint) or openai (Codex, an '
                         'OpenAI Responses-compatible endpoint): %r' % api)
    auth = (auth or 'bearer').strip().lower()
    if auth not in AUTHS:
        raise ValueError('--auth is bearer or x-api-key: %r' % auth)
    if auth != 'bearer' and api != 'anthropic':
        raise ValueError('--auth x-api-key is for an Anthropic-compatible route; Codex sends its key as a bearer token')
    if not re.match(r'^https?://[^\s]+$', base_url or ''):
        raise ValueError('--base-url must be an http(s) URL: %r' % base_url)
    served = [m.strip() for m in (model or '').split(',') if m.strip()]
    if not served:
        raise ValueError('--model is required (one, or several comma separated: the first is the default)')
    key = key or default_key(name)
    key_sources(key)
    if until:
        _day(until)
    routes = load(store)
    routes.pop(name, None)
    routes[name] = {'host': APIS[api], 'api': api, 'auth': auth, 'base_url': base_url.rstrip('/'), 'model': served[0],
                    'models': served, 'key': key,
                    'label': (label or '%s at %s' % (served[0], base_url)).strip(), 'until': until or None,
                    'enabled': True, 'added': now()}
    _write(store, routes)
    forget_refusal(store, name)           # added again (a new key, a new plan): what it refused before is past
    return dict(routes[name], name=name)


def remove(store, name):
    routes = load(store)
    if name not in routes:
        raise ValueError('no added route named %r (built-in routes cannot be removed: route off %s)' % (name, name))
    routes.pop(name)
    _write(store, routes)
    return {'removed': name, 'note': 'a key you put in your keychain stays there: route key %s --remove first to '
                                     'delete it' % name}


def _keychain_service(route):
    services = [value for kind, value in key_sources(route.get('key')) if kind == 'keychain']
    if not services or sys.platform != 'darwin':
        envs = [value for kind, value in key_sources(route.get('key')) if kind == 'env']
        raise ValueError('route %s reads its key from %s: put it in your shell profile (export %s=…)' % (
            route['name'], ', '.join('env:' + e for e in envs) or route.get('key'), envs[0] if envs else 'VARIABLE')
                         if envs else 'route %s has no keychain source (its key: %s)' % (route['name'], route.get('key')))
    return services[0]


def _security_line(*args):
    """One command line for `security -i`, which reads its commands from stdin: the key never reaches argv."""
    return ' '.join('"%s"' % str(a).replace('\\', '\\\\').replace('"', '\\"') for a in args) + '\n'


def set_key(store, name, value, runner=None):
    """`route key NAME`: put this user's key for the route into their keychain. The value is never printed."""
    route = load(store).get(name)
    if not route:
        raise ValueError('no added route named %r (built-in routes use your own claude or codex login)' % name)
    value = (value or '').strip()
    if not value or len(value) < 8 or any(c.isspace() for c in value):
        raise ValueError('that is not a key (empty, too short, or it has spaces or line breaks)')
    service = _keychain_service(route)
    import getpass
    line = _security_line('add-generic-password', '-U', '-s', service, '-a', getpass.getuser(),
                          '-l', 'cosmos-orbit route %s' % name, '-w', value)
    done = (runner or subprocess.run)(['security', '-i'], input=line, capture_output=True, text=True, timeout=20)
    if done.returncode != 0 or 'error' in (done.stderr or '').lower():
        raise ValueError('the keychain did not take the key (%s)' % ((done.stderr or '').strip()[:200] or done.returncode))
    if read_key(dict(route, key='keychain:' + service), environ={}) != value:
        raise ValueError('the key was written but does not read back from keychain:%s' % service)
    sources = key_sources(route.get('key'))
    before = sources[:sources.index(('keychain', service))]
    shadow = [v for kind, v in before if kind == 'env' and os.environ.get(v)]
    return {'route': name, 'stored': 'keychain:' + service, 'characters': len(value),
            'note': ('env:%s is set in this shell and is read before the keychain' % shadow[0]) if shadow else ''}


def delete_key(store, name, runner=None):
    route = load(store).get(name)
    if not route:
        raise ValueError('no added route named %r' % name)
    service = _keychain_service(route)
    done = (runner or subprocess.run)(['security', 'delete-generic-password', '-s', service], capture_output=True,
                                      text=True, timeout=20)
    return {'route': name, 'removed': 'keychain:' + service if done.returncode == 0 else None,
            'note': '' if done.returncode == 0 else 'no key was there'}


def _quiet(argv):
    try:
        return subprocess.run(argv, capture_output=True, timeout=10).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def key_present(route, environ=None, runner=None):
    """Whether the route's key can be read, without reading it."""
    environ = os.environ if environ is None else environ
    for kind, value in key_sources(route.get('key')):
        if kind == 'env' and environ.get(value):
            return True
        if kind == 'keychain' and sys.platform == 'darwin' \
                and (runner or _quiet)(['security', 'find-generic-password', '-s', value]) == 0:
            return True
    return False


def read_key(route, environ=None):
    environ = os.environ if environ is None else environ
    for kind, value in key_sources(route.get('key')):
        if kind == 'env' and environ.get(value):
            return environ[value]
        if kind == 'keychain' and sys.platform == 'darwin':
            try:
                found = subprocess.run(['security', 'find-generic-password', '-s', value, '-w'],
                                       capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                continue
            if found.returncode == 0 and found.stdout.strip():
                return found.stdout.strip()
    return None


def days_left(route, at=None):
    """Whole days left before the route ends (0 = its last day), or None when it does not end."""
    if not route.get('until'):
        return None
    today = datetime.datetime.fromtimestamp(time.time() if at is None else at).date()
    return (_day(route['until']) - today).days


def current_route(host, environ=None):
    environ = os.environ if environ is None else environ
    return environ.get(ROUTE_ENV) or host


# ---------------------------------------------------------------- limits

def _tail(path, size=262144):
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - size))
            data = handle.read().decode('utf-8', 'replace')
    except (OSError, TypeError):
        return []
    return data.splitlines()


def _stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def reset_from_text(text, seen):
    """'… resets 2:50pm (Asia/Seoul)' or '… resets Oct 19 at 6pm (Asia/Seoul)' -> epoch seconds, or None."""
    match = re.search(r'resets\s+(?:(?P<mon>[A-Za-z]{3})[a-z]*\s+(?P<day>\d{1,2})\s+at\s+)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?'
                      r'\s*(?P<ap>am|pm)\s*(?:\((?P<tz>[^)]+)\))?', text or '', re.I)
    if not match:
        return None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(match['tz']) if match['tz'] else None
    except Exception:  # noqa: BLE001 - an unknown zone reads as local time
        zone = None
    base = datetime.datetime.fromtimestamp(seen, zone) if zone else datetime.datetime.fromtimestamp(seen).astimezone()
    hour = int(match['h']) % 12 + (12 if match['ap'].lower() == 'pm' else 0)
    try:
        if match['mon']:
            target = base.replace(month=MONTHS.index(match['mon'].lower()) + 1, day=int(match['day']), hour=hour,
                                  minute=int(match['m'] or 0), second=0, microsecond=0)
            if target < base - datetime.timedelta(days=1):
                target = target.replace(year=target.year + 1)
        else:
            target = base.replace(hour=hour, minute=int(match['m'] or 0), second=0, microsecond=0)
            if target <= base:
                target += datetime.timedelta(days=1)
    except ValueError:
        return None
    return target.timestamp()


def _content_text(message):
    content = (message or {}).get('content')
    if isinstance(content, str):
        return content
    return ' '.join(part.get('text', '') for part in content or () if isinstance(part, dict)).strip()


def claude_login(path):
    """A native /login command followed by its parent-linked successful local output."""
    records = []
    for line in _tail(path):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and not row.get('isSidechain'):
            records.append(row)
    commands = {r.get('uuid'): r for r in records if r.get('uuid')}
    for row in reversed(records):
        if row.get('type') != 'user' or row.get('userType') != 'external':
            continue
        content = (row.get('message') or {}).get('content')
        if content != '<local-command-stdout>Login successful</local-command-stdout>':
            continue
        parent = commands.get(row.get('parentUuid')) or {}
        command = (parent.get('message') or {}).get('content')
        if (parent.get('type') == 'user' and parent.get('userType') == 'external'
                and isinstance(command, str)
                and re.fullmatch(r'\s*<command-name>/login</command-name>\s*'
                                 r'<command-message>login</command-message>\s*'
                                 r'<command-args>.*?</command-args>\s*', command, re.S)
                and _stamp(row.get('timestamp'))):
            return {'id': row.get('uuid'), 'seen_at': _stamp(row['timestamp'])}
    return None


def _after_auth(store, route, limit):
    """Old account limits cannot block a retry after a newer successful native login."""
    changed = _json(store, AUTH_KEY + route) if route == 'claude' else {}
    if limit and changed.get('seen_at', 0) > (limit.get('seen_at') or 0):
        return None
    return limit


def _refresh_auth(store, path, route):
    if route != 'claude':
        return
    changed = claude_login(path)
    held = _json(store, AUTH_KEY + route)
    if changed and changed['seen_at'] > held.get('seen_at', 0):
        store.preference(AUTH_KEY + route, json.dumps(changed))


def claude_limit(path, auth=True):
    """The limit a Claude Code session stopped on: its last answer is the host's synthetic limit line. Else None."""
    login = claude_login(path) if auth else None
    for line in reversed(_tail(path)):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('type') != 'assistant' or record.get('isSidechain'):
            continue
        if login and login['seen_at'] > (_stamp(record.get('timestamp')) or 0):
            return None
        if not record.get('isApiErrorMessage'):
            return None                                   # the last answer was a real one: no limit holds
        text = _content_text(record.get('message'))
        seen = _stamp(record.get('timestamp')) or time.time()
        if record.get('error') != 'rate_limit' and paid_refusal(text, record.get('apiErrorStatus')):
            return refusal(record.get('uuid') or str(seen), text, seen)
        if record.get('error') not in LIMIT_ERRORS:
            return None                                   # another error (a network one, say): no limit holds
        quota = record.get('quotaLimits') if isinstance(record.get('quotaLimits'), dict) else {}
        kind = re.search(r"hit your ([\w-]+) limit", text or '')
        resets = quota.get('resetsAt')
        return {'id': record.get('uuid') or str(seen), 'kind': kind[1] if kind else (
                    'credits' if 'credits' in (text or '') else quota.get('rateLimitType') or record['error']),
                'resets_at': float(resets) if isinstance(resets, (int, float)) and resets > 0 else reset_from_text(text, seen),
                'text': (text or record['error'])[:200], 'seen_at': seen}
    return None


def codex_limit(path):
    """The limit a Codex session stopped on: its latest token count says a rate limit was reached. Else None."""
    for line in reversed(_tail(path)):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get('payload') if isinstance(record, dict) else None
        if not isinstance(payload, dict):
            continue
        seen = _stamp(record.get('timestamp')) or time.time()
        error = payload.get('error') if payload.get('type') == 'task_complete' else None
        if isinstance(error, dict):                       # Codex 0.167: the turn ended on this error
            info = error.get('codex_error_info') if isinstance(error.get('codex_error_info'), dict) else {}
            failed = info.get('http_connection_failed') if isinstance(info.get('http_connection_failed'), dict) else {}
            message = str(error.get('message') or '')
            if not USAGE_WORDS.search(message) and paid_refusal(message, failed.get('http_status_code')):
                return refusal('codex-paid:%s' % (payload.get('turn_id') or record.get('timestamp')), message, seen)
        if payload.get('type') == 'error' and USAGE_WORDS.search(str(payload.get('message'))):
            text = str(payload['message'])
            return {'id': 'codex-error:%s' % record.get('timestamp'), 'kind': 'usage', 'text': text[:200],
                    'resets_at': reset_from_text(text, seen), 'seen_at': seen}
        if payload.get('type') == 'error' and paid_refusal(str(payload.get('message'))):
            return refusal('codex-paid:%s' % record.get('timestamp'), str(payload.get('message')), seen)
        if payload.get('type') != 'token_count':
            continue
        limits = payload.get('rate_limits') if isinstance(payload.get('rate_limits'), dict) else {}
        reached = limits.get('rate_limit_reached_type')
        if not reached:
            return None                                   # the latest count reached nothing: no limit holds
        windows = [w for w in (limits.get('primary'), limits.get('secondary')) if isinstance(w, dict) and w.get('resets_at')]
        full = [w for w in windows if (w.get('used_percent') or 0) >= 100] or windows
        return {'id': 'codex:%s' % record.get('timestamp'), 'kind': str(reached), 'seen_at': seen,
                'resets_at': float(max(w['resets_at'] for w in full)) if full else None,
                'text': 'Codex rate limit reached (%s)' % reached}
    return None


def paid_refusal(text, status=None):
    """Whether an API error is its service asking for payment: HTTP 402, or the service's own words, at any status."""
    return status == 402 or bool(PAID.search(text or ''))


def refusal(ident, text, seen):
    """A refusal for payment, held like a limit that has no reset: waiting does not end it."""
    return {'id': ident, 'kind': 'payment', 'paid': True, 'text': (text or 'payment required')[:200],
            'resets_at': None, 'seen_at': seen}


def forget_refusal(store, name):
    limit = route_limit(store, name)
    if limit and limit.get('paid'):
        store.preference(LIMIT_KEY + name, 'null')


def read_limit(path, host, route=None):
    if not path:
        return None
    return codex_limit(path) if host == 'codex' else claude_limit(path, auth=route in (None, 'claude'))


def active(limit, at=None):
    if not limit:
        return False
    if limit.get('paid'):
        return True                       # until the route answers again: `route on NAME`, or the route added again
    at = time.time() if at is None else at
    if limit.get('resets_at'):
        return limit['resets_at'] > at
    return (limit.get('seen_at') or 0) + UNKNOWN_RESET > at


def _json(store, key):
    try:
        value = json.loads(store.preference(key) or 'null')
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def session_limited(store, session, host, at=None):
    """The limit a session stopped on, read from the transcript its hooks last reported, while it holds; else None."""
    from .verifier import TRANSCRIPT_KEY
    route = _json(store, SESSION_KEY + session).get('route') or host or 'claude'
    path = store.preference(TRANSCRIPT_KEY + session) if session else None
    _refresh_auth(store, path, route)
    limit = _after_auth(store, route, read_limit(path, host or 'claude', route))
    return limit if active(limit, at) else None


def record_limit(store, route, limit):
    if _after_auth(store, route, limit):
        store.preference(LIMIT_KEY + route, json.dumps(dict(limit, route=route), ensure_ascii=False))


def route_limit(store, route):
    return _after_auth(store, route, _json(store, LIMIT_KEY + route) or None)


def _clock(stamp, at):
    when = datetime.datetime.fromtimestamp(stamp)
    text = when.strftime('%H:%M') if when.date() == datetime.datetime.fromtimestamp(at).date() else when.strftime('%m-%d %H:%M')
    minutes = max(0, int((stamp - at) // 60))
    return '%s (in %s)' % (text, '%dh %02dm' % divmod(minutes, 60) if minutes >= 60 else '%dm' % minutes)


def _limited_text(limit, at):
    if limit.get('paid'):
        return 'refused for payment %s (route on %s once it answers again)' % (
            datetime.datetime.fromtimestamp(limit.get('seen_at') or at).strftime('%m-%d %H:%M'), limit.get('route') or 'NAME')
    if limit.get('resets_at'):
        return 'limited until ' + _clock(limit['resets_at'], at)
    return 'limited (%s) since %s' % (limit.get('kind') or 'limit', datetime.datetime.fromtimestamp(limit.get('seen_at') or at).strftime('%H:%M'))


# ---------------------------------------------------------------- availability and the offer

def availability(store, route, at=None, environ=None, runner=None, which=None):
    """(usable, why not) for one route right now."""
    at = time.time() if at is None else at
    if not route_on(route):
        return False, 'off (route on %s)' % route['name']
    if not (which or shutil.which)(route['host']):
        return False, '%s is not installed' % route['host']
    left = days_left(route, at)
    if left is not None and left < 0:
        return False, 'ended %s' % route['until']
    if route['name'] not in BUILTIN and not key_present(route, environ, runner):
        return False, 'no key (set %s)' % route.get('key')
    limit = route_limit(store, route['name'])
    if active(limit, at):
        return False, _limited_text(limit, at)
    return True, ''


def _until_note(route, at):
    left = days_left(route, at)
    if left is None:
        return ''
    if left < 0:
        return ' · ended %s' % route['until']
    return ' · until %s (%s)' % (route['until'], 'last day' if left == 0 else '%d day%s left' % (left, '' if left == 1 else 's'))


def choices(store, exclude, at=None, environ=None, runner=None, which=None):
    """The usable routes (built-in first, then the added ones by name) and the unusable ones with the reason."""
    usable, unusable = [], []
    routes = all_routes(store)
    for name in sorted(routes, key=lambda n: (n not in BUILTIN, n)):
        if name == exclude:
            continue
        ok, why = availability(store, routes[name], at, environ, runner, which)
        (usable if ok else unusable).append(routes[name] if ok else (routes[name], why))
    return usable, unusable


def offer_rows(usable):
    """[(pick, route, model)]: each usable route on its default model, then each other model it serves as
    NAME:MODEL. A pick is what the user types (or its number); a route name never has a colon, so it splits."""
    rows = []
    for route in usable:
        served = models(route)
        rows.append((route['name'], route, served[0] if served else None))
        rows += [('%s:%s' % (route['name'], m), route, m) for m in served[1:]]
    return rows


def split_pick(pick):
    name, _, model = (pick or '').partition(':')
    return name, (model or None)


def guidance(store, label, limit, usable, unusable, at, held):
    orbit = _orbit(store)
    when = (' · resets ' + _clock(limit['resets_at'], at)) if limit.get('resets_at') else ''
    lines = ['Orbit: %s was refused: its service asks for payment ("%s").' % (label, _cut(limit.get('text'), 160))
             if limit.get('paid') else 'Orbit: %s stopped at its %s limit%s.' % (label, limit.get('kind') or 'usage', when)]
    if usable:
        lines.append('Switch in this pane — type a number or a name and press Enter (after the words put back in the '
                     'input is fine)%s:' % ('; the request you just typed is carried over' if held else ''))
        for number, (pick, route, model) in enumerate(offer_rows(usable), 1):
            if pick == route['name']:
                many = len(models(route)) > 1
                lines.append('  %d  %-10s %s%s%s' % (number, pick, route['label'], ' · model ' + model if many else '',
                                                    _until_note(route, at)))
            else:
                lines.append('  %d  %-10s the same route on model %s' % (number, pick, model))
        lines.append('  (or quit and run: %s route run NAME [--model MODEL] --carry)' % orbit)
    else:
        lines.append('No other route is usable right now.')
    for route, why in unusable:
        lines.append('  -  %-10s %s' % (route['name'], why))
    again = ('Waiting will not bring it back: it is not offered until %s route on %s says it answers again (paid, or '
             'free again).' % (orbit, limit.get('route') or 'NAME') if limit.get('paid') else 'Or wait%s and type again.' % (
                 ' until ' + _clock(limit['resets_at'], at).split(' (')[0] if limit.get('resets_at') else ''))
    lines.append('%s Routes: %s route list · add one: %s route add NAME --base-url URL --model MODEL[,MODEL…] '
                 '[--api anthropic|openai] · this offer off: %s route off' % (again, orbit, orbit, orbit))
    return '\n'.join(lines)


def _orbit(store):
    from .attach import orbit_command
    return orbit_command(store)


_shell_word = shlex.quote


def _pick(text, offer, blocked=None):
    """The pick a prompt makes from the offer, or None. Claude Code puts a blocked prompt back into its input, so
    what arrives is often those words with the pick typed after them ("stubo로 바꿔줘1", seen 2026-11-17 in a real
    pane): the words the hook blocked, then a number or a name, count as that pick. A pick typed before the words
    are back arrives ahead of them, on a line of its own ("1\ncodex로 바꿔줘", seen 2026-11-18 in cmux with Claude
    Code 2.1.327: it went to the model, which used the turn to work out why it had not switched)."""
    text, blocked = (text or '').strip(), (blocked or '').strip()
    if blocked and text != blocked:
        if text.startswith(blocked):
            text = text[len(blocked):]
        elif text.endswith(blocked):
            text = text[:-len(blocked)]
    word = text.strip().strip('`"\'').strip().lower()
    if word.isdigit() and 1 <= int(word) <= len(offer):
        return offer[int(word) - 1]
    return next((pick for pick in offer if pick.lower() == word), None)   # a model name may have capitals


def _orbit_line(text):
    """A line Orbit typed itself (carry, resume, wake): never held as the user's request."""
    from .supervision import RESUME_LINE
    from .typist import CARRY_LINE
    from .wake import WAKE_LINE
    return text in (CARRY_LINE, RESUME_LINE, WAKE_LINE)


def _label(store, route_name, host):
    route = all_routes(store).get(route_name)
    return route['label'] if route else route_name or host


# ---------------------------------------------------------------- a switch the user asks for

def targets(store):
    """The words that name a route or a model: route names, the models they serve and each model's stem ('kimi' of
    'kimi-k3.1-pro'), the Korean spellings of the hosts and of Ollama."""
    words = {'ollama', '올라마'}
    for name, route in all_routes(store).items():
        words.add(name.lower())
        for model in models(route):
            words.add(model.lower())
            stem = re.split(r'[-:/_.\s]', model.lower())[0]
            if len(stem) >= 3:
                words.add(stem)
    words |= {korean for korean, english in ALIASES.items() if english in words}
    return sorted(words, key=len, reverse=True)       # the longest first: 'kimi-k3.1-pro' before 'kimi'


def asks_switch(store, text):
    """Whether a prompt asks to change the model or to run on a route or model it names. Deterministic and narrow:
    one short line; a request for work that mentions a model ("Organization 모델 변경", "모델 전환 기능 개발") is not
    one. A route's or a model's name alone is one ("claude", "코덱스": typed on 2026-11-18 to switch, it went to the
    model and into the task as a requirement). Returns the words named ([] for "다른 모델로"), or None when the prompt
    is not a switch request."""
    text = (text or '').strip()
    if not text or len(text) > ASK_LENGTH or '\n' in text or _orbit_line(text):
        return None
    words = targets(store)
    named = [w for w in words if re.search(r'(?<![a-z0-9])' + re.escape(w) + r'(?![a-z])', text, re.I)]
    if text.strip('`"\'').strip().lower() in words:
        return named
    bare = text
    for word in named:
        bare = re.sub(re.escape(word), ' ', bare, flags=re.I)
    if ASK_NOT.search(bare) or ASK_CLASS.search(bare):
        return None
    if ASK_ANY.search(text):
        return named
    for word in named:
        target = re.escape(word) + r'[a-z0-9._:-]{0,24}\s*(?:모델\s*)?'
        if re.search(target + r'(?:으로|로|쪽으로)\s*(?:좀\s*)?(?:바꾸|바꿔|바꿀|변경|전환|교체|스위치|갈아타|넘어가|옮겨|'
                              r'가자|가줘|하자|해\s?줘|할래|돌아가|실행|시작|진행|계속)', text, re.I) \
                or re.search(target + r'(?:을|를)?\s*(?:좀\s*)?(?:쓰고\s?싶|쓸래|쓰자|써\s?줘|써\s?봐|써\s?보|사용하고\s?싶|'
                                      r'사용할래|사용하자|사용해)', text, re.I) \
                or re.search(r'\b(?:(?:switch|change|move|go)\s+(?:back\s+)?to|use|try|run\s+on)\s+(?:the\s+)?'
                             + re.escape(word), text, re.I):
            return named
    return None


def ask_guidance(store, label, usable, unusable, at, named):
    orbit = _orbit(store)
    rows = offer_rows(usable)
    lines = ['Orbit: you asked to switch. This session runs on %s.' % label]
    if rows:
        lines.append('Switch in this pane — type a number or a name and press Enter (after the words put back in the '
                     'input is fine); this conversation\'s context goes with it to the new session:')
        for number, (pick, route, model) in enumerate(rows, 1):
            if pick == route['name']:
                many = len(models(route)) > 1
                lines.append('  %d  %-10s %s%s%s' % (number, pick, route['label'], ' · model ' + model if many else '',
                                                    _until_note(route, at)))
            else:
                lines.append('  %d  %-10s the same route on model %s' % (number, pick, model))
        meant = [str(n) for n, (pick, route, model) in enumerate(rows, 1)
                 if any(w in (pick.lower(), (model or '').lower(), route['name']) or ALIASES.get(w) == route['name']
                        or (model and w in model.lower().split('-')) for w in named)]
        if meant:
            lines.append('  (what you named: %s)' % ', '.join(meant))
    else:
        lines.append('No other route is usable right now.')
    for route, why in unusable:
        lines.append('  -  %-10s %s' % (route['name'], why))
    lines.append('Not a switch? Send the same words again and they go to this session. Routes: %s route list · add one: '
                 '%s route add NAME --base-url URL --model MODEL[,MODEL…] [--api anthropic|openai] · this offer off: '
                 '%s route off' % (orbit, orbit, orbit))
    return '\n'.join(lines)


# ---------------------------------------------------------------- the context a switch carries

def _cut(text, size=TURN_CHARS):
    text = re.sub(r'\s+\n', '\n', (text or '').strip())
    return text if len(text) <= size else text[:size // 2].rstrip() + ' … ' + text[-(size // 2):].lstrip()


def _texts(content):
    """The text of a message's content; '' for a tool result (it is the host's, not a turn)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list) or any(isinstance(p, dict) and p.get('type') == 'tool_result' for p in content):
        return ''
    return '\n'.join(p.get('text') or '' for p in content
                     if isinstance(p, dict) and p.get('type') in ('text', 'Text', 'input_text', 'output_text')).strip()


def conversation(path, host, turns=TURNS):
    """(turns, files) of a session from its own transcript: the last `turns` messages [[role, text]] the user and the
    model exchanged (oldest first, consecutive ones of one side joined) and the files the session changed."""
    from .requirements import classify
    said, files = [], []

    def say(role, text):
        text = (text or '').strip()
        if not text or (role == 'user' and (classify(text) == 'system' or _orbit_line(text))):
            return
        if said and said[-1][0] == role:
            said[-1][1] = said[-1][1] + '\n' + text
        else:
            said.append([role, text])

    def touched(path_):
        if path_ and isinstance(path_, str):
            path_ = path_[7:] if path_.startswith('file://') else path_
            if path_ in files:
                files.remove(path_)
            files.append(path_)

    lines = _tail(path, 4_000_000)       # a long turn's tool output alone can fill a megabyte
    items = host == 'codex' and any('"item_completed"' in line for line in lines[-400:])
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('isSidechain') or record.get('isMeta'):
            continue
        kind = record.get('type')
        attached = record.get('attachment') if isinstance(record.get('attachment'), dict) else {}
        if kind == 'attachment' and attached.get('type') == 'queued_command':
            say('user', _texts(attached.get('prompt')))             # Claude Code: a prompt typed during a turn
            continue
        if kind in ('user', 'assistant'):                           # Claude Code
            content = (record.get('message') or {}).get('content')
            if kind == 'user':
                say('user', _texts(content))
                continue
            if record.get('isApiErrorMessage'):
                continue
            say('assistant', _texts(content))
            for part in content if isinstance(content, list) else ():
                if isinstance(part, dict) and part.get('type') == 'tool_use' and part.get('name') in (
                        'Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
                    touched((part.get('input') or {}).get('file_path') or (part.get('input') or {}).get('notebook_path'))
            continue
        payload = record.get('payload') if isinstance(record.get('payload'), dict) else {}
        if items:                                                   # Codex 0.16x: completed items
            item = payload.get('item') if payload.get('type') == 'item_completed' else None
            if not isinstance(item, dict):
                continue
            if item.get('type') == 'UserMessage':
                say('user', _texts(item.get('content')))
            elif item.get('type') == 'AgentMessage':
                say('assistant', _texts(item.get('content')))
            elif item.get('type') == 'FileChange' and isinstance(item.get('changes'), dict):
                for changed in item['changes']:
                    touched(changed)
        elif kind == 'event_msg' and payload.get('type') in ('user_message', 'agent_message'):   # earlier Codex
            say('user' if payload['type'] == 'user_message' else 'assistant', payload.get('message'))
        elif kind == 'response_item' and payload.get('name') == 'apply_patch':
            for changed in re.findall(r'^\*\*\* (?:Add|Update|Delete) File: (.+)$',
                                      str(payload.get('input') or payload.get('arguments') or ''), re.M):
                touched(changed.strip())
    return [[role, _cut(text)] for role, text in said[-turns:]], files[-FILES:]


def make_capsule(store, session, host, to_route, model, state, task, transcript, cwd, at=None, environ=None,
                 mode=None):
    """Keep where the session left was, for the session the switch starts: its own words, never a summary by a model.

    On a second or later switch, prepend the turns and files this session itself received. A new process's transcript
    does not contain its SessionStart brief, so without this the previous conversation would fall out after one hop.
    Only the capsule served to this exact session may be inherited; an environment variable alone is not proof.
    """
    import uuid
    from .attach import CLOSED
    turns, files = conversation(transcript, host) if transcript else ([], [])
    environ = os.environ if environ is None else environ
    inherited = _json(store, CAPSULE_KEY + environ.get(CAPSULE_ENV, ''))
    if session and inherited.get('served_to') == session:
        turns = (inherited.get('turns') or []) + turns
        files = (inherited.get('files') or []) + [path for path in files if path not in (inherited.get('files') or [])]
    turns, files = turns[-TURNS:], files[-FILES:]
    held, asked = state.get('held'), state.get('asked')
    if turns and turns[-1][0] == 'user' and turns[-1][1].strip() in {(held or '').strip(), (asked or '').strip()}:
        turns = turns[:-1]
    capsule = {'id': uuid.uuid4().hex[:12], 'at': now(), 'reason': 'asked' if asked else 'limit',
               'asked': asked, 'held': held, 'from_route': state.get('route') or host, 'from_host': host,
               'from_session': session, 'to_route': to_route, 'model': model, 'cwd': cwd,
               'task': task['id'] if task and task.get('status') not in CLOSED else None, 'turns': turns, 'files': files,
               'mode': mode, 'served_to': None}
    store.preference(CAPSULE_KEY + capsule['id'], json.dumps(capsule, ensure_ascii=False))
    return capsule


def capsule_text(store, capsule):
    to = all_routes(store).get(capsule['to_route']) or {'host': '?', 'label': capsule['to_route']}
    stamp = _stamp(capsule.get('at'))
    when = datetime.datetime.fromtimestamp(stamp).strftime('%m-%d %H:%M') if stamp else '?'
    why = ('the user chose to switch (they typed "%s")' % capsule['asked']) if capsule.get('reason') == 'asked' else \
        'the previous session stopped at its usage limit and the user chose this route'
    lines = ['[orbit] Carried over by a route switch at %s: the previous session ran on route %s (%s); this one runs on '
             'route %s (%s%s). Why: %s.' % (when, capsule['from_route'], capsule['from_host'], capsule['to_route'],
                                            to['label'], (' · model ' + capsule['model']) if capsule.get('model') else '',
                                            why),
             'Folder: %s · Orbit task: %s' % (capsule.get('cwd') or '?', (capsule['task'] + ' (its brief has the goal, '
                                              'corrections and checkpoint)') if capsule.get('task') else 'none')]
    if capsule.get('held'):
        lines.append('The user\'s request carried over (it was blocked by the limit, never run): "%s"' % _cut(capsule['held'], 4000))
    if capsule.get('turns'):
        lines.append('Where the previous session was — its last turns, oldest first, in its own words:')
        lines += ['  [%s] %s' % (role, text.replace('\n', '\n    ')) for role, text in capsule['turns']]
    else:
        lines.append('The previous session\'s transcript had no turns to carry.')
    if capsule.get('files'):
        lines.append('Files it changed: ' + ', '.join(capsule['files']))
    if capsule.get('held'):
        lines.append('Now: run the request carried over, with this context.')
    elif capsule.get('task'):
        lines.append('Now: continue the task from where the previous session was.')
    else:
        lines.append('Now: if the last request above was still being worked on, continue it; if it was already '
                     'answered, tell the user in one line that this session has the previous context, and wait.')
    return '\n'.join(lines)


def capsule_note(store, session, environ=None):
    """The context a switch carried, for the session `route run --capsule` started: once, to its first session (a
    /clear later in that process does not get it again)."""
    environ = os.environ if environ is None else environ
    capsule_id = environ.get(CAPSULE_ENV)
    if not capsule_id or not session:
        return None
    capsule = _json(store, CAPSULE_KEY + capsule_id)
    if not capsule or capsule.get('served_to'):
        return None
    store.preference(CAPSULE_KEY + capsule_id, json.dumps(dict(capsule, served_to=session, served_at=now()),
                                                          ensure_ascii=False))
    return capsule_text(store, capsule)


def on_prompt(store, payload, host, task, environ=None, at=None, terminal=None, spawn=None, pid=None,
              which=None, runner=None):
    """The prompt hook's part: a block reply (the offer, or the switch it started), or None to let the prompt on."""
    session = payload.get('session_id')
    if not session or not enabled(store):
        return None                       # routes off: a limited session is left alone, as before routes existed
    at = time.time() if at is None else at
    text = (payload.get('prompt') or '').strip()
    on = current_route(host, environ)
    _refresh_auth(store, payload.get('transcript_path'), on)
    state = _json(store, SESSION_KEY + session)
    offer = state.get('offer') or []
    live = offer and (_stamp(state.get('at')) or 0) + (ASKED_TTL if state.get('asked') else OFFER_TTL) > at

    def go(chosen, state, note=''):
        mode = session_mode(payload, host, pid, environ=environ) if mode_carried(store) else None
        reply = switch(store, session, host, task, chosen, state, text, environ=environ, at=at, terminal=terminal,
                       spawn=spawn, pid=pid, which=which, runner=runner, transcript=payload.get('transcript_path'),
                       cwd=payload.get('cwd'), mode=mode)
        return dict(reply, reason=note + '\n' + reply['reason']) if note else reply

    if live:
        chosen = _pick(text, offer, state.get('blocked_text'))
        if chosen:
            return go(chosen, state)
    # A switch this session started, and the session is still here to be asked. The pick typed again is never the
    # model's to answer: on 2026-11-18 "1" and "claude", typed again after a switch cmux had refused, went through as
    # prompts and were recorded as changes of what the task must deliver.
    note = ''
    result, why = switch_result(store, session, state, at)
    if result:
        # Claude Code put the pick itself back into its input: "1" typed behind it arrives as "…11".
        again = (_pick(text, state.get('picked_from') or [], state.get('blocked_text'))
                 or _pick(text, state.get('picked_from') or [], state.get('picked_text')))
        if result == 'typing':
            if again:
                return {'decision': 'block', 'reason': (
                    'Orbit: already switching this pane to %s: this session quits, then `%s` starts. Nothing more to '
                    'type.' % (state.get('switched'), state.get('run_line')))}
        else:
            note = ('Orbit: the switch to %s did not happen: %s. Nothing was started. Type its number or name again '
                    'to retry, or do it by hand: quit this session with /exit and run:\n  %s'
                    % (state.get('switched'), why, state.get('run_line')))
            state = dict(state, switch_told=True)
            store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
            if again:
                return go(again, state, note)
    limit = _after_auth(store, on, read_limit(payload.get('transcript_path'), host, on))
    if state.get('blocked') and not state.get('switched') and not active(limit, at):
        state = dict(state, offer=[], blocked=None, limit=None, held=None, blocked_text=None)
        offer, live = [], False
        store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
    if not active(limit, at):
        if state.get('asked') and offer:
            # An offer the user asked for ends at the next prompt that picks nothing: that prompt is theirs to send,
            # and the very words that asked, sent again, were meant for the session.
            state = dict(state, offer=[])
            store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
            if text == state['asked'] and live:
                return None
        named = asks_switch(store, text)
        if named is None and not note:
            return None
        usable, unusable = choices(store, on, at, environ, runner, which)
        picks = [pick for pick, _, _ in offer_rows(usable)]
        if named is None:                 # the prompt asked for no switch: it is blocked this once, to say the above
            note += '\n(This prompt was not sent. Send it again to go on in this session.)'
        state = dict(state, offer=picks, at=now(), asked=text if named is not None else state.get('asked') or text,
                     route=on, host=host, held=None, blocked_text=text)
        store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
        reason = ask_guidance(store, _label(store, on, host), usable, unusable, at, named or [])
        return {'decision': 'block', 'reason': note + '\n' + reason if note else reason}
    limit = dict(limit, route=on)
    record_limit(store, on, limit)
    asking = asks_switch(store, text) is not None
    if not limit.get('resets_at') and state.get('blocked') == limit['id'] and not note and not asking:
        return None                       # no reset time known: shown once, then the user may simply try again
    usable, unusable = choices(store, on, at, environ, runner, which)
    picks = [pick for pick, _, _ in offer_rows(usable)]
    # What is held is work to carry over: not Orbit's own lines, not a pick, not the words that ask for the switch.
    held = text if text and not _orbit_line(text) and not _pick(text, picks) and not asking else state.get('held')
    state = {'offer': picks, 'at': now(), 'blocked': limit['id'], 'route': on, 'host': host,
             'held': held, 'limit': limit, 'blocked_text': text}
    store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
    reason = guidance(store, _label(store, on, host), limit, usable, unusable, at, bool(held))
    return {'decision': 'block', 'reason': note + '\n' + reason if note else reason}


def switch_outcome(store, session, switch, error=None):
    """The detached typist's report on the switch `switch` (its capsule id) that `session` started."""
    store.preference(OUTCOME_KEY + session, json.dumps(
        {'switch': switch, 'ok': error is None, 'reason': ' '.join(str(error or '').split())[:300], 'at': now()},
        ensure_ascii=False))


def switch_result(store, session, state, at):
    """What became of the switch this session started, asked while the session is still there: (None, None) when
    there is nothing to say (none started, a command shown to run by hand, typed to the end, or told already),
    ('typing', None) while the keys are being typed, ('failed', why) when it did not happen. A typist that left no
    report at all (it died: seen 2026-11-17) counts as failed once the time a switch can take has passed."""
    if not state.get('switched') or not state.get('typed') or state.get('switch_told'):
        return None, None
    outcome = _json(store, OUTCOME_KEY + session)
    if outcome.get('switch') and outcome.get('switch') == state.get('capsule'):
        if outcome.get('ok'):
            return None, None
        return 'failed', 'Orbit could not type into this pane — ' + (outcome.get('reason') or 'no reason given')
    if (_stamp(state.get('switched_at')) or 0) + SWITCH_WAIT > at:
        return 'typing', None
    return 'failed', 'the typing left no result within %d seconds (see hooks.log in %s)' % (SWITCH_WAIT, store.home)


# ---------------------------------------------------------------- the permission mode a switch carries

def _host_argv(pid, runner=None):
    """The command line the host process was started with ([] when it cannot be read)."""
    if not pid:
        return []
    try:
        out = (runner or subprocess.run)(['ps', '-o', 'args=', '-p', str(pid)], capture_output=True, text=True, timeout=5)
        return shlex.split((out.stdout or '').strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _option(argv, *names):
    """The value of the first of `names` in argv (`--name value` or `--name=value`), or None."""
    for index, arg in enumerate(argv):
        name, eq, value = arg.partition('=')
        if name in names:
            return value if eq else (argv[index + 1] if index + 1 < len(argv) else None)
    return None


def _codex_config(environ=None):
    """approval_policy and sandbox_mode at the top of the user's Codex config.toml (before any [table])."""
    environ = os.environ if environ is None else environ
    home = environ.get('CODEX_HOME') or os.path.join(os.path.expanduser('~'), '.codex')
    found = {}
    try:
        with open(os.path.join(home, 'config.toml'), encoding='utf-8') as handle:
            for line in handle:
                if line.lstrip().startswith('['):
                    break
                match = re.match(r'\s*(approval_policy|sandbox_mode)\s*=\s*["\']([\w-]+)["\']', line)
                if match:
                    found[match.group(1)] = match.group(2)
    except OSError:
        pass
    return found


def session_mode(payload, host, pid=None, runner=None, environ=None):
    """The permission mode the session runs in now, to start the session a switch opens in: {'host': 'claude',
    'mode': …} or {'host': 'codex', 'approval': …, 'sandbox': …}; None when nothing says.

    Claude Code: the hook payload's `permission_mode` (what the session is in now, after any shift+tab), else its
    transcript's latest `permissionMode`, else the options it was started with. Codex: the latest `turn_context` of its
    rollout (the policy and the sandbox in effect, whether a flag or config.toml set them), else the options it was
    started with over config.toml. Seen 2026-11-18: this user runs `claude --dangerously-skip-permissions` and Codex
    with approval_policy "never" and sandbox_mode "danger-full-access" in config.toml; `route run` started the bare
    host, so the Claude Code session a switch opened stopped at an approval prompt on its first command."""
    payload = payload or {}
    if host == 'claude':
        mode = payload.get('permission_mode')
        if not mode:
            for line in reversed(_tail(payload.get('transcript_path'))):
                if '"permissionMode"' in line:
                    try:
                        mode = json.loads(line).get('permissionMode')
                    except ValueError:
                        mode = None
                    if mode:
                        break
        if not mode:
            argv = _host_argv(pid or host_pid(host, runner=runner), runner)
            mode = 'bypassPermissions' if '--dangerously-skip-permissions' in argv else _option(argv, '--permission-mode')
        return {'host': 'claude', 'mode': mode} if mode else None
    if host != 'codex':
        return None
    approval = sandbox = None
    for line in reversed(_tail(payload.get('transcript_path'))):
        if '"turn_context"' not in line:
            continue
        try:
            turn = json.loads(line)
        except ValueError:
            continue
        if turn.get('type') == 'turn_context':
            context = turn.get('payload') or {}
            policy = context.get('sandbox_policy')
            approval = context.get('approval_policy')
            sandbox = policy.get('type') if isinstance(policy, dict) else policy
            break
    if not approval and not sandbox:
        argv = _host_argv(pid or host_pid(host, runner=runner), runner)
        config = _codex_config(environ)
        if '--dangerously-bypass-approvals-and-sandbox' in argv or '--yolo' in argv:
            approval, sandbox = 'never', 'danger-full-access'
        else:
            approval = _option(argv, '-a', '--ask-for-approval') or config.get('approval_policy')
            sandbox = _option(argv, '-s', '--sandbox') or config.get('sandbox_mode')
    return {'host': 'codex', 'approval': approval, 'sandbox': sandbox} if approval or sandbox else None


def mode_args(mode, host):
    """The options that start `host` in the permission mode of the session left.

    The same host: the mode as it was. The other host: only the one mode both have — nothing asked and no sandbox
    (Claude Code's bypassPermissions, Codex's approval never with danger-full-access); any other mode has no equal
    there and the host starts in its own default, the cautious side. Every value is checked against the names the
    hosts know: nothing read from a transcript reaches a command line as it is."""
    if not isinstance(mode, dict):
        return []
    free = mode.get('mode') == 'bypassPermissions' or (mode.get('approval') == 'never'
                                                       and mode.get('sandbox') == 'danger-full-access')
    if host == 'claude':
        if free:
            return ['--dangerously-skip-permissions']
        return ['--permission-mode', mode['mode']] if mode.get('host') == 'claude' and mode.get('mode') in CLAUDE_MODES else []
    if host == 'codex':
        if free:
            return ['-a', 'never', '-s', 'danger-full-access']
        if mode.get('host') != 'codex':
            return []
        return ((['-a', mode['approval']] if mode.get('approval') in CODEX_APPROVALS else [])
                + (['-s', mode['sandbox']] if mode.get('sandbox') in CODEX_SANDBOXES else []))
    return []


def mode_words(mode):
    """The mode in the user's words, for the lines Orbit shows."""
    if not isinstance(mode, dict):
        return ''
    if mode.get('mode'):
        return 'bypass permissions (nothing asked)' if mode['mode'] == 'bypassPermissions' else mode['mode']
    return 'approval %s · sandbox %s' % (mode.get('approval') or 'default', mode.get('sandbox') or 'default')


def needs_terminal(store, name, extra=()):
    """Whether `route run NAME -- EXTRA` starts a session someone has to watch (anything but a one-off answer)."""
    route = all_routes(store).get(split_pick(name)[0])
    return not any(arg in ONE_OFF.get((route or {}).get('host'), ()) for arg in extra)


def host_pid(host, start=None, runner=None):
    """The pid of the host process this hook runs under (claude or codex), found by walking up the parents."""
    def ps(field, pid):
        try:
            out = (runner or subprocess.run)(['ps', '-o', field + '=', '-p', str(pid)], capture_output=True, text=True,
                                             timeout=5)
            return (out.stdout or '').strip()
        except (OSError, subprocess.SubprocessError):
            return ''
    pid = start or os.getppid()
    for _ in range(8):
        if pid <= 1:
            return None
        if os.path.basename(ps('comm', pid).split(' ')[0]).startswith(host):
            return pid
        parent = ps('ppid', pid)
        if not parent.isdigit():
            return None
        pid = int(parent)
    return None


def switch_steps(host, presses, pid, run_line):
    """Quit the host in its pane, wait until it is gone, then start the route from the shell it leaves."""
    from .typist import clear_steps
    steps = [('wait', 0.8)]
    if host == 'claude':
        # Claude Code puts the blocked pick back into its input. cmux 0.65.2 then refuses send-panel unless --force
        # is passed (seen live 2026-11-18: "the agent prompt … holds text someone is typing"). Only presses cmux
        # refuses over the pick this hook just blocked are forced: later input keeps its protection for the user.
        steps += clear_steps(presses)
    steps += [('text', '/exit')]
    if host == 'codex':                   # Codex takes an Enter right behind typed text as part of a paste: a newline,
        steps += [('wait', 1.2)]          # so /exit stayed in its input (seen 2026-11-17 in a real pane)
    steps += [('key', 'enter'), ('gone', [pid, 30.0]), ('wait', 1.0), ('text', run_line), ('key', 'enter')]
    return steps


def switch(store, session, host, task, name, state, text, environ=None, at=None, terminal=None, spawn=None, pid=None,
           which=None, runner=None, transcript=None, cwd=None, mode=None):
    """Start route `name` in this session's pane with this session's context (a capsule), carrying the held request
    (or a continuation) to the task when one is attached. `mode`: this session's permission mode (session_mode),
    which the new session starts in."""
    from . import typist
    from .attach import CLOSED
    at = time.time() if at is None else at
    name, model = split_pick(name)
    route = all_routes(store).get(name)
    ok, why = availability(store, route, at, environ, runner, which) if route else (False, 'no such route')
    if not ok:
        return {'decision': 'block', 'reason': 'Orbit: route %s is not usable now (%s). Type another number or name.' % (name, why)}
    terminal = typist.detect(environ) if terminal is None else terminal
    carry = bool(task and task['status'] not in CLOSED)
    if carry:
        with store.lock('task:' + task['id'], timeout=5.0):
            current = store.get(task['id'])
            data = current['data']
            data['carried_prompt'] = {'text': (state.get('held') or (ASKED_TEXT if state.get('asked') else
                                                                      CONTINUE_TEXT))[:4000],
                                      'at': now(), 'from_session': session, 'by': 'route:' + name}
            handoff = dict(data.get('handoff_state') or {})
            for stale in ('claimed_by', 'claimed_at', 'adopted_by', 'adopted_at'):
                handoff.pop(stale, None)
            handoff.update(state='auto', auto_at=now(), session=session, panel=(terminal or {}).get('target'),
                           by='route', route=name)
            data['handoff_state'] = handoff
            store.save(current, current['status'], 'route_switch',
                       {'route': name, 'from': state.get('route') or host, 'session': session, 'layer': 'context',
                        'terminal': (terminal or {}).get('kind')})
    capsule = make_capsule(store, session, host, name, model, state, task if carry else None,
                           transcript or store.preference(_transcript_key() + session), cwd, at, environ=environ,
                           mode=mode)
    left_route = state.get('route') or host
    run_line = '%s route run %s%s --carry --capsule %s%s' % (
        _orbit(store), name, (' --model ' + _shell_word(model)) if model else '', capsule['id'],
        ' --from ' + left_route if left_route != name else '')
    # What the next prompt here needs if this session is still there: the pick again, and the command by hand.
    state = dict(state, offer=[], switched=name, switched_at=now(), capsule=capsule['id'], run_line=run_line,
                 picked_from=list(state.get('offer') or state.get('picked_from') or []), typed=bool(terminal),
                 picked_text=text)
    state.pop('switch_told', None)
    store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
    note = (' · model ' + model if model else '') + _until_note(route, at)
    carried = ('The new session takes the task over with this conversation\'s context%s.' % (
        ' and runs your request; nothing to retype' if state.get('held') else '')) if carry else \
        ('The new session gets this conversation\'s context%s.' % (
            ' and your request; nothing to retype' if state.get('held') else ''))
    if mode_args(mode, route['host']):
        carried += ' It starts in this session\'s permission mode: %s.' % mode_words(mode)
    if not terminal:
        return {'decision': 'block', 'reason': (
            'Orbit: to continue on %s (%s%s), quit this session with /exit and run:\n  %s\n%s' % (
                name, route['label'], note, run_line, carried))}
    steps = switch_steps(host, typist.clear_presses(text), pid or host_pid(host), run_line)
    (spawn or typist.spawn)(terminal, steps, store.home / 'hooks.log', home=store.home,
                            task=task['id'] if carry else None, report={'session': session, 'switch': capsule['id']})
    return {'decision': 'block', 'reason': (
        'Orbit: switching this pane to %s (%s%s): quitting this session, then `%s`. %s' % (
            name, route['label'], note, run_line, carried))}


def _transcript_key():
    from .verifier import TRANSCRIPT_KEY
    return TRANSCRIPT_KEY


def prompt_note(store, payload, host, environ=None, at=None):
    """A line for the model, once: the route this session left is usable again, or the route it runs on ends soon."""
    environ = os.environ if environ is None else environ
    session = payload.get('session_id')
    on = current_route(host, environ)
    home = environ.get(FROM_ENV) or (host if on != host else None)   # the route this session was switched away from
    if not session or not home or home == on or not enabled(store):
        return None
    at = time.time() if at is None else at
    state = _json(store, SESSION_KEY + session)
    notes, changed = [], False
    home_limit = route_limit(store, home)
    if home_limit and not active(home_limit, at) and state.get('back_told') != home_limit.get('id'):
        state.pop('asked', None)          # this offer is the way back, not one the user asked for
        state.update(back_told=home_limit.get('id'), offer=[home], at=now(), route=on, host=host)
        changed = True
        notes.append('route %s is usable again (its %s limit has reset). Typing `%s` as the next prompt switches this '
                     'pane back and carries the task over; otherwise the session stays on %s'
                     % (home, home_limit.get('kind') or 'usage', home, on))
    route = all_routes(store).get(on)
    left = days_left(route, at) if route else None
    today = datetime.datetime.fromtimestamp(at).date().isoformat()
    if left is not None and left <= 3 and state.get('end_told') != today:
        state['end_told'] = today
        changed = True
        notes.append('route %s ends %s (%s)' % (on, route['until'], 'today' if left == 0 else '%d days left' % left))
    if changed:
        store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
    if not notes:
        return None
    return '[orbit] This session runs on route %s. Tell the user once, in one line: %s.' % (on, '; '.join(notes))


def on_stop_failure(store, payload, host, task, environ=None, deliver=None):
    """Claude Code's StopFailure: a turn ended on an API error. A limit is recorded and announced in the task's
    panes (the host shows nothing a hook returns here); the user's next prompt there gets the offer."""
    if not enabled(store):
        return None
    on = current_route(host, environ)
    limit = read_limit(payload.get('transcript_path'), host, on)
    if not limit and str(payload.get('error') or payload.get('error_type') or '') in LIMIT_ERRORS:
        text = str(payload.get('error_message') or payload.get('message') or payload.get('last_assistant_message') or '')
        seen = time.time()
        limit = {'id': 'stop-failure:%s:%s' % (payload.get('session_id'), int(seen)), 'kind': payload.get('error'),
                 'text': text[:200] or str(payload.get('error')), 'resets_at': reset_from_text(text, seen), 'seen_at': seen}
    words = str(payload.get('last_assistant_message') or payload.get('error_message') or '')
    if not limit and paid_refusal(words):
        limit = refusal('stop-failure:%s:%s' % (payload.get('session_id'), int(time.time())), words, time.time())
    if not limit:
        return None
    on = current_route(host, environ)
    record_limit(store, on, limit)
    usable, _ = choices(store, on, environ=environ)
    body = '%s %s. %s' % (
        on, 'was refused: its service asks for payment' if limit.get('paid') else 'stopped at its %s limit%s' % (
            limit.get('kind') or 'usage',
            (' · resets ' + _clock(limit['resets_at'], time.time())) if limit.get('resets_at') else ''),
        ('Type anything in that pane to see the routes to switch to: ' + ', '.join(r['name'] for r in usable)) if usable
        else 'No other route is usable now.')
    if task:
        try:
            from .attention import deliver as default_deliver
            (deliver or default_deliver)(store, task, 'Orbit: ' + body)
        except Exception:  # noqa: BLE001 - an announcement never breaks the hook
            pass
    return None


# ---------------------------------------------------------------- the command line

def describe(store, at=None, environ=None, runner=None, which=None):
    at = time.time() if at is None else at
    rows = []
    routes = all_routes(store)
    for name in sorted(routes, key=lambda n: (n not in BUILTIN, n)):
        route = routes[name]
        ok, why = availability(store, route, at, environ, runner, which)
        rows.append({'name': name, 'host': route['host'], 'api': route.get('api') or ('anthropic' if name not in BUILTIN else None),
                     'auth': route.get('auth') or ('bearer' if name not in BUILTIN else None), 'label': route['label'],
                     'model': route.get('model'), 'models': models(route), 'base_url': route.get('base_url'),
                     'key': route.get('key'), 'until': route.get('until'), 'days_left': days_left(route, at),
                     'enabled': route_on(route), 'usable': ok, 'why': why})
    return rows


def list_text(store, at=None, environ=None):
    at = time.time() if at is None else at
    orbit = _orbit(store)
    if enabled(store):
        head = 'ROUTES: on — a session that hits its limit offers the usable ones (all off: %s route off)' % orbit
    else:
        head = 'ROUTES: off — a session that hits its limit is left alone (on again: %s route on)' % orbit
    lines = [head]
    routes = all_routes(store)
    for row in describe(store, at, environ):
        state = 'ok' if row['usable'] else ('off' if not row['enabled'] else '-')
        lines.append('  %-10s %-4s %s%s%s%s' % (
            row['name'], state, row['label'],
            (' · models ' + ', '.join(row['models'])) if len(row['models']) > 1 else '',
            _until_note(routes[row['name']], at), '' if row['usable'] else ' · ' + row['why']))
    lines.append('MODE: %s' % ('carried — a switch starts the new session in the permission mode of the one left '
                               '(host default instead: %s route mode off)' % orbit if mode_carried(store) else
                               'host default — a switch starts the new session in its host\'s own permission mode '
                               '(carried instead: %s route mode on)' % orbit))
    lines.append('`route run NAME [--model MODEL]` starts one here · `route on|off NAME` · `route key NAME` stores '
                 'your key · `route add NAME --base-url URL --model M[,M…] [--api anthropic|openai]`')
    return '\n'.join(lines)


def _toml(value):
    return json.dumps(str(value), ensure_ascii=False)   # a JSON string is a TOML basic string


def run(store, name, carry=False, extra=(), environ=None, execvpe=os.execvpe, force=False, at=None,
        which=None, out=sys.stderr, came_from=None, model=None, capsule=None):
    """Start route `name` in this terminal: the host program with the route's endpoint, key and model (the
    route's default, or `model`: any model its service serves, listed or not). `capsule`: the context a switch
    carried, handed to the new session at its start."""
    from .typist import CARRY_LINE
    environ = dict(os.environ if environ is None else environ)
    at = time.time() if at is None else at
    routes = all_routes(store)
    name, picked = split_pick(name)
    model = (model or picked or '').strip() or None
    route = routes.get(name)
    if not route:
        raise ValueError('no route named %r (cosmos-orbit route list)' % name)
    if not route_on(route):
        raise ValueError('route %s is off (cosmos-orbit route on %s)' % (name, name))
    if not (which or shutil.which)(route['host']):
        raise ValueError('%s is not installed' % route['host'])
    left = days_left(route, at)
    if left is not None and left < 0 and not force:
        raise ValueError('route %s ended %s (--force starts it anyway)' % (name, route['until']))
    inherited = routes.get(environ.get(ROUTE_ENV) or '')
    if inherited and inherited['name'] not in BUILTIN:
        for var in ENDPOINT_VARS:          # started from inside another route's session: none of its endpoint leaks
            environ.pop(var, None)
    environ[ROUTE_ENV] = name
    environ.pop(FROM_ENV, None)
    environ.pop(CAPSULE_ENV, None)        # a capsule belongs to the one session a switch started, never to a later one
    if came_from and came_from != name:
        environ[FROM_ENV] = came_from
    mode = None
    if capsule:
        if not re.match(r'^[0-9a-f]{12}$', capsule) or not _json(store, CAPSULE_KEY + capsule):
            print('Orbit: no carried context %r here: starting without it' % capsule, file=out, flush=True)
        else:
            environ[CAPSULE_ENV] = capsule
            mode = _json(store, CAPSULE_KEY + capsule).get('mode')
    # The permission mode of the session left, unless it is turned off or the user gave their own after `--`.
    own = any(arg.partition('=')[0] in PERMISSION_OPTIONS for arg in extra)
    carried_mode = mode_args(mode, route['host']) if mode_carried(store) and not own else []
    argv = [route['host']] + carried_mode
    if name in BUILTIN:
        if model:                         # the host's own account, on a model the user picked
            argv += ['--model', model] if route['host'] == 'claude' else ['-c', 'model=' + _toml(model)]
    else:
        key = read_key(route, environ)
        if not key:
            raise ValueError('route %s has no key: cosmos-orbit route key %s (or set %s)' % (name, name, route['key']))
        model = model or route['model']
        if (route.get('api') or 'anthropic') == 'openai':
            provider = PROVIDER + name    # Codex: a model provider defined for this run only, its key in the env
            environ[KEY_ENV] = key
            for field, value in (('name', name), ('base_url', route['base_url']), ('env_key', KEY_ENV),
                                 ('wire_api', 'responses')):
                argv += ['-c', 'model_providers.%s.%s=%s' % (provider, field, _toml(value))]
            argv += ['-c', 'model_provider=' + _toml(provider), '-c', 'model=' + _toml(model)]
        else:
            environ['ANTHROPIC_BASE_URL'] = route['base_url']
            if route.get('auth') == 'x-api-key':
                # Not ANTHROPIC_API_KEY: Claude Code drops a key it has not been asked to approve and falls back to
                # the user's claude.ai login, whose token would then go to this endpoint (seen 2026-11-17). An
                # apiKeyHelper is used as given and sent as both x-api-key and bearer; it prints the key from the
                # environment, so the key stays out of argv.
                environ[KEY_ENV] = key
                environ.pop('ANTHROPIC_AUTH_TOKEN', None)
                environ.pop('ANTHROPIC_API_KEY', None)
                argv += ['--settings', json.dumps({'apiKeyHelper': 'printenv ' + KEY_ENV})]
            else:
                environ.update(ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_API_KEY='')
            for var in MODEL_VARS:
                environ[var] = model
            argv += ['--model', model]
    argv += list(extra)
    if carry:
        argv.append(CARRY_LINE)
    limit = route_limit(store, name)
    print('Orbit: starting %s on route %s (%s%s%s)%s%s' % (
        route['host'], name, route['label'], (' · model ' + model) if model else '', _until_note(route, at),
        (' · in the permission mode of the session left: ' + mode_words(mode)) if carried_mode else '',
        (' — note: ' + _limited_text(limit, at)) if active(limit, at) else ''), file=out, flush=True)
    if hasattr(store, 'close'):
        store.close()                     # the host replaces this process: nothing of the ledger stays open in it
    execvpe(argv[0], argv, environ)
    return {'argv': argv}
