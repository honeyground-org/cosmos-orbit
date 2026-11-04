"""Type into the terminal the session runs in: the one way to start a fresh context without the user.

A hook cannot clear its own conversation: Claude Code and Codex offer no hook output for that. A terminal
multiplexer can type into a pane, and the hook inherits the pane's identity from its environment. So when the
session runs in cmux or tmux, an automatic handoff is possible: Orbit types /clear and then one short line that
picks the carried-over request up in the fresh context. Anywhere else there is no typist and Orbit only tells.

The typing runs in a detached process: the hook must return first, so that the host is back at its input line
when the keys arrive.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

# The line typed into the fresh session. Plain ASCII, fixed: the user's own words travel in the ledger and the
# brief, never through the keyboard (a multi-line request would be submitted half-way).
CARRY_LINE = 'Continue with the request Orbit carried over from the previous context.'


def detect(env=None):
    """The terminal this hook can type into, or None."""
    env = os.environ if env is None else env
    if env.get('CMUX_PANEL_ID'):
        cli = env.get('CMUX_BUNDLED_CLI_PATH') or shutil.which('cmux')
        if cli and os.access(cli, os.X_OK):
            return {'kind': 'cmux', 'cli': cli, 'target': env['CMUX_PANEL_ID']}
    if env.get('TMUX') and env.get('TMUX_PANE'):
        cli = shutil.which('tmux')
        if cli:
            return {'kind': 'tmux', 'cli': cli, 'target': env['TMUX_PANE']}
    return None


def command(terminal, text=None, key=None):
    """argv that types `text` literally, or presses `key` ('enter')."""
    cli, target = terminal['cli'], terminal['target']
    if terminal['kind'] == 'cmux':
        return [cli, 'send-key-panel', '--panel', target, key] if key else [cli, 'send-panel', '--panel', target, '--', text]
    if terminal['kind'] == 'tmux':
        return [cli, 'send-keys', '-t', target, 'Enter' if key == 'enter' else key] if key \
            else [cli, 'send-keys', '-t', target, '-l', text]
    raise ValueError('unknown terminal kind: ' + str(terminal.get('kind')))


def handoff_steps(host, carry=True, settle=1.5, start=6.0):
    """/clear (Claude Code) or /new (Codex), then the carry line once the fresh session has started."""
    steps = [('wait', settle), ('text', '/new' if host == 'codex' else '/clear'), ('key', 'enter')]
    if carry:
        steps += [('wait', start), ('text', CARRY_LINE), ('key', 'enter')]
    return steps


def run(terminal, steps, runner=None, sleep=time.sleep):
    """Perform the steps; returns the argv list that ran. Stops at the first failure."""
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, timeout=15).returncode)
    done = []
    for kind, value in steps:
        if kind == 'wait':
            sleep(value)
            continue
        argv = command(terminal, text=value) if kind == 'text' else command(terminal, key=value)
        code = runner(argv)
        done.append(argv)
        if code != 0:
            raise RuntimeError('typing failed (%s exit %s)' % (terminal['kind'], code))
    return done


def spawn(terminal, steps, log_path):
    """Run the steps in a detached process so the hook can return first."""
    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    with open(log_path, 'a', encoding='utf-8') as output:
        process = subprocess.Popen([sys.executable, '-m', 'harness.typist', json.dumps({'terminal': terminal, 'steps': steps})],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
    return process.pid


if __name__ == '__main__':
    request = json.loads(sys.argv[1])
    try:
        run(request['terminal'], [tuple(step) for step in request['steps']])
    except Exception as error:  # the log is the only place a detached typist can report to
        print(time.strftime('%Y-%m-%dT%H:%M:%S') + ' auto handoff typing error: ' + str(error)[:200], flush=True)
        sys.exit(1)
