#!/usr/bin/env python3
"""A large prompt reaches the provider whole, even when the provider starts reading late. Model-free, with a control.

Why this exists (2026-11-03): reviews hung with no output, no network and almost no CPU. The prompt went
through a pipe with Python 3.9's communicate(timeout=0.25) in a loop that passed the prompt on the first call
only. On a retry without input, 3.9 no longer writes, so a prompt larger than the pipe buffer never got its
end-of-file and the provider waited forever. Prompts grew with every review round until they crossed it.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'

for _marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):  # these runs stand for sessions a person opened,
    os.environ.pop(_marker, None)                                    # not ones an agent delegated (attach.delegated)
from harness import providers  # noqa: E402

FAILED = []

# A stand-in for `claude -p`: starts slowly, reads stdin to the end, answers with the size and digest it got.
FAKE = r'''#!/usr/bin/env python3
import hashlib, json, sys, time
if '--version' in sys.argv:
    print('0.0.0 (fake)'); sys.exit(0)
time.sleep(0.6)
data = sys.stdin.buffer.read()
print(json.dumps({'type': 'result', 'is_error': False, 'session_id': 's',
                  'structured_output': {'size': len(data), 'sha': hashlib.sha256(data).hexdigest()}}))
'''


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def old_pattern_hangs(size, budget=6.0):
    """The loop the provider used before (control): True when the reader never gets end-of-file."""
    child = [sys.executable, '-c', 'import sys,time; time.sleep(0.6); sys.stdin.read()']
    process = subprocess.Popen(child, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    first, start = True, time.monotonic()
    while time.monotonic() - start < budget:
        try:
            process.communicate('x' * size if first else None, timeout=.25)
            return False
        except subprocess.TimeoutExpired:
            first = False
    process.kill()
    process.wait()
    return True


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-providers-'))
    bin_dir = base / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'claude'
    fake.write_text(FAKE, encoding='utf-8')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    os.environ['PATH'] = str(bin_dir) + os.pathsep + os.environ['PATH']

    print('① control: the old pipe loop never delivers end-of-file for a 300 KB prompt to a late reader')
    check('old communicate(timeout) loop hangs', old_pattern_hangs(300_000))

    print('② the provider delivers the whole prompt, with and without the progress monitor')
    prompt = ('A large review prompt with evidence. ' * 9000)[:300_000] + ' 한국어 끝'
    expected = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    schema = {'type': 'object'}
    for label, on_event in (('with the monitor (the worker path)', lambda kind, payload: None), ('without it', None)):
        provider = providers.Provider('claude', base / 'home')
        if on_event:
            provider.on_event = on_event
        start = time.monotonic()
        answer, _ = provider.call('t-' + label.split()[0], prompt, schema, workspace=base)
        took = time.monotonic() - start
        check('%s: the provider got all %d bytes, in %.1fs' % (label, len(prompt.encode('utf-8')), took),
              answer.get('sha') == expected and took < 20, answer)
    leftovers = [p.name for p in (base / 'home' / 'runs').rglob('*.prompt.txt')]
    check('the prompt file is removed after the call', not leftovers, leftovers)

    print('③ a call that skips the person\'s settings keeps the model they chose (2026-11-09: reviews ran on the CLI default)')
    os.environ.pop('ANTHROPIC_MODEL', None)
    config = base / 'claude-config'
    config.mkdir()
    os.environ['CLAUDE_CONFIG_DIR'] = str(config)

    def argv_of(task_id, model=None, read_only=False, workspace=base):
        provider = providers.Provider('claude', base / 'home', model)
        provider.read_only = read_only
        provider.call(task_id, 'review this', schema, workspace=workspace)
        run = base / 'home' / 'runs' / task_id
        argv = json.loads(next(run.glob('*.invocation.json')).read_text())['argv']
        receipt = json.loads(next(run.glob('*.receipt.json')).read_text())
        return argv, receipt.get('requested_model')

    def model_flag(argv):
        return argv[argv.index('--model') + 1] if '--model' in argv else None

    argv, _ = argv_of('m-none', read_only=True)
    check('control: no model in the settings → no --model (the CLI default, as before)', model_flag(argv) is None, argv)
    (config / 'settings.json').write_text(json.dumps({'model': 'opus', 'effortLevel': 'xhigh'}), encoding='utf-8')
    argv, requested = argv_of('m-review', read_only=True)
    check('reviewer (settings skipped) runs on the settings model', '--setting-sources' in argv and model_flag(argv) == 'opus', argv)
    check('the receipt records the model Orbit requested', requested == 'opus', requested)
    argv, _ = argv_of('m-plan', workspace=None)
    check('a planner call without a workspace also keeps it', model_flag(argv) == 'opus', argv)
    argv, requested = argv_of('m-explicit', model='sonnet', read_only=True)
    check('an explicit task model wins over the settings', model_flag(argv) == 'sonnet' and argv.count('--model') == 1 and requested == 'sonnet', argv)
    argv, _ = argv_of('m-exec')
    check('control: the executor loads the settings itself → no --model added', '--setting-sources' not in argv and model_flag(argv) is None, argv)
    os.environ['ANTHROPIC_MODEL'] = 'haiku'
    argv, _ = argv_of('m-env', read_only=True)
    os.environ.pop('ANTHROPIC_MODEL')
    check('control: ANTHROPIC_MODEL is left to the CLI', model_flag(argv) is None, argv)
    (config / 'settings.json').write_text('{not json', encoding='utf-8')
    argv, _ = argv_of('m-broken', read_only=True)
    check('control: unreadable settings → no --model, the call still runs', model_flag(argv) is None, argv)

    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
