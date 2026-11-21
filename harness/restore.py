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
off and the command that lists them.

Asked in words (§asks: "세션 목록 보여줘", "세션 이어서 하고 싶어", "show my sessions"), the prompt hook answers with the
same list, numbered, and runs no turn. A number typed next reopens that session: in cmux a new workspace in the folder it
started in runs its resume command; in tmux a new window does; an open session's tab is brought forward instead of
being opened twice. Elsewhere the command is shown to paste. Nothing is started unless the user typed a number.
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
        # Followed: a newer session in the same pane, within the same boot. Pane names come back after a restart
        # (tmux numbers its panes from %0 again; cmux restores its panes under their old IDs), so the first session
        # opened in a pane after a restart does not follow the one the restart cut off there.
        later = [r for r in rows if r['session'] != row['session'] and row['target'] and r['target'] == row['target']
                 and (_epoch(r['at']) or 0) > hook_at and not (boot and hook_at < boot <= (_epoch(r['at']) or 0))]
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
            '`%s` lists them with the command that resumes each; or that saying "세션 목록 보여줘" (show my sessions) '
            'lists them numbered and a number reopens one in a new tab.'
            % (_local(boot), len(cut), '' if len(cut) == 1 else 's', newest['folder'] or newest['session'],
               newest['last_local'], COMMAND))


# ---------------------------------------------------------------- asked in words: "세션 목록 보여줘"

OFFER_KEY = 'restore_offer:'
OFFER_TTL = 30 * 60            # a number answers the list within this time
ASK_LENGTH = 80
# The user, 2026-11-21: "세션 목록 보여줘", "세션 리스트 보여줘", "세션을 이어서 하고 싶어 세션 내용을 다 보여줘".
ASK = re.compile(
    r'세션\S*\s*(목록|리스트|list)|(목록|리스트)\S*\s*세션'
    r'|세션들?\s*(좀\s*)?(다\s*)?(보여|알려)'
    r'|(끊긴|꺼진|닫힌|종료된|죽은|날아간|멈춘|사라진|남은|이전|예전|지난)\s*세션'
    r'|세션\S*\s*(좀\s*)?(다시\s*)?(이어서?|다시)\s*(하고\s*싶|할래|하자|해\s*줘|받|열|켜|살려|시작|띄워)'
    r'|세션\S*\s*(좀\s*)?(재개|복구|복원|살려|다시\s*열|다시\s*켜|되살려)'
    r'|세션\s*내용\S*\s*(을|를)?\s*(다|전부|모두)?\s*(보여|알려)'
    r'|\b(list|show)\b.{0,15}\bsessions\b|\bsessions?\s+list\b|\b(resume|reopen|restore)\b.{0,15}\bsessions?\b',
    re.I)
# A request for work that mentions sessions is not one: "세션 목록 기능 만들어줘", "fix the session list".
ASK_NOT = re.compile(r'기능|개발|구현|만들|고쳐|고치|수정|코드|버그|테스트|추가|설계|함수|화면|페이지|api|'
                     r'\b(feature|implement|build|fix|bug|test|code|page|endpoint)\b', re.I)
ALL_WORDS = ('전부', '모두', '다', '다 열어', '전부 열어', '모두 열어', 'all')
# Words about this very session ask it for work: "이 세션에서 이어서 해줘".
ASK_HERE = re.compile(r'(이|현재|지금|요)\s*세션(?!\S*\s*(목록|리스트|들))')
HANGUL = re.compile(r'[가-힣]')


def asks(text):
    """Whether a prompt asks for the session list: one short line, not a request for work."""
    text = (text or '').strip()
    if not text or len(text) > ASK_LENGTH or '\n' in text:
        return False
    return bool(ASK.search(text)) and not ASK_NOT.search(text) and not ASK_HERE.search(text)


def _terminal():
    from .typist import detect
    return detect()


def _run(argv):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        return 1, str(error)
    return out.returncode, (out.stdout + out.stderr).strip()


def _home(folder):
    home = os.path.expanduser('~')
    return ('~' + folder[len(home):]) if folder and home != '/' and (folder == home or folder.startswith(home + '/')) \
        else (folder or '')


LATEST = 'Continue the latest request:'


def _plain(value, limit=80):
    """One line a person reads: no tags of pasted or relayed text, no Orbit wording around the user's words."""
    value = re.sub(r'<[^<>]{1,200}>', ' ', str(value or ''))
    value = ' '.join(value.replace(LATEST, ' ').split())
    if value.startswith(('This session is being continued', '[')):
        return ''
    return _one(value, limit)


def _what(row):
    """What the session was doing, in one line: its task's goal, else the user's last request."""
    for value in (row.get('goal'), row.get('last_request')):
        value = _plain(value)
        if value:
            return value
    return ''


def _next(row):
    """(label, words): the next step the session recorded, or the user's latest request when that is all it says."""
    value = row.get('next') or ''
    if not value:
        return None, ''
    return ('latest' if value.strip().startswith(LATEST) else 'next'), _plain(value)


GROUPS = (('interrupted', '재부팅으로 끊긴 세션', 'Cut off by the restart'),
          ('closed', '닫힌 세션 (터미널이 닫혔거나 앱이 꺼짐)', 'Closed without ending'),
          ('since_boot', '부팅 뒤에 쓴 세션', 'Active since this boot'),
          ('open', '지금 열려 있는 세션 (번호를 고르면 그 탭으로 이동)', 'Open now (a number brings its tab forward)'))


def offer_rows(found, current=None):
    """The listed sessions in the order they are numbered: the ones to reopen first, the open ones last."""
    rows = [r for r in found['sessions'] if r['state'] != 'followed' and r['session'] != current]
    return [r for state, _, _ in GROUPS for r in rows if r['state'] == state]


def offer_text(found, rows, korean, opens):
    """The numbered list the prompt hook answers with: plain words, the user's language."""
    if not rows:
        return ('Orbit: 이어서 할 세션이 없습니다 (최근 %d일). 다른 세션은 모두 정상 종료됐어요.' % found['days'] if korean else
                'Orbit: no session to take up (last %d days): every other session ended normally.' % found['days'])
    if korean:
        lines = ['Orbit: 이어서 할 수 있는 세션 %d개 (최근 %d일 · 이번 부팅 %s)' % (len(rows), found['days'],
                                                                 found['boot_local'] or '알 수 없음')]
        lines.append('번호를 입력하면 %s. 여러 개는 "1 3", 끊긴·닫힌 세션 전부는 "전부".'
                     % ('그 세션이 새 탭에서 이어서 열립니다' if opens else '그 세션을 여는 명령을 보여 드립니다'))
    else:
        lines = ['Orbit: %d sessions to take up (last %d days · this boot %s)' % (len(rows), found['days'],
                                                                             found['boot_local'] or 'unknown')]
        lines.append('Type a number and %s. Several: "1 3"; every cut-off or closed one: "all".'
                     % ('that session reopens in a new tab' if opens else 'you get the command that reopens it'))
    number = 0
    for state, ko, en in GROUPS:
        group = [r for r in rows if r['state'] == state]
        if not group:
            continue
        lines += ['', '[%s]' % (ko if korean else en)]
        for r in group:
            number += 1
            where = _home(r['folder']) or ('폴더 모름' if korean else 'folder unknown')
            lines.append(' %2d  %s · %s%s' % (number, r['last_local'][5:], where, ' · Codex' if r['host'] == 'codex' else ''))
            what = _what(r)
            if what:
                lines.append('     %s %s' % ('하던 일:' if korean else 'doing:', what))
            label, step = _next(r)
            if step and step != what:
                words = {'latest': ('마지막 요청:', 'latest request:'), 'next': ('다음 할 일:', 'next:')}[label]
                lines.append('     %s %s' % (words[0] if korean else words[1], step))
    lines += ['', ('다른 뜻이었나요? 같은 말을 한 번 더 보내면 이 세션이 받습니다.' if korean else
                   'Meant something else? Send the same words again and they go to this session.')]
    return '\n'.join(lines)


def _picks(text, rows, blocked=None):
    """The rows a prompt picks from the list (numbers, or "전부"/"all"), or None when it picks nothing. Claude Code puts
    a blocked prompt back into its input, so the words it blocked may arrive with the pick before or after them."""
    text, blocked = (text or '').strip(), (blocked or '').strip()
    if blocked and text != blocked:
        if text.startswith(blocked):
            text = text[len(blocked):]
        elif text.endswith(blocked):
            text = text[:-len(blocked)]
    text = text.strip().strip('`"\'.').strip().lower()
    if text in ALL_WORDS:
        return [r for r in rows if r['state'] != 'open'] or None
    if not re.fullmatch(r'\d+(\s*[,\s]\s*\d+)*(\s*번)?', text):
        return None
    numbers = [int(n) for n in re.findall(r'\d+', text)]
    if not numbers or any(n < 1 or n > len(rows) for n in numbers):
        return None
    return [rows[n - 1] for n in dict.fromkeys(numbers)]


def _name(row):
    what = _what(row) or os.path.basename(row['folder'] or '') or row['session'][:8]
    return '↻ ' + _one(what, 28)


def reopen(row, terminal, focus=True, run=None):
    """Open one listed session: (done, words for the user). An open session's tab is brought forward; a cut-off or
    closed one is resumed in a new tab in the folder it started in."""
    run = run or _run
    host_run = ('codex resume ' if row['host'] == 'codex' else 'claude --resume ') + shlex.quote(row['session'])
    folder = row['folder'] if row['folder'] and os.path.isdir(row['folder']) else None
    kind = (terminal or {}).get('kind')
    if row['state'] == 'open':
        if kind == 'cmux' and row.get('pane'):
            code, out = run([terminal['cli'], 'focus-panel', '--panel', row['pane']])
            if code == 0:
                return True, 'already open: its tab is in front now'
        if kind == 'tmux' and row.get('pane'):
            code, out = run([terminal['cli'], 'select-window', '-t', row['pane']])
            if code == 0:
                run([terminal['cli'], 'select-pane', '-t', row['pane']])
                return True, 'already open: its window is in front now'
        return False, 'already open in another tab (not opened twice)'
    if kind == 'cmux':
        argv = [terminal['cli'], 'new-workspace', '--name', _name(row), '--command', host_run,
                '--focus', 'true' if focus else 'false']
        if folder:
            argv[2:2] = ['--cwd', folder]
        code, out = run(argv)
        if code == 0:
            return True, 'opened in a new tab'
        return False, 'cmux did not open a tab (%s); run it by hand: %s' % (_one(out, 120), row['resume'])
    if kind == 'tmux':
        argv = [terminal['cli'], 'new-window', '-n', _name(row)] + (['-c', folder] if folder else []) + [host_run]
        code, out = run(argv)
        if code == 0:
            return True, 'opened in a new tmux window'
        return False, 'tmux did not open a window (%s); run it by hand: %s' % (_one(out, 120), row['resume'])
    return False, 'run it in a new terminal: ' + row['resume']


KO_RESULT = {'already open: its tab is in front now': '이미 열려 있어서 그 탭으로 옮겼습니다',
             'already open: its window is in front now': '이미 열려 있어서 그 창으로 옮겼습니다',
             'already open in another tab (not opened twice)': '이미 다른 탭에서 열려 있습니다 (두 번 열지 않음)',
             'opened in a new tab': '새 탭에서 열었습니다',
             'opened in a new tmux window': '새 tmux 창에서 열었습니다'}


def _say(words, korean):
    if not korean:
        return words
    if words in KO_RESULT:
        return KO_RESULT[words]
    return (words.replace('run it in a new terminal: ', '새 터미널에서 실행하세요: ')
            .replace('; run it by hand: ', ' — 직접 실행하세요: '))


def on_prompt(store, payload, host, at=None, terminal=None, run=None, running=None, boot=None):
    """The prompt hook's part: a block reply (the list, or what a number opened), or None to let the prompt on."""
    import time
    session = payload.get('session_id')
    text = (payload.get('prompt') or '').strip()
    if not session or not text:
        return None
    at = time.time() if at is None else at
    key = OFFER_KEY + session
    try:
        state = json.loads(store.preference(key) or 'null') or {}
    except ValueError:
        state = {}
    if state and state.get('at', 0) + OFFER_TTL > at:
        rows = state.get('rows') or []
        picked = _picks(text, rows, state.get('asked'))
        if picked:
            terminal = _terminal() if terminal is None else terminal
            korean = state.get('korean')
            lines = []
            for index, row in enumerate(picked):
                done, words = reopen(row, terminal, focus=index == len(picked) - 1, run=run)
                lines.append('  %d  %s · %s — %s' % (rows.index(row) + 1, row['last_local'][5:],
                                                     _home(row['folder']) or row['session'][:8], _say(words, korean)))
            head = 'Orbit: 세션 %d개를 열었습니다.' if korean else 'Orbit: %d sessions taken up.'
            tail = ('열린 세션에 "이어서 해줘"라고 입력하면 하던 일을 계속합니다.' if korean else
                    'Type "continue" in a reopened session to go on with its work.')
            store.preference(key, 'null')
            return {'decision': 'block', 'reason': '\n'.join([head % len(picked)] + lines + [tail])}
        store.preference(key, 'null')                   # the list is answered by anything else: it is put away
        if text == state.get('asked'):
            return None                                 # the same words again were meant for the session
    if not asks(text):
        return None
    found = sessions(store, boot=boot, now=at, current=session, running=running)
    rows = offer_rows(found, session)
    korean = bool(HANGUL.search(text))
    terminal = _terminal() if terminal is None else terminal
    keep = ('session', 'host', 'state', 'folder', 'pane', 'last_local', 'resume', 'goal', 'last_request', 'next')
    store.preference(key, json.dumps({'at': at, 'asked': text, 'korean': korean,
                                      'rows': [{k: r.get(k) for k in keep} for r in rows]}, ensure_ascii=False))
    return {'decision': 'block', 'reason': offer_text(found, rows, korean, bool(terminal))}
