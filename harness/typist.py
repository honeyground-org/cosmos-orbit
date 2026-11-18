"""Type into the terminal the session runs in: the one way to start a fresh context without the user.

A hook cannot clear its own conversation: Claude Code and Codex offer no hook output for that. A terminal
multiplexer can type into a pane, and the hook inherits the pane's identity from its environment. So when the
session runs in cmux or tmux, an automatic handoff is possible: Orbit types /clear and then one short line that
picks the carried-over request up in the fresh context. Anywhere else there is no typist and Orbit only tells.

The typing runs in a detached process: the hook must return first, so that the host is back at its input line
when the keys arrive. Two things were learned from the first real run (Claude Code 2.1.318 in cmux):
  - a prompt the hook blocked comes back into the input line, so '/clear' typed after it was sent as part of the
    old text. The input is emptied first with ctrl+u (a press on an empty input does nothing; ctrl+l and Escape
    left the text in place). Measured again 2026-11-18 (Claude Code 2.1.327 in cmux): a press removes the text of
    one row, the next press the line break before it, so a text of N lines takes 2N-1 presses, and one more for
    every row a long line wraps onto (1 line: 1, 2: 3, 3: 5, 4: 7; 359 characters on two rows: 2);
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


def command(terminal, text=None, key=None, force=False):
    """argv that types `text` literally, or presses `key` ('enter' or 'ctrl+u').

    cmux refuses input while an agent prompt holds a draft. `force` is for the one known draft a hook has just
    blocked, never for ordinary typing: callers must opt in step by step. tmux needs no such flag.
    """
    cli, target, kind = terminal['cli'], terminal['target'], terminal['kind']
    if kind not in KEYS:
        raise ValueError('unknown terminal kind: ' + str(kind))
    if key:
        how, name = KEYS[kind][key]
        if how == 'text':
            text = name
        else:
            return ([cli, 'send-key-panel', '--panel', target] + (['--force'] if force else []) + [name]
                    if kind == 'cmux' else [cli, 'send-keys', '-t', target, name])
    return ([cli, 'send-panel', '--panel', target] + (['--force'] if force else []) + ['--', text]
            if kind == 'cmux' else [cli, 'send-keys', '-t', target, '-l', text])


def clear_presses(text):
    """ctrl+u presses that empty an input holding `text`, with a margin of two: a row's text and then its line break
    (2N-1 for N lines), one more for each row a long line wraps onto (a row taken as 60 characters, so a wider pane
    is given more presses than it needs, never fewer)."""
    lines = (text or '').split('\n')
    return min(400, 2 * len(lines) - 1 + sum(len(line) // 60 for line in lines) + 2)


def clear_steps(presses):
    """The step that empties the input of the prompt a hook has just blocked and the host put back: `presses` ctrl+u
    (clear_presses of that prompt), of which all but the margin of two may be forced.

    cmux refuses outside input while the agent prompt holds a draft, and the restored prompt is one: it refused the
    first ctrl+u, so neither /exit (a route switch) nor /clear (the automatic handoff) was ever typed (hooks.log
    2026-11-16 and 11-18: "typing failed (cmux exit 1)"). How many presses are forced is not guessed: each press is
    sent unforced first, and only one cmux refuses — it says a draft is still there — is sent again with --force,
    as often as the blocked prompt itself can take. Once the input is empty the rest go through unforced and do
    nothing; a press refused after that budget is what the user began to type, and the typing stops there with
    their text in place. Forcing a fixed count failed both ways: one press left a request of several lines a draft,
    and its remaining presses were refused."""
    return [('clear', presses)] if presses > 0 else []


def handoff_steps(host, presses=3, settle=1.5, adopt_wait=25.0, new_wait=40.0, deliver_wait=60.0, expect='',
                  mod_grace=0):
    """Claude Code: empty the input, /clear, and the carry line once the task was taken over.

    Codex (0.166, measured 2026-11-05 in tmux): the blocked prompt does not come back into the input, so nothing is
    emptied; /new in a Git repository first asks where the conversation runs (Enter keeps the current worktree);
    the fresh session's hooks run only when its first prompt is submitted, so the carry line is typed once the screen
    shows a fresh conversation, and the takeover is confirmed after it. `expect`: the start of the request the hook
    just blocked; the pane is typed into only when its screen shows that block (see new_conversation). None: no
    request was blocked — the supervisor starts the handoff after reading this session's own pane as stopped with an
    empty input (supervision.tick), so that reading is the pane's confirmation."""
    if host == 'codex':
        return [('wait', settle), ('codex_new', {'wait': new_wait, 'expect': expect}), ('deliver', deliver_wait),
                ('adopted', adopt_wait)]
    # Claude Code with Orbit's mod loaded: the mod clears the session and submits the carry line through the host's
    # own API, nothing typed; the typist waits `mod_grace` for it to take the handoff, then only watches the takeover.
    grace = [('mod_grace', mod_grace)] if mod_grace else []
    return ([('wait', settle)] + grace + clear_steps(presses)
            + [('text', '/clear'), ('key', 'enter'),
               ('adopted', adopt_wait), ('text', CARRY_LINE), ('key', 'enter')])


BLOCKED = 'Blocked by hook'


def _signature(text):
    return ' '.join((text or '').split())


def shows_block(screen, expect):
    """True when the screen shows Codex's block of the request starting with `expect` (normalised spaces)."""
    flat = _signature(screen)
    return BLOCKED in flat and (not expect or _signature(expect)[:32] in flat)


def cmux_surfaces(tree):
    """The terminal surfaces of a `cmux --json --id-format both tree --all` answer, every window and workspace."""
    return [surface['id'] for window in (tree or {}).get('windows') or []
            for workspace in window.get('workspaces') or [] for pane in workspace.get('panes') or []
            for surface in pane.get('surfaces') or [] if surface.get('type') == 'terminal' and surface.get('id')]


def list_panes(terminal, run=None):
    """Every pane (tmux) or terminal surface (cmux: every window and workspace) the multiplexer knows."""
    if run is None:
        from .wake import _run as run
    if terminal['kind'] == 'tmux':
        code, out = run([terminal['cli'], 'list-panes', '-a', '-F', '#{pane_id}'])
        return out.split() if code == 0 else []
    code, out = run([terminal['cli'], '--json', '--id-format', 'both', 'tree', '--all'])
    try:
        return cmux_surfaces(json.loads(out)) if code == 0 else []
    except ValueError:
        return []


def read_pane(terminal, pane, run=None):
    """The whole visible screen of `pane`, in both terminals; '' when it cannot be read."""
    from .wake import screen_command
    if run is None:
        from .wake import _run as run
    code, out = run(screen_command(dict(terminal, target=pane)))
    return out if code == 0 else ''


def find_pane(terminal, expect, lister=None, reader=None):
    """The one pane (tmux) or terminal surface (cmux) whose screen shows this block, or None (none, or more than one).

    Codex can run hooks in its app-server daemon, whose environment is the pane the first Codex of that home was
    started in (measured 2026-11-05: the hook said %260, a pane closed minutes before; Codex ran in %271). The
    screen is the truth: it shows the request the hook just blocked. cmux names its surfaces by UUID, the same id
    send-panel and read-screen take (CMUX_PANEL_ID is the surface id)."""
    if terminal['kind'] not in ('tmux', 'cmux'):
        return None
    lister = lister or (lambda: list_panes(terminal))
    reader = reader or (lambda pane: read_pane(terminal, pane))
    found = [pane for pane in lister() if shows_block(reader(pane), expect)]
    return dict(terminal, target=found[0]) if len(found) == 1 else None


def new_conversation(terminal, wait, press, screen, sleep=time.sleep, clock=time.monotonic, expect='', find=None):
    """Codex: confirm the pane, /new at an empty input, Enter on the 'where' question, until the screen is a fresh
    conversation. Returns the terminal typed into. Raises when the pane cannot be confirmed or the conversation is
    not fresh, so neither /new nor the carry line reaches another session's pane or the old conversation."""
    from .wake import CODEX_WHERE, PASTE_GAP, codex_fresh, read_state
    deadline = clock() + wait
    before, answered = None, 0
    if expect is not None and not shows_block(screen(terminal), expect):  # None: the supervisor confirmed the pane
        found = (find or find_pane)(terminal, expect)
        if not found:
            raise RuntimeError('the pane %s does not show the blocked request and no other pane does; nothing was '
                               'typed' % terminal['target'])
        terminal = found
    while True:
        text = screen(terminal)
        if before is None:
            if read_state(text) == 'empty':
                before = text
                press(command(terminal, text='/new'))
                sleep(PASTE_GAP)
                press(command(terminal, key='enter'))
                sleep(1.0)
                continue
        elif CODEX_WHERE in text and answered < 2:
            press(command(terminal, key='enter'))         # 1. Use current Git worktree (selected)
            answered += 1
            sleep(1.0)
            continue
        elif text != before and codex_fresh(text) and BLOCKED not in _signature(text):
            return terminal                               # a short old conversation shows its logo too, and the block
        if clock() >= deadline:
            raise RuntimeError('no fresh Codex conversation within %ds (%s); the carry line was not typed'
                               % (wait, 'the input never emptied' if before is None else 'after /new'))
        sleep(0.5)


def carry_steps(settle=2.0, wait=60.0):
    """The carry line alone, for a fresh session that took the task over while no typist waited for it (the
    automatic /clear had failed and the user cleared by hand): typed once its input line reads empty."""
    return [('wait', settle), ('deliver', wait)]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True                                       # it exists but is not ours to signal
    return True


SAID = ['']   # what the last terminal command that failed said: the reason a failed typing reports


def _terminal_run(argv):
    """Run one terminal command, leaving its own error in the detached typist's log before returning its status."""
    SAID[0] = ''
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        SAID[0] = 'could not run: ' + str(error)[:200]
        print('typing command could not run: ' + str(error)[:500], flush=True)
        return 1
    if done.returncode:                       # kept for the error of the step that fails: a press refused over a draft
        detail = (done.stderr or done.stdout or '').strip()   # is an answer the clear step expects, not a failure
        SAID[0] = ' '.join(detail.split())[-300:] or 'no error output'
    return done.returncode


def _failed(terminal, code):
    """The error of a typing command that failed, with the terminal's own words when it gave any."""
    said, SAID[0] = SAID[0], ''
    return RuntimeError('typing failed (%s exit %s%s)' % (terminal['kind'], code, ': ' + said if said else ''))


def run(terminal, steps, runner=None, sleep=time.sleep, adopted=None, clock=time.monotonic, deliver=None, screen=None,
        find=None, claim=None, alive=alive):
    """Perform the steps; returns the argv list that ran. Stops at the first failure.

    An 'adopted' step waits until `adopted()` is true: the fresh session took the task over. Without it, or
    when the wait runs out, the typing stops, so the carry line never lands in the old session. A 'deliver'
    step types the carry line once the screen shows an empty input line (wake.deliver), never over the user's
    own typing. A 'clear' step empties the input of the prompt a hook just blocked (clear_steps). A text/key step
    may be {'text'|'key': value, 'force': True}; force is a cmux-only exception, never inherited by a later step."""
    runner = runner or _terminal_run
    done = []

    def press(argv):
        done.append(argv)
        code = runner(argv)
        if code != 0:
            raise _failed(terminal, code)

    watch_only = False
    for kind, value in steps:
        if kind == 'wait':
            sleep(value)
            continue
        if kind == 'clear':
            if watch_only:
                continue
            budget = max(0, value - 2) if terminal['kind'] == 'cmux' else 0   # the presses the blocked prompt can take
            for _ in range(value):
                argv = command(terminal, key='ctrl+u')
                done.append(argv)
                code = runner(argv)
                if code != 0 and budget > 0:              # cmux: a draft is still there
                    budget -= 1
                    argv = command(terminal, key='ctrl+u', force=True)
                    done.append(argv)
                    code = runner(argv)
                if code != 0:
                    raise _failed(terminal, code)
            continue
        if kind == 'mod_grace':
            who = claim(value) if claim else 'typist'
            if who == 'mod':
                watch_only = True                         # the mod clears and submits; only the takeover is watched
            elif who != 'typist':
                raise RuntimeError('the automatic handoff is no longer waiting; nothing was typed')
            elif not terminal:
                raise RuntimeError('the mod did not take the handoff and there is no terminal to type into')
            continue
        if watch_only and kind != 'adopted':
            continue
        if kind == 'codex_new':
            if screen is None:
                from .wake import _run, screen_command
                screen = lambda where: _run(screen_command(where))[1]  # noqa: E731
            options = value if isinstance(value, dict) else {'wait': value}
            terminal = new_conversation(terminal, options.get('wait', 40.0), press, screen, sleep=sleep, clock=clock,
                                        expect=options.get('expect', ''), find=find)
            continue
        if kind == 'deliver':
            if deliver is None:
                from .wake import deliver
            outcome, typed = deliver(terminal, wait=value, line=CARRY_LINE)
            done.extend(typed)
            if outcome != 'woken':
                raise RuntimeError('the carry line was not delivered (%s)' % outcome)
            continue
        if kind == 'gone':
            pid, wait = value                             # the host quit by /exit: type into the shell only after
            deadline = clock() + wait
            while pid and alive(pid):
                if clock() >= deadline:
                    raise RuntimeError('the session in the pane did not quit within %ds; nothing more was typed' % wait)
                sleep(0.25)
            if not pid:
                sleep(3.0)                                # its pid was not found: give it time to quit
            continue
        if kind == 'adopted':
            deadline = clock() + value
            while not (adopted and adopted()):
                if clock() >= deadline:
                    raise RuntimeError('the fresh session did not take the task over within %ds; the carry line '
                                       'was not typed' % value)
                sleep(0.5)
            continue
        if kind in ('text', 'key') and isinstance(value, dict):
            force = bool(value.get('force'))
            value = value[kind]
        else:
            force = False
        argv = command(terminal, text=value, force=force) if kind == 'text' else command(terminal, key=value, force=force)
        code = runner(argv)
        done.append(argv)
        if code != 0:
            raise _failed(terminal, code)
    return done


def spawn(terminal, steps, log_path, home=None, task=None, report=None):
    """Run the steps in a detached process so the hook can return first. `report` ({'session', 'switch'}): a route
    switch, whose outcome the typist leaves for that session's next prompt (routes.switch_outcome)."""
    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    request = {'terminal': terminal, 'steps': steps, 'home': str(home) if home else None, 'task': task,
               'report': report}
    with open(log_path, 'a', encoding='utf-8') as output:
        process = subprocess.Popen([sys.executable, '-m', 'harness.typist', json.dumps(request)],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
    return process.pid


def _main(request):
    # A route switch from a session with no task types with neither (2026-11-17: `claim` was left unbound there, and
    # the detached typist died before /exit, so the pane never switched).
    store = adopted = claim = None
    report = request.get('report') or {}
    if request.get('home') and (request.get('task') or report):
        from .store import Store
        store = Store(request['home'])

    def outcome(error=None):
        # A route switch: the session left reads this at its next prompt. A hook's block reason is the one place the
        # user sees; the log alone left a refused switch looking like nothing had happened (2026-11-18).
        if store is not None and report.get('session'):
            try:
                from .routes import switch_outcome
                switch_outcome(store, report['session'], report.get('switch'), error)
            except Exception as failure:  # noqa: BLE001 - the report never hides the typing error itself
                print('route switch outcome not recorded: ' + repr(failure)[:200], flush=True)

    if store is not None and request.get('task'):
        def adopted():
            # Taken over by the fresh session's own hook, or handed to the session that appeared in this pane.
            from .continuity import adopt_in_panel
            return adopt_in_panel(store, request['task'], (request['terminal'] or {}).get('target'))

        def claim(wait):
            # The mod says it takes the handoff (cosmos-orbit handoff-claim); after `wait` the typist takes it.
            from .continuity import typist_claim
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                state = store.get(request['task'])['data'].get('handoff_state') or {}
                if state.get('claimed_by') == 'mod' or state.get('state') == 'adopted':
                    return 'mod'
                time.sleep(0.25)
            return typist_claim(store, request['task'])
    try:
        run(request['terminal'], [tuple(step) for step in request['steps']], adopted=adopted, claim=claim)
        outcome()
        return 0
    except Exception as error:  # the log, and for a route switch the session's next prompt
        print(time.strftime('%Y-%m-%dT%H:%M:%S') + ' auto handoff typing error: ' + str(error)[:300], flush=True)
        outcome(str(error))
        if store is not None and request.get('task'):
            from .continuity import auto_failed
            auto_failed(store, request['task'], str(error))
        return 1


if __name__ == '__main__':
    sys.exit(_main(json.loads(sys.argv[1])))
