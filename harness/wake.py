"""Wake a session when another session sends it a message: the sender's words arrive without the user relaying them.

A message channel between agent sessions (a mailbox read by a hook, say) delivers only when the receiving host runs a
hook: when the user submits a prompt, after a tool call, at the end of a turn. A session waiting at its input line
runs none of them, so a message sat unread until the user typed something like "check it" into that session.

Orbit already knows the terminal each session runs in (its hooks record it, §note). A sender calls
`cosmos-orbit wake --session ID` (or `--surface ID`) right after delivering a message, and a detached waker looks at
that terminal's screen:
  - the host is working ("esc to interrupt" on screen): with `--when-busy skip` it stops, because the channel delivers
    at the next tool call or turn end; with `--when-busy wait` (the default) it waits until the turn ends;
  - the input line is empty: it types one fixed ASCII line and Enter. The prompt hook of that session then delivers
    the message, and Orbit marks the line as typed by Orbit (never as the user's words);
  - the user is typing there: nothing is erased, ever. It notifies once and waits until the input is empty;
  - the screen is not one it can read (a dialog, a shell): nothing is typed; it notifies and keeps looking.
Outside cmux and tmux nothing can type, so it only notifies. Typing follows the same switch as the automatic handoff
(`cosmos-orbit context auto|tell`); with `tell` it only notifies.

Measured on screens of Claude Code 2.1 and Codex (2026-11-04, cmux read-screen, plain text without colours):
  - busy: both show "esc to interrupt" on the footer or the status line; idle screens do not;
  - Claude Code: the input is the line starting with U+276F between two rule lines; an empty input is that sign alone,
    or, in a fresh session, a placeholder shaped `Try "<example>"` (typed text arrives after the same no-break space);
  - Codex: the input starts with U+203A; an empty input shows a dimmed placeholder that reads like typed text without
    colours, so only the placeholders found in its binary count as empty ("Ask Codex to do anything", "Ask a follow-up
    question"). Any other text there is treated as the user's: the safe direction.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

# The line typed into the receiving session. Plain ASCII, fixed: the message itself travels in the sender's channel.
WAKE_LINE = 'Orbit: a message from another session arrived. Read it and fold it into the task; reply only if a reply is needed.'
BUSY = re.compile(r'esc to interrupt', re.I)
CLAUDE_INPUT = '❯'
CODEX_INPUT = '›'
RULE = '─'
CODEX_PLACEHOLDERS = ('Ask Codex to do anything', 'Ask a follow-up question')
# Claude Code shows `Try "<example>"` in an empty input of a fresh session (2.1.318 builds it from eight examples,
# some naming a file of the project); a user's text arrives after the same no-break space, so the shape decides.
CLAUDE_PLACEHOLDER = re.compile(r'Try "[^"\n]{1,160}"')
SCREEN_LINES = 14
POLL = 3.0               # seconds between looks at the screen
WAIT = 1800.0            # seconds a waker keeps looking before it gives up (the message stays in the channel)
MAX_PER_HOUR = 20        # typed wakes per terminal: two agents acknowledging each other cannot loop for ever
MIN_GAP = 20.0           # seconds between two typed wakes in one terminal
WHEN_BUSY = ('wait', 'skip')
PASTE_GAP = 1.0          # seconds between typing a line and pressing Enter (Codex reads a faster Enter as pasted)
# Codex after /new in a Git repository asks where the conversation runs; option 1 (the current worktree) is selected.
CODEX_WHERE = 'Where should the new conversation run?'
BRAILLE = re.compile('[\u2800-\u28ff]{8,}')


def codex_fresh(screen):
    """True when the screen is a Codex conversation with nothing in it yet: its logo above an empty input. The
    logo is drawn only before the first message (measured 2026-11-05, 0.166), so the old conversation is gone."""
    return (CODEX_WHERE not in (screen or '') and read_state(screen) == 'empty'
            and sum(1 for line in (screen or '').splitlines() if BRAILLE.search(line)) >= 3)


def _blank(text):
    return not (text or '').replace('\xa0', ' ').strip()


def _is_rule(line):
    stripped = line.strip()
    return len(stripped) >= 10 and stripped.startswith(RULE) and stripped.endswith(RULE)


def read_state(screen):
    """'busy' | 'empty' | 'typing' | 'unknown' from the bottom of a terminal screen (plain text)."""
    lines = [line.rstrip() for line in (screen or '').splitlines()]
    while lines and _blank(lines[-1]):
        lines.pop()                   # tmux returns the full pane height: the empty rows below the cursor
    lines = lines[-SCREEN_LINES:]
    if any(BUSY.search(line) for line in lines):
        return 'busy'
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].lstrip()
        if stripped.startswith(CLAUDE_INPUT) and i > 0 and _is_rule(lines[i - 1]):
            body = [stripped[1:]]
            for later in lines[i + 1:]:
                if _is_rule(later):
                    text = ' '.join(part.replace('\xa0', ' ').strip() for part in body).strip()
                    return 'empty' if not text or CLAUDE_PLACEHOLDER.fullmatch(text) else 'typing'
                body.append(later)
            return 'unknown'  # no closing rule: not the input box
        if stripped.startswith(CODEX_INPUT):
            body = [stripped[1:]]
            for later in lines[i + 1:]:
                if _blank(later):
                    break
                body.append(later)
            text = ' '.join(part.replace('\xa0', ' ').strip() for part in body).strip()
            return 'empty' if not text or text in CODEX_PLACEHOLDERS else 'typing'
    return 'unknown'


def note(store, session, host, event, terminal):
    """Remember the terminal a session runs in (every hook event); forget it when the session ends."""
    if not session:
        return
    with store.db:
        if event == 'session-end':
            store.db.execute('DELETE FROM terminals WHERE session=?', (session,))
            return
        terminal = terminal or {}
        surface = os.environ.get('CMUX_SURFACE_ID') if terminal.get('kind') == 'cmux' else None
        store.db.execute('INSERT OR REPLACE INTO terminals VALUES(?,?,?,?,?,?,?,?)',
                         (session, host, terminal.get('kind'), terminal.get('cli'), terminal.get('target'), surface,
                          'busy' if event == 'prompt' else 'idle', _now()))


def _now():
    from .store import now
    return now()


def resolve(store, session=None, surface=None):
    """(terminal or None, reason). A pane holds one session at a time: a session that a newer one replaced in the
    same pane is not woken there (the pane now belongs to the newer one)."""
    if session:
        row = store.db.execute('SELECT * FROM terminals WHERE session=?', (session,)).fetchone()
        if not row:
            return None, 'unknown_session'
        row = dict(row)
        if not row['kind']:
            return None, 'no_terminal'
        newer = store.db.execute('SELECT session FROM terminals WHERE target=? AND at>? AND session!=?',
                                 (row['target'], row['at'], session)).fetchone()
        if newer:
            return None, 'replaced'
        return {'kind': row['kind'], 'cli': row['cli'], 'target': row['target']}, ''
    if surface:
        row = store.db.execute('SELECT * FROM terminals WHERE (lower(target)=lower(?) OR lower(surface)=lower(?)) '
                               'AND kind IS NOT NULL ORDER BY at DESC LIMIT 1', (surface, surface)).fetchone()
        if row:
            return {'kind': row['kind'], 'cli': row['cli'], 'target': row['target']}, ''
        from .typist import detect
        here = detect()
        if here and here['kind'] == 'cmux':  # a cmux surface Orbit has not seen yet: the same cli reaches it
            return dict(here, target=surface), ''
        return None, 'unknown_surface'
    return None, 'no_target'


def screen_command(terminal):
    if terminal['kind'] == 'cmux':
        return [terminal['cli'], 'read-screen', '--surface', terminal['target'], '--lines', str(SCREEN_LINES)]
    return [terminal['cli'], 'capture-pane', '-p', '-t', terminal['target']]


def notify_command(terminal, text):
    if terminal['kind'] == 'cmux':
        return [terminal['cli'], 'notify', '--title', 'Orbit', '--body', text, '--surface', terminal['target']]
    return [terminal['cli'], 'display-message', '-t', terminal['target'], text]


def _run(argv):
    try:
        done = subprocess.run(argv, capture_output=True, timeout=15)
        return done.returncode, done.stdout.decode('utf-8', 'replace')
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)


def deliver(terminal, sender='', when_busy='wait', wait=WAIT, runner=None, sleep=time.sleep, clock=time.monotonic,
            allowed=lambda: True, line=WAKE_LINE):
    """Look at the screen until the line can be typed, then type it once. Returns (outcome, typed argv list).

    `allowed()` is asked right before typing (the hourly bound and the gap between wakes). `line` is the wake line,
    or the carry line a fresh session needs when no typist is waiting for it (continuity.resume_carried)."""
    from . import typist
    runner = runner or _run
    deadline = clock() + wait
    notified = False
    who = (' from ' + sender) if sender else ''
    while True:
        code, screen = runner(screen_command(terminal))
        state = read_state(screen) if code == 0 else 'unreadable'
        if state == 'busy' and when_busy == 'skip':
            return 'busy_delivers_itself', []
        if state == 'empty':
            if not allowed():
                runner(notify_command(terminal, 'A message%s is waiting (wake limit reached).' % who))
                return 'throttled', []
            typed = [typist.command(terminal, text=line)]
            if runner(typed[0])[0] != 0:
                return 'typing_failed', typed
            for _ in range(2):
                # Codex 0.166 takes text and an Enter that follows at once as a paste and keeps the line in its
                # input (measured 2026-11-05); Enter after a pause submits it. One more Enter if it still sits there.
                sleep(PASTE_GAP)
                enter = typist.command(terminal, key='enter')
                typed.append(enter)
                if runner(enter)[0] != 0:
                    return 'typing_failed', typed
                sleep(1.5)
                code, after = runner(screen_command(terminal))
                if not (code == 0 and line[:40] in after and read_state(after) == 'typing'):
                    return 'woken', typed
            return 'not_submitted', typed
        if state in ('typing', 'unknown', 'unreadable') and not notified:
            text = ('A message%s is waiting; it is delivered when your input line is empty.' % who if state == 'typing'
                    else 'A message%s is waiting for this session.' % who)
            runner(notify_command(terminal, text))
            notified = True
        if clock() >= deadline:
            return 'expired_' + state, []
        sleep(POLL)


# ── bounds: one waker per terminal, a gap between wakes, an hourly limit ───────────────────────────────────────────

def _slug(target):
    return re.sub(r'[^A-Za-z0-9_.-]', '_', target)[:120]


def _wake_dir(home):
    path = Path(home) / 'wake'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def claim_waker(home, target, pid=None):
    """True when no live waker watches this terminal: one waker delivers every message waiting there."""
    path = _wake_dir(home) / (_slug(target) + '.pid')
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                owner = int(path.read_text().strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if owner and owner != (pid or os.getpid()) and _alive(owner):
                return False
            try:
                path.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(fd, 'w') as handle:
            handle.write(str(pid or os.getpid()))
        return True
    return False


def release_waker(home, target):
    path = _wake_dir(home) / (_slug(target) + '.pid')
    try:
        if int(path.read_text().strip() or 0) == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def _history_path(home):
    return _wake_dir(home) / 'history.json'


def allowed(home, target, at=None, gap=MIN_GAP, per_hour=MAX_PER_HOUR):
    """(True, wait seconds) while this terminal has had fewer than `per_hour` typed wakes in the last hour."""
    at = time.time() if at is None else at
    try:
        stamps = json.loads(_history_path(home).read_text()).get(target, [])
    except (OSError, ValueError):
        stamps = []
    recent = [s for s in stamps if at - s < 3600]
    if len(recent) >= per_hour:
        return False, 0.0
    last = max(recent) if recent else 0
    return True, max(0.0, gap - (at - last))


def record_wake(home, target, at=None):
    path = _history_path(home)
    at = time.time() if at is None else at
    try:
        history = json.loads(path.read_text())
    except (OSError, ValueError):
        history = {}
    history[target] = [s for s in history.get(target, []) if at - s < 3600] + [at]
    for key in [k for k, v in history.items() if not v or at - max(v) > 3600]:
        history.pop(key, None)
    tmp = path.with_name(path.name + '.%d.tmp' % os.getpid())
    tmp.write_text(json.dumps(history))
    os.replace(str(tmp), str(path))


# ── the command ────────────────────────────────────────────────────────────────────────────────────────────────────

def wake(store, sessions=(), surfaces=(), sender='', when_busy='wait', spawn=None, mode=None):
    """Start one detached waker per receiving terminal. Returns one row per target; never types itself."""
    from .continuity import handoff_mode
    if when_busy not in WHEN_BUSY:
        raise ValueError('--when-busy is wait or skip')
    mode = mode or handoff_mode(store)
    rows = []
    seen = set()
    targets = [('session', s) for s in sessions if s] + [('surface', s) for s in surfaces if s]
    for kind, value in targets:
        terminal, reason = resolve(store, **{kind: value})
        row = {kind: value}
        if not terminal:
            rows.append(dict(row, outcome=reason))
            continue
        if terminal['target'] in seen:
            rows.append(dict(row, outcome='same_terminal'))
            continue
        seen.add(terminal['target'])
        if mode != 'auto':
            _run(notify_command(terminal, 'A message%s is waiting for this session.' % ((' from ' + sender) if sender else '')))
            rows.append(dict(row, outcome='notified', why='handoff mode tell: Orbit does not type'))
            continue
        pid = (spawn or _spawn)(store.home, terminal, sender, when_busy)
        rows.append(dict(row, outcome='waker_started' if pid else 'waker_running', terminal=terminal['kind']))
    _log(store.home, 'wake request from %s: %s' % (sender or '?', json.dumps(rows, ensure_ascii=False)))
    return {'wake': rows, 'mode': mode}


def _spawn(home, terminal, sender, when_busy):
    """A detached waker, unless one already watches this terminal (it delivers everything waiting there)."""
    if not claim_waker(home, terminal['target']):
        return None
    package_root = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    request = {'home': str(home), 'terminal': terminal, 'sender': sender, 'when_busy': when_busy}
    with open(Path(home) / 'hooks.log', 'a', encoding='utf-8') as output:
        process = subprocess.Popen([sys.executable, '-m', 'harness.wake', json.dumps(request)],
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
    path = _wake_dir(home) / (_slug(terminal['target']) + '.pid')
    path.write_text(str(process.pid))  # the waker owns the claim from here on
    return process.pid


def record_outcome(home, terminal, sender, outcome, at=None):
    """One line per waker run in <home>/wake/log.jsonl (the last 200), and an event in the receiving session's task."""
    row = {'at': at or _now(), 'terminal': terminal['kind'], 'target': terminal['target'], 'sender': sender, 'outcome': outcome}
    path = _wake_dir(home) / 'log.jsonl'
    try:
        lines = path.read_text(encoding='utf-8').splitlines()[-199:] if path.exists() else []
    except OSError:
        lines = []
    tmp = path.with_name(path.name + '.%d.tmp' % os.getpid())
    tmp.write_text('\n'.join(lines + [json.dumps(row, ensure_ascii=False)]) + '\n', encoding='utf-8')
    os.replace(str(tmp), str(path))
    from .store import Store
    store = Store(home)
    try:
        found = store.db.execute('SELECT session FROM terminals WHERE target=? ORDER BY at DESC LIMIT 1',
                                 (terminal['target'],)).fetchone()
        bound = store.session_task(found['session']) if found else None
        if bound:
            with store.db:
                store._event(bound['task_id'], 'message_wake', {'outcome': outcome, 'sender': sender,
                                                                 'terminal': terminal['kind'], 'layer': 'context'})
    finally:
        store.close()
    return row


def summary(store, recent=5, day=86400):
    """What the observer shows: panes a wake can reach (seen in the last day) and the latest waker runs."""
    from datetime import datetime, timezone
    rows = [dict(r) for r in store.db.execute('SELECT target, kind, host, at FROM terminals WHERE kind IS NOT NULL')]
    current = time.time()

    def fresh(stamp):
        try:
            return current - datetime.fromisoformat(stamp).astimezone(timezone.utc).timestamp() < day
        except (TypeError, ValueError):
            return False
    panes = {r['target'] for r in rows if fresh(r['at'])}
    path = Path(store.home) / 'wake' / 'log.jsonl'
    try:
        log = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()[-recent:] if line.strip()]
    except (OSError, ValueError):
        log = []
    return {'reachable': len(panes), 'recent': list(reversed(log))}


def _log(home, message):
    try:
        with open(Path(home) / 'hooks.log', 'a', encoding='utf-8') as out:
            out.write(time.strftime('%Y-%m-%dT%H:%M:%S') + ' ' + message[:600] + '\n')
    except OSError:
        pass


def _main(request):
    home, terminal = request['home'], request['terminal']

    def ok():
        permitted, pause = allowed(home, terminal['target'])
        if permitted and pause:
            time.sleep(pause)
        return permitted
    try:
        outcome, typed = deliver(terminal, request.get('sender', ''), request.get('when_busy', 'wait'), allowed=ok)
        if outcome in ('woken', 'not_submitted'):
            record_wake(home, terminal['target'])
        _log(home, 'wake %s %s: %s' % (terminal['kind'], terminal['target'], outcome))
        record_outcome(home, terminal, request.get('sender', ''), outcome)
        return 0 if outcome in ('woken', 'busy_delivers_itself') else 1
    except Exception as error:  # the log is the only place a detached waker can report to
        _log(home, 'wake error: ' + repr(error)[:200])
        return 1
    finally:
        release_waker(home, terminal['target'])


if __name__ == '__main__':
    sys.exit(_main(json.loads(sys.argv[1])))
