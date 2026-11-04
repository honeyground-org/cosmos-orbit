"""Self-evolution, in two layers.

Personal layer (always on): what one person's usage teaches lives in that person's store - the learned prompt
endings (`attach.learned`), measured context points. It never enters the code.

Public layer (off by default; a maintainer turns it on with `cosmos-orbit evolve --mode merge --repo PATH`):
when the same Orbit defect repeats - a hook that fails, a review that ends without an answer, a ledger someone
had to correct - an agent works in an isolated worktree of the upstream repository, writes a failing check
first, fixes it, and the change is merged only when every gate passes:

  1. checks: every check passes in the worktree, and a new or changed check fails on the base commit
  2. personal traces: no added line carries the user's home, name, host, addresses, ids, workspace paths or
     phrases from the ledger
  3. no vocabulary: added lines are plain ASCII; words people use belong to the personal layer
  4. generality: a fresh, read-only reviewer judges the change useful to any user and free of regressions

A change that fails any gate is never pushed. Mode `propose` runs the same gates and keeps the result as a
local branch and report. The run is event-driven (a hook sees a due candidate), one candidate at a time, and a
candidate is tried again only after it occurs again.
"""
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from .store import now

STATE_KEY = 'evolve_state'
MODES = ('local', 'propose', 'merge')
MAX_ATTEMPTS = 3
CHECK_TIMEOUT = 600
SEVERE = ('hook_error',)          # one occurrence is enough: a failing hook breaks every session silently

AGENT_SCHEMA = {'type': 'object', 'properties': {
    'fixable': {'type': 'boolean'}, 'summary': {'type': 'string'},
    'test_files': {'type': 'array', 'items': {'type': 'string'}}, 'reason': {'type': 'string'}},
    'required': ['fixable', 'summary', 'test_files', 'reason'], 'additionalProperties': False}
REVIEW_SCHEMA = {'type': 'object', 'properties': {
    'general': {'type': 'boolean'}, 'regression_risk': {'type': 'boolean'}, 'reason': {'type': 'string'}},
    'required': ['general', 'regression_risk', 'reason'], 'additionalProperties': False}


# ── state ────────────────────────────────────────────────────────────────────────────────────────────────

def state(store):
    try:
        value = json.loads(store.preference(STATE_KEY) or '{}')
    except ValueError:
        value = {}
    value.setdefault('mode', 'local')
    value.setdefault('repo', None)
    value.setdefault('scan', {'seq': 0, 'log_offset': 0})
    value.setdefault('candidates', {})
    value.setdefault('history', [])
    return value


def save(store, value):
    value['history'] = value['history'][-50:]
    store.preference(STATE_KEY, json.dumps(value, ensure_ascii=False))


def configure(store, mode=None, repo=None):
    value = state(store)
    if mode is not None:
        if mode not in MODES:
            raise ValueError('mode must be one of ' + ', '.join(MODES))
        value['mode'] = mode
    if repo is not None:
        path = Path(repo).expanduser().resolve()
        if not (path / '.git').exists():
            raise ValueError('The evolution repository must be a Git checkout of Orbit: ' + str(path))
        value['repo'] = str(path)
    if value['mode'] != 'local' and not value.get('repo'):
        raise ValueError('Modes propose and merge need --repo PATH (a clone of the upstream repository)')
    if value['mode'] != 'local' and not value['scan'].get('started_at'):
        # Count only what happens from now on: earlier defects were seen, and usually fixed, by people.
        top = store.db.execute('SELECT MAX(seq) FROM events').fetchone()[0] or 0
        log = store.home / 'hooks.log'
        value['scan'] = {'seq': top, 'log_offset': log.stat().st_size if log.exists() else 0, 'started_at': now()}
    save(store, value)
    return value


# ── anonymising and signals ──────────────────────────────────────────────────────────────────────────────

def anonymise(text):
    text = str(text or '')
    home = str(Path.home())
    text = text.replace(home, '<home>')
    text = re.sub(r'[\w.+-]+@[\w-]+\.[\w.]+', '<email>', text)
    text = re.sub(r'(?:/[\w.@+-]+){2,}', '<path>', text)
    text = re.sub(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', '<id>', text)
    text = re.sub(r'\b[0-9a-f]{12,}\b', '<id>', text)
    text = re.sub(r'[^\x00-\x7f]+', '<text>', text)
    return re.sub(r'\s+', ' ', text).strip()[:400]


def _key(prefix, text):
    words = re.sub(r'\d+', 'N', anonymise(text)).split()
    return prefix + ':' + ' '.join(words[:12])


def scan(store, value=None):
    """Read new ledger events and hook-log lines since the last scan; count candidates. Cheap and incremental."""
    value = value or state(store)
    cursor = value['scan']
    found = []
    rows = store.db.execute(
        "SELECT seq, kind, payload FROM events WHERE seq > ? AND kind IN "
        "('needs_input', 'verifier_rejected', 'ledger_corrected') ORDER BY seq", (cursor.get('seq', 0),)).fetchall()
    for seq, kind, payload in rows:
        cursor['seq'] = seq
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if kind == 'needs_input' and data.get('reason') == 'provider_failure':
            found.append(('provider_failure', data.get('question') or ''))
        elif kind == 'verifier_rejected':
            found.append(('verifier_rejected', data.get('reason') or ''))
        elif kind == 'ledger_corrected':
            found.append(('ledger_corrected', data.get('reason') or ''))
    log = store.home / 'hooks.log'
    if log.exists():
        size = log.stat().st_size
        offset = cursor.get('log_offset', 0) if cursor.get('log_offset', 0) <= size else 0
        with log.open('rb') as stream:
            stream.seek(offset)
            chunk = stream.read()
        cursor['log_offset'] = offset + len(chunk)
        for line in chunk.decode('utf-8', 'replace').splitlines():
            message = line.split(' ', 1)[1] if ' ' in line else line
            if ' error' in message:
                found.append(('hook_error', message))
    for prefix, text in found:
        key = _key(prefix, text)
        row = value['candidates'].setdefault(key, {'kind': prefix, 'count': 0, 'first': now(), 'samples': [],
                                                    'attempts': 0, 'attempted_at_count': 0, 'status': 'open'})
        row['count'] += 1
        row['last'] = now()
        sample = anonymise(text)
        if sample not in row['samples']:
            row['samples'] = (row['samples'] + [sample])[-3:]
        if row['status'] in ('merged', 'proposed') and row['count'] > row['attempted_at_count']:
            row['status'] = 'open'  # it came back after a fix: try again
    save(store, value)
    return value


def due(value):
    rows = []
    for key, row in value['candidates'].items():
        threshold = 1 if row['kind'] in SEVERE else 2
        if (row['status'] in ('open', 'rejected') and row['count'] >= threshold
                and row['count'] > row['attempted_at_count'] and row['attempts'] < MAX_ATTEMPTS):
            rows.append((row['count'], key))
    return [key for _, key in sorted(rows, reverse=True)]


def maybe_start(store, launch=None):
    """Called from hooks. Starts one background run when a candidate is due; never raises, never blocks."""
    try:
        value = state(store)
        if value['mode'] == 'local' or not value.get('repo') or store.locked('evolve'):
            return None
        value = scan(store, value)
        if not due(value):
            return None
        return (launch or _launch)(store)
    except Exception:  # a hook must never fail because of evolution
        return None


def _launch(store):
    """One detached run; it takes the evolve lock itself."""
    env = dict(os.environ, ORBIT='off')
    package_root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    log = store.home / 'evolve.log'
    with log.open('ab') as output:
        process = subprocess.Popen([sys.executable, '-m', 'harness.cli', '--home', str(store.home), 'evolve', '--run'],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   cwd=str(store.home), env=env, start_new_session=True)
    return process.pid


# ── gates ────────────────────────────────────────────────────────────────────────────────────────────────

def private_corpus(store):
    """What must never reach a public commit: this machine's identity and the user's own words and places."""
    exact = {str(Path.home()), getpass.getuser(), socket.gethostname()}
    grams = set()
    for (data,) in store.db.execute('SELECT data FROM tasks'):
        try:
            task = json.loads(data)
        except ValueError:
            continue
        texts = [task.get('goal') or ''] + [p.get('text', '') for p in task.get('prompts') or []] \
            + [f.get('text', '') for f in task.get('feedback') or []]
        for session in task.get('session_ids') or []:
            exact.add(session)
        source = (task.get('development') or {}).get('source')
        if source:
            exact.add(source)
        for text in texts:
            words = re.findall(r'\w+', text.lower())
            grams.update(' '.join(words[i:i + 4]) for i in range(len(words) - 3))
    return {'exact': {e for e in exact if e and len(e) >= 4}, 'grams': grams}


def added_lines(work, base):
    subprocess.run(['git', '-C', str(work), 'add', '-A'], check=True, capture_output=True)
    diff = subprocess.run(['git', '-C', str(work), 'diff', '--cached', '--unified=0', base], capture_output=True, check=True).stdout
    rows, path = [], None
    for raw in diff.decode('utf-8', 'replace').splitlines():
        if raw.startswith('+++ '):
            path = raw[6:] if raw.startswith('+++ b/') else None
        elif raw.startswith('+') and not raw.startswith('+++') and path:
            rows.append((path, raw[1:]))
    return rows


def personal_hits(rows, corpus):
    hits = []
    for path, line in rows:
        lowered = line.lower()
        for item in corpus['exact']:
            if item.lower() in lowered:
                hits.append('%s: machine or user identifier' % path)
        if re.search(r'[\w.+-]+@[\w-]+\.[\w.]+', line) and 'noreply' not in lowered:
            hits.append('%s: an e-mail address' % path)
        if re.search(r'(ghp_|github_pat_|sk-[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|xox[bap]-)', line):
            hits.append('%s: a credential-like token' % path)
        words = re.findall(r'\w+', lowered)
        if any(' '.join(words[i:i + 4]) in corpus['grams'] for i in range(len(words) - 3)):
            hits.append('%s: a phrase from the user\'s own prompts' % path)
    return sorted(set(hits))


def vocabulary_hits(rows):
    return sorted({'%s: non-ASCII text' % path for path, line in rows if re.search(r'[^\x00-\x7f]', line)})


def run_checks(work):
    """Every tests/verify_*.py, then the demo freshness check when the repository has one. Returns failures."""
    failures = []
    commands = [[sys.executable, str(p)] for p in sorted((Path(work) / 'tests').glob('verify_*.py'))]
    demo = Path(work) / 'scripts' / 'build_demo.py'
    if demo.exists():
        commands.append([sys.executable, str(demo), '--check'])
    env = dict(os.environ, ORBIT='off', PYTHONDONTWRITEBYTECODE='1')
    for argv in commands:
        try:
            result = subprocess.run(argv, cwd=str(work), capture_output=True, timeout=CHECK_TIMEOUT, env=env)
            if result.returncode != 0:
                failures.append('%s exit %d: %s' % (Path(argv[-1]).name if argv[-1] != '--check' else 'build_demo --check',
                                                     result.returncode, result.stdout.decode('utf-8', 'replace')[-300:]))
        except subprocess.TimeoutExpired:
            failures.append(Path(argv[1]).name + ' timed out')
    return failures


def reproduces(repo, base, work, tests):
    """At least one new or changed check must fail on the base commit: it captures the defect."""
    control = Path(tempfile.mkdtemp(prefix='orbit-evolve-base-'))
    try:
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(control), base], check=True, capture_output=True)
        failed = []
        for name in tests:
            target = control / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(Path(work) / name, target)
            result = subprocess.run([sys.executable, str(target)], cwd=str(control), capture_output=True, timeout=CHECK_TIMEOUT,
                                    env=dict(os.environ, ORBIT='off', PYTHONDONTWRITEBYTECODE='1'))
            if result.returncode != 0:
                failed.append(name)
        return failed
    finally:
        subprocess.run(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(control)], capture_output=True)
        shutil.rmtree(control, ignore_errors=True)


# ── agents ───────────────────────────────────────────────────────────────────────────────────────────────

def agent_prompt(key, row):
    return (
        'You are improving Cosmos Orbit, an open-source harness for Claude Code and Codex, in an isolated worktree of '
        'its repository (the current directory). One installation saw the same defect class %d times.\n'
        'Defect class: %s\nAnonymised samples:\n- %s\n\n'
        'Do this:\n1. Write a failing check first: a self-contained tests/verify_<name>.py that exits non-zero when the '
        'defect is present, built from synthetic data only.\n2. Make the smallest general fix in harness/.\n'
        '3. Run every tests/verify_*.py and make sure all pass.\n\n'
        'Rules: never write anyone\'s paths, names, ids, addresses or phrases; added lines must be plain ASCII English; '
        'if the right fix is a value, threshold or word list tuned to one person, do not write code - answer '
        'fixable=false, because such things belong in the installation\'s own learned data. Do not commit, push or '
        'install. Answer with fixable, a one-line summary, the test files you added or changed, and your reason.'
        % (row['count'], key, '\n- '.join(row['samples'])))


def claude_agent(store):
    def run(work, prompt):
        from . import providers
        adapter = providers.Provider('claude', store.home)
        answer, _ = adapter.call('evolve', prompt, AGENT_SCHEMA, None, workspace=str(work))
        return answer
    return run


def claude_reviewer(store):
    def run(work, diff_text, key):
        from . import providers
        adapter = providers.Provider('claude', store.home)
        adapter.read_only = True
        prompt = ('Review this change to an open-source developer tool. It was written automatically because one '
                  'installation saw a repeated defect (%s). Answer general=true only if it would help any user, with '
                  'any language, projects and habits, and is not tuned to one person; regression_risk=true if it could '
                  'break existing behaviour. Diff:\n%s' % (key, diff_text[-60000:]))
        answer, _ = adapter.call('evolve', prompt, REVIEW_SCHEMA, None, workspace=str(work))
        return answer
    return run


def pip_installer(work):
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--force-reinstall', '--no-deps', str(work)],
                   check=True, capture_output=True)


# ── one run ──────────────────────────────────────────────────────────────────────────────────────────────

def run(store, key=None, agent=None, reviewer=None, installer=None):
    """One bounded evolution attempt for one candidate. Returns the history row."""
    with store.lock('evolve', timeout=0.2):
        value = scan(store)
        if value['mode'] == 'local' or not value.get('repo'):
            raise ValueError('Evolution of code is off (mode local). A maintainer turns it on: '
                             'cosmos-orbit evolve --mode merge --repo PATH')
        keys = [key] if key else due(value)
        if not keys or keys[0] not in value['candidates']:
            return {'at': now(), 'outcome': 'nothing_due'}
        key = keys[0]
        row = value['candidates'][key]
        row['attempts'] += 1
        row['attempted_at_count'] = row['count']
        save(store, value)
        record = _attempt(store, value, key, row, agent or claude_agent(store), reviewer or claude_reviewer(store),
                          installer or pip_installer)
        value = state(store)
        value['candidates'][key]['status'] = {'merged': 'merged', 'proposed': 'proposed'}.get(record['outcome'], 'rejected')
        value['history'].append(record)
        save(store, value)
        with store.db:
            store._event('evolve', 'evolution_' + record['outcome'], {k: v for k, v in record.items() if k != 'diff'})
        return record


def _attempt(store, value, key, row, agent, reviewer, installer):
    repo = Path(value['repo'])
    record = {'at': now(), 'candidate': key, 'mode': value['mode'], 'gates': {}}
    has_origin = subprocess.run(['git', '-C', str(repo), 'remote', 'get-url', 'origin'], capture_output=True).returncode == 0
    if has_origin:
        subprocess.run(['git', '-C', str(repo), 'fetch', '-q', 'origin'], capture_output=True, timeout=120)
    ref = 'origin/main' if has_origin else 'main'
    base = subprocess.run(['git', '-C', str(repo), 'rev-parse', ref], capture_output=True, text=True).stdout.strip()
    if not base:
        record.update(outcome='rejected', reason='no base commit ' + ref)
        return record
    work = Path(tempfile.mkdtemp(prefix='orbit-evolve-'))
    old_orbit = os.environ.get('ORBIT')
    os.environ['ORBIT'] = 'off'  # the agent's own session must not attach to Orbit
    try:
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '--detach', str(work), base], check=True, capture_output=True)
        try:
            answer = agent(work, agent_prompt(key, row)) or {}
        except Exception as error:  # the agent failed; nothing was changed upstream
            record.update(outcome='rejected', reason='agent failed: ' + anonymise(error))
            return record
        record['summary'] = anonymise(answer.get('summary'))
        if not answer.get('fixable', True):
            record.update(outcome='rejected', reason='the agent judged it personal or not a code defect: ' + anonymise(answer.get('reason')))
            return record
        rows = added_lines(work, base)
        changed = subprocess.run(['git', '-C', str(work), 'diff', '--cached', '--name-only', base],
                                 capture_output=True, text=True).stdout.split()
        tests = [p for p in changed if p.startswith('tests/') and p.endswith('.py')]
        if not rows:
            record.update(outcome='rejected', reason='no change')
            return record
        gates = record['gates']
        gates['personal'] = personal_hits(rows, private_corpus(store))
        gates['vocabulary'] = vocabulary_hits(rows)
        gates['checks'] = run_checks(work)
        gates['reproduces'] = reproduces(repo, base, work, tests) if tests else []
        failed = [name for name in ('personal', 'vocabulary', 'checks') if gates[name]]
        if not gates['reproduces']:
            failed.append('reproduces')
        if failed:
            record.update(outcome='rejected', reason='gates failed: ' + ', '.join(failed))
            return record
        diff_text = subprocess.run(['git', '-C', str(work), 'diff', '--cached', base], capture_output=True).stdout.decode('utf-8', 'replace')
        review = reviewer(work, diff_text, key) or {}
        gates['review'] = {'general': bool(review.get('general')), 'regression_risk': bool(review.get('regression_risk', True)),
                           'reason': anonymise(review.get('reason'))}
        if not gates['review']['general'] or gates['review']['regression_risk']:
            record.update(outcome='rejected', reason='independent review: ' + gates['review']['reason'])
            return record
        message = 'evolve: %s\n\nRepeated defect class (%d occurrences): %s\nGates: checks pass, a new check fails on %s, ' \
                  'no personal traces, plain ASCII, independent review general.' % (record['summary'] or key, row['count'], key, base[:7])
        subprocess.run(['git', '-C', str(work), 'commit', '-q', '-m', message], check=True, capture_output=True)
        commit = subprocess.run(['git', '-C', str(work), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
        record['commit'] = commit
        if value['mode'] == 'merge':
            push = subprocess.run(['git', '-C', str(work), 'push', '-q', 'origin', 'HEAD:main'], capture_output=True, text=True, timeout=120)
            if push.returncode != 0:
                record.update(outcome='rejected', reason='push refused (main moved?): ' + anonymise(push.stderr))
                return record
            installer(work)
            record['outcome'] = 'merged'
        else:
            branch = 'evolve/' + re.sub(r'[^a-z0-9]+', '-', key.lower())[:40].strip('-')
            subprocess.run(['git', '-C', str(repo), 'branch', '-f', branch, commit], check=True, capture_output=True)
            report = store.home / 'evolve' / (branch.split('/')[-1] + '.md')
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text('# %s\n\n%s\n\nBranch: %s\nCommit: %s\n\n```diff\n%s\n```\n' % (key, message, branch, commit, diff_text[-20000:]),
                              encoding='utf-8')
            record.update(outcome='proposed', branch=branch, report=str(report))
        return record
    finally:
        if old_orbit is None:
            os.environ.pop('ORBIT', None)
        else:
            os.environ['ORBIT'] = old_orbit
        subprocess.run(['git', '-C', str(repo), 'worktree', 'remove', '--force', str(work)], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)


def view(store):
    from .attach import learned
    value = state(store)
    table = learned(store)
    return {'mode': value['mode'], 'repo': value.get('repo'),
            'personal_layer': {'learned_endings': len(table), 'stored_in': str(store.home)},
            'candidates': {k: {f: v[f] for f in ('kind', 'count', 'attempts', 'status')} for k, v in value['candidates'].items()},
            'due': due(value), 'history': value['history'][-5:]}
