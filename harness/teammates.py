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
- Claude Code writes one transcript line per block of a reply (thinking, text, a tool call), each with the reply's usage,
  and lines of its own (model <synthetic>, usage 0) for the shell calls a REPL script makes: a request is counted once,
  by its message id (2026-11-23: one teammate's 400 assistant lines were 72 requests). What it read in total is the sum
  of its requests.
- SubagentStart's additionalContext reaches the subagent: Claude Code puts it in the subagent's first request
  ("SubagentStart hook additional context: …", seen in its transcript).

Where the tokens went (12 teammates of one session, 2026-11-23, 25 minutes): 72 requests read 13.2M in total, from 47k
to 316k; the size it started at was a quarter of that, the rest was the conversation growing (3.8k a request), each
addition read again by every later request. Fewer calls and smaller results are both the remedy, so every subagent is
given the rules when it starts (SubagentStart), a teammate is told its own numbers when it reaches half the point
(once), and the lead's line names each one's requests and total read.

The point: settings.json "handoff.teammates" (`cosmos-orbit context teammates 300k|off|reset`), else the host's own
handoff point as a session of that host gets it. Advice only: nothing is refused, no model is called, and nothing is
written outside the store.
"""
import json
import os
from pathlib import Path
import re
import time

RENOTIFY = 100_000            # told again after this much growth (the teammate, and the lead about it)
EARLY_SHARE = 0.5             # a teammate is told its numbers once at this share of the point
EARLY_KEY = 'teammate-early:'     # + agent id: the early notice was given ({tokens, at})
QUIET_SECONDS = 30 * 60       # no team to ask: a transcript written this recently still runs
STALE_SECONDS = 24 * 3600     # a transcript not written for this long is not looked at
TAIL_FIRST = 262_144          # the newest request ends the transcript; the 4 MB tail is read only if it is not here
SETTING = 'teammates'         # settings.json, section "handoff"
NOTICE_KEY = 'teammate-notice:'   # + agent id: the last notice that teammate got ({tokens, at})
LEAD_KEY = 'teammate-lead:'       # + session: what the lead was last told ({agent id: tokens})
LINE_CHARS = 700
TEAMMATE_FILE = re.compile(r'^agent-a(?P<name>.+)-[0-9a-f]{16}$')
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$')


def _k(tokens):
    return '%dk' % round(tokens / 1000)


def _k1(tokens):
    return '%.1fk' % (tokens / 1000)


def _amount(tokens):
    return '%.1fM' % (tokens / 1_000_000) if tokens >= 1_000_000 else _k(tokens)


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


def usage(path):
    """Every request in a subagent's transcript: how many (calls), the size of the first (start) and the latest, and
    their sum (read: what it has read in total). Counted once per message id, Claude Code's own lines not at all. None
    when there is no request yet. Reads the whole file: for the command and for a notice about to be given, never at
    every tool call."""
    sizes = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as stream:
            for number, line in enumerate(stream):
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                message = event.get('message') if isinstance(event, dict) else None
                if event.get('type') != 'assistant' or not isinstance(message, dict) or message.get('model') == '<synthetic>':
                    continue
                spent = message.get('usage') or {}
                tokens = sum(spent.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens',
                                                          'cache_read_input_tokens'))
                if tokens > 0:
                    sizes[message.get('id') or event.get('requestId') or ('line', number)] = tokens
    except OSError:
        return None
    if not sizes:
        return None
    ordered = list(sizes.values())
    return {'calls': len(ordered), 'start': ordered[0], 'latest': ordered[-1], 'read': sum(ordered)}


def _name(stem, meta):
    """A teammate's name (the .meta.json's, else the file's), or None for an anonymous subagent."""
    name = meta.get('name')
    if isinstance(name, str) and name.strip():
        return name.strip()
    match = TEAMMATE_FILE.match(stem)
    return match['name'] if match else None


def _members(config, team, cache):
    if team not in cache:
        data = _json_file(config / 'teams' / team / 'config.json') if config and SAFE_ID.match(team) else None
        members = (data or {}).get('members')
        cache[team] = ({m['name'] for m in members if isinstance(m, dict) and isinstance(m.get('name'), str)}
                       if isinstance(members, list) else None)
    return cache[team]


def agents(transcript_path, now=None):
    """The teammates (named subagents) of the session whose transcript this is, newest first: name, agent id,
    transcript, last write, whether it still runs and why, and its size when it runs (None: unknown). An anonymous
    subagent returns by itself and is not listed."""
    folder = subagents_dir(transcript_path)
    try:
        files = [p for p in folder.iterdir() if p.name.startswith('agent-') and p.name.endswith('.jsonl')]
    except OSError:
        return []
    now = time.time() if now is None else now
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
                         'meta': meta})
    teams = {r['meta'].get('teamName') for r in rows if isinstance(r['meta'].get('teamName'), str)}
    shared = next(iter(teams)) if len(teams) == 1 else None   # a transcript without .meta.json: its session's team
    newest = {}
    for row in rows:
        if row['name'] not in newest or row['written'] > newest[row['name']]['written']:
            newest[row['name']] = row
    config, cache = config_dir(transcript_path), {}
    for row in rows:
        meta = row.pop('meta')
        team = meta.get('teamName') if isinstance(meta.get('teamName'), str) else shared
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
    parts = ['%s %s%s' % (r['name'], _k(r['tokens']), (' (%d calls, %s read)' % (r['calls'], _amount(r['read'])))
                          if r.get('calls') else '') for r in sorted(rows, key=lambda r: -r['tokens'])]
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
    for row in rows:                                   # what each has read so far: counted only now, when told
        row.update({k: v for k, v in (usage(row['transcript']) or {}).items() if k in ('calls', 'read')})
    _record(store, session, 'teammates_over', {'at': at, 'session': session, 'layer': 'context',
                                               'teammates': [{'name': r['name'], 'agent_id': r['agent_id'],
                                                              'tokens': r['tokens'], 'calls': r.get('calls'),
                                                              'read': r.get('read')} for r in rows]})
    return lead_line(rows, at)


def notice_text(tokens, at):
    return ('Orbit: your conversation is %s tokens (teammate handoff point %s), measured at this tool call, and every '
            'call re-reads all of it. Finish the step in hand, then write a handoff note to a file (what you did · code '
            'locations · branch · open PRs · what remains), send its path to the lead, and ask the lead to replace you '
            'with a new teammate given only that note.' % (_k(tokens), _k(at)))


def early_text(tokens, at, used):
    """Half the point: the teammate's own numbers, what they cost, and what to do for the rest of the work."""
    used = used or {}
    calls, start = used.get('calls') or 1, used.get('start') or tokens
    added = (tokens - start) / (calls - 1) if calls > 1 else 0
    so_far = 'after %d call%s' % (calls, '' if calls == 1 else 's')
    if added > 0:
        so_far += ' (it started at %s, about %s added per call)' % (_k(start), _k1(added))
    ratio = tokens / start if start else 1
    return ('Orbit: your conversation is %s tokens %s and every call re-reads all of it%s. For the rest of this work '
            'put several steps in each call and keep what comes back small (screenshots and long output to files, open '
            'only what you need to decide). At %s you will be asked to hand off.'
            % (_k(tokens), so_far, (': a call now costs %.1fx your first' % ratio) if ratio >= 1.5 else '', _k(at)))


def notice(home, host, payload, environ=None):
    """A teammate's own tool call: its numbers once at half the point (EARLY_SHARE), the notice past the point, once,
    then every RENOTIFY; None otherwise. Cheapest test first: below half the point (or unknown) it opens no store."""
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
    if not tokens or tokens < at * EARLY_SHARE:
        return None
    from .store import Store, now
    store = Store(home)
    try:
        early = tokens < at
        if early and store.preference(EARLY_KEY + agent):
            return None                                # its numbers were given already, once is enough
        from .attach import hook_root
        if hook_root(store, payload.get('cwd') or os.getcwd(), session, 'pre-tool') is None:
            return None                                # Orbit is off where the teammate works
        if early:
            used = usage(path)
            store.preference(EARLY_KEY + agent, json.dumps({'tokens': tokens, 'at': now()}))
            _record(store, session, 'teammate_early', dict({'name': name, 'agent_id': agent, 'tokens': tokens, 'at': at,
                                                            'session': session, 'layer': 'context'}, **(used or {})))
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                           'additionalContext': early_text(tokens, at, used)}}
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
    at = point(home, host, environ)[0]
    rows = over(transcript, at) if at else []
    if not rows:
        return None
    from .store import Store
    store = Store(home)
    try:
        text = lead_reply(store, payload, host, environ, rows)
    finally:
        store.close()
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': text}} if text else None


# ---------------------------------------------------------------- the rules, when a subagent starts

RULES = ('Orbit, token rules for subagents: each of your tool calls re-reads this whole conversation, and whatever a '
         'call adds is read again by every later call. So put several steps in one call (one script or one chained '
         'command, not a call per step) and keep what comes back small: save screenshots and long output to files, open '
         'only the one or two you need to decide, print a summary or the first lines rather than a whole log, and do '
         'not open again what you have already read. When Orbit says you are past the handoff point, write one '
         'handoff note.')


def start(home, host, payload, environ=None):
    """SubagentStart: the rules, once, for every subagent (named or not) where Orbit is on; none when the teammates'
    point is off. The store is opened only to ask whether Orbit is on where the subagent works."""
    if host != 'claude' or not isinstance(payload, dict):
        return None
    home = Path(home)
    if not (home / 'tasks.sqlite3').is_file() or not point(home, host, environ)[0]:
        return None                                    # no ledger here, or Orbit says nothing to subagents
    from .store import Store
    store = Store(home)
    try:
        from .attach import hook_root
        session = payload.get('session_id')
        if hook_root(store, payload.get('cwd') or os.getcwd(), session, 'pre-tool') is None:
            return None                                # Orbit is off where the subagent works
        agent = payload.get('agent_id')
        _record(store, session, 'teammate_rules', {'agent_id': agent if isinstance(agent, str) and SAFE_ID.match(agent)
                                                   else None, 'agent_type': str(payload.get('agent_type') or '')[:80],
                                                   'session': session, 'layer': 'context'})
        return {'hookSpecificOutput': {'hookEventName': 'SubagentStart', 'additionalContext': RULES}}
    finally:
        store.close()


def start_main(home, host, stdin=None):
    """`cosmos-orbit hook subagent-start`: stdin carries the event, stdout one JSON object or nothing. Exit 0 always."""
    import sys
    stdin = sys.stdin if stdin is None else stdin
    home = Path(home or os.environ.get('HARNESS_HOME', '~/.local/share/local-harness')).expanduser()
    try:
        raw = stdin.read(400000) if not stdin.isatty() else ''
        payload = json.loads(raw) if raw.strip() else {}
        output = start(home, host, payload if isinstance(payload, dict) else {})
    except Exception as error:  # noqa: BLE001 - a hook must not break the user's session
        try:
            from .store import now
            with (home / 'hooks.log').open('a', encoding='utf-8') as handle:
                handle.write(now() + ' subagent-start error: ' + repr(error)[:300] + '\n')
        except OSError:
            pass
        return 0
    if output:
        print(json.dumps(output, ensure_ascii=False))
    return 0


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
        for row in agents(transcript):
            row = dict(row, written=time.strftime('%Y-%m-%d %H:%M', time.localtime(row['written'])))
            if row['running'] and row['tokens'] is None:
                row['tokens'] = 'unknown'
            used = usage(row['transcript']) or {}
            row.update(calls=used.get('calls'), read=used.get('read'), started_at=used.get('start'))
            row['past'] = bool(at) and row['running'] and isinstance(row['tokens'], int) and row['tokens'] >= at
            measured.append(row)
    return {'teammates': {'at': at, 'off': not at, 'source': source, 'again_every': RENOTIFY,
                          'numbers_at': int(at * EARLY_SHARE) if at else None, 'rules': RULES if at else None,
                          'set': 'cosmos-orbit context teammates 300k|off|reset (settings.json handoff.teammates)'},
            'changed': changed, 'session': session, 'session_from': how, 'transcript': transcript,
            'measured': measured if transcript else 'no transcript known for this session (its hooks have not run yet)'}
