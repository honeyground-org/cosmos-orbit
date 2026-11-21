"""After a restart: which sessions were cut off, when each last worked, and the command that resumes it.

The user, 2026-11-21: a reboot ends every open session at once, so Orbit should always know when each session last
worked and have the command ready that takes it up again.

What Orbit already has: every hook event of a session it serves writes that session's row in `terminals` (§wake.note:
host, pane, time), and only SessionEnd deletes it. A restart kills the hosts without a SessionEnd, so the rows of the
sessions it cut off stay. This module reads them:

  - interrupted: the session's last activity is older than this boot (the machine went down while it was open);
  - open: a host still runs it (§live: its session ID, or its pane running a host while it is the pane's newest);
  - closed: it worked after this boot and never ended, and no host runs it now (its terminal was closed hard);
  - followed: a newer session started in the same pane (a /clear, a handoff): not listed, its successor is.

The resume command runs in the folder the session ★started in★, read from the first record of its own transcript
(Claude Code: `cwd` of the first entry; Codex: `session_meta.cwd`). Claude Code finds a session to resume by that
folder, and a session that later cd'd elsewhere records the new folder in Orbit's task row (measured 2026-11-21: this
module's own session started in projects/ and its task row says projects/cosmos-orbit).

Last activity is the newer of the last hook and the transcript's modification time: a turn that ended without a hook
(the machine went down mid-turn) still wrote its transcript.

The first session that starts after a restart is told once (SessionStart context) how many sessions the restart cut
off and the command that lists them. Nothing here starts a session or types into a terminal.
"""
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

DAYS = 7                       # sessions whose last activity is older than this are not listed by default
NOTICE_KEY = 'restore_noticed_boot'
HEAD_BYTES = 262_144           # the start of a transcript holds its first folder
TAIL_BYTES = 262_144           # the end holds its last request
COMMAND = 'cosmos-orbit sessions'


def boot_time():
    """This boot, in epoch seconds, or None where it cannot be read."""
    try:
        if sys.platform == 'darwin':
            out = subprocess.run(['sysctl', '-n', 'kern.boottime'], capture_output=True, text=True, timeout=5).stdout
            found = re.search(r'sec\s*=\s*(\d+)', out)
            return float(found.group(1)) if found else None
        with open('/proc/stat', encoding='ascii') as handle:
            for line in handle:
                if line.startswith('btime '):
                    return float(line.split()[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


SESSION_ENV = re.compile(r'\b(?:CLAUDE_CODE_SESSION_ID|CODEX_THREAD_ID)=([0-9A-Za-z-]+)')
PANE_ENV = re.compile(r'\b(?:CMUX_PANEL_ID|CMUX_SURFACE_ID|TMUX_PANE)=([0-9A-Za-z%-]+)')


def _environments():
    """One environment string per running process of this user, or None where processes cannot be read."""
    try:
        if sys.platform == 'darwin':
            out = subprocess.run(['ps', '-Eww', '-axo', 'command'], capture_output=True, text=True, timeout=10)
            return out.stdout.splitlines() if out.returncode == 0 else None
        found = []
        for environ in Path('/proc').glob('[0-9]*/environ'):
            try:
                found.append(environ.read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace'))
            except OSError:
                continue
        return found
    except (OSError, subprocess.SubprocessError):
        return None


def live(environments=None):
    """(session IDs, panes) where a Claude Code or Codex session runs now; None where processes cannot be read.

    Every process a host starts (its MCP servers, its shells) carries the host's session ID and the pane it runs in.
    The ID is the one the process started under: after /clear or a handoff the host goes on in the same process
    under a new ID, so its children still carry the old one (measured 2026-11-21: a cmux pane whose newest session
    was 8587dbba ran a live Claude Code whose MCP servers carried 637ba7aa). So a session is open when its own ID is
    carried, or when its pane runs a host and it is that pane's newest session."""
    environments = _environments() if environments is None else environments
    if environments is None:
        return None
    ids, panes = set(), set()
    for env in environments:
        carried = SESSION_ENV.findall(env)
        if not carried:
            continue
        ids.update(carried)
        panes.update(PANE_ENV.findall(env))
    return ids, panes


def _epoch(stamp):
    try:
        return datetime.datetime.fromisoformat(str(stamp)).timestamp()
    except (TypeError, ValueError):
        return None


def _iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()


def _local(epoch):
    return datetime.datetime.fromtimestamp(epoch).strftime('%Y-%m-%d %H:%M')


def _one(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def transcript(store, session, host):
    """The transcript or rollout file of a session: the path its hooks reported, else the host's usual place."""
    from .verifier import TRANSCRIPT_KEY
    known = store.preference(TRANSCRIPT_KEY + session)
    if known and os.path.isfile(known):
        return known
    home = Path(os.environ.get('HOME') or '~').expanduser()
    if host == 'codex':
        root = Path(os.environ.get('CODEX_HOME') or home / '.codex') / 'sessions'
        pattern = '*/*/*/rollout-*-%s.jsonl' % session
    else:
        root = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude') / 'projects'
        pattern = '*/%s.jsonl' % session
    for path in root.glob(pattern) if root.is_dir() else ():
        return str(path)
    return None


def _records(path, head=True):
    try:
        with open(path, 'rb') as handle:
            if not head:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - TAIL_BYTES))
            chunk = handle.read(HEAD_BYTES if head else TAIL_BYTES)
    except OSError:
        return []
    lines = chunk.decode('utf-8', 'replace').splitlines()
    if not head and lines:
        lines = lines[1:]                                # the first line of a tail is cut
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def started_in(path, host):
    """The folder a session started in, from its own first records; None when the transcript does not say."""
    for item in _records(path):
        if host == 'codex':
            meta = item.get('payload') if item.get('type') == 'session_meta' else None
            if isinstance(meta, dict) and meta.get('cwd'):
                return meta['cwd']
        elif item.get('cwd'):
            return item['cwd']
    return None


def last_request(path, host):
    """The user's last request in the transcript (one line), or ''."""
    found = ''
    for item in _records(path, head=False):
        if host == 'codex':
            payload = item.get('payload') or {}
            if item.get('type') == 'event_msg' and payload.get('type') == 'user_message':
                found = payload.get('message') or found
        elif item.get('type') == 'user' and not item.get('isMeta'):
            content = (item.get('message') or {}).get('content')
            if isinstance(content, str) and not content.lstrip().startswith('<'):
                found = content                           # a command or hook record starts with a tag
    return _one(found, 160)


def resume_command(host, session, folder):
    run = ('codex resume ' if host == 'codex' else 'claude --resume ') + shlex.quote(session)
    return ('cd %s && %s' % (shlex.quote(folder), run)) if folder else run


def _task(store, session):
    from .attach import NO_TASK
    row = store.session_task(session)
    if not row or not row['task_id'] or row['task_id'] == NO_TASK:
        return None
    try:
        return store.get(row['task_id'])
    except ValueError:
        return None


def sessions(store, days=DAYS, boot=None, now=None, current=None, followed=False, running=None):
    """The sessions Orbit served that never ended, newest activity first. `running`: (IDs, panes) as §live returns
    them (None: read them now; False: do not tell open from closed)."""
    import time
    now = time.time() if now is None else now
    boot = boot_time() if boot is None else boot
    running = live() if running is None else (running or None)
    rows = [dict(r) for r in store.db.execute('SELECT * FROM terminals')]
    out = []
    for row in rows:
        hook_at = _epoch(row['at'])
        if hook_at is None:
            continue
        later = [r for r in rows if r['session'] != row['session'] and row['target'] and r['target'] == row['target']
                 and (_epoch(r['at']) or 0) > hook_at]
        if later and not followed:
            continue
        host = row['host'] or 'claude'
        path = transcript(store, row['session'], host)
        try:
            written = os.path.getmtime(path) if path else None
        except OSError:
            written = None
        last = max(t for t in (hook_at, written) if t is not None)
        if days and now - last > days * 86400:
            continue
        task = _task(store, row['session'])
        data = (task or {}).get('data') or {}
        checkpoint = (data.get('checkpoints') or [{}])[-1]
        folder = (started_in(path, host) if path else None) or (task or {}).get('workspace')
        if boot and last < boot:
            state = 'interrupted'
        elif running is None:
            state = 'since_boot'
        else:
            ids, panes = running
            on = row['session'] in ids or row['session'] == current or bool(row['target'] and row['target'] in panes)
            state = 'open' if on else 'closed'
        out.append({
            'session': row['session'], 'host': host, 'state': 'followed' if later else state,
            'current': row['session'] == current, 'last_at': _iso(last), 'last_local': _local(last),
            'folder': folder, 'pane': row['target'], 'terminal': row['kind'],
            'task': task['id'] if task else None, 'task_status': task['status'] if task else None,
            'goal': _one(task['prompt'], 160) if task else None,
            'next': _one(checkpoint.get('next') or '', 200) or None,
            'last_request': last_request(path, host) if path else '',
            'resume': resume_command(host, row['session'], folder),
        })
    out.sort(key=lambda r: r['last_at'], reverse=True)
    return {'boot_at': _iso(boot) if boot else None, 'boot_local': _local(boot) if boot else None,
            'days': days, 'sessions': out}


STATE_TEXT = {'interrupted': 'cut off by the restart', 'closed': 'closed without ending (no process carries it now)',
              'since_boot': 'active since this boot (may still be open)', 'open': 'open now',
              'followed': 'followed by a newer session in its pane'}
ORDER = ('interrupted', 'closed', 'since_boot', 'open', 'followed')


def text(result):
    rows = result['sessions']
    head = ['Orbit sessions that never ended (last %d days) · this boot: %s'
            % (result['days'], result['boot_local'] or 'unknown')]
    if not rows:
        return head[0] + '\nNone.'
    lines = head
    for state in ORDER:
        group = [r for r in rows if r['state'] == state]
        if not group:
            continue
        lines += ['', '%s — %d' % (STATE_TEXT[state].capitalize(), len(group))]
        for r in group:
            mark = '  (this session)' if r['current'] else ''
            lines.append('  %s  %s  %s%s' % (r['last_local'], r['host'], r['folder'] or '(folder unknown)', mark))
            if r['goal']:
                lines.append('      task %s (%s): %s' % (r['task'], r['task_status'], r['goal']))
            if r['next']:
                lines.append('      next: ' + r['next'])
            elif r['last_request'] and r['last_request'] != r['goal']:
                lines.append('      last request: ' + r['last_request'])
            lines.append('      $ ' + r['resume'])
    if any(r['host'] == 'claude' for r in rows):
        lines += ['', 'A Claude Code session started in Code Mode resumes only with Code Mode on.']
    return '\n'.join(lines)


def notice(store, session, boot=None, now=None):
    """Once per boot, for the first session that starts after it: how many sessions the restart cut off."""
    boot = boot_time() if boot is None else boot
    if not boot:
        return ''
    stamp = str(int(boot))
    if store.preference(NOTICE_KEY) == stamp:
        return ''
    store.preference(NOTICE_KEY, stamp)
    cut = [r for r in sessions(store, boot=boot, now=now, current=session, running=False)['sessions']
           if r['state'] == 'interrupted' and not r['current']]
    if not cut:
        return ''
    newest = cut[0]
    return ('[orbit] The restart at %s cut off %d session%s (newest: %s, last worked %s). Tell the user, and that '
            '`%s` lists them with the command that resumes each.'
            % (_local(boot), len(cut), '' if len(cut) == 1 else 's', newest['folder'] or newest['session'],
               newest['last_local'], COMMAND))
