"""Offload: a verification suite runs outside the conversation, and a tool-call hook says so when it matters.

Measured 2026-11-18 on one Claude Code session: 55 of its 133 requests (41%) were verification runs or waits for
them, each re-reading a conversation of ~400k tokens, and they made 45% of all the tokens it re-read. A suite's
verdict needs no model: Orbit runs the command and gives back a bounded summary (`offload run`) while the full log
stays on disk. When the results need interpreting, a subagent on a cheaper model reads them in a small fresh context
and returns only its summary. The hook (PreToolUse, matcher Bash) steers a large conversation to those paths. It
reads the conversation's size from the transcript at the moment of the call, not from the ledger: Orbit measures at
prompt and stop events only, and the ledger still said 185k while one 65-minute turn had grown the conversation to
511k.

Settings, winner first: this session (kept in the store, bounded) -> ORBIT_OFFLOAD for one terminal (mode only) ->
`<home>/settings.json` section "offload" (hand-editable, written by `cosmos-orbit offload ... --global`) -> built in
(off, 150k, Claude Code: haiku). Nothing here calls a model, and offload changes no handoff state; the mid-turn notice
that shares the hook (midturn.py) records the measurement it acts on.
"""
import collections
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

MODES = ('off', 'advise', 'block')
ALIASES = {'on': 'advise'}
BUILT_IN = {'mode': 'off', 'at': 150_000, 'models': {'claude': 'haiku'}}  # no built-in model for Codex
ENV = 'ORBIT_OFFLOAD'
SECTION = 'offload'
SESSION_KEEP = 100            # session overrides kept (newest first); they are ephemeral
SESSION_DAYS = 30             # and none older than this
LOG_DIR = 'offload'
LOG_KEEP = 200
SUMMARY_LINES = 40
SUMMARY_CHARS = 3000
FAILING_LINES = 15
LINE_CHARS = 200
MESSAGE_CHARS = 600
HOSTS = ('claude', 'codex')


# ---------------------------------------------------------------- what looks like a verification suite

# A command word: at the start, after a separator, or after a wrapper (env assignments, npx, uv run, a loop's do).
_CMD = (r'(?:^\s*|[;&|(`]\s*|\$\(\s*|\b(?:do|then|else|time|exec|env|nice|sudo|xargs|timeout\s+\S+)\s+(?:-\S+\s+)*'
        r'|\b(?:npx|bunx|pnpx)\s+(?:-\S+\s+)*|\bpnpm\s+(?:exec\s+|dlx\s+)?|\byarn\s+(?:run\s+)?'
        r'|\b(?:uv|poetry|pipenv|pdm|hatch)\s+run\s+|\b[A-Za-z_][A-Za-z0-9_]*=\S*\s+)(?:\S*/)?')
_PY = r'python[\d.]*(?:\s+-[A-Za-z]\S*)*\s+'
_LOOP_BODY = r';\s*do\s+(?:[A-Za-z_]\w*=\S*\s+)*(?:\S*/)?(?:python[\d.]*|node|bash|sh|zsh|php|ruby|perl|deno|bun|timeout|"?\$)'
SUITES = (
    ('pytest', _CMD + r'(?:py\.test|pytest)(?:\s|$)'),
    ('python -m pytest|unittest', _CMD + _PY + r'-m\s*(?:pytest|unittest)\b'),
    ('tests/verify_*.py', _CMD + r'(?:' + _PY + r')?(?:\S*/)?tests?/verify_[\w*?\[\]-]*\.py\b'),
    ('loop over test files', r'\bfor\s+\w+\s+in\s+[^;]*?(?:\btests?/|\btest_|_test\.|\.test\.|\.spec\.|\bspec/)[^;]*'
                             + _LOOP_BODY),
    ('npm|pnpm|yarn|bun test', _CMD + r'(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?test(?::\S+)?(?:\s|$)'),
    ('vitest', _CMD + r'vitest(?:\s|$)'),
    ('jest', _CMD + r'jest(?:\s|$)'),
    ('go test', _CMD + r'go\s+test(?:\s|$)'),
    ('cargo test', _CMD + r'cargo\s+(?:\+\S+\s+)?(?:test|nextest)(?:\s|$)'),
    ('phpunit|pest', _CMD + r'(?:phpunit|pest|paratest)(?:\s|$)'),
    ('php artisan test', _CMD + r'php\s+(?:\S*/)?artisan\s+test(?:\s|$)'),
    ('make test|check', _CMD + r'g?make\s+(?:-\S+\s+)*(?:test|tests|check)(?:\s|$)'),
    ('tox|nox', _CMD + r'(?:tox|nox)(?:\s|$)'),
)
SUITE_RES = tuple((name, re.compile(pattern)) for name, pattern in SUITES)
# Already through Orbit: it runs the command itself and returns a bounded result.
ROUTED = re.compile(r'(?:\bcosmos-orbit|(?:^|[\s;&|(/])harness|\bharness\.cli)(?:\s+--home\s+(?:\'[^\']*\'|"[^"]*"|\S+))?'
                    r'\s+(?:offload\s+run|evidence|claim)\b')
BYPASS = re.compile(r'(?:^|[\s;&|(])' + ENV + r'=(?:off|0|no|false)\b')


def suite_name(command, patterns=()):
    """The built-in pattern (or 'user pattern') a shell command matches, or None."""
    for name, pattern in SUITE_RES:
        if pattern.search(command):
            return name
    for pattern in patterns:
        try:
            if re.search(pattern, command):
                return 'user pattern'
        except (re.error, TypeError):
            continue
    return None


# ---------------------------------------------------------------- settings

def mode_value(value):
    """'off' | 'advise' | 'block' (on = advise), or None when it is not one."""
    if not isinstance(value, str):
        return None
    value = ALIASES.get(value.strip().lower(), value.strip().lower())
    return value if value in MODES else None


def at_value(value):
    if value is None or isinstance(value, (bool, list, dict)):
        return None
    from .continuity import parse_tokens
    try:
        tokens = parse_tokens(value)
    except ValueError:
        return None
    return tokens or None


def model_value(value):
    """A model name, '' for an explicit none (no subagent advice), or None when unreadable."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.lower() in ('none', 'off', '-'):
        return ''
    if not value or len(value) > 120 or any(c.isspace() for c in value):
        return None
    return value


def _section(data):
    section = (data or {}).get(SECTION)
    return section if isinstance(section, dict) else {}


def _file_settings(home):
    """The settings file read directly (the hook reads user patterns before opening the store)."""
    try:
        data = json.loads((Path(home) / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def user_patterns(section):
    patterns = section.get('patterns')
    return [p for p in patterns if isinstance(p, str)] if isinstance(patterns, list) else []


SESSION_TABLES = ('offload_sessions', 'handoff_sessions')  # handoff_sessions: continuity's mid-turn notice and ceiling


def session_override(store, session, table='offload_sessions'):
    if not session or table not in SESSION_TABLES:
        return {}
    row = store.db.execute('SELECT data FROM %s WHERE session=?' % table, (session,)).fetchone()
    try:
        data = json.loads(row[0]) if row else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_session(store, session, data, table='offload_sessions'):
    """Session overrides are ephemeral: the newest SESSION_KEEP within SESSION_DAYS are kept, nothing more."""
    from datetime import datetime, timedelta, timezone
    from .store import now
    if table not in SESSION_TABLES:
        raise ValueError('Unknown session table: ' + str(table))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SESSION_DAYS)).isoformat()
    with store.db:
        if data:
            store.db.execute('INSERT OR REPLACE INTO %s VALUES(?,?,?)' % table,
                             (session, json.dumps(data, sort_keys=True), now()))
        else:
            store.db.execute('DELETE FROM %s WHERE session=?' % table, (session,))
        store.db.execute('DELETE FROM %s WHERE at<? OR session NOT IN '
                         '(SELECT session FROM %s ORDER BY at DESC LIMIT ?)' % (table, table), (cutoff, SESSION_KEEP))


def effective(store, session=None, host=None, environ=None, data=None):
    """Mode, point and model that apply here, each with where it came from."""
    env = os.environ if environ is None else environ
    if data is None:
        from .continuity import read_settings
        data = read_settings(store)[0]
    section = _section(data)
    override = session_override(store, session)
    host = host if host in HOSTS else 'claude'
    result = {'host': host}
    candidates = [(mode_value(override.get('mode')), 'session'), (mode_value(env.get(ENV) or ''), 'environment'),
                  (mode_value(section.get('mode')), 'global'), (BUILT_IN['mode'], 'built-in')]
    result['mode'], result['mode_source'] = next((v, s) for v, s in candidates if v is not None)
    candidates = [(at_value(override.get('at')), 'session'), (at_value(section.get('at')), 'global'),
                  (BUILT_IN['at'], 'built-in')]
    result['at'], result['at_source'] = next((v, s) for v, s in candidates if v is not None)
    models = [(override.get('models') or {}, 'session'), (section.get('models') or {}, 'global'),
              (BUILT_IN['models'], 'built-in')]
    result['model'], result['model_source'] = None, 'none'
    for table, source in models:
        value = model_value(table.get(host)) if isinstance(table, dict) else None
        if value is not None:
            result['model'], result['model_source'] = (value or None), source
            break
    return result


def here_session(store=None, explicit=None, environ=None):
    """(session, host, how): --session ID > CLAUDE_CODE_SESSION_ID > CODEX_THREAD_ID > this pane's last session."""
    env = os.environ if environ is None else environ
    if explicit:
        host = None
        if store is not None:
            row = store.db.execute('SELECT host FROM terminals WHERE session=?', (explicit,)).fetchone()
            host = row[0] if row and row[0] in HOSTS else None
        return explicit, host, 'argument'
    if env.get('CLAUDE_CODE_SESSION_ID'):
        return env['CLAUDE_CODE_SESSION_ID'], 'claude', 'CLAUDE_CODE_SESSION_ID'
    if env.get('CODEX_THREAD_ID'):
        return env['CODEX_THREAD_ID'], 'codex', 'CODEX_THREAD_ID'
    pane = env.get('CMUX_PANEL_ID') or (env.get('TMUX_PANE') if env.get('TMUX') else None)
    if pane and store is not None:
        row = store.db.execute('SELECT session, host FROM terminals WHERE lower(target)=lower(?) AND kind IS NOT NULL '
                               'ORDER BY at DESC LIMIT 1', (pane,)).fetchone()
        if row:
            return row[0], (row[1] if row[1] in HOSTS else None), 'pane'
    return None, None, None


def configure(store, scope, session=None, host=None, mode=None, at=None, model=None, reset=False):
    """Write a setting at one scope ('session' or 'global'); returns what changed."""
    changed = {'scope': scope}
    if mode is not None:
        value = mode_value(mode)
        if value is None:
            raise ValueError('The offload mode is on, off, advise or block')
        changed['mode'] = value
    if at is not None:
        from .continuity import parse_tokens
        if not re.match(r'\s*\d', str(at)):
            raise ValueError('Give a token count such as 150k (on/off is the mode: cosmos-orbit offload on|off)')
        try:
            tokens = parse_tokens(at)
        except ValueError:
            raise ValueError('Give a token count such as 150k; an offload point below 20k would speak on almost every call')
        if not tokens:
            raise ValueError('Give a token count such as 150k; to stop the hook use `cosmos-orbit offload off`')
        changed['at'] = tokens
    if model is not None:
        value = model_value(model)
        if value is None:
            raise ValueError('Give a model name without spaces, or none')
        host = host or 'claude'
        changed['model'] = {host: value or None}

    def apply(section):
        if reset:
            for key in ('mode', 'at', 'models'):
                section.pop(key, None)
        for key in ('mode', 'at'):
            if key in changed:
                section[key] = changed[key]
        if 'model' in changed:
            models = section.get('models')
            if not isinstance(models, dict):
                models = section['models'] = {}
            models[host] = list(changed['model'].values())[0] or 'none'

    if scope == 'global':
        from .continuity import settings_path, update_settings
        update_settings(store, apply, SECTION)
        changed['settings_file'] = str(settings_path(store))
    else:
        if not session:
            raise ValueError('No session here; add --global, or --session ID')
        data = dict(session_override(store, session))
        apply(data)
        _write_session(store, session, data)
        changed['session'] = session
    if reset:
        changed['reset'] = True
    return changed


def view(store, session=None, host=None, how=None, environ=None, changed=None):
    """`cosmos-orbit offload` with no change: shaped like `cosmos-orbit context`."""
    from .continuity import read_settings, settings_path
    env = os.environ if environ is None else environ
    data, problem = read_settings(store)
    section = _section(data)
    problems = [problem] if problem else []
    if section.get('mode') is not None and mode_value(section.get('mode')) is None:
        problems.append('offload.mode = %r is not off, advise or block (ignored)' % (section.get('mode'),))
    if section.get('at') is not None and at_value(section.get('at')) is None:
        problems.append('offload.at = %r is not a token count of at least 20k (ignored)' % (section.get('at'),))
    if section.get('models') is not None and not isinstance(section.get('models'), dict):
        problems.append('offload.models must map claude/codex to a model name (ignored)')
    for pattern in user_patterns(section):
        try:
            re.compile(pattern)
        except re.error as error:
            problems.append('offload.patterns: %r is not a regular expression (%s; ignored)' % (pattern, error))
    if env.get(ENV) and mode_value(env.get(ENV)) is None:
        problems.append('%s=%r is not off, advise or block (ignored)' % (ENV, env.get(ENV)))
    here = effective(store, session, host, env, data)
    root = None
    try:
        from .attach import project_root, resolve
        root = project_root()[0]
        here['orbit'] = resolve(store, root)[0]
    except Exception:  # noqa: BLE001 - the view never fails on the switch lookup
        here['orbit'] = None
    here.update(session=session, session_from=how)
    logs = Path(store.home) / LOG_DIR
    from .attach import hook_status
    hooks = {}
    for name in HOSTS:
        status = hook_status(name)
        hooks[name] = {'pre_tool_hook': 'PreToolUse' in (status.get('events') or []), 'file': status.get('file')}
    path = settings_path(store)
    return {'here': here, 'session': session_override(store, session) or None if session else None,
            'global': {k: section.get(k) for k in ('mode', 'at', 'models') if k in section},
            'built_in': BUILT_IN, 'environment': env.get(ENV),
            'settings_file': {'path': str(path), 'exists': path.exists(), 'problems': problems},
            'patterns': {'built_in': [name for name, _ in SUITES], 'user': user_patterns(section)},
            'hooks': hooks, 'logs': {'dir': str(logs), 'kept': len(list(logs.glob('*.log'))) if logs.is_dir() else 0,
                                     'keep': LOG_KEEP},
            'root': str(root) if root else None, 'changed': changed}


# ---------------------------------------------------------------- the ledger (never creates a task)

def session_task(store, session, cwd=None):
    """The attached task this session works on, or (no session known) the checkout's; None otherwise."""
    task_id = None
    if session:
        row = store.session_task(session)
        task_id = row['task_id'] if row and row['task_id'] else None
    elif cwd is not None:
        from .attach import project_root
        task_id = store.attached(str(project_root(cwd)[0]))
    if not task_id:
        return None
    try:
        task = store.get(task_id)
    except ValueError:
        return None
    return task if task['data'].get('mode') == 'attached' else None


def record(store, task, kind, payload):
    if task is None:
        return False
    with store.db:
        store._event(task['id'], kind, dict(payload, layer='offload'))
    return True


# ---------------------------------------------------------------- recognising a suite's output

ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
PYTEST_SUMMARY = re.compile(r'^=*\s*((?:\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?|rerun)'
                            r'(?:, )?)+) in \d+(?:\.\d+)?s\b')
PYTEST_ITEM = re.compile(r'(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b')
UNIT_RAN = re.compile(r'^Ran (\d+) tests? in [\d.]+s')
UNIT_END = re.compile(r'^(OK|FAILED)(?: \((.*)\))?\s*$')
UNIT_ITEM = re.compile(r'(failures|errors|skipped)=(\d+)')
CARGO_RESULT = re.compile(r'^test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored')
JEST_TESTS = re.compile(r'^Tests:\s+(.*\d+ total)\s*$')
VITEST_TESTS = re.compile(r'^\s*Tests\s+(\d+ (?:failed|passed|skipped|todo).*?)\s*\(\d+\)\s*$')
JS_ITEM = re.compile(r'(\d+) (failed|passed|skipped|todo|pending)\b')
GO_PKG = re.compile(r'^(ok|FAIL)\s+\S+\s+(?:[\d.]+s|\(cached\)|\[build failed\]|\[setup failed\])')


def _add(counts, key, n):
    key = {'error': 'errors', 'failures': 'failed', 'pending': 'skipped', 'todo': 'skipped', 'ignored': 'skipped',
           'xfailed': 'skipped', 'deselected': 'skipped'}.get(key, key)
    if key == 'xpassed':
        key = 'passed'
    counts[key] = counts.get(key, 0) + n


def _pytest(line, state):
    match = PYTEST_SUMMARY.match(line)
    if match:
        state['seen'] = True
        for n, key in PYTEST_ITEM.findall(match.group(1)):
            _add(state['counts'], key, int(n))
    return line.startswith(('FAILED ', 'ERROR '))


def _unittest(line, state):
    match = UNIT_RAN.match(line)
    if match:
        state['ran'] = state.get('ran', 0) + int(match.group(1))
        state['open'] = int(match.group(1))
        return False
    match = UNIT_END.match(line)
    if match and state.get('open') is not None:
        state['seen'] = True
        ran, state['open'] = state.pop('open'), None
        bad = 0
        for key, n in UNIT_ITEM.findall(match.group(2) or ''):
            _add(state['counts'], key, int(n))
            bad += int(n)
        _add(state['counts'], 'passed', max(0, ran - bad))
        return False
    return line.startswith(('FAIL: ', 'ERROR: '))


VERIFY_FINAL = re.compile(r'^(?:PASS|FAIL)\s*(?:·|$)|^FAIL(?:ED)?: |\s·\s/\S*$')  # a file's closing line, not a check


def _orbit_verify(line, state):
    """This repository's tests/verify_*.py: `  ok  name` / `  FAIL name — detail` / `  skip …`, or (verify_telemetry)
    `PASS name` / `FAIL name  detail` at the margin. The closing line (`PASS · /tmp/x`, `FAIL a, b · /tmp/x`,
    `FAIL: 2 — …`) is not a check."""
    if line.startswith('  ok  '):
        state['seen'] = True
        _add(state['counts'], 'passed', 1)
    elif line.startswith('  FAIL ') and len(line) > 7:
        state['seen'] = True
        _add(state['counts'], 'failed', 1)
        return True
    elif line.startswith('  skip '):
        _add(state['counts'], 'skipped', 1)
    elif line.startswith(('PASS ', 'FAIL ')) and len(line) > 5 and not VERIFY_FINAL.search(line):
        state['seen'] = True
        _add(state['counts'], 'passed' if line.startswith('PASS') else 'failed', 1)
        return line.startswith('FAIL')
    return False


def _go(line, state):
    stripped = line.lstrip()
    if stripped.startswith('--- '):
        for word, key in (('--- PASS: ', 'passed'), ('--- FAIL: ', 'failed'), ('--- SKIP: ', 'skipped')):
            if stripped.startswith(word):
                state['seen'] = True
                _add(state['counts'], key, 1)
                return key == 'failed'
        return False
    match = GO_PKG.match(line)
    if match:
        state['seen'] = True
        state['packages_' + ('ok' if match.group(1) == 'ok' else 'failed')] = \
            state.get('packages_' + ('ok' if match.group(1) == 'ok' else 'failed'), 0) + 1
        return match.group(1) == 'FAIL'
    return bool(re.match(r'^\s+\S+_test\.go:\d+: ', line))


def _cargo(line, state):
    match = CARGO_RESULT.match(line)
    if match:
        state['seen'] = True
        for key, n in zip(('passed', 'failed', 'skipped'), match.groups()):
            _add(state['counts'], key, int(n))
        return False
    return line.startswith('test ') and line.endswith(' ... FAILED')


def _jest(line, state):
    match = JEST_TESTS.match(line)
    if match:
        state['seen'] = True
        for n, key in JS_ITEM.findall(match.group(1)):
            _add(state['counts'], key, int(n))
        return False
    stripped = line.lstrip()
    return stripped.startswith(('FAIL ', '✕ ', '● '))


def _vitest(line, state):
    match = VITEST_TESTS.match(line)
    if match:
        state['seen'] = True
        for n, key in JS_ITEM.findall(match.group(1)):
            _add(state['counts'], key, int(n))
        return False
    stripped = line.lstrip()
    return stripped.startswith(('FAIL ', '× ', '✗ '))


def _tap(line, state):
    if line.startswith('not ok '):
        state['seen'] = True
        skipped = re.search(r'#\s*(?:SKIP|TODO)\b', line, re.I)
        _add(state['counts'], 'skipped' if skipped else 'failed', 1)
        return not skipped
    if line.startswith('ok ') and line[3:4].isdigit():
        state['seen'] = True
        _add(state['counts'], 'skipped' if re.search(r'#\s*SKIP\b', line, re.I) else 'passed', 1)
    return False


# Each entry: a name and a function reading one line (ANSI stripped) that updates its state and says whether the
# line names a failure. Failing lines count only for a recogniser whose summary appeared.
RECOGNISERS = (('pytest', _pytest), ('unittest', _unittest), ('orbit-verify', _orbit_verify), ('go test', _go),
               ('cargo test', _cargo), ('jest', _jest), ('vitest', _vitest), ('tap', _tap))


def _counts_text(name, state):
    counts = state['counts']
    parts = ['%d passed' % counts.get('passed', 0)]
    parts += ['%d %s' % (counts[k], k) for k in ('failed', 'errors', 'skipped') if counts.get(k)]
    if name == 'go test' and (state.get('packages_ok') or state.get('packages_failed')):
        packages = '%d packages ok' % state.get('packages_ok', 0)
        if state.get('packages_failed'):
            packages += ', %d failed' % state['packages_failed']
        if not any(counts.values()):
            return packages + ' (go test)'
        return ', '.join(parts) + ' (go test; ' + packages + ')'
    return ', '.join(parts) + ' (' + name + ')'


def recognise(lines):
    """(texts, failing lines, any failure counted, totals) for an iterable of output lines."""
    states = {name: {'seen': False, 'counts': {}, 'failing': [], 'failing_total': 0} for name, _ in RECOGNISERS}
    for index, line in enumerate(lines):
        if '\x1b' in line:
            line = ANSI.sub('', line)
        line = line.rstrip('\r\n')
        for name, read in RECOGNISERS:
            state = states[name]
            if read(line, state):
                state['failing_total'] += 1
                if len(state['failing']) < 50:
                    state['failing'].append((index, line))
    texts, failing, failed, totals = [], {}, False, {}
    for name, _ in RECOGNISERS:
        state = states[name]
        if not state['seen']:
            continue
        texts.append(_counts_text(name, state))
        failed = failed or bool(state['counts'].get('failed') or state['counts'].get('errors')
                                or state.get('packages_failed'))
        for key in ('passed', 'failed', 'errors', 'skipped'):
            totals[key] = totals.get(key, 0) + state['counts'].get(key, 0)
        totals.setdefault('recognised', []).append(name)
        totals['failing_lines'] = max(totals.get('failing_lines', 0), state['failing_total'])
        for index, line in state['failing']:
            failing.setdefault(index, line)
    ordered = [failing[i] for i in sorted(failing)]
    totals['failing_lines'] = max(totals.get('failing_lines', 0), len(ordered))
    return texts, ordered, failed, totals


def _cut(text, limit=LINE_CHARS):
    text = text.replace('\t', '    ').rstrip()
    return text if len(text) <= limit else text[:limit - 1] + '…'


def summarise(path, code, seconds, label=None, timed_out=False, timeout=None):
    """The bounded plain-text summary of a finished run (at most SUMMARY_LINES lines and SUMMARY_CHARS characters)."""
    tail = collections.deque(maxlen=FAILING_LINES)
    count = [0]

    def lines():
        with open(path, 'rb') as stream:
            for raw in stream:
                count[0] += 1
                text = raw[:4096].decode('utf-8', 'replace')
                tail.append(text)
                yield text

    texts, failing, failed, totals = recognise(lines())
    size = Path(path).stat().st_size
    verdict = 'PASS' if code == 0 and not failed else 'FAIL'
    facts = dict(totals, verdict=verdict, lines=count[0], bytes=size)
    head = 'offload%s: %s exit=%d in %.1fs' % ('[' + label + ']' if label else '', verdict, code, seconds)
    if timed_out:
        head += ', timed out after %ss' % (('%g' % timeout) if timeout else '?')
    head += ' - ' + ('; '.join(texts) if texts else 'output not recognised')
    head = _cut(head, 400)
    if failing:
        body = ['  ' + _cut(ANSI.sub('', line)) for line in failing[:FAILING_LINES]]
        more = totals['failing_lines'] - len(body)
        intro = 'failing:'
    elif verdict == 'FAIL' or not texts:
        body, more = ['  ' + _cut(ANSI.sub('', line)) for line in tail if line.strip()], 0
        intro = 'last lines:'
    else:
        body, more, intro = [], 0, None
    last = 'log: %s (%d lines, %d bytes) - kept out of the conversation' % (path, count[0], size)
    out = [head]
    budget = SUMMARY_CHARS - len(head) - len(last) - 2 - 60   # room for the "more" line
    if intro and body:
        out.append(intro)
        budget -= len(intro) + 1
        shown = 0
        for line in body:
            if len(out) >= SUMMARY_LINES - 2 or len(line) + 1 > budget:
                break
            out.append(line)
            budget -= len(line) + 1
            shown += 1
        more += len(body) - shown
        if more > 0:
            out.append('  … %d more %s in the log' % (more, 'failing lines' if intro == 'failing:' else 'lines'))
    out.append(last)
    return '\n'.join(out), facts


# ---------------------------------------------------------------- the runner

def _slug(text):
    return re.sub(r'[^A-Za-z0-9._-]+', '-', text or '')[:40].strip('-.') or 'run'


def _prune_logs(folder):
    logs = sorted(folder.glob('*.log'), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    for old in logs[LOG_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


class _Stopped(Exception):
    pass


def run(store, argv=None, shell=None, cwd=None, timeout=None, label=None, session=None):
    """Run a command with no model call; returns (exit code, summary). The exit code is the command's, 124 on timeout."""
    if bool(argv) == bool(shell):
        raise ValueError('Give the command after -- (cosmos-orbit offload run -- pytest -q) or as --shell \'…\'')
    if timeout is not None and timeout <= 0:
        raise ValueError('--timeout is a number of seconds above 0')
    run_in = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    if not run_in.is_dir():
        raise ValueError('--cwd must be an existing folder: ' + str(cwd))
    if shell:
        command = [shutil.which('bash') or '/bin/sh', '-c', shell]
        shown = shell
    else:
        command = list(argv)
        shown = ' '.join(shlex.quote(a) for a in argv)
    folder = Path(store.home) / LOG_DIR
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / ('%s-%d-%s.log' % (time.strftime('%Y%m%d-%H%M%S'), os.getpid(),
                                       _slug(label or (Path(argv[0]).name if argv else 'shell'))))
    started = time.monotonic()
    timed_out = False

    def stop(signum, frame):
        raise _Stopped(signum)

    previous = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGHUP)}
    process = None
    try:
        with open(path, 'wb') as log:
            try:
                # Its own process group, so a timeout (or Orbit being stopped) ends the whole suite, workers included.
                process = subprocess.Popen(command, cwd=str(run_in), stdin=subprocess.DEVNULL, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                log.write(('offload: could not start %s: %s\n' % (shown, error)).encode())
                code = 127
            else:
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out, code = True, 124
                    _kill(process)
                if code < 0:
                    code = 128 - code
    except (_Stopped, KeyboardInterrupt):
        if process is not None and process.poll() is None:
            _kill(process)
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    seconds = time.monotonic() - started
    _prune_logs(folder)
    summary, facts = summarise(path, code, seconds, label, timed_out, timeout)
    try:
        task = session_task(store, session, None if session else run_in)
        record(store, task, 'offload_run', {'exit': code, 'seconds': round(seconds, 1), 'timed_out': timed_out,
                                            'verdict': facts['verdict'], 'passed': facts.get('passed'),
                                            'failed': facts.get('failed'), 'errors': facts.get('errors'),
                                            'recognised': facts.get('recognised'), 'log': str(path),
                                            'bytes': facts['bytes'], 'lines': facts['lines'],
                                            'command': shown[:200], 'label': label, 'session': session})
    except Exception as error:  # noqa: BLE001 - the ledger is never worth failing a run for
        print('offload: not recorded in the ledger (%s)' % str(error)[:120], file=sys.stderr)
    return code, summary


def _kill(process):
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
        try:
            os.killpg(process.pid, sig)
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


# ---------------------------------------------------------------- the hook

SHELL_TOOLS = ('Bash',)  # Claude Code's shell tool; Codex reports its shell and exec_command calls as Bash too


def _command(payload):
    tool_input = payload.get('tool_input')
    command = tool_input.get('command') if isinstance(tool_input, dict) else None
    if isinstance(command, list):
        command = ' '.join(str(part) for part in command)
    return command if isinstance(command, str) else None


def decide(home, host, payload, environ=None):
    """The hook's answer for one tool call, or None (silence). Cheapest test first: this runs on every shell call."""
    env = os.environ if environ is None else environ
    if payload.get('tool_name') not in SHELL_TOOLS:
        return None
    command = _command(payload)
    if not command:
        return None
    command = command[:20000]   # a heredoc can be long; a suite's command word is near its start
    suite = suite_name(command)
    if suite is None:
        suite = suite_name(command, user_patterns(_section(_file_settings(home))))
        if suite is None:
            return None                                      # 1. not a verification suite
    if ROUTED.search(command) or BYPASS.search(command):
        return None                                          # 2. already through Orbit, or bypassed
    if payload.get('agent_id'):
        return None                                          # 3. a subagent's call: the subagent is the offload
    from .attach import delegated
    if delegated(host, payload, env):
        return None                                          # 3. a `codex exec` another session started
    from .store import Store
    store = Store(home)
    try:
        session = payload.get('session_id')
        settings = effective(store, session, host, env)
        if settings['mode'] == 'off':
            return None                                      # 4. offload off here
        from .attach import hook_root
        cwd = payload.get('cwd') or os.getcwd()
        if hook_root(store, cwd, session, 'pre-tool') is None:
            return None                                      # 4. Orbit off here
        from .continuity import measure
        measured = measure(payload, host)                    # 5. now, from the transcript; read only
        tokens = (measured or {}).get('tokens')
        if not tokens or tokens < settings['at']:
            return None
        text = message(store, tokens, settings)
        block = settings['mode'] == 'block'
        if block:
            out = {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny', 'permissionDecisionReason': text}
        else:
            out = {'hookEventName': 'PreToolUse', 'additionalContext': text}
        record(store, session_task(store, session) if session else None,
               'offload_blocked' if block else 'offload_advised',
               {'tokens': tokens, 'at': settings['at'], 'suite': suite, 'session': session,
                'command': command[:200], 'source': (measured or {}).get('source')})
        return {'hookSpecificOutput': out}
    finally:
        store.close()


def _k(tokens):
    return '%dk' % round(tokens / 1000)


def message(store, tokens, settings):
    """At most MESSAGE_CHARS characters: the size, the point, the way out, and the model when one is set."""
    from .attach import orbit_command
    commands = [orbit_command(store), 'cosmos-orbit']
    for orbit in commands:
        parts = ['Orbit: this conversation is %s tokens (offload point %s) and every tool call re-reads all of it.'
                 % (_k(tokens), _k(settings['at'])),
                 'Run the suite as `%s offload run -- <command>` (or `--shell \'<pipeline>\'`): no model, a short summary '
                 'back, the full log on disk.' % orbit]
        if settings.get('model'):
            parts.append('If the results need interpreting, hand the run to a subagent on model %s and take back '
                         'only its summary.' % settings['model'])
        if settings['mode'] == 'block':
            parts.append('Blocked (offload block); to run it here anyway, prefix %s=off.' % ENV)
        text = ' '.join(parts)
        if len(text) <= MESSAGE_CHARS:
            return text
    return text[:MESSAGE_CHARS]


def pre_tool_main(home, host, stdin=None):
    """`cosmos-orbit hook pre-tool`: stdin carries the event, stdout one JSON object or nothing. Exit 0 always.

    Three independent answers share the hook: offload's (a suite run in a large conversation), the mid-turn notice
    (midturn.py: the conversation is past its handoff point) and the teammates' (teammates.py: a subagent's own
    conversation is past the point, told to it and to the lead). One failing never silences the others."""
    stdin = sys.stdin if stdin is None else stdin
    home = Path(home or os.environ.get('HARNESS_HOME', '~/.local/share/local-harness')).expanduser()

    def logged(error):
        try:
            from .store import now
            with (home / 'hooks.log').open('a', encoding='utf-8') as handle:
                handle.write(now() + ' pre-tool error: ' + repr(error)[:300] + '\n')
        except OSError:
            pass

    try:
        raw = stdin.read(400000) if not stdin.isatty() else ''
        payload = json.loads(raw) if raw.strip() else {}
        payload = payload if isinstance(payload, dict) else {}
    except Exception as error:  # noqa: BLE001 - a hook must not break the user's session
        logged(error)
        return 0
    output = None
    try:
        output = decide(home, host, payload)
    except Exception as error:  # noqa: BLE001
        logged(error)
    try:
        from .midturn import decide as midturn_decide, merge
        output = merge(output, midturn_decide(home, host, payload))
    except Exception as error:  # noqa: BLE001
        logged(error)
    try:
        from .midturn import merge
        from .teammates import decide as teammates_decide
        output = merge(output, teammates_decide(home, host, payload))
    except Exception as error:  # noqa: BLE001
        logged(error)
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0
