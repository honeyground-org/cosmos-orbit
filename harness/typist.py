"""Type into the terminal the session runs in: the one way to start a fresh context without the user.

A hook cannot clear its own conversation: Claude Code and Codex offer no hook output for that. A terminal
multiplexer can type into a pane, and the hook inherits the pane's identity from its environment. So when the
session runs in cmux or tmux, an automatic handoff is possible: Orbit types /clear and then one short line that
picks the carried-over request up in the fresh context. Anywhere else there is no typist and Orbit only tells.

The typing runs in a detached process: the hook must return first, so that the host is back at its input line
when the keys arrive. Two things were learned from the first real run (Claude Code 2.1.318 in cmux):
  - a prompt the hook blocked comes back into the input line, so '/clear' typed after it was sent as part of the
    old text. The input is emptied first with ctrl+u, once per line (measured: one press removes one line, a
    press on an empty input does nothing; ctrl+l and Escape left the text in place);
  - the carry line is typed only once the fresh session has taken the task over. If its own hook did not take it
    (seen 2026-11-04: it started in another folder than the task's and skipped it), the typist hands the task to
    the session that appeared in this pane after the /clear. If none did within the wait, nothing more is typed
    and the handoff falls back to telling the user.
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


# How each key reaches the program in the pane. cmux's send-key-panel delivers Enter, but no Ctrl combination
# reached Claude Code (measured: ctrl+u, ctrl+a, ctrl+k and ctrl+c all left the input as it was), while the raw
# control byte typed as text does: ctrl+u is 0x15. tmux's send-keys C-u reaches it as it is.
KEYS = {'cmux': {'enter': ('key', 'enter'), 'ctrl+u': ('text', '\x15')},
        'tmux': {'enter': ('key', 'Enter'), 'ctrl+u': ('key', 'C-u')}}


def command(terminal, text=None, key=None):
    """argv that types `text` literally, or presses `key` ('enter' or 'ctrl+u')."""
    cli, target, kind = terminal['cli'], terminal['target'], terminal['kind']
    if kind not in KEYS:
        raise ValueError('unknown terminal kind: ' + str(kind))
    if key:
        how, name = KEYS[kind][key]
        if how == 'text':
            text = name
        else:
            return [cli, 'send-key-panel', '--panel', target, name] if kind == 'cmux' else [cli, 'send-keys', '-t', target, name]
    return [cli, 'send-panel', '--panel', target, '--', text] if kind == 'cmux' else [cli, 'send-keys', '-t', target, '-l', text]


def clear_presses(text):
    """ctrl+u presses that empty an input holding `text`: one per line, wrapped lines counted, a margin of two."""
    lines = (text or '').split('\n')
    return min(400, len(lines) + sum(len(line) // 60 for line in lines) + 2)


def handoff_steps(host, presses=3, settle=1.5, adopt_wait=25.0):
    """Empty the input, /clear (Claude Code) or /new (Codex), and the carry line once the task was taken over."""
    return ([('wait', settle)] + [('key', 'ctrl+u')] * presses
            + [('text', '/new' if host == 'codex' else '/clear'), ('key', 'enter'),
               ('adopted', adopt_wait), ('text', CARRY_LINE), ('key', 'enter')])


def carry_steps(settle=2.0, wait=60.0):
    """The carry line alone, for a fresh session that took the task over while no typist waited for it (the
    automatic /clear had failed and the user cleared by hand): typed once its input line reads empty."""
    return [('wait', settle), ('deliver', wait)]


def run(terminal, steps, runner=None, sleep=time.sleep, adopted=None, clock=time.monotonic, deliver=None):
    """Perform the steps; returns the argv list that ran. Stops at the first failure.

    An 'adopted' step waits until `adopted()` is true: the fresh session took the task over. Without it, or
    when the wait runs out, the typing stops, so the carry line never lands in the old session. A 'deliver'
    step types the carry line once the screen shows an empty input line (wake.deliver), never over the user's
    own typing."""
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, timeout=15).returncode)
    done = []
    for kind, value in steps:
        if kind == 'wait':
            sleep(value)
            continue
        if kind == 'deliver':
            if deliver is None:
                from .wake import deliver
            outcome, typed = deliver(terminal, wait=value, line=CARRY_LINE)
            done.extend(typed)
            if outcome != 'woken':
                raise RuntimeError('the carry line was not delivered (%s)' % outcome)
            continue
        if kind == 'adopted':
            deadline = clock() + value
            while not (adopted and adopted()):
                if clock() >= deadline:
                    raise RuntimeError('the fresh session did not take the task over within %ds; the carry line '
                                       'was not typed' % value)
                sleep(0.5)
            continue
        argv = command(terminal, text=value) if kind == 'text' else command(terminal, key=value)
        code = runner(argv)
        done.append(argv)
        if code != 0:
            raise RuntimeError('typing failed (%s exit %s)' % (terminal['kind'], code))
    return done


def spawn(terminal, steps, log_path, home=None, task=None):
    """Run the steps in a detached process so the hook can return first."""
    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    request = {'terminal': terminal, 'steps': steps, 'home': str(home) if home else None, 'task': task}
    with open(log_path, 'a', encoding='utf-8') as output:
        process = subprocess.Popen([sys.executable, '-m', 'harness.typist', json.dumps(request)],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
    return process.pid


def _main(request):
    store = adopted = None
    if request.get('home') and request.get('task'):
        from .store import Store
        store = Store(request['home'])

        def adopted():
            # Taken over by the fresh session's own hook, or handed to the session that appeared in this pane.
            from .continuity import adopt_in_panel
            return adopt_in_panel(store, request['task'], request['terminal']['target'])
    try:
        run(request['terminal'], [tuple(step) for step in request['steps']], adopted=adopted)
        return 0
    except Exception as error:  # the log is the only place a detached typist can report to
        print(time.strftime('%Y-%m-%dT%H:%M:%S') + ' auto handoff typing error: ' + str(error)[:200], flush=True)
        if store is not None:
            from .continuity import auto_failed
            auto_failed(store, request['task'], str(error))
        return 1


if __name__ == '__main__':
    sys.exit(_main(json.loads(sys.argv[1])))
