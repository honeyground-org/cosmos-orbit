#!/usr/bin/env python3
"""After a restart: the sessions it cut off, when each last worked, and the command that resumes it. Model-free,
with controls.

① a session whose last activity is older than this boot is listed as cut off; one active since is not (control)
② open is told from closed by the running hosts: its own ID, or its pane running a host while it is the pane's
   newest session; a pane whose shell alone runs is not a host (control)
③ a session a newer one followed in the same pane is not listed (it is with --followed)
④ the resume command runs where the session started (its transcript's first folder), not where it cd'd to;
   without a transcript the task's folder is the fallback (control); Codex resumes with `codex resume`
⑤ last activity is the newer of the last hook and the transcript: a turn the restart cut mid-way still counts
⑥ the first session to start after a restart is told once per boot, through the real SessionStart hook; a second
   start is not told again, a new boot is, and a boot that cut nothing off says nothing (controls)
⑦ the command line: text with the commands, and --json
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH', 'CLAUDE_CODE_SESSION_ID',
             'CODEX_THREAD_ID', 'TMUX', 'TMUX_PANE', 'CMUX_SURFACE_ID', 'CMUX_PANEL_ID', 'CMUX_WORKSPACE_ID',
             'CLAUDECODE'):
    os.environ.pop(name, None)

from harness import attach  # noqa: E402
from harness.store import Store  # noqa: E402

try:
    from harness import restore  # noqa: E402
except ImportError:                # before the change: every check below fails instead of crashing
    restore = None

FAILED = []
NOW = time.time()              # the notice reads the real clock
BOOT = NOW - 3600                  # the machine came back an hour ago


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', '-C', str(root), 'init', '-q'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '--allow-empty',
                    '-m', 'init'], check=True)
    return root


def claude_transcript(path, started, moved, request):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{'type': 'user', 'cwd': started, 'message': {'role': 'user', 'content': 'first request'}},
             {'type': 'user', 'cwd': moved, 'message': {'role': 'user', 'content': [{'type': 'tool_result'}]}},
             {'type': 'user', 'cwd': moved, 'isMeta': True, 'message': {'role': 'user', 'content': 'meta'}},
             {'type': 'user', 'cwd': moved, 'message': {'role': 'user', 'content': request}},
             {'type': 'user', 'cwd': moved, 'message': {'role': 'user', 'content': '<command-name>/x</command-name>'}}]
    path.write_text(''.join(json.dumps(x) + '\n' for x in lines), encoding='utf-8')
    return str(path)


def codex_rollout(path, started, request):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [{'type': 'session_meta', 'payload': {'id': 'x', 'cwd': started, 'source': 'cli'}},
             {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': request}}]
    path.write_text(''.join(json.dumps(x) + '\n' for x in lines), encoding='utf-8')
    return str(path)


def at(store, session, epoch, transcript=None):
    """The session's last hook at `epoch`; its transcript written then too."""
    stamp = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()
    with store.db:
        store.db.execute('UPDATE terminals SET at=? WHERE session=?', (stamp, session))
    if transcript:
        os.utime(transcript, (epoch, epoch))


def pane(store, session, target):
    with store.db:
        store.db.execute("UPDATE terminals SET kind='cmux', target=? WHERE session=?", (target, session))


def rows(found):
    return {r['session']: r for r in found['sessions']}


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-restore-'))
    home = base / 'user'
    os.environ['HOME'] = str(home)                       # the hosts' usual transcript places live here
    store = Store(base / 'home')
    if restore is None:
        check('harness.restore exists', False, 'module missing')
        print('\nFAIL ' + ', '.join(FAILED))
        return 1
    started = make_repo(base, 'launch')
    moved = make_repo(base, 'moved')
    attach.set_switch(store, 'on', moved)

    def hook(event, session, host='claude', **payload):
        return attach.handle(store, event, dict(payload, cwd=str(moved), session_id=session), host)

    tr_a = claude_transcript(home / '.claude' / 'projects' / '-launch' / 'A.jsonl', str(started), str(moved),
                             'finish the login page')
    hook('session-start', 'A', source='startup', transcript_path=tr_a)
    hook('prompt', 'A', prompt='Build the login page with email and password fields', transcript_path=tr_a)
    for name in ('B', 'C', 'D', 'E', 'G', 'N'):
        hook('session-start', name, source='startup')
    tr_f = codex_rollout(home / '.codex' / 'sessions' / '2026' / '11' / '20' / 'rollout-2026-11-20T10-00-00-F.jsonl',
                         str(started), 'port the parser')
    hook('session-start', 'F', host='codex', source='startup')     # no transcript_path: found in the usual place

    at(store, 'A', BOOT - 7200, tr_a)                    # worked two hours before the restart
    at(store, 'B', NOW - 600)                            # since boot, its pane runs a host
    pane(store, 'B', 'PANE-B')
    at(store, 'C', NOW - 900)                            # since boot, nothing runs it
    pane(store, 'C', 'PANE-C')
    at(store, 'D', BOOT - 9000)                          # followed by E in the same pane
    pane(store, 'D', 'PANE-D')
    at(store, 'E', BOOT - 8000)
    pane(store, 'E', 'PANE-D')
    at(store, 'F', BOOT - 5000, tr_f)
    at(store, 'G', NOW - 10 * 86400)                     # older than the window
    at(store, 'N', NOW - 300)                            # since boot, its own ID still carried
    pane(store, 'N', 'PANE-N')

    env = ['/bin/zsh TERM=x CMUX_PANEL_ID=PANE-C',                              # a shell alone: no host
           'node mcp.js CLAUDE_CODE_SESSION_ID=OLD-ID CMUX_PANEL_ID=PANE-B',   # a host under its first ID
           'python3 brain CLAUDE_CODE_SESSION_ID=N CMUX_PANEL_ID=PANE-ELSE']
    running = restore.live(env)
    check('② live() reads session IDs and the panes that run a host', running == ({'OLD-ID', 'N'}, {'PANE-B', 'PANE-ELSE'}),
          running)

    found = restore.sessions(store, boot=BOOT, now=NOW, running=running)
    got = rows(found)
    check('① a session older than this boot is cut off', got.get('A', {}).get('state') == 'interrupted', got.get('A'))
    check('① a Codex session older than this boot is cut off', got.get('F', {}).get('state') == 'interrupted', got.get('F'))
    check('① control: a session active since this boot is not cut off', got.get('C', {}).get('state') != 'interrupted',
          got.get('C'))
    check('② its pane runs a host and it is the newest there: open', got.get('B', {}).get('state') == 'open', got.get('B'))
    check('② its own ID is carried: open', got.get('N', {}).get('state') == 'open', got.get('N'))
    check('② control: its pane runs only a shell: closed', got.get('C', {}).get('state') == 'closed', got.get('C'))
    unknown = rows(restore.sessions(store, boot=BOOT, now=NOW, running=False))
    check('② control: processes not read: since boot, neither open nor closed',
          unknown.get('B', {}).get('state') == 'since_boot', unknown.get('B'))
    check('③ a session followed in its pane is not listed', 'D' not in got and 'E' in got, sorted(got))
    followed = rows(restore.sessions(store, boot=BOOT, now=NOW, running=running, followed=True))
    check('③ … it is with --followed', followed.get('D', {}).get('state') == 'followed', followed.get('D'))
    check('the window: a session older than 7 days is not listed', 'G' not in got, sorted(got))
    check('the window: --days 0 lists it',
          'G' in rows(restore.sessions(store, days=0, boot=BOOT, now=NOW, running=running)))

    a = got.get('A', {})
    check('④ the task row says where it cd\'d to (the trap)', a.get('task') and store.get(a['task'])['workspace'] == str(moved.resolve()),
          a.get('task'))
    check('④ resume runs where it started', a.get('resume') == 'cd %s && claude --resume A' % started, a.get('resume'))
    check('④ control: no transcript, the task\'s folder', (got.get('B') or {}).get('folder') in (None, str(moved)),
          got.get('B'))
    f = got.get('F', {})
    check('④ Codex: codex resume, in its rollout\'s folder', f.get('resume') == 'cd %s && codex resume F' % started,
          f.get('resume'))
    check('the task and its goal are shown', 'login page' in (a.get('goal') or ''), a.get('goal'))
    check('the last request is read from the transcript (not a tool result, a meta line or a command)',
          a.get('last_request') == 'finish the login page' and f.get('last_request') == 'port the parser',
          (a.get('last_request'), f.get('last_request')))

    os.utime(tr_a, (BOOT - 60, BOOT - 60))               # the turn went on after its last hook, until the restart
    late = rows(restore.sessions(store, boot=BOOT, now=NOW, running=running)).get('A', {})
    stamp = datetime.datetime.fromisoformat(late.get('last_at', '1970-01-01T00:00:00+00:00')).timestamp()
    check('⑤ last activity is the transcript\'s, when newer than the last hook', abs(stamp - (BOOT - 60)) < 1, late)

    text = restore.text(restore.sessions(store, boot=BOOT, now=NOW, running=running))
    check('the text groups by state and gives each command', 'Cut off by the restart' in text and
          '$ cd %s && claude --resume A' % started in text and 'Open now' in text, text[:400])

    # ⑥ the notice, through the real SessionStart hook
    real_boot = restore.boot_time
    try:
        restore.boot_time = lambda: BOOT
        reply = hook('session-start', 'S1', source='startup')
        context = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
        check('⑥ the first session after a restart is told', 'cut off 3 sessions' in context and 'cosmos-orbit sessions'
              in context, context[:400])
        reply = hook('session-start', 'S2', source='startup')
        context = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
        check('⑥ control: the next start is not told again', 'cut off' not in context, context[:300])
        restore.boot_time = lambda: BOOT + 60
        reply = hook('session-start', 'S3', source='startup')
        context = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
        check('⑥ a new boot is told again', 'cut off' in context, context[:300])
        restore.boot_time = lambda: BOOT - 86400 * 30        # a boot before everything: nothing was cut off
        check('⑥ control: a boot that cut nothing off says nothing', restore.notice(store, 'S4') == '')
    finally:
        restore.boot_time = real_boot

    out = subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(base / 'home'), 'sessions',
                          '--days', '0', '--json'], capture_output=True, text=True, env=dict(os.environ))
    try:
        listed = json.loads(out.stdout)
    except ValueError:
        listed = {}
    check('⑦ cosmos-orbit sessions --json', out.returncode == 0 and any(r['session'] == 'A' for r in listed.get('sessions', [])),
          out.stderr[-300:] or out.stdout[:300])
    out = subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(base / 'home'), 'sessions',
                          '--days', '0'], capture_output=True, text=True, env=dict(os.environ))
    check('⑦ cosmos-orbit sessions (text)', out.returncode == 0 and 'claude --resume A' in out.stdout, out.stderr[-300:])

    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
