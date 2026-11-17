"""Routes: where a Claude Code or Codex session can run, and the switch when the one in use hits its limit.

A session that hits its usage limit cannot answer until the reset. Claude Code writes a synthetic assistant line
(`error: rate_limit`, `apiErrorStatus: 429`, "You've hit your 5-hour limit · resets 2:50pm (Asia/Seoul)", with
`quotaLimits.resetsAt`); Codex reports `rate_limits.rate_limit_reached_type` in its token counts. The user's next
prompt there is blocked — it would only fail again — and shows the routes usable now, numbered, with their
commands. Typing a route's number or name as the next prompt starts that route in the same pane and carries the
blocked request over. Orbit never switches by itself: the user picks.

A route is a host program (claude or codex). The two built in run on the account the shell already has; a route the
user adds runs Claude Code against an Anthropic-compatible endpoint (Ollama's cloud, say) with its base URL, model
and key. The key is read from the environment or the macOS keychain only when the route starts: never written to a
file, typed into a pane or printed. A route may end (`until`, a free period, say): after that day it is not offered.
"""
import datetime
import json
import os
import re

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
ENDPOINT_VARS = ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY') + MODEL_VARS
CONTINUE_TEXT = ('Continue the unfinished task on this route. The previous session stopped at its usage limit and '
                 'the user chose this route; they asked nothing new.')
MONTHS = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')


# ---------------------------------------------------------------- the routes

def _path(store):
    return Path(store.home) / 'routes.json'


def load(store):
    """The routes the user added: {name: route}."""
    try:
        with _path(store).open(encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    routes = data.get('routes') if isinstance(data, dict) else None
    return {name: dict(route, name=name) for name, route in (routes or {}).items() if isinstance(route, dict)}


def _write(store, routes):
    path = _path(store)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump({'routes': routes}, handle, indent=2, ensure_ascii=False)
        handle.write('\n')
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def all_routes(store):
    routes = {name: dict(route) for name, route in BUILTIN.items()}
    routes.update(load(store))
    return routes


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


def add(store, name, base_url, model, key, label=None, until=None, host='claude'):
    if not NAME.match(name or '') or name in BUILTIN:
        raise ValueError('a route name is lowercase letters, digits and dashes (not claude or codex): %r' % name)
    if host != 'claude':
        raise ValueError('an added route runs Claude Code against an Anthropic-compatible endpoint (host claude)')
    if not re.match(r'^https?://[^\s]+$', base_url or ''):
        raise ValueError('--base-url must be an http(s) URL: %r' % base_url)
    if not (model or '').strip():
        raise ValueError('--model is required')
    key_sources(key)
    if until:
        _day(until)
    routes = load(store)
    routes.pop(name, None)
    routes[name] = {'host': host, 'base_url': base_url.rstrip('/'), 'model': model.strip(), 'key': key,
                    'label': (label or '%s at %s' % (model.strip(), base_url)).strip(), 'until': until or None,
                    'added': now()}
    _write(store, {k: {f: v for f, v in r.items() if f != 'name'} for k, r in routes.items()})
    return dict(routes[name], name=name)


def remove(store, name):
    routes = load(store)
    if name not in routes:
        raise ValueError('no added route named %r (built-in routes cannot be removed)' % name)
    routes.pop(name)
    _write(store, {k: {f: v for f, v in r.items() if f != 'name'} for k, r in routes.items()})
    return {'removed': name}


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


def claude_limit(path):
    """The limit a Claude Code session stopped on: its last answer is the host's synthetic limit line. Else None."""
    for line in reversed(_tail(path)):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('type') != 'assistant' or record.get('isSidechain'):
            continue
        if not record.get('isApiErrorMessage') or record.get('error') not in LIMIT_ERRORS:
            return None                                   # the last answer was a real one: no limit holds
        text = _content_text(record.get('message'))
        quota = record.get('quotaLimits') if isinstance(record.get('quotaLimits'), dict) else {}
        seen = _stamp(record.get('timestamp')) or time.time()
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
        if payload.get('type') == 'error' and re.search(r'usage limit|rate limit|hit your', str(payload.get('message')), re.I):
            text = str(payload['message'])
            return {'id': 'codex-error:%s' % record.get('timestamp'), 'kind': 'usage', 'text': text[:200],
                    'resets_at': reset_from_text(text, seen), 'seen_at': seen}
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


def read_limit(path, host):
    if not path:
        return None
    return codex_limit(path) if host == 'codex' else claude_limit(path)


def active(limit, at=None):
    if not limit:
        return False
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
    limit = read_limit(store.preference(TRANSCRIPT_KEY + session) if session else None, host or 'claude')
    return limit if active(limit, at) else None


def record_limit(store, route, limit):
    if limit:
        store.preference(LIMIT_KEY + route, json.dumps(dict(limit, route=route), ensure_ascii=False))


def route_limit(store, route):
    return _json(store, LIMIT_KEY + route) or None


def _clock(stamp, at):
    when = datetime.datetime.fromtimestamp(stamp)
    text = when.strftime('%H:%M') if when.date() == datetime.datetime.fromtimestamp(at).date() else when.strftime('%m-%d %H:%M')
    minutes = max(0, int((stamp - at) // 60))
    return '%s (in %s)' % (text, '%dh %02dm' % divmod(minutes, 60) if minutes >= 60 else '%dm' % minutes)


def _limited_text(limit, at):
    if limit.get('resets_at'):
        return 'limited until ' + _clock(limit['resets_at'], at)
    return 'limited (%s) since %s' % (limit.get('kind') or 'limit', datetime.datetime.fromtimestamp(limit.get('seen_at') or at).strftime('%H:%M'))


# ---------------------------------------------------------------- availability and the offer

def availability(store, route, at=None, environ=None, runner=None, which=None):
    """(usable, why not) for one route right now."""
    at = time.time() if at is None else at
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


def guidance(store, label, limit, usable, unusable, at, held):
    orbit = _orbit(store)
    when = (' · resets ' + _clock(limit['resets_at'], at)) if limit.get('resets_at') else ''
    lines = ['Orbit: %s stopped at its %s limit%s.' % (label, limit.get('kind') or 'usage', when)]
    if usable:
        lines.append('Switch in this pane — type a number or a name as your next prompt%s:' % (
            '; the request you just typed is carried over' if held else ''))
        for number, route in enumerate(usable, 1):
            lines.append('  %d  %-10s %s%s' % (number, route['name'], route['label'], _until_note(route, at)))
        lines.append('  (or quit and run: %s route run NAME --carry)' % orbit)
    else:
        lines.append('No other route is usable right now.')
    for route, why in unusable:
        lines.append('  -  %-10s %s' % (route['name'], why))
    lines.append('Or wait%s and type again. Add a route: %s route add NAME --base-url URL --model MODEL '
                 '--key env:VAR|keychain:SERVICE [--until YYYY-MM-DD]' % (
                     ' until ' + _clock(limit['resets_at'], at).split(' (')[0] if limit.get('resets_at') else '', orbit))
    return '\n'.join(lines)


def _orbit(store):
    from .attach import orbit_command
    return orbit_command(store)


def _pick(text, offer):
    word = (text or '').strip().strip('`"\'').strip().lower()
    if word.isdigit() and 1 <= int(word) <= len(offer):
        return offer[int(word) - 1]
    return word if word in offer else None


def _orbit_line(text):
    """A line Orbit typed itself (carry, resume, wake): never held as the user's request."""
    from .supervision import RESUME_LINE
    from .typist import CARRY_LINE
    from .wake import WAKE_LINE
    return text in (CARRY_LINE, RESUME_LINE, WAKE_LINE)


def _label(store, route_name, host):
    route = all_routes(store).get(route_name)
    return route['label'] if route else route_name or host


def on_prompt(store, payload, host, task, environ=None, at=None, terminal=None, spawn=None, pid=None,
              which=None, runner=None):
    """The prompt hook's part: a block reply (the offer, or the switch it started), or None to let the prompt on."""
    session = payload.get('session_id')
    if not session:
        return None
    at = time.time() if at is None else at
    text = (payload.get('prompt') or '').strip()
    state = _json(store, SESSION_KEY + session)
    offer = state.get('offer') or []
    if offer and (_stamp(state.get('at')) or 0) + OFFER_TTL > at:
        chosen = _pick(text, offer)
        if chosen:
            return switch(store, session, host, task, chosen, state, text, environ=environ, at=at, terminal=terminal,
                          spawn=spawn, pid=pid, which=which, runner=runner)
    limit = read_limit(payload.get('transcript_path'), host)
    if not active(limit, at):
        return None
    on = current_route(host, environ)
    record_limit(store, on, limit)
    if not limit.get('resets_at') and state.get('blocked') == limit['id']:
        return None                       # no reset time known: shown once, then the user may simply try again
    usable, unusable = choices(store, on, at, environ, runner, which)
    held = text if text and not _orbit_line(text) and not _pick(text, [r['name'] for r in usable]) else state.get('held')
    state = {'offer': [r['name'] for r in usable], 'at': now(), 'blocked': limit['id'], 'route': on, 'host': host,
             'held': held, 'limit': limit}
    store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
    return {'decision': 'block', 'reason': guidance(store, _label(store, on, host), limit, usable, unusable, at, bool(held))}


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
    steps = [('wait', 0.8)]
    if host == 'claude':                  # Claude Code puts a blocked prompt back into its input: empty it first
        steps += [('key', 'ctrl+u')] * presses
    steps += [('text', '/exit'), ('key', 'enter'), ('gone', [pid, 30.0]), ('wait', 1.0), ('text', run_line),
              ('key', 'enter')]
    return steps


def switch(store, session, host, task, name, state, text, environ=None, at=None, terminal=None, spawn=None, pid=None,
           which=None, runner=None):
    """Start route `name` in this session's pane, carrying the held request (or a continuation) to the task."""
    from . import typist
    from .attach import CLOSED
    at = time.time() if at is None else at
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
            data['carried_prompt'] = {'text': (state.get('held') or CONTINUE_TEXT)[:4000], 'at': now(),
                                      'from_session': session, 'by': 'route:' + name}
            handoff = dict(data.get('handoff_state') or {})
            for stale in ('claimed_by', 'claimed_at', 'adopted_by', 'adopted_at'):
                handoff.pop(stale, None)
            handoff.update(state='auto', auto_at=now(), session=session, panel=(terminal or {}).get('target'),
                           by='route', route=name)
            data['handoff_state'] = handoff
            store.save(current, current['status'], 'route_switch',
                       {'route': name, 'from': state.get('route') or host, 'session': session, 'layer': 'context',
                        'terminal': (terminal or {}).get('kind')})
    state = dict(state, offer=[], switched=name, switched_at=now())
    store.preference(SESSION_KEY + session, json.dumps(state, ensure_ascii=False))
    left_route = state.get('route') or host
    run_line = '%s route run %s%s%s' % (_orbit(store), name, ' --carry' if carry else '',
                                        ' --from ' + left_route if left_route != name else '')
    note = _until_note(route, at)
    if not terminal:
        return {'decision': 'block', 'reason': (
            'Orbit: to continue on %s (%s%s), quit this session with /exit and run:\n  %s%s' % (
                name, route['label'], note, run_line,
                '\nThe new session takes the task over and finds your request in its brief.' if carry else
                '\nThis session has no task, so retype your request there.'))}
    steps = switch_steps(host, typist.clear_presses(text), pid or host_pid(host), run_line)
    (spawn or typist.spawn)(terminal, steps, store.home / 'hooks.log', home=store.home, task=task['id'] if carry else None)
    return {'decision': 'block', 'reason': (
        'Orbit: switching this pane to %s (%s%s): quitting this session, then `%s`.%s' % (
            name, route['label'], note, run_line,
            ' The new session takes the task over and runs your request; nothing to retype.' if carry else
            ' This session has no task: retype your request there.'))}


def prompt_note(store, payload, host, environ=None, at=None):
    """A line for the model, once: the route this session left is usable again, or the route it runs on ends soon."""
    environ = os.environ if environ is None else environ
    session = payload.get('session_id')
    on = current_route(host, environ)
    home = environ.get(FROM_ENV) or (host if on != host else None)   # the route this session was switched away from
    if not session or not home or home == on:
        return None
    at = time.time() if at is None else at
    state = _json(store, SESSION_KEY + session)
    notes, changed = [], False
    home_limit = route_limit(store, home)
    if home_limit and not active(home_limit, at) and state.get('back_told') != home_limit.get('id'):
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
    limit = read_limit(payload.get('transcript_path'), host)
    if not limit and str(payload.get('error') or payload.get('error_type') or '') in LIMIT_ERRORS:
        text = str(payload.get('error_message') or payload.get('message') or payload.get('last_assistant_message') or '')
        seen = time.time()
        limit = {'id': 'stop-failure:%s:%s' % (payload.get('session_id'), int(seen)), 'kind': payload.get('error'),
                 'text': text[:200] or str(payload.get('error')), 'resets_at': reset_from_text(text, seen), 'seen_at': seen}
    if not limit:
        return None
    on = current_route(host, environ)
    record_limit(store, on, limit)
    usable, _ = choices(store, on, environ=environ)
    body = '%s stopped at its %s limit%s. %s' % (
        on, limit.get('kind') or 'usage', (' · resets ' + _clock(limit['resets_at'], time.time())) if limit.get('resets_at') else '',
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
        rows.append({'name': name, 'host': route['host'], 'label': route['label'], 'model': route.get('model'),
                     'base_url': route.get('base_url'), 'key': route.get('key'), 'until': route.get('until'),
                     'days_left': days_left(route, at), 'usable': ok, 'why': why})
    return rows


def list_text(store, at=None, environ=None):
    at = time.time() if at is None else at
    lines = ['ROUTES (a session that hits its limit offers the usable ones; `route run NAME` starts one here)']
    routes = all_routes(store)
    for row in describe(store, at, environ):
        lines.append('  %-10s %-6s %s%s%s' % (row['name'], 'ok' if row['usable'] else '-', row['label'],
                                            _until_note(routes[row['name']], at),
                                            '' if row['usable'] else ' · ' + row['why']))
    return '\n'.join(lines)


def run(store, name, carry=False, extra=(), environ=None, execvpe=os.execvpe, force=False, at=None,
        which=None, out=sys.stderr, came_from=None):
    """Start route `name` in this terminal: the host program with the route's endpoint, key and model."""
    from .typist import CARRY_LINE
    environ = dict(os.environ if environ is None else environ)
    at = time.time() if at is None else at
    routes = all_routes(store)
    route = routes.get(name)
    if not route:
        raise ValueError('no route named %r (cosmos-orbit route list)' % name)
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
    if came_from and came_from != name:
        environ[FROM_ENV] = came_from
    argv = [route['host']]
    if name not in BUILTIN:
        key = read_key(route, environ)
        if not key:
            raise ValueError('route %s has no key: set %s' % (name, route['key']))
        environ.update(ANTHROPIC_BASE_URL=route['base_url'], ANTHROPIC_AUTH_TOKEN=key, ANTHROPIC_API_KEY='')
        for var in MODEL_VARS:
            environ[var] = route['model']
        argv += ['--model', route['model']]
    argv += list(extra)
    if carry:
        argv.append(CARRY_LINE)
    limit = route_limit(store, name)
    print('Orbit: starting %s on route %s (%s%s)%s' % (
        route['host'], name, route['label'], _until_note(route, at),
        (' — note: ' + _limited_text(limit, at)) if active(limit, at) else ''), file=out, flush=True)
    if hasattr(store, 'close'):
        store.close()                     # the host replaces this process: nothing of the ledger stays open in it
    execvpe(argv[0], argv, environ)
    return {'argv': argv}
