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

    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
