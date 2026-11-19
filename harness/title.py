"""A session's name follows it through a handoff.

Claude Code names a session after its first prompt and shows the name as the terminal's title (cmux puts it on the
tab). After a handoff the first prompt is the line Orbit submits (typist.CARRY_LINE), so the fresh session was named
after that line, whatever the pane was called before. Measured 2026-11-19 on one folder: 11 of the 14 latest sessions
were called 'Orbit 이전 요청 이어서', 'Orbit 이어서 진행', '이어서 진행'.

The session id does not survive a /clear; the name can. A UserPromptSubmit hook may return `sessionTitle`, and Claude
Code keeps it as the session's own title: the record /rename writes (`custom-title`; measured on 2.1.328 with a hook
answering the carry line: that record, and no generated title beside it). A SessionStart hook's title is dropped
after /clear (only startup, resume and fork take it), so the hook of the carry line itself is the place.

The name carried is the one the leaving session had:
- the title the user gave it, or an earlier handoff carried into it (`custom-title`), else
- the title Claude Code generated from the user's own first prompt (`ai-title`).
A session that began with the carry line and was never named has only a title generated from Orbit's line. That one
is not carried: the sessions the task had before it are asked in turn, newest first. Nothing found: nothing is set,
and the host names the session as it always did. A session the user has named already keeps that name.

handoff.title = keep (built in) | off, set like the other handoff settings (continuity.limits_effective):
  cosmos-orbit context title keep|off|reset [--session [ID] | --global]

Claude Code only: Codex takes no title from a hook.
"""
import json
import os

MODES = ('keep', 'off')
WHOLE = 64 * 1024 * 1024      # a transcript up to this size is read whole (20 MB takes about 30 ms)
HEAD = 1024 * 1024            # of a larger one: the start (how the session began) …
TAIL = 16 * 1024 * 1024       # … and the end (Claude Code writes the title again as the session goes on)
LENGTH = 120                  # a title longer than this is cut: it has to fit a tab
WALK = 12                     # how many earlier sessions of the task are asked for a name


def mode_value(value):
    """'keep' | 'off' (on = keep), or None when it is not one."""
    if not isinstance(value, str):
        return None
    value = {'on': 'keep'}.get(value.strip().lower(), value.strip().lower())
    return value if value in MODES else None


def clean(text):
    """A title as one printable line of at most LENGTH characters, or None."""
    if not isinstance(text, str):
        return None
    text = ' '.join(''.join(ch if ch.isprintable() else ' ' for ch in text).split())
    if not text:
        return None
    return text if len(text) <= LENGTH else text[:LENGTH - 1] + '…'


def _prompt_text(row):
    """The words of a transcript row the user (or Orbit) submitted, or None for any other row."""
    if row.get('type') != 'user' or row.get('isMeta') or row.get('isSidechain'):
        return None
    content = (row.get('message') or {}).get('content')
    if isinstance(content, list):
        if any(isinstance(part, dict) and part.get('type') == 'tool_result' for part in content):
            return None
        content = ' '.join(part.get('text') or '' for part in content if isinstance(part, dict) and part.get('type') == 'text')
    if not isinstance(content, str):
        return None
    content = content.strip()
    return content if content and not content.startswith('<') else None    # <local-command-…>, <system-reminder>


def read(path):
    """What a Claude Code transcript says about its session's name: {'custom': the title the user or a hook gave it,
    'generated': the one Claude Code made, 'carried': it began with Orbit's carry line}. {} when it cannot be read."""
    from .typist import CARRY_LINE
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as stream:
            if size <= WHOLE:
                parts = [stream.read()]
            else:
                head = stream.read(HEAD)
                stream.seek(size - TAIL)
                parts = [head, stream.read().split(b'\n', 1)[-1]]
    except (OSError, TypeError, ValueError):
        return {}
    found = {'custom': None, 'generated': None, 'carried': False}
    began = False
    for index, part in enumerate(parts):
        for raw in part.split(b'\n'):
            titled = b'-title"' in raw
            if not titled and (began or index or b'"user"' not in raw):
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            if row.get('type') == 'custom-title':
                found['custom'] = clean(row.get('customTitle'))        # the last one wins, as in Claude Code
            elif row.get('type') == 'ai-title':
                found['generated'] = clean(row.get('aiTitle'))
            elif not began and not index:
                text = _prompt_text(row)
                if text:
                    began = True
                    found['carried'] = CARRY_LINE in text              # the mod's submit is wrapped: '… sent a message: <line>'
    return found


def name_of(info):
    """The name a session hands on, or None: its own title, else the generated one unless Orbit's line produced it."""
    if info.get('custom'):
        return info['custom']
    if info.get('generated') and not info.get('carried'):
        return info['generated']
    return None


def carried_name(store, task_id, session, leaving=None):
    """The name for `session`, the fresh session of a task: asked of the session it follows, then of the sessions the
    task had before, newest first. Returns (name, the session it came from) or (None, None)."""
    from .verifier import TRANSCRIPT_KEY
    rows = sorted((row for row in store.task_sessions(task_id) if row['session'] != session),
                  key=lambda row: row.get('at') or '', reverse=True)
    order = [leaving] if leaving and leaving != session else []
    order += [row['session'] for row in rows if row['session'] not in order]
    for other in order[:WALK]:
        path = store.preference(TRANSCRIPT_KEY + other)
        name = name_of(read(path)) if path else None
        if name:
            return name, other
    return None, None


def keep(store, payload, output, host):
    """The reply to a prompt hook, with the name the fresh session keeps when the prompt is the carry line of a
    handoff. Anything else, and any failure, returns the reply as it was."""
    from .typist import CARRY_LINE
    session = (payload or {}).get('session_id')
    if host != 'claude' or not session or ((payload or {}).get('prompt') or '').strip() != CARRY_LINE:
        return output
    if (output or {}).get('decision'):
        return output                                   # blocked: the line reached the session being handed off
    from . import continuity
    if continuity.limits_effective(store, session)['title'] != 'keep':
        return output
    row = store.session_task(session)
    if not row or not row.get('task_id'):
        return output
    try:
        task = store.get(row['task_id'])
    except ValueError:
        return output                                   # no task (attach.NO_TASK), or one that is gone
    if read(payload.get('transcript_path')).get('custom'):
        return output                                   # named already, by the user or by another hook: it stays
    data = task['data']
    leaving = (data.get('carried_prompt') or {}).get('from_session') or (data.get('handoff_state') or {}).get('session')
    name, source = carried_name(store, task['id'], session, leaving)
    if not name:
        return output
    output = dict(output or {})
    output['hookSpecificOutput'] = dict(output.get('hookSpecificOutput') or {'hookEventName': 'UserPromptSubmit'},
                                        sessionTitle=name)
    store._event(task['id'], 'handoff_title_kept', {'session': session, 'from': source, 'title': name})
    return output
