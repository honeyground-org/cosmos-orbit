#!/usr/bin/env python3
"""Model-free check of offload: a suite run outside the conversation, its settings, and the PreToolUse hook (§offload).

Why (2026-11-18): one session spent 41% of its requests on verification runs and waits, each re-reading ~400k tokens.
Everything here drives the real command line (`bin/cosmos-orbit`), the hook included: a PreCompact hook once failed in
production from its first day because its test called the function while the CLI rejected the event name. Each rule
has a control. Nothing types into a terminal, starts a model, or touches the live Orbit home or host settings: the
store, CLAUDE_CONFIG_DIR and CODEX_HOME are temporary folders.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
STRIP = ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_OFFLOAD', 'ORBIT_HANDOFF_AT', 'HARNESS_HOME', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID',
         'CMUX_SURFACE_ID', 'CMUX_BUNDLED_CLI_PATH', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID',
         'CLAUDE_CONFIG_DIR', 'CODEX_HOME')
for _name in STRIP:
    os.environ.pop(_name, None)  # these runs stand for a plain terminal; each check adds what it needs
CLI = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit')]
try:
    from harness import offload  # noqa: E402
except ImportError:              # a checkout without offload: every check below fails one by one, none crashes
    offload = None
from harness import attach  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


class Loose(dict):
    """A reply read leniently: a missing key is an empty reply, so a checkout without offload fails check by check."""

    def __missing__(self, key):
        return Loose()


class Env:
    def __init__(self, base):
        self.base = base
        self.home = base / 'home'
        self.vars = dict(os.environ, CLAUDE_CONFIG_DIR=str(base / 'claude'), CODEX_HOME=str(base / 'codex'))

    def run(self, args, stdin=None, cwd=None, home=None, **extra):
        env = dict(self.vars)
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(CLI + ['--home', str(home or self.home)] + list(args), input=stdin, capture_output=True,
                              text=True, cwd=str(cwd or self.base), env=env, timeout=120)

    def view(self, *args, **extra):
        result = self.run(['offload'] + list(args), **extra)
        try:
            return json.loads(result.stdout, object_hook=Loose)
        except ValueError:
            return Loose(error=result.stderr.strip(), code=result.returncode)

    def hook(self, payload, host='claude', **extra):
        result = self.run(['hook', 'pre-tool', '--host', host], stdin=json.dumps(payload), **extra)
        try:
            out = json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError:
            out = {'unreadable': result.stdout}
        return result.returncode, out, result.stderr


def ours(entry):
    """An Orbit hook entry, by the test's own reading (not attach._is_ours, whose blind spot this file found)."""
    return any('cosmos-orbit' in str(h.get('command')) and ' hook ' in str(h.get('command')) for h in entry.get('hooks') or [])


def transcript(path, tokens, sidechain_tokens=None):
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': 'run the tests'}}]
    rows.append({'type': 'assistant', 'message': {'model': 'claude-x', 'usage': {
        'input_tokens': 1000, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': tokens - 1000}}})
    if sidechain_tokens:
        rows.append({'type': 'assistant', 'isSidechain': True, 'message': {'usage': {'input_tokens': sidechain_tokens}}})
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return str(path)


def codex_rollout(path, tokens):
    rows = [{'type': 'session_meta', 'payload': {'id': 'c-1', 'source': 'cli'}},
            {'timestamp': '2026-11-18T10:00:00Z', 'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'total_tokens': tokens}, 'model_context_window': 258400}}}]
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
    return str(path)


def bash(command, cwd, transcript_path, session='s-main', **more):
    return dict({'session_id': session, 'transcript_path': transcript_path, 'cwd': str(cwd), 'hook_event_name': 'PreToolUse',
                 'permission_mode': 'default', 'tool_name': 'Bash', 'tool_use_id': 'toolu_1',
                 'tool_input': {'command': command, 'description': 'x', 'timeout': 120000}}, **more)


SAMPLES = {
    'pytest': ('============================= test session starts ==============================\ncollected 215 items\n\n'
               'tests/test_a.py ....F.                                                   [ 50%]\n'
               '=================================== FAILURES ===================================\n'
               '=========================== short test summary info ============================\n'
               'FAILED tests/test_a.py::test_x - AssertionError: assert 1 == 2\n'
               'FAILED tests/test_b.py::test_y - KeyError: \'k\'\n'
               '================== 2 failed, 212 passed, 1 skipped in 42.10s ===================\n',
               1, '212 passed, 2 failed, 1 skipped (pytest)', 'FAILED tests/test_a.py::test_x'),
    'pytest -q': ('..........\n10 passed in 0.12s\n', 0, '10 passed (pytest)', None),
    'unittest': ('.F.E\n======================================================================\n'
                 'ERROR: test_e (tests.test_m.T)\n----------------------------------------------------------------------\n'
                 'Traceback (most recent call last):\nKeyError: 1\n'
                 '======================================================================\n'
                 'FAIL: test_f (tests.test_m.T)\n----------------------------------------------------------------------\n'
                 'AssertionError: 1 != 2\n\n----------------------------------------------------------------------\n'
                 'Ran 4 tests in 0.003s\n\nFAILED (failures=1, errors=1)\n',
                 1, '2 passed, 1 failed, 1 errors (unittest)', 'FAIL: test_f (tests.test_m.T)'),
    'unittest ok': ('.....\n----------------------------------------------------------------------\nRan 5 tests in 0.1s\n\n'
                    'OK (skipped=1)\n', 0, '4 passed, 1 skipped (unittest)', None),
    'orbit verify': ('① reading\n  ok  the first rule\n  ok  control: the second\n  FAIL a broken rule — (1, 2)\n'
                     '  skip  tmux is not installed\n\nFAIL a broken rule · /tmp/x\n',
                     1, '2 passed, 1 failed, 1 skipped (orbit-verify)', 'FAIL a broken rule — (1, 2)'),
    'orbit verify, margin style': ('PASS the hook-trust banner is not a CLI error\nFAIL turn.failed is a CLI error  []\n'
                                   '\n1 failed\n', 1, '1 passed, 1 failed (orbit-verify)', 'FAIL turn.failed is a CLI error'),
    'go test': ('=== RUN   TestA\n--- PASS: TestA (0.00s)\n=== RUN   TestB\n    b_test.go:12: got 1 want 2\n'
                '--- FAIL: TestB (0.00s)\nFAIL\nFAIL\texample.com/m\t0.004s\nok  \texample.com/n\t0.002s\n',
                1, '1 passed, 1 failed (go test; 1 packages ok, 1 failed)', '--- FAIL: TestB (0.00s)'),
    'cargo test': ('running 3 tests\ntest a ... ok\ntest b ... FAILED\ntest c ... ignored\n\nfailures:\n\n'
                   '---- b stdout ----\nthread \'b\' panicked at src/lib.rs:10:5\n\n'
                   'test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s\n',
                   101, '1 passed, 1 failed, 1 skipped (cargo test)', 'test b ... FAILED'),
    'jest': (' FAIL  src/sum.test.js\n  ● sum › adds\n    expect(received).toBe(expected)\n PASS  src/mul.test.js\n\n'
             'Test Suites: 1 failed, 1 passed, 2 total\nTests:       1 failed, 5 passed, 6 total\nSnapshots:   0 total\n'
             'Time:        1.2 s\n', 1, '5 passed, 1 failed (jest)', '● sum › adds'),
    'vitest (coloured)': (' \x1b[31m❯\x1b[39m src/a.test.ts (3 tests | 1 failed) 12ms\n   \x1b[31m×\x1b[39m a > fails 5ms\n'
                          ' \x1b[32m✓\x1b[39m src/b.test.ts (2 tests) 3ms\n\n'
                          ' Test Files  \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m | \x1b[1m\x1b[32m1 passed\x1b[39m\x1b[22m (2)\n'
                          '      Tests  \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m | \x1b[1m\x1b[32m4 passed\x1b[39m\x1b[22m (5)\n'
                          '   Duration  1.2s\n', 1, '4 passed, 1 failed (vitest)', '× a > fails 5ms'),
    'tap': ('TAP version 13\n1..4\nok 1 - adds\nnot ok 2 - subtracts\n  ---\n  ...\nok 3 - db # SKIP no db\n'
            'not ok 4 - later # TODO later\n', 1, '1 passed, 1 failed, 2 skipped (tap)', 'not ok 2 - subtracts'),
}

PATTERNS = {   # each built-in pattern: (a command it must catch, a near miss it must leave alone)
    'pytest': ('cd app && pytest -x -q tests/', 'pip install pytest'),
    'python -m pytest|unittest': ('/usr/bin/python3 -m unittest discover -s tests', 'python3 -c "import unittest"'),
    'tests/verify_*.py': ('python3 tests/verify_wake.py < /dev/null', 'sed -n 1,40p tests/verify_wake.py'),
    'loop over test files': ('for f in tests/verify_*.py; do python3 "$f" < /dev/null; done',
                             'for f in src/*.py; do wc -l "$f"; done'),
    'npm|pnpm|yarn|bun test': ('pnpm test -- --run', 'npm install --save-dev @testing-library/react'),
    'vitest': ('npx vitest run', 'cat vitest.config.ts'),
    'jest': ('CI=1 jest --ci', 'npm i -D jest'),
    'go test': ('go test ./...', 'go build ./...'),
    'cargo test': ('cargo +nightly test --workspace', 'cargo build --release'),
    'phpunit|pest': ('vendor/bin/phpunit --filter Foo', 'composer require --dev phpunit/phpunit'),
    'php artisan test': ('php artisan test --parallel', 'php artisan migrate'),
    'make test|check': ('make -j4 check', 'make build'),
    'tox|nox': ('tox -e py39', 'cat tox.ini'),
}


def settings(env, base, repo, store):
    print('① settings: session > ORBIT_OFFLOAD > global > built-in, each with where it came from')
    here = env.view().get('here', {})
    check('built in: off, 150k, Claude Code → haiku', (here.get('mode'), here.get('at'), here.get('model'), here.get('mode_source'))
          == ('off', 150000, 'haiku', 'built-in'), here)
    no_session = env.run(['offload', 'on'])
    check('no session and no flag: a one-line refusal that names --global', no_session.returncode == 2
          and '--global' in no_session.stderr and len(no_session.stderr.strip().splitlines()) == 1, no_session.stderr)
    check('control: nothing was written by the refusal', not (env.home / 'settings.json').exists())
    env.run(['offload', 'advise', '--global'])
    here = env.view()['here']
    check('--global advise applies everywhere (source global)', (here['mode'], here['mode_source']) == ('advise', 'global'), here)
    here = env.view(ORBIT_OFFLOAD='block')['here']
    check('ORBIT_OFFLOAD beats the global setting', (here['mode'], here['mode_source']) == ('block', 'environment'), here)
    changed = env.view('off', CLAUDE_CODE_SESSION_ID='s-1', ORBIT_OFFLOAD='block')
    check('default scope is this session (CLAUDE_CODE_SESSION_ID)', (changed.get('changed') or {}).get('scope') == 'session'
          and (changed.get('changed') or {}).get('session') == 's-1', changed.get('changed'))
    here = env.view(CLAUDE_CODE_SESSION_ID='s-1', ORBIT_OFFLOAD='block')['here']
    check('the session beats ORBIT_OFFLOAD', (here['mode'], here['mode_source']) == ('off', 'session'), here)
    here = env.view(CLAUDE_CODE_SESSION_ID='s-2')['here']
    check('control: another session still gets the global setting', (here['mode'], here['mode_source']) == ('advise', 'global'), here)
    env.run(['offload', '--at', '220k', '--global'])
    env.run(['offload', '--at', '400k', '--session', 's-1'])
    check('the point: session > global > built-in', (env.view(CLAUDE_CODE_SESSION_ID='s-1')['here']['at'],
                                                     env.view(CLAUDE_CODE_SESSION_ID='s-2')['here']['at']) == (400000, 220000))
    env.run(['offload', 'reset', '--session', 's-1'])
    here = env.view(CLAUDE_CODE_SESSION_ID='s-1', ORBIT_OFFLOAD='block')['here']
    check('reset --session: the environment applies again', (here['mode_source'], here['at_source']) == ('environment', 'global'), here)
    env.run(['offload', 'reset', '--global'])
    here = env.view()['here']
    check('reset --global: the built-in values apply again', (here['mode'], here['at'], here['mode_source'])
          == ('off', 150000, 'built-in'), here)
    env.run(['offload', '--model', 'sonnet', '--global'])
    env.run(['offload', '--model', 'gpt-5-mini', '--host', 'codex', '--global'])
    check('--model is per host', (env.view(CLAUDE_CODE_SESSION_ID='s-3')['here']['model'],
                                  env.view(CODEX_THREAD_ID='c-3')['here']['model']) == ('sonnet', 'gpt-5-mini'))
    here = env.view(CODEX_THREAD_ID='c-3')['here']
    check('CODEX_THREAD_ID names a Codex session', (here['session'], here['host'], here['session_from'])
          == ('c-3', 'codex', 'CODEX_THREAD_ID'), here)
    env.run(['offload', '--model', 'none', '--session', 's-4'])
    check('--model none: no model for this session (the global one for others)',
          (env.view(CLAUDE_CODE_SESSION_ID='s-4')['here']['model'], env.view(CLAUDE_CODE_SESSION_ID='s-5')['here']['model'])
          == (None, 'sonnet'))
    with store.db:
        store.db.execute('INSERT OR REPLACE INTO terminals VALUES(?,?,?,?,?,?,?,?)',
                         ('p-1', 'codex', 'tmux', '/usr/bin/tmux', '%77', None, 'idle', '2026-11-18T10:00:00+00:00'))
    here = env.view(TMUX='/tmp/tmux-1/default', TMUX_PANE='%77')['here']
    check('no session variable: the pane\'s last session (wake table)', (here['session'], here['host'], here['session_from'])
          == ('p-1', 'codex', 'pane'), here)
    both = env.run(['offload', 'on', '--global', '--session', 's-1'])
    check('--session and --global together are refused', both.returncode == 2 and 'not both' in both.stderr, both.stderr)
    bad = env.run(['offload', '--at', '5k', '--global'])
    check('a point below 20k is refused, in offload\'s words', bad.returncode == 2 and 'offload point below 20k' in bad.stderr,
          bad.stderr[-200:])
    settings = json.loads((env.home / 'settings.json').read_text())
    settings['handoff'] = {'at': 250000}
    settings['offload']['mode'] = 'block'
    (env.home / 'settings.json').write_text(json.dumps(settings))
    here = env.view()['here']
    check('a hand edit applies at once', (here['mode'], here['mode_source']) == ('block', 'global'), here)
    env.run(['offload', 'advise', '--global'])
    settings = json.loads((env.home / 'settings.json').read_text())
    check('control: writing offload keeps the handoff section', settings.get('handoff') == {'at': 250000}
          and settings['offload']['mode'] == 'advise', settings)
    (env.home / 'settings.json').write_text('{"offload": {"mode": "advise",}')
    broken = env.run(['offload', 'block', '--global'])
    check('a broken settings file is never overwritten', broken.returncode == 2
          and (env.home / 'settings.json').read_text() == '{"offload": {"mode": "advise",}', broken.stderr)
    (env.home / 'settings.json').write_text(json.dumps(settings))
    if offload is not None:
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO offload_sessions VALUES(?,?,?)',
                             ('ancient', '{"mode": "block"}', '2020-01-01T00:00:00+00:00'))
        for n in range(150):
            offload.configure(store, 'session', 'many-%03d' % n, mode='block')
    final = env.run(['offload', 'off', '--session', 'many-last'])
    rows = sqlite3.connect(str(env.home / 'tasks.sqlite3')).execute('SELECT session FROM offload_sessions').fetchall() \
        if final.returncode == 0 else []
    names = {r[0] for r in rows}
    check('session overrides do not accumulate (151 written, at most %d kept, the newest among them)'
          % getattr(offload, 'SESSION_KEEP', 100), 0 < len(rows) <= getattr(offload, 'SESSION_KEEP', 100)
          and 'many-last' in names and 'many-149' in names, len(rows))
    check('an override older than its days is dropped', 'ancient' not in names and rows)


def runner(env, base, repo, store):
    print('② the runner: the command\'s exit code, a bounded summary, the full log on disk')
    for name, (text, code, totals, failing) in SAMPLES.items():
        sample = base / ('sample-%s.txt' % name.replace(' ', '_').replace('(', '').replace(')', ''))
        sample.write_text(text)
        result = env.run(['offload', 'run', '--shell', 'cat %s; exit %d' % (sample, code)])
        first = result.stdout.splitlines()[0] if result.stdout else ''
        check('%s: totals recognised' % name, totals in first and result.returncode == code, (result.returncode, first))
        if failing:
            check('%s: the failing line is shown' % name, failing in result.stdout, result.stdout[-400:])
    plain = env.run(['offload', 'run', '--shell', 'echo one; echo two; echo the last words; exit 3'])
    check('unrecognised output: exit code and the last lines', plain.returncode == 3 and 'exit=3' in plain.stdout
          and 'the last words' in plain.stdout and 'not recognised' in plain.stdout, plain.stdout)
    chained = subprocess.run(' '.join(CLI) + ' --home %s offload run -- false && echo AFTER' % env.home, shell=True,
                             capture_output=True, text=True, env=env.vars)
    check('exit code passes through, so && chains still stop', chained.returncode == 1 and 'AFTER' not in chained.stdout,
          (chained.returncode, chained.stdout[-200:]))
    ok = env.run(['offload', 'run', '--', sys.executable, '-c', 'print("fine")'])
    check('control: a passing command exits 0 and says PASS', ok.returncode == 0 and ok.stdout.startswith('offload: PASS exit=0'),
          ok.stdout)
    generator = base / 'big.py'
    generator.write_text("import sys\nfor i in range(100000):\n    sys.stdout.write('FAILED tests/t.py::test_%06d - "
                         "AssertionError: ' % i + 'x' * 30 + '\\n')\nprint('== 100000 failed in 9.00s ==')\n")
    result = env.run(['offload', 'run', '--label', 'big', '--shell', '%s %s; exit 1' % (sys.executable, generator)])
    log = Path(result.stdout.strip().splitlines()[-1].split(' ')[1]) if result.stdout.strip() else None
    size = log.stat().st_size if log and log.is_file() else 0
    check('a %.1f MB output gives at most 40 lines and 3000 characters' % (size / 1e6), size > 5_000_000
          and len(result.stdout.splitlines()) <= 40 and len(result.stdout.rstrip('\n')) <= 3000,
          (size, len(result.stdout.splitlines()), len(result.stdout)))
    check('… and says how many failing lines it left in the log (100000 - 15 shown)',
          '99985 more failing lines in the log' in result.stdout
          and '100000 failed (pytest)' in result.stdout, result.stdout[:300])
    check('… and the whole output is in the log', log is not None and log.read_bytes().count(b'\n') == 100001, log)
    one_line = env.run(['offload', 'run', '--shell', 'python3 -c "import sys; sys.stdout.write(\'y\' * 5300000)"; exit 2'])
    check('a 5.3 MB single line is still bounded', one_line.returncode == 2 and 'offload: FAIL exit=2' in one_line.stdout
          and len(one_line.stdout) <= 3000, (one_line.returncode, len(one_line.stdout), one_line.stderr[-200:]))
    mark = base / 'mark'
    started = time.monotonic()
    timed = env.run(['offload', 'run', '--timeout', '1', '--shell', '(sleep 2; touch %s) & sleep 30' % mark])
    took = time.monotonic() - started
    time.sleep(2.5)
    check('timeout: exit 124 and the summary says it timed out', timed.returncode == 124 and 'timed out' in timed.stdout,
          (timed.returncode, timed.stdout[:200]))
    check('timeout ends the whole process group (a background child never finishes)', timed.returncode == 124
          and not mark.exists() and took < 15, (timed.returncode, mark.exists(), took))
    folder = env.home / 'offload'
    folder.mkdir(exist_ok=True)
    for n in range(205):
        stale = folder / ('20200101-000000-%d-old.log' % n)
        stale.write_text('old')
        os.utime(stale, (1577836800 + n, 1577836800 + n))
    env.run(['offload', 'run', '--', 'true'])
    kept = list(folder.glob('*.log'))
    check('at most 200 logs kept, the newest', len(kept) == 200 and not (folder / '20200101-000000-0-old.log').exists(), len(kept))
    tasks_before = len(store.list())
    quiet = env.run(['offload', 'run', '--', 'true'], CLAUDE_CODE_SESSION_ID='s-none')
    check('no task: none is created and the run still works', quiet.returncode == 0 and len(store.list()) == tasks_before,
          (quiet.returncode, quiet.stderr[-200:]))
    task = attach.create(store, repo, 'Make the offload runner record its runs in the ledger', 'claude', 's-task')
    env.run(['offload', 'run', '--', 'true'], CLAUDE_CODE_SESSION_ID='s-task')
    kinds = [e['kind'] for e in store.events(task['id'])]
    run_event = [e for e in store.events(task['id']) if e['kind'] == 'offload_run']
    check('a session with a task gets one offload_run event', kinds.count('offload_run') == 1
          and run_event[0]['payload'].get('exit') == 0 and run_event[0]['payload'].get('log'), run_event)


def hook(env, base, repo, store):
    print('③ the hook, through the real command line: silent unless it matters')
    big_t = transcript(base / 'big.jsonl', 511_000, sidechain_tokens=900_000)
    small_t = transcript(base / 'small.jsonl', 50_000)
    env.run(['on', str(repo)])
    env.run(['offload', 'reset', '--global'])        # the models set in ① go: the built-in ones apply again
    env.run(['offload', 'advise', '--global'])
    code, out, err = env.hook(bash('python3 -m pytest -q', repo, big_t))
    speaks = bool(out) and 'additionalContext' in (out.get('hookSpecificOutput') or {})
    check('control: a suite in a 511k conversation (point 150k) is advised', code == 0 and speaks, (code, out, err[-300:]))
    note = ((out or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
    check('advise: additionalContext only, no permission decision', speaks and 'permissionDecision' not in out['hookSpecificOutput']
          and out['hookSpecificOutput'].get('hookEventName') == 'PreToolUse', out)
    check('the note: measured size, the point, the form to use, the model; at most 600 characters',
          '511k' in note and '150k' in note and 'offload run -- <command>' in note and "--shell '<pipeline>'" in note
          and 'subagent on model haiku' in note and 'no model' in note and len(note) <= 600, (len(note), note))
    check('a subagent\'s own context in the transcript is not the conversation (511k, not 900k)', '900k' not in note, note)
    for label, payload in (('a non-shell tool', dict(bash('pytest', repo, big_t), tool_name='Read', tool_input={'file_path': 'x'})),
                           ('a shell command that is not a suite', bash('ls -la && git status', repo, big_t)),
                           ('already through Orbit (offload run)', bash('cosmos-orbit offload run -- pytest -q', repo, big_t)),
                           ('already through Orbit (evidence)', bash('/usr/bin/python3 /x/bin/cosmos-orbit --home /h evidence '
                                                                     'abc D1 --command \'["pytest"]\'', repo, big_t)),
                           ('already through Orbit (--home before the command)',
                            bash("cosmos-orbit --home '/tmp/a b' offload run --shell 'pytest -q'", repo, big_t)),
                           ('the bypass prefix', bash('ORBIT_OFFLOAD=off pytest -q', repo, big_t)),
                           ('a subagent\'s call (agent_id)', bash('pytest -q', repo, big_t, agent_id='a1b2', agent_type='Explore')),
                           ('a conversation below the point', bash('pytest -q', repo, small_t)),
                           ('an empty event', {}),
                           ('a folder where Orbit is off', bash('pytest -q', base, big_t))):
        code, out, err = env.hook(payload)
        check('silent: %s' % label, code == 0 and out is None and not err.strip(), (code, out, err[-200:]))
    code, out, _ = env.hook(bash('cd ~/src/cosmos-orbit && pytest -k evidence', repo, big_t))
    check('control: a checkout named cosmos-orbit is not a route through Orbit', bool(out), out)
    code, out, _ = env.run(['hook', 'pre-tool'], stdin='{not json').returncode, None, None
    check('silent: an unreadable event (exit 0)', code == 0)
    code, out, _ = env.hook(bash('pytest -q', repo, big_t), ORBIT_OFFLOAD='off')
    check('silent: ORBIT_OFFLOAD=off over a global advise', out is None, out)
    env.run(['offload', 'off', '--session', 's-quiet'])
    code, out, _ = env.hook(bash('pytest -q', repo, big_t, session='s-quiet'), ORBIT_OFFLOAD='advise')
    check('silent: this session off beats ORBIT_OFFLOAD=advise', out is None, out)
    code, out, _ = env.hook(bash('pytest -q', repo, big_t, session='s-other'), ORBIT_OFFLOAD='advise')
    check('control: another session with ORBIT_OFFLOAD=advise is advised', bool(out), out)
    env.run(['off', str(repo)])
    code, out, _ = env.hook(bash('pytest -q', repo, big_t))
    check('silent: Orbit switched off for this project', out is None, out)
    env.run(['on', str(repo)])
    env.run(['offload', 'block', '--session', 's-block'])
    code, out, _ = env.hook(bash('go test ./...', repo, big_t, session='s-block'))
    spec = (out or {}).get('hookSpecificOutput') or {}
    check('block: permissionDecision deny with the reason, naming the bypass', spec.get('permissionDecision') == 'deny'
          and 'ORBIT_OFFLOAD=off' in spec.get('permissionDecisionReason', '') and len(spec.get('permissionDecisionReason', '')) <= 600
          and 'additionalContext' not in spec, out)
    code, out, _ = env.hook(bash('ORBIT_OFFLOAD=off go test ./...', repo, big_t, session='s-block'))
    check('control: the bypass named in the reason lets it run', out is None, out)
    env.run(['offload', '--model', 'none', '--session', 's-nomodel'])
    code, out, _ = env.hook(bash('pytest', repo, big_t, session='s-nomodel'))
    check('no model for this host: no subagent advice', bool(out) and 'subagent' not in json.dumps(out), out)
    stale = attach.create(store, repo, 'A task whose ledger says the conversation is small', 'claude', 's-stale')
    with store.lock('task:' + stale['id']):
        stale = store.get(stale['id'])
        stale['data']['context_live'] = {'session': 's-stale', 'tokens': 185_000, 'peak': 185_000}
        store.save(stale, stale['status'], 'context_measured', {'tokens': 185_000})
    revision = store.get(stale['id'])['revision']
    env.run(['offload', '--at', '300k', '--global'])
    code, out, _ = env.hook(bash('pytest', repo, big_t, session='s-stale'))
    check('measured now beats a stale ledger (ledger 185k, transcript 511k, point 300k)', bool(out), out)
    stale_big = attach.create(store, repo, 'A task whose ledger says the conversation is large', 'claude', 's-stale2')
    with store.lock('task:' + stale_big['id']):
        stale_big = store.get(stale_big['id'])
        stale_big['data']['context_live'] = {'session': 's-stale2', 'tokens': 511_000, 'peak': 511_000}
        store.save(stale_big, stale_big['status'], 'context_measured', {'tokens': 511_000})
    code, out, _ = env.hook(bash('pytest', repo, small_t, session='s-stale2'))
    check('control: a large ledger number with a small transcript stays silent', out is None, out)
    after = store.get(stale['id'])
    events = [e['kind'] for e in store.events(stale['id'])]
    check('the hook records offload_advised on the session\'s task', 'offload_advised' in events, events[-4:])
    check('… and changes no task state (revision, handoff state)', after['revision'] == revision
          and not after['data'].get('handoff_state'), (after['revision'], revision, after['data'].get('handoff_state')))
    code, out, _ = env.hook(bash('go test ./...', repo, big_t, session='s-block'))
    tasks_before = len(store.list())
    env.hook(bash('pytest', repo, big_t, session='s-no-task-at-all'))
    check('no task for the session: none is created', len(store.list()) == tasks_before)
    rollout = codex_rollout(base / 'rollout.jsonl', 240_000)
    env.run(['offload', '--at', '200k', '--global'])
    code, out, _ = env.hook(bash('cargo test', repo, rollout, session='c-1'), host='codex')
    check('Codex: the same contract (additionalContext), measured from its rollout', bool(out)
          and '240k' in out['hookSpecificOutput'].get('additionalContext', '')
          and 'subagent' not in out['hookSpecificOutput'].get('additionalContext', ''), out)
    personal = bash('./scripts/run-checks.sh --all', repo, big_t)
    code, out, _ = env.hook(personal)
    check('control: a personal script is not a suite by default', out is None, out)
    settings = json.loads((env.home / 'settings.json').read_text())
    settings['offload']['patterns'] = [r'scripts/run-checks\.sh', '(unclosed']
    (env.home / 'settings.json').write_text(json.dumps(settings))
    code, out, _ = env.hook(personal)
    check('a user pattern from the settings file makes it one (a broken pattern is skipped)', bool(out), out)
    problems = env.view()['settings_file']['problems']
    check('… and the broken pattern is reported', any('(unclosed' in p for p in problems), problems)
    missing_home = base / 'never-created'
    code, out, _ = env.hook(bash('ls -la', repo, big_t), home=missing_home)
    check('the no-op path opens no store (its home is not even created)', code == 0 and out is None and not missing_home.exists())


def patterns(env, base, repo, store):
    print('④ built-in patterns: each catches its suite and leaves its near miss alone')
    for name, (positive, negative) in PATTERNS.items():
        got = offload.suite_name(positive) if offload else None
        check('%s catches: %s' % (name, positive), got == name, got)
        check('%s leaves alone: %s' % (name, negative), (offload.suite_name(negative) if offload else 'x') is None)


def install(env, base, repo, store):
    print('⑤ hooks install / uninstall / reinstall (temporary host files)')
    (base / 'claude').mkdir(exist_ok=True)
    foreign = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': '/opt/guard --x'}]}
    (base / 'claude' / 'settings.json').write_text(json.dumps({'hooks': {'PreToolUse': [foreign]}}))
    for _ in range(2):
        installed = env.run(['hooks', 'install', '--host', 'claude', '--host', 'codex'])
    claude = json.loads((base / 'claude' / 'settings.json').read_text())['hooks']
    codex = json.loads((base / 'codex' / 'hooks.json').read_text())['hooks']
    mine = [e for e in claude.get('PreToolUse', []) if ours(e)]
    check('installed twice: one PreToolUse entry of ours, matcher Bash, command hook pre-tool', installed.returncode == 0
          and len(mine) == 1 and mine[0]['matcher'] == 'Bash' and 'hook pre-tool --host claude' in mine[0]['hooks'][0]['command'],
          claude.get('PreToolUse'))
    check('… with a small timeout and no status line on every shell call', mine and mine[0]['hooks'][0]['timeout'] <= 10
          and 'statusMessage' not in mine[0]['hooks'][0], mine)
    check('control: the user\'s own PreToolUse hook is kept', foreign in claude.get('PreToolUse', []))
    counts = {k: len([e for e in v if ours(e)]) for k, v in claude.items()}
    check('installed twice with --home: every event holds exactly one of ours (no duplicates)',
          len(counts) == 7 and set(counts.values()) == {1}, counts)
    check('control: the other events keep matcher ""', all(e['matcher'] == '' for k, v in claude.items() if k != 'PreToolUse'
                                                          for e in v if ours(e)))
    codex_ours = [e for e in codex.get('PreToolUse', []) if ours(e)]
    check('Codex gets the same PreToolUse hook (--host codex)', len(codex_ours) == 1 and codex_ours[0]['matcher'] == 'Bash'
          and '--host codex' in codex_ours[0]['hooks'][0]['command'], codex.get('PreToolUse'))
    status = json.loads(env.run(['hooks', 'status', '--host', 'claude']).stdout)['claude']
    check('status lists PreToolUse and says installed', status['installed'] and 'PreToolUse' in status['events'], status)
    data = json.loads((base / 'claude' / 'settings.json').read_text())
    data['hooks']['PreToolUse'] = [foreign]
    (base / 'claude' / 'settings.json').write_text(json.dumps(data))
    status = json.loads(env.run(['hooks', 'status', '--host', 'claude']).stdout)['claude']
    check('an installation from before PreToolUse: not installed, PreToolUse missing, the fix named',
          not status['installed'] and status.get('missing') == ['PreToolUse'] and status.get('fix'), status)
    env.run(['hooks', 'uninstall', '--host', 'claude', '--host', 'codex'])
    claude = json.loads((base / 'claude' / 'settings.json').read_text())['hooks']
    codex = json.loads((base / 'codex' / 'hooks.json').read_text()).get('hooks', {})
    check('uninstall removes every Orbit hook, PreToolUse included', not any(ours(e) for v in claude.values() for e in v)
          and not any(ours(e) for v in codex.values() for e in v), claude)
    check('control: uninstall keeps the user\'s own PreToolUse hook', claude.get('PreToolUse') == [foreign], claude)


def reference(env, base, repo, store):
    print('⑥ the command reference and latency')
    reference = env.run(['commands']).stdout
    check('cosmos-orbit commands lists offload', 'cosmos-orbit offload run' in reference and 'offload on|off' in reference,
          reference[:200])
    timings = {}
    big_t, small_t = transcript(base / 'big2.jsonl', 511_000), transcript(base / 'small2.jsonl', 50_000)
    for label, payload in (('not a suite', bash('git status', repo, big_t)),
                           ('a suite below the point', bash('pytest -q', repo, small_t))):
        runs = []
        for _ in range(10):
            started = time.monotonic()
            env.hook(payload)
            runs.append((time.monotonic() - started) * 1000)
        timings[label] = statistics.median(runs)
    print('  info hook pre-tool median over 10 runs: ' + ', '.join('%s %.0f ms' % kv for kv in timings.items()))


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-offload-'))
    env = Env(base)
    repo = base / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    (repo / 'app.py').write_text('print(1)\n')
    store = Store(env.home)

    for section in (settings, runner, hook, patterns, install, reference):
        try:
            section(env, base, repo, store)
        except Exception:  # noqa: BLE001 - a section that cannot finish fails as one check; the next still runs
            import traceback
            check('%s: ran to the end' % section.__name__, False, traceback.format_exc()[-300:])
    store.close()
    shutil.rmtree(base, ignore_errors=True)
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
