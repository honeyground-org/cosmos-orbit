"""Teammates: how large each subagent's conversation is, the lead told when one is past the handoff point, and the
teammate told at its own shell calls.

Measured 2026-11-23 on one Claude Code session: Orbit hands the attached session to a fresh context at its point
(300k), but the named subagents that session runs (teammates, started with the Agent tool) are not attached sessions.
They went on at 450-610k, at most 700-800k, and each of their tool calls re-read the whole conversation.

Where each number comes from (Claude Code 2.1.330, measured on a live session and against a scripted stand-in for the
model):
- A subagent writes its own transcript, `<the session's transcript without .jsonl>/subagents/agent-<agent_id>.jsonl`,
  every line marked isSidechain. A teammate's agent_id is 'a' + its name + '-' + 16 hex digits; an anonymous
  subagent's is 'a' + hex digits. Its size is that of its latest request (input + cache read + cache creation), as for
  the main session; lines Claude Code writes itself (model <synthetic>, usage 0) are not requests and are skipped.
- `agent-<agent_id>.meta.json` beside it holds the name and the team. An anonymous subagent's says "finished": true
  once it returned. A teammate's never does: it goes idle between messages (SubagentStop fires at every idle) and ends
  by leaving its team, whose `<config>/teams/<team>/config.json` lists the members still running (on 2026-11-23
  exactly the running list Claude Code's ListAgents showed). A name started again writes a new transcript; the older
  one was replaced. Without a team to ask, a transcript written in the last 30 minutes counts as running.
- The PreToolUse hook fires for a teammate's shell calls too. Its input carries `agent_id` (the transcript's name) and
  `agent_type` (a teammate's name); its `transcript_path` is the main session's.

The point: settings.json "handoff.teammates" (`cosmos-orbit context teammates 300k|off|reset`), else the host's own
handoff point as a session of that host gets it. Advice only: nothing is refused, no model is called, and nothing is
written outside the store.

Idle teammates (measured 2026-11-25): a teammate ends each turn idle, and only a message wakes it. What it sent the
lead reaches the lead only when the lead's own turn ends, and the lead was told nothing about who sits idle and why:
two handoff requests and two teammates waiting for the user sat for three hours while the Stop guard kept the lead in
its turn. So the lead's prompt, session start and shell calls carry one line when a teammate went idle or changed its
state (its last answer's state line), and at the lead's Stop a teammate still working, or idle with a report the lead
has not read, lets the turn end instead of the unfinished-work guard sending it back (at_stop).
"""
import json
import os
from pathlib import Path
import re
import time

RENOTIFY = 100_000            # told again after this much growth (the teammate, and the lead about it)
QUIET_SECONDS = 30 * 60       # no team to ask: a transcript written this recently still runs
STALE_SECONDS = 24 * 3600     # a transcript not written for this long is not looked at
TAIL_FIRST = 262_144          # the newest request ends the transcript; the 4 MB tail is read only if it is not here
SETTING = 'teammates'         # settings.json, section "handoff"
NOTICE_KEY = 'teammate-notice:'   # + agent id: the last notice that teammate got ({tokens, at})
LEAD_KEY = 'teammate-lead:'       # + session: what the lead was last told ({agent id: tokens})
LINE_CHARS = 700
TEAMMATE_FILE = re.compile(r'^agent-a(?P<name>.+)-[0-9a-f]{16}$')
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$')
# Idle teammates (measured 2026-11-25 on one lead and its teammates, Claude Code 2.1.33x):
IDLE_KEY = 'teammate-idle:'        # + session: the idle teammates the lead was last told about ({agent id: stamp|state})
STOP_KEY = 'teammate-stop:'        # + session: the idle teammates a Stop was sent back for ({agent id: idle stamp})
TURN_TAIL = 65_536                 # a turn's last answer ends the transcript; 4 MB are read only if no turn is here
DELIVERY_TAIL = 8_388_608          # the end of each lead transcript searched for a teammate's message reaching the lead
TURN_TYPES = ('user', 'assistant', 'api-request')   # lines a turn writes; attachments and hook results are not turns
STATE_CHARS = 120                  # no state line: this much of the answer's last line stands in for it
STATE_MAX = 300
STATE_LINE = re.compile(r'^[\s>*_`#-]*(?P<label>상태|state)[\s*_`]*[:：](?P<rest>.*)$', re.I)
DELIVERY = re.compile(r'<teammate-message teammate_id="(?P<name>[^"]+)"')
KINDS = (('handoff', re.compile(r'교대|인계|handoff|hand[- ]off|replace me', re.I)),
         ('done', re.compile(r'^\W*(끝|done|finished)', re.I)),
         ('waiting', re.compile(r'대기|waiting|blocked', re.I)),
         ('working', re.compile(r'진행|working|in progress', re.I)))
ACT = ('handoff', 'done')          # an idle teammate in one of these waits for the lead, not for itself
GUIDE = {'handoff': 'handoff requested → give a new teammate only that handoff note, then shut the old one down',
         'done': 'done → read its report, then shut it down',
         'waiting': 'waiting for the user → list them for the user (who waits, for what)'}


def _k(tokens):
    return '%dk' % round(tokens / 1000)


def _json_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _json_text(text):
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- measurement

def subagents_dir(transcript_path):
    """Where Claude Code keeps the subagent transcripts of the session whose transcript this is."""
    return Path(transcript_path).with_suffix('') / 'subagents'


def config_dir(transcript_path):
    """Claude Code's configuration folder (~/.claude or CLAUDE_CONFIG_DIR): <config>/projects/<folder>/<session>.jsonl."""
    parents = Path(transcript_path).parents
    return parents[2] if len(parents) > 2 and parents[1].name == 'projects' else None


def measure(path):
    """The latest request size in a subagent's transcript, or None (unknown: no request yet, or compacted since)."""
    from .continuity import measure_transcript
    found = measure_transcript(path, TAIL_FIRST, sidechain=True) or measure_transcript(path, sidechain=True)
    return (found or {}).get('tokens')


def _name(stem, meta):
    """A teammate's name (the .meta.json's, else the file's), or None for an anonymous subagent."""
    name = meta.get('name')
    if isinstance(name, str) and name.strip():
        return name.strip()
    match = TEAMMATE_FILE.match(stem)
    return match['name'] if match else None


def _team(config, team, cache):
    """A team's config.json ({} when there is none to read), read once per call."""
    if team not in cache:
        cache[team] = (_json_file(config / 'teams' / team / 'config.json') if config and SAFE_ID.match(team) else None) or {}
    return cache[team]


def _members(config, team, cache):
    members = _team(config, team, cache).get('members')
    return ({m['name'] for m in members if isinstance(m, dict) and isinstance(m.get('name'), str)}
            if isinstance(members, list) else None)


def _scan(folder, now):
    try:
        files = [p for p in folder.iterdir() if p.name.startswith('agent-') and p.name.endswith('.jsonl')]
    except OSError:
        return []
    rows = []
    for path in files:
        try:
            written = path.stat().st_mtime
        except OSError:
            continue
        if now - written > STALE_SECONDS:
            continue
        stem = path.name[:-len('.jsonl')]
        meta = _json_file(path.with_name(stem + '.meta.json')) or {}
        name = _name(stem, meta)
        if name:
            rows.append({'name': name, 'agent_id': stem[len('agent-'):], 'transcript': str(path), 'written': written,
                         'meta': meta, 'folder': str(folder)})
    return rows


def agents(transcript_path, now=None, also=()):
    """The teammates (named subagents) of the session whose transcript this is, newest first: name, agent id,
    transcript, last write, whether it still runs and why, and its size when it runs (None: unknown). An anonymous
    subagent returns by itself and is not listed.

    A teammate writes into the folder of the lead's conversation that last woke it, so after a handoff (a new
    conversation, a new transcript) one that has not run since is still in the folder of an earlier one (measured
    2026-11-25: a teammate idle since before the lead's handoff, under the session that made the team). Read with it:
    the folders of `also` (the lead's earlier transcripts) and of the session that made the team (its config.json,
    leadSessionId); the newest transcript of each name counts."""
    now = time.time() if now is None else now
    config, cache = config_dir(transcript_path), {}
    folders = []
    for path in [transcript_path] + [p for p in also if p]:
        folder = subagents_dir(path)
        if folder not in folders:
            folders.append(folder)
    rows = [row for folder in folders for row in _scan(folder, now)]
    teams = {r['meta'].get('teamName') for r in rows if isinstance(r['meta'].get('teamName'), str)}
    for team in sorted(teams):
        lead = _team(config, team, cache).get('leadSessionId')
        if isinstance(lead, str) and SAFE_ID.match(lead):
            folder = subagents_dir(Path(transcript_path).parent / (lead + '.jsonl'))
            if folder not in folders:
                folders.append(folder)
                rows += _scan(folder, now)
    # A transcript without .meta.json (written after a handoff by a teammate started before it): its folder's team. A
    # .meta.json without teamName is another thing (measured 2026-11-25 22:49): a teammate resumed by its agent id after
    # it left the team gets its .meta.json written again with no name and no team (spawnDepth 1, isBuiltIn), and the
    # team's config.json does not list it again, though it works. It has no team to ask: recency decides.
    teams, by_folder = set(), {}
    for row in rows:
        if isinstance(row['meta'].get('teamName'), str):
            teams.add(row['meta']['teamName'])
            by_folder.setdefault(row['folder'], set()).add(row['meta']['teamName'])
    shared = next(iter(teams)) if len(teams) == 1 else None
    newest = {}
    for row in rows:
        if row['name'] not in newest or row['written'] > newest[row['name']]['written']:
            newest[row['name']] = row
    for row in rows:
        meta, here = row.pop('meta'), by_folder.get(row.pop('folder')) or set()
        team = (meta.get('teamName') if isinstance(meta.get('teamName'), str)
                else None if meta else next(iter(here)) if len(here) == 1 else shared)
        members = _members(config, team, cache) if team else None
        if meta.get('finished') is True:
            row['running'], row['why'] = False, 'finished'
        elif newest[row['name']] is not row:
            row['running'], row['why'] = False, 'replaced by a newer transcript of the same name'
        elif members is not None:
            row['running'] = row['name'] in members
            row['why'] = ('a member of team ' if row['running'] else 'no longer a member of team ') + team
        else:
            row['running'] = now - row['written'] <= QUIET_SECONDS
            row['why'] = 'no team to ask; last written %d min ago' % ((now - row['written']) // 60)
        row['tokens'] = measure(row['transcript']) if row['running'] else None
    rows.sort(key=lambda r: r['written'], reverse=True)
    return rows


def over(transcript_path, at, now=None):
    """The running teammates at or past `at` tokens. An unknown size is never past anything."""
    return [r for r in agents(transcript_path, now) if r['running'] and isinstance(r['tokens'], int) and r['tokens'] >= at]


# ---------------------------------------------------------------- idle teammates

def _epoch(stamp):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def _text(message):
    content = (message or {}).get('content')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text') or '' for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def last_turn(path):
    """How a teammate's transcript ends: {'idle': True, 'stamp', 'at' (epoch), 'text'} when its newest conversation line
    is a model's answer that ended the turn (stop_reason end_turn), else {'idle': False}. Measured 2026-11-25 (7
    teammates, every turn end): Claude Code writes the answer, then 0.1 s later the SubagentStop hook's attachment, and
    nothing until a message wakes it — then a user line (the message), the SubagentStart attachment, an api-request
    line. Attachments, hook results and request blobs are not turns; Claude Code's own answers (model <synthetic>: a
    usage limit, subagents.py) do not end one here. An answer written over several lines (one per content block) is
    read whole."""
    from .continuity import tail_lines
    for limit in (TURN_TAIL, TAIL_FIRST * 16):
        try:
            lines = tail_lines(path, limit)
        except OSError:
            return {'idle': False}
        found, texts, message_id = None, [], None
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get('type') not in TURN_TYPES:
                continue
            message = event.get('message') if isinstance(event.get('message'), dict) else {}
            if found is not None:
                if event.get('type') == 'assistant' and message_id and message.get('id') == message_id:
                    texts.insert(0, _text(message))
                    continue
                break
            if (event.get('type') != 'assistant' or message.get('stop_reason') != 'end_turn'
                    or message.get('model') == '<synthetic>'):
                return {'idle': False}
            at = _epoch(event.get('timestamp'))
            if at is None:
                return {'idle': False}
            found, message_id = {'idle': True, 'stamp': event.get('timestamp'), 'at': at}, message.get('id')
            texts.append(_text(message))
        if found is not None:
            found['text'] = '\n'.join(t for t in texts if t)
            return found
    return {'idle': False}


def state_of(text):
    """(the state line, its kind) of a teammate's last answer. The state line is the last line headed '상태:' or
    'State:' (the lead's rule for teammates: 진행중 | 사용자대기(#PR · what) | 폰대기 | 교대요청(handoff note) | 끝);
    without one, the first STATE_CHARS of the answer's last line, of no kind."""
    lines = [line.strip() for line in str(text or '').splitlines() if line.strip()]
    for line in reversed(lines):
        match = STATE_LINE.match(line)
        if match:
            rest = match['rest'].strip(' *_`')
            kind = next((k for k, pattern in KINDS if pattern.search(rest)), None)
            return (match['label'] + ': ' + rest)[:STATE_MAX], kind
    return ('last line: ' + lines[-1][:STATE_CHARS]) if lines else 'no answer text', None


def lead_transcripts(store, session, transcript):
    """The lead's transcripts, this one first: the earlier conversations of its task follow (a handoff starts a new
    one and ends the one before), newest first, at most 10. A session of the same task that has not ended is another
    conversation working beside this one, with teammates of its own: not followed."""
    paths = [str(transcript)] if transcript else []
    binding = store.session_task(session) if store is not None and session else None
    if binding and binding.get('task_id'):
        from .verifier import TRANSCRIPT_KEY
        rows = sorted(store.task_sessions(binding['task_id']), key=lambda r: r.get('at') or '', reverse=True)
        for row in rows:
            if row.get('session') == session or not row.get('ended'):
                continue
            path = store.preference(TRANSCRIPT_KEY + row['session'])
            if path and path not in paths and len(paths) < 10:
                paths.append(path)
    return paths


def turns(transcripts, now=None):
    """The lead's running teammates, each with how its transcript ends: idle (its turn ended and nothing woke it), the
    time it went idle, its state line and the kind of that state. Newest first."""
    if not transcripts:
        return []
    now = time.time() if now is None else now
    rows = []
    for row in agents(transcripts[0], now, also=transcripts[1:]):
        if not row['running']:
            continue
        turn = last_turn(row['transcript'])
        row = dict(row, idle=turn['idle'])
        if turn['idle']:
            row['state'], row['kind'] = state_of(turn.get('text'))
            row.update(idle_stamp=turn['stamp'], idle_at=turn['at'], idle_for=max(0, now - turn['at']))
        rows.append(row)
    return rows


def deliveries(transcripts, limit=DELIVERY_TAIL, since=None):
    """{teammate name: when a message of theirs last reached the lead (epoch)}. Measured 2026-11-25: a teammate's
    SendMessage to the lead answers "Message sent to team-lead's inbox" at once, but the lead reads it only when its
    own turn has ended — as a user line "Another Claude session sent a message: <teammate-message teammate_id=…>",
    several at once, each turn end of a teammate followed by its idle_notification. So a teammate idle since after
    its last message reached the lead holds a report the lead has not read. `since` ({name: epoch}): the lead's
    earlier transcripts are read only while one of these names has no message at or after its time yet."""
    from .continuity import tail_lines
    found = {}
    for path in transcripts:
        if since is not None and all(found.get(name, 0) >= at for name, at in since.items()):
            break
        try:
            lines = tail_lines(path, limit)
        except OSError:
            continue
        for line in lines:
            if '<teammate-message' not in line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get('type') != 'user' or event.get('isSidechain'):
                continue
            at = _epoch(event.get('timestamp'))
            if at is None:
                continue
            for match in DELIVERY.finditer(_text(event.get('message'))):   # a tool result is not a delivery
                found[match['name']] = max(found.get(match['name'], 0), at)
    return found


def _age(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return '%dm' % minutes
    hours, rest = divmod(minutes, 60)
    return '%dh%02dm' % (hours, rest) if rest and hours < 10 else '%dh' % hours


def _squeeze(text, limit):
    """At most `limit` characters, keeping the start and the end (a handoff note's file name ends the state)."""
    if len(text) <= limit:
        return text
    head = max(8, limit // 3)
    return text[:head] + '…' + text[-(limit - head - 1):]


def _ordered(rows):
    rank = {'handoff': 0, 'done': 1, 'waiting': 2}
    return sorted(rows, key=lambda r: (rank.get(r.get('kind'), 3), -r.get('idle_for', 0)))


def idle_line(rows):
    """One line, at most LINE_CHARS: each idle teammate, how long it has been idle, its state line; then what to do for
    the states that ask something of the lead."""
    rows = _ordered(rows)
    head = '[orbit] TEAMMATES idle: '
    kinds = [k for k in ('handoff', 'done', 'waiting') if any(r.get('kind') == k for r in rows)]
    guide = ' · '.join(GUIDE[k] for k in kinds)

    def part(row, limit):
        return '%s %s — %s' % (row['name'], _age(row.get('idle_for', 0)), _squeeze(row['state'], limit))

    def whole(body):
        ended = body.endswith(('.', '。'))
        return head + body + ((' ' if ended else '. ') + guide + '.' if guide else '' if ended else '.')
    for limit in (STATE_MAX, 160, 120, 90, 60):
        line = whole(' · '.join(part(r, limit) for r in rows))
        if len(line) <= LINE_CHARS:
            return line
    shown = [part(r, 60) for r in rows]
    while len(shown) > 1 and len(whole(' · '.join(shown) + ' and %d more' % (len(rows) - len(shown) + 1))) > LINE_CHARS:
        shown.pop()
    more = (' and %d more' % (len(rows) - len(shown))) if len(shown) < len(rows) else ''
    return whole(' · '.join(shown) + more)[:LINE_CHARS]


def _orbit_on(store, payload, session):
    from .attach import hook_root, resolve
    cwd = payload.get('cwd') or os.getcwd()
    return resolve(store, cwd)[0] == 'on' or hook_root(store, cwd, session, 'pre-tool') is not None


def idle_reply(store, payload, host, now=None):
    """The lead's idle line, or None: only when one of its running teammates went idle, or changed its state, since the
    lead was last told (the same idle teammates are not told twice). Records what it told."""
    if host != 'claude' or not isinstance(payload, dict) or payload.get('agent_id'):
        return None
    session, transcript = payload.get('session_id'), payload.get('transcript_path')
    if not session or not transcript:
        return None
    rows = [r for r in turns(lead_transcripts(store, session, transcript), now) if r['idle']]
    if not rows or not _orbit_on(store, payload, session):
        return None
    told = _json_text(store.preference(IDLE_KEY + session)) or {}
    seen = {r['agent_id']: '%s|%s' % (r['idle_stamp'], r['state']) for r in rows}
    if all(told.get(agent) == mark for agent, mark in seen.items()):
        return None                                    # told about these idle teammates in these states already
    store.preference(IDLE_KEY + session, json.dumps(seen, sort_keys=True))
    _record(store, session, 'teammates_idle', {'session': session, 'layer': 'context', 'teammates': [
        {'name': r['name'], 'agent_id': r['agent_id'], 'idle_since': r['idle_stamp'], 'state': r['state'][:200],
         'kind': r['kind']} for r in rows]})
    return idle_line(rows)


def busy(rows, transcripts):
    """(working, unread): the running teammates still in a turn, and the idle ones whose last turn ended after the
    lead last received a message of theirs."""
    working = [r for r in rows if not r['idle']]
    idle = [r for r in rows if r['idle']]
    received = deliveries(transcripts, since={r['name']: r['idle_at'] for r in idle}) if idle else {}
    return working, [r for r in idle if received.get(r['name'], 0) < r['idle_at']]


def stop_text(rows):
    parts = []
    for row in _ordered(rows):
        if row['kind'] == 'handoff':
            parts.append('%s asked for a handoff (%s): start a new teammate given only that handoff note, then shut %s '
                         'down' % (row['name'], _squeeze(row['state'], STATE_MAX), row['name']))
        else:
            parts.append('%s is done (%s): read its report, then shut it down' % (row['name'], _squeeze(row['state'], 160)))
    return ('Orbit: before this turn ends, teammates wait for you — ' + '; '.join(parts) + '. A report not yet here '
            'arrives when this turn ends (teammate messages reach you only between turns). Said once.')[:LINE_CHARS * 2]


def working_text(working, unread):
    parts = []
    if working:
        parts.append('teammates working: ' + ', '.join(r['name'] for r in working))
    if unread:
        parts.append('idle with a report not yet read: ' + ', '.join(r['name'] for r in unread))
    return ('Orbit: ' + '; '.join(parts) + ' — the turn ends so their reports reach the lead (they arrive only between '
            'turns); the unfinished-work guard waits.')[:LINE_CHARS]


def at_stop(store, session, transcript, now=None):
    """What the lead's teammates mean for its Stop (attach._stop_reply), or None (none runs: the guard decides alone).

    Measured 2026-11-25 18:47-21:44: the lead tried to stop, the guard sent it back ("stopped before the work was
    finished") and it waited inside its turn; the reports of five teammates (two handoff requests, two waiting for the
    user, one for permission) reach a lead only between turns, so all of them waited three hours. Now:
    - an idle teammate whose state asks the lead to act (a handoff request, done) and that no Stop was sent back for
      yet: {'block': what to do}, once per idle turn of that teammate;
    - else a teammate still working, or idle with a report the lead has not read: {'allow': the line} — the turn may
      end, so the reports arrive; the unfinished-work guard does not send it back."""
    if not session or not transcript:
        return None
    transcripts = lead_transcripts(store, session, transcript)
    rows = turns(transcripts, now)
    if not rows:
        return None
    told = _json_text(store.preference(STOP_KEY + session)) or {}
    fresh = [r for r in rows if r['idle'] and r['kind'] in ACT and told.get(r['agent_id']) != r['idle_stamp']]
    if fresh:
        running = {r['agent_id'] for r in rows}
        told = {agent: stamp for agent, stamp in told.items() if agent in running}
        told.update({r['agent_id']: r['idle_stamp'] for r in fresh})
        store.preference(STOP_KEY + session, json.dumps(told, sort_keys=True))
        _record(store, session, 'teammates_stop', {'session': session, 'layer': 'control', 'decision': 'block',
                                                   'teammates': [{'name': r['name'], 'kind': r['kind'],
                                                                  'state': r['state'][:200]} for r in fresh]})
        return {'block': stop_text(fresh)}
    working, unread = busy(rows, transcripts)
    if not working and not unread:
        return None
    _record(store, session, 'teammates_stop', {'session': session, 'layer': 'control', 'decision': 'allow',
                                               'working': [r['name'] for r in working],
                                               'unread': [r['name'] for r in unread]})
    return {'allow': working_text(working, unread)}


def waits_on(store, session, now=None):
    """The supervisor about to type a continuation into the lead's idle terminal (supervision.tick): the names it waits
    for — teammates working, or idle with a report the lead has not read — or []. A typed continuation starts a turn,
    and the reports would wait for that turn to end."""
    from .verifier import TRANSCRIPT_KEY
    transcript = store.preference(TRANSCRIPT_KEY + session) if session else None
    transcripts = lead_transcripts(store, session, transcript)
    rows = turns(transcripts, now)
    if not rows:
        return []
    working, unread = busy(rows, transcripts)
    return [r['name'] for r in working + unread]


# ---------------------------------------------------------------- the point

def point(home, host='claude', environ=None):
    """(tokens, where it came from): settings.json handoff.teammates, else the host's handoff point as a session of the
    host gets it (ORBIT_HANDOFF_AT -> handoff.hosts.<host> -> handoff.at -> built in, Claude Code capped at 300k). A
    project's own point is not used: one session's teammates work in several folders. 0: off."""
    from .continuity import _hosts, _section, _tokens, host_cap, host_default, parse_tokens
    env = os.environ if environ is None else environ
    section = _section(_json_file(Path(home) / 'settings.json') or {})
    value = _tokens(section.get(SETTING))
    if value is not None:
        return value, 'handoff.teammates'
    value = source = None
    if env.get('ORBIT_HANDOFF_AT'):
        try:
            value, source = parse_tokens(env['ORBIT_HANDOFF_AT'], strict=False), 'ORBIT_HANDOFF_AT'
        except ValueError:
            pass
    if value is None:
        value, source = next(((v, s) for v, s in ((_tokens(_hosts(section).get(host)), 'handoff.hosts.%s' % host),
                                                   (_tokens(section.get('at')), 'handoff.at')) if v is not None),
                             (host_default(host), 'built in'))
    return host_cap(value, host, None), 'the handoff point (%s)' % source


def set_point(store, value):
    """`cosmos-orbit context teammates 300k|off|on|reset`: on and reset follow the handoff point again."""
    from .continuity import parse_tokens, update_settings
    if str(value).strip().lower() in ('on', 'reset', 'default'):
        update_settings(store, lambda section: section.pop(SETTING, None))
        return None
    tokens = parse_tokens(value)
    update_settings(store, lambda section: section.__setitem__(SETTING, tokens))
    return tokens


# ---------------------------------------------------------------- telling

def _due(told, tokens):
    """The first time past the point, RENOTIFY after the last notice, or past it again after shrinking. A conversation
    only shrinks when it is compacted, and the dip below the point is not recorded (that path opens no store), so a
    size clearly below the last notice means a new crossing."""
    if not isinstance(told, int) or isinstance(told, bool):
        return True
    return tokens >= told + RENOTIFY or tokens < told * 0.9


def _record(store, session, kind, payload):
    """One event on the session's task, when it has one; a teammate needs no task to be told."""
    binding = store.session_task(session) if session else None
    if binding and binding.get('task_id'):
        try:
            with store.db:
                store._event(binding['task_id'], kind, payload)
        except Exception:  # noqa: BLE001 - the record is evidence, never worth the notice
            pass


def lead_line(rows, at):
    """One line, at most LINE_CHARS: who is past the point, by how much, and what to do."""
    head = '[orbit] TEAMMATES past the handoff point %s: ' % _k(at)
    tail = ('. When each has finished the step in hand, have it write a handoff note (what it did · code locations · '
            'branch · open PRs · what remains) and replace it with a new teammate given only that note.')
    parts = ['%s %s' % (r['name'], _k(r['tokens'])) for r in sorted(rows, key=lambda r: -r['tokens'])]
    shown = list(parts)
    while len(shown) > 1 and len(head + ', '.join(shown) + tail) > LINE_CHARS:
        shown.pop()
    names = ', '.join(shown) + ((' and %d more' % (len(parts) - len(shown))) if len(shown) < len(parts) else '')
    return (head + names + tail)[:LINE_CHARS]


def lead_reply(store, payload, host, environ=None, rows=None):
    """The lead's line, or None: only when a running teammate is past the point and the lead was not yet told about it
    at this size (again after RENOTIFY of growth). Records what it told."""
    if host != 'claude' or not isinstance(payload, dict) or payload.get('agent_id'):
        return None
    session, transcript = payload.get('session_id'), payload.get('transcript_path')
    if not session or not transcript:
        return None
    at = point(store.home, host, environ)[0]
    if not at:
        return None
    rows = over(transcript, at) if rows is None else rows
    if not rows:
        return None
    from .attach import hook_root, resolve
    cwd = payload.get('cwd') or os.getcwd()
    if resolve(store, cwd)[0] != 'on' and hook_root(store, cwd, session, 'pre-tool') is None:
        return None                                    # Orbit is off for this session
    told = _json_text(store.preference(LEAD_KEY + session)) or {}
    if not any(_due(told.get(r['agent_id']), r['tokens']) for r in rows):
        return None                                    # told at these sizes already
    store.preference(LEAD_KEY + session, json.dumps({r['agent_id']: r['tokens'] for r in rows}, sort_keys=True))
    _record(store, session, 'teammates_over', {'at': at, 'session': session, 'layer': 'context',
                                               'teammates': [{'name': r['name'], 'agent_id': r['agent_id'],
                                                              'tokens': r['tokens']} for r in rows]})
    return lead_line(rows, at)


def notice_text(tokens, at):
    return ('Orbit: your conversation is %s tokens (teammate handoff point %s), measured at this tool call, and every '
            'call re-reads all of it. Finish the step in hand, then write a handoff note to a file (what you did · code '
            'locations · branch · open PRs · what remains), send its path to the lead, and ask the lead to replace you '
            'with a new teammate given only that note.' % (_k(tokens), _k(at)))


def notice(home, host, payload, environ=None):
    """A teammate's own tool call: the notice past the point, once, then every RENOTIFY; None otherwise. Cheapest test
    first: below the point (or unknown) it opens no store."""
    agent, session, transcript = payload.get('agent_id'), payload.get('session_id'), payload.get('transcript_path')
    if host != 'claude' or not isinstance(agent, str) or not SAFE_ID.match(agent) or not transcript:
        return None
    home = Path(home)
    if not (home / 'tasks.sqlite3').is_file():
        return None                                    # no ledger here, and none is created
    path = subagents_dir(transcript) / ('agent-%s.jsonl' % agent)
    at = point(home, host, environ)[0]
    if not at or not path.is_file():
        return None
    name = _name(path.name[:-len('.jsonl')], _json_file(path.with_name('agent-%s.meta.json' % agent)) or {})
    if not name:
        return None                                    # an anonymous subagent returns by itself
    tokens = measure(path)
    if not tokens or tokens < at:
        return None
    from .store import Store, now
    store = Store(home)
    try:
        from .attach import hook_root
        if hook_root(store, payload.get('cwd') or os.getcwd(), session, 'pre-tool') is None:
            return None                                # Orbit is off where the teammate works
        last = _json_text(store.preference(NOTICE_KEY + agent)) or {}
        if not _due(last.get('tokens'), tokens):
            return None                                # told for this stretch already
        store.preference(NOTICE_KEY + agent, json.dumps({'tokens': tokens, 'at': now()}))
        _record(store, session, 'teammate_notice', {'name': name, 'agent_id': agent, 'tokens': tokens, 'at': at,
                                                    'session': session, 'layer': 'context'})
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': notice_text(tokens, at)}}
    finally:
        store.close()


def decide(home, host, payload, environ=None):
    """The PreToolUse answer for one shell call, or None. A teammate's call: its own notice. The main session's call:
    the lead's line. With no running teammate past the point it opens no store."""
    if not isinstance(payload, dict) or host != 'claude':
        return None
    if payload.get('agent_id'):
        return notice(home, host, payload, environ)
    session, transcript, home = payload.get('session_id'), payload.get('transcript_path'), Path(home)
    if not session or not transcript or not (home / 'tasks.sqlite3').is_file():
        return None
    mates = agents(transcript)
    if not any(r['running'] for r in mates):
        return None                                    # no teammate of this conversation runs: no store opened
    at = point(home, host, environ)[0]
    rows = [r for r in mates if r['running'] and isinstance(r['tokens'], int) and r['tokens'] >= at] if at else []
    from .store import Store
    store = Store(home)
    try:
        texts = [lead_reply(store, payload, host, environ, rows) if rows else None, idle_reply(store, payload, host)]
    finally:
        store.close()
    text = '\n'.join(t for t in texts if t)
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': text}} if text else None


# ---------------------------------------------------------------- the command

def view(store, value=None, session=None, how=None, host='claude'):
    """`cosmos-orbit context teammates [300k|off|reset]`: the point, where it came from, and this session's teammates."""
    from .verifier import TRANSCRIPT_KEY
    changed = {'teammates': set_point(store, value)} if value is not None else None
    at, source = point(store.home, host)
    transcript = store.preference(TRANSCRIPT_KEY + session) if session else None
    measured = None
    if transcript:
        measured = []
        transcripts = lead_transcripts(store, session, transcript)
        idle = {r['agent_id']: r for r in turns(transcripts)}
        for row in agents(transcript, also=transcripts[1:]):
            row = dict(row, written=time.strftime('%Y-%m-%d %H:%M', time.localtime(row['written'])))
            if row['running'] and row['tokens'] is None:
                row['tokens'] = 'unknown'
            row['past'] = bool(at) and row['running'] and isinstance(row['tokens'], int) and row['tokens'] >= at
            turn = idle.get(row['agent_id'])
            if turn:
                row['idle'] = turn['idle']
                if turn['idle']:
                    row.update(idle_since=turn['idle_stamp'], state=turn['state'], state_kind=turn['kind'])
            measured.append(row)
    return {'teammates': {'at': at, 'off': not at, 'source': source, 'again_every': RENOTIFY,
                          'set': 'cosmos-orbit context teammates 300k|off|reset (settings.json handoff.teammates)'},
            'changed': changed, 'session': session, 'session_from': how, 'transcript': transcript,
            'measured': measured if transcript else 'no transcript known for this session (its hooks have not run yet)'}
