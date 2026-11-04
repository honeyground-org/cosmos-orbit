#!/usr/bin/env python3
"""Self-evolution: repeated defects are fixed behind gates, and nothing personal reaches the public repository.
Model-free (the agent and the reviewer are stand-ins), with controls.

  ① signals: a hook the CLI rejects is logged (control: before, it failed in the host with only a usage line)
     and becomes a candidate; the agent's prompt is anonymised
  ② the trigger: mode local never starts a run (control); merge mode with a due candidate does
  ③ a good fix - a new check that fails on the base, a general fix, all checks green, review general - is pushed
     to upstream main and installed
  ④ every gate stops a change before it is pushed (upstream unchanged each time): a phrase from the user's
     prompts, the home path, non-ASCII text, no reproducing check, a check that passes on the base too, a broken
     check, a reviewer who finds it personal, an agent that says it is not a code defect
  ⑤ propose mode keeps the gated change as a local branch and report; upstream unchanged
  ⑥ a candidate is tried again only when it occurs again
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT'):
    os.environ.pop(name, None)

from harness import attach, evolve  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []
USER_PHRASE = 'please build the quarterly invoice exporter for the finance team'


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def git(*args, cwd=None):
    return subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t'] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def upstream(base):
    origin = base / 'origin.git'
    git('init', '-q', '--bare', '-b', 'main', str(origin))
    seed = base / 'seed'
    seed.mkdir()
    git('init', '-q', '-b', 'main', cwd=seed)
    (seed / 'lib.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    (seed / 'tests').mkdir()
    (seed / 'tests' / 'verify_basic.py').write_text('import sys\nsys.exit(0)\n', encoding='utf-8')
    git('add', '-A', cwd=seed)
    git('commit', '-q', '-m', 'init', cwd=seed)
    git('remote', 'add', 'origin', str(origin), cwd=seed)
    git('push', '-q', 'origin', 'main', cwd=seed)
    clone = base / 'clone'
    git('clone', '-q', str(origin), str(clone))
    git('config', 'user.email', 't@t', cwd=clone)
    git('config', 'user.name', 't', cwd=clone)
    return origin, clone


GOOD_TEST = 'import sys\nsys.path.insert(0, ".")\nfrom lib import add\nsys.exit(0 if add(2, 3) == 5 else 1)\n'


RUNS = []


def agent(extra=None, test=GOOD_TEST, fix=True, fixable=True, break_basic=False):
    def run(work, prompt):
        work = Path(work)
        RUNS.append(1)
        if test is not None:  # a new check each run: an earlier merged run may already have added verify_add.py
            (work / 'tests' / ('verify_add_%d.py' % len(RUNS))).write_text(test, encoding='utf-8')
        if fix:
            (work / 'lib.py').write_text('def add(a, b):\n    return a + b\n' + (extra or ''), encoding='utf-8')
        if break_basic:
            (work / 'tests' / 'verify_basic.py').write_text('import sys\nsys.exit(1)\n', encoding='utf-8')
        return {'fixable': fixable, 'summary': 'add returns the sum', 'test_files': ['tests/verify_add.py'], 'reason': 'synthetic'}
    return run


def reviewer(general=True, risk=False):
    return lambda work, diff, key: {'general': general, 'regression_risk': risk, 'reason': 'stand-in'}


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-evolve-'))
    store = Store(base / 'home')
    origin, clone = upstream(base)
    installs = []

    # the user's own words, in the ledger: the personal-trace gate must keep them out of a commit
    repo = base / 'user-project'
    repo.mkdir()
    git('init', '-q', cwd=repo)
    git('commit', '-q', '--allow-empty', '-m', 'init', cwd=repo)
    attach.set_switch(store, 'on', repo)
    attach.handle(store, 'prompt', {'cwd': str(repo), 'session_id': 'U', 'prompt': USER_PHRASE}, 'claude')

    print('① signals')
    result = subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(base / 'home'),
                             'hook', 'not-an-event', '--host', 'claude'], input='{}', capture_output=True, text=True)
    log = (base / 'home' / 'hooks.log').read_text(encoding='utf-8') if (base / 'home' / 'hooks.log').exists() else ''
    check('a hook the CLI rejects exits non-zero and is logged', result.returncode != 0 and 'hook usage error' in log, log)
    value = evolve.scan(store)
    keys = [k for k, v in value['candidates'].items() if v['kind'] == 'hook_error']
    check('…and becomes a due candidate', keys and keys[0] in evolve.due(value), value['candidates'])
    key = keys[0]
    prompt = evolve.agent_prompt(key, value['candidates'][key])
    check("the agent's prompt carries no home path or store path", str(Path.home()) not in prompt and str(base) not in prompt)

    print('② the trigger')
    launched = []
    check('mode local never starts a run (control)', evolve.maybe_start(store, launch=lambda s: launched.append(1) or 1) is None and not launched)
    history = Store(base / 'history-home')
    (base / 'history-home' / 'hooks.log').write_text('2026-01-01T00:00:00 prompt error: OldError() at attach.py:_handle\n', encoding='utf-8')
    evolve.configure(history, 'merge', str(clone))
    check('turning it on counts only what happens from then on (old, hand-fixed defects are not candidates)',
          not evolve.due(evolve.scan(history)))
    history.close()
    evolve.configure(store, 'merge', str(clone))
    subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(base / 'home'),
                    'hook', 'not-an-event', '--host', 'claude'], input='{}', capture_output=True, text=True)
    check('merge mode with a due candidate starts one', evolve.maybe_start(store, launch=lambda s: launched.append(1) or 1) == 1 and launched == [1])

    print('③ a good fix is pushed and installed')
    before = git('rev-parse', 'main', cwd=origin)
    row = evolve.run(store, key, agent=agent(), reviewer=reviewer(), installer=installs.append)
    after = git('rev-parse', 'main', cwd=origin)
    check('outcome merged', row['outcome'] == 'merged', row)
    check('upstream main advanced to the gated commit', after != before and after == row.get('commit'))
    message = git('log', '-1', '--format=%B', 'main', cwd=origin)
    check('the commit says what and why, with no personal trace', message.startswith('evolve:') and str(Path.home()) not in message
          and 'quarterly invoice' not in message, message)
    check('the installed copy was updated from the merged tree', len(installs) == 1)

    print('④ every gate stops a change before it is pushed')
    cases = [
        ("a phrase from the user's prompts", agent(extra='# ' + USER_PHRASE + '\n'), reviewer(), 'personal'),
        ('the home path', agent(extra='CACHE = "%s/.cache"\n' % Path.home()), reviewer(), 'personal'),
        ('non-ASCII text', agent(extra='# résumé handling\n'), reviewer(), 'vocabulary'),
        ('no reproducing check', agent(test=None), reviewer(), 'reproduces'),
        ('a check that passes on the base too', agent(test='import sys\nsys.exit(0)\n'), reviewer(), 'reproduces'),
        ('a broken check', agent(break_basic=True), reviewer(), 'checks'),
        ('a reviewer who finds it personal', agent(), reviewer(general=False), 'independent review'),
        ('an agent that says it is not a code defect', agent(fixable=False), reviewer(), 'not a code defect'),
    ]
    # bring the upstream bug back once; a rejected change never touches upstream, so it stays for every case
    git('fetch', '-q', 'origin', cwd=clone)
    git('checkout', '-q', '-B', 'main', 'origin/main', cwd=clone)
    (clone / 'lib.py').write_text('def add(a, b):\n    return a - b\n', encoding='utf-8')
    git('commit', '-q', '-am', 'bring the bug back', cwd=clone)
    git('push', '-q', 'origin', 'main', cwd=clone)
    for label, stand_in, review, expect in cases:
        top = git('rev-parse', 'main', cwd=origin)
        row = evolve.run(store, key, agent=stand_in, reviewer=review, installer=installs.append)
        reason = row.get('reason', '')
        check('%s → rejected (%s), upstream unchanged' % (label, expect), row['outcome'] == 'rejected' and expect in reason
              and git('rev-parse', 'main', cwd=origin) == top, reason)
    check('no rejected change was installed', len(installs) == 1)

    print('⑤ propose mode')
    evolve.configure(store, 'propose')
    top = git('rev-parse', 'main', cwd=origin)
    row = evolve.run(store, key, agent=agent(), reviewer=reviewer(), installer=installs.append)
    branches = git('branch', '--list', 'evolve/*', cwd=clone)
    check('the gated change is kept as a local branch and a report', row['outcome'] == 'proposed' and row['branch'] in branches
          and Path(row['report']).exists(), row)
    check('…and upstream is unchanged, nothing installed', git('rev-parse', 'main', cwd=origin) == top and len(installs) == 1)

    print('⑥ a candidate is tried again only when it occurs again')
    def bogus():
        subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(base / 'home'),
                        'hook', 'another-bad-event', '--host', 'claude'], input='{}', capture_output=True, text=True)
    bogus()
    value = evolve.scan(store)
    fresh = [k for k in evolve.due(value) if 'another-bad-event' in k][0]
    evolve.run(store, fresh, agent=agent(fixable=False), reviewer=reviewer(), installer=installs.append)
    check('after an attempt the candidate is not due', fresh not in evolve.due(evolve.state(store)))
    bogus()
    check('…and is due again once it recurs', fresh in evolve.due(evolve.scan(store)), evolve.state(store)['candidates'].get(fresh))
    for _ in range(evolve.MAX_ATTEMPTS):
        evolve.run(store, fresh, agent=agent(fixable=False), reviewer=reviewer(), installer=installs.append)
        bogus()
    check('…but never more than %d attempts (no endless loop)' % evolve.MAX_ATTEMPTS, fresh not in evolve.due(evolve.scan(store)))

    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
