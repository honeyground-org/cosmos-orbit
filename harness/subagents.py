"""Subagents: how many a session runs at once, at most `MAX` (5); the next one refused with the reason; each start,
refusal and end recorded on the session's task; the session told when it starts that a subagent is a whole new
conversation, so the work stays in its own conversation unless it cannot.

Why: a subagent re-reads its own system prompt and tools before doing anything. Measured 2026-11-24 (Claude Code
2.1.331, model haiku): a subagent asked only to reply "OK" used 26,314 tokens. Several at once also add up on one
machine (2026-11-23: the PC stopped answering and was restarted).

What Claude Code tells a hook (measured 2026-11-24, three Agent calls in one message, every hook input saved):
- PreToolUse fires for each Agent call, the three within 80 ms of each other: the hooks run in parallel, so counting
  and taking a place are one IMMEDIATE transaction. A refusal (permissionDecision deny) reaches the model as
  "Permission denied for Agent: PreToolUse:Agent hook error: <reason>".
- Every Agent call returns at once (status async_launched), with or without run_in_background: the subagent works on.
- Beside its transcript (`<session transcript without .jsonl>/subagents/agent-<agent_id>.jsonl`) Claude Code writes
  `agent-<agent_id>.meta.json` at launch: the Agent call's `toolUseId`, `description`, `agentType`, and
  `"finished": true` once an anonymous subagent has returned. A teammate (started with a name) never finishes: it goes
  idle and leaves its team (teammates.py). A teammate's .meta.json carries no toolUseId (2026-11-26, Claude Code
  2.1.332: 8 of 8 in one lead's folder), so the place its call took is matched by its name instead; before that, a new
  teammate counted twice for 120 s (its folder entry and its unmatched place) and its row ended "lost" while it ran.
- Claude Code's own helpers (the prompt suggestion) run as agents too: they fire SubagentStop with no agent_type, make
  no Agent call and write no file here, so they are never counted.

So the count needs no other hook: the session's subagent folder says what runs (an anonymous one until its .meta.json
says finished, at most QUIET_SECONDS without a write; a teammate while its team lists it), and Orbit's own rows hold the
places taken in the second between an allowed call and its .meta.json. Agents started some other way (a workflow) are
counted too, though only an Agent call can be refused. Codex has no Agent call here: nothing changes for it.

The limit: settings.json "subagents": {"max": N} (`cosmos-orbit context subagents 5|off|reset`), else 5.

Stopped at the usage limit (measured 2026-11-23/24, Claude Code 2.1.331): when the account hits its limit, every
subagent of the session stops with it. Its own transcript then ends on the host's synthetic line (`isApiErrorMessage`,
`error: rate_limit`, `apiError: usage_limit_reached`), and a teammate tells the lead it went idle with
`failureReason`. The subagent runs inside the session's process, on the session's account, so once the session answers
again (the user signed in to another account, or the limit reset) the subagent can go on there — but only when someone
sends it a message, and the address matters:
- /login inside the session keeps the process and its teammates: a message by name or agent id resumes them
  (2026-11-18: 7 of 7 finished; 2026-11-23 13:58: 10 of 10 went on).
- An account switched from outside the session, or a restart (`--resume`), starts the team again: the name is gone
  ("No agent named 'lumi-d66-engine' is reachable", 10 of 10 on 2026-11-23; ListAgents empty on 2026-11-24 12:48, the
  team's config.json created again at that second), while the agent id still resumes it from its transcript with its
  whole conversation ("Teammate … was not running; resumed it as an in-process teammate with 13 prior messages";
  "Resuming agent a6628b2"; both then skipped the step their transcript showed done and finished the rest).
Left to itself, the lead forgot them (5 of 5 never ran again on 2026-11-23 23:16), tried the names, or started new
agents and wrote each a handoff to replace the conversation it had lost. So at each prompt that goes on, the lead is
given the list — the subagents whose transcript ends on that line and have not run since, none whose name was started
again after — and the one call that resumes each: SendMessage to its agent id. At most STOPPED_TELLS prompts per stop.
"""
import json
import os
from pathlib import Path
import time

MAX = 5                       # at once, per session
SETTING = 'subagents'         # settings.json section: {"max": N}; 0 = off
AGENT_TOOLS = ('Agent', 'Task')   # Task: the Agent tool's earlier name
RESERVE_SECONDS = 120         # an allowed call whose .meta.json never appears did not launch (refused elsewhere, failed)
QUIET_SECONDS = 60 * 60       # an anonymous subagent not finished but silent this long is gone (its session died)
MATCH_SLACK = 5               # a teammate's place matches a folder entry of its name written no earlier than this before it
SHOWN = 8                     # subagents named in a refusal, at most (each counted one, up to this many)
KEEP_SECONDS = 30 * 86400     # rows older than this are dropped
MEASURED_COST = 26_314        # tokens, a subagent that only replied "OK" (2026-11-24, Claude Code 2.1.331)
LINE_CHARS = 600
GUIDE_KEY = 'subagent-guide:'     # + session: the SessionStart sources it was told at


def _iso(seconds):
    import datetime
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat()


def _seconds(iso):
    import datetime
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def _json_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- the limit

def limit(home):
    """(at most this many at once, where it came from); 0 = off."""
    section = (_json_file(Path(home) / 'settings.json') or {}).get(SETTING)
    value = section.get('max') if isinstance(section, dict) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value, 'settings.json subagents.max'
    return MAX, 'built in'


def set_limit(store, value):
    """`cosmos-orbit context subagents N|off|on|reset`: on and reset return to the built-in MAX."""
    text = str(value).strip().lower()
    path = Path(store.home) / 'settings.json'
    data = _json_file(path) or {}
    section = data.get(SETTING) if isinstance(data.get(SETTING), dict) else {}
    if text in ('on', 'reset', 'default'):
        section.pop('max', None)
        result = None
    elif text == 'off':
        section['max'] = result = 0
    else:
        try:
            result = int(text)
        except ValueError:
            raise ValueError('subagents takes a number from 1 to 50, off or reset: %r' % value)
        if not 1 <= result <= 50:
            raise ValueError('subagents takes a number from 1 to 50, off or reset: %r' % value)
        section['max'] = result
    if section:
        data[SETTING] = section
    else:
        data.pop(SETTING, None)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    return result


# ---------------------------------------------------------------- what runs

def folder_agents(transcript_path, now=None):
    """The subagents in the session's folder: agent id, toolUseId, description, type, name (a teammate), running and
    why, last write, transcript. Newest first."""
    from .teammates import agents as teammate_rows, subagents_dir, _name
    now = time.time() if now is None else now
    folder = subagents_dir(transcript_path)
    try:
        metas = [p for p in folder.iterdir() if p.name.startswith('agent-') and p.name.endswith('.meta.json')]
    except OSError:
        return []
    teammates = None
    rows = []
    for meta_path in metas:
        stem = meta_path.name[:-len('.meta.json')]
        meta = _json_file(meta_path) or {}
        transcript = meta_path.with_name(stem + '.jsonl')
        try:
            written = max(transcript.stat().st_mtime, meta_path.stat().st_mtime)
        except OSError:
            try:
                written = meta_path.stat().st_mtime
            except OSError:
                continue
        name = _name(stem, meta)
        row = {'agent_id': stem[len('agent-'):], 'call': meta.get('toolUseId'), 'description': meta.get('description'),
               'type': meta.get('agentType'), 'name': name, 'written': written, 'transcript': str(transcript)}
        if name:
            if teammates is None:
                teammates = {r['agent_id']: r for r in teammate_rows(transcript_path, now)}
            mate = teammates.get(row['agent_id'])
            row['running'] = bool(mate and mate['running'])
            row['why'] = mate['why'] if mate else 'not written for a day'
        elif meta.get('finished') is True:
            row['running'], row['why'] = False, 'finished'
        elif now - written > QUIET_SECONDS:
            row['running'], row['why'] = False, 'silent for %d min' % ((now - written) // 60)
        else:
            row['running'], row['why'] = True, 'not finished'
        rows.append(row)
    rows.sort(key=lambda r: r['written'], reverse=True)
    return rows


def _settle(store, session, transcript, now):
    """Bring this session's rows up to date with its folder; return (running subagents, rows ended now). Runs inside the
    caller's transaction."""
    found = folder_agents(transcript, now) if transcript else []
    by_call = {r['call']: r for r in found if r['call']}
    by_agent = {r['agent_id']: r for r in found}
    by_name = {}                                   # a teammate's .meta.json has no toolUseId: the newest of each name
    for r in found:
        if r['name'] and not r['call'] and r['name'] not in by_name:
            by_name[r['name']] = r
    claimed = {r['agent'] for r in store.db.execute('SELECT agent FROM subagents WHERE session=? AND agent IS NOT NULL',
                                                    (session,)).fetchall()}
    ended = []
    for row in store.db.execute("SELECT * FROM subagents WHERE session=? AND state IN ('starting','running')",
                                (session,)).fetchall():
        row = dict(row)
        seen = by_call.get(row['call']) or (by_agent.get(row['agent']) if row['agent'] else None)
        if not seen and not row['agent'] and row['name']:
            mate = by_name.get(row['name'])
            if mate and mate['agent_id'] not in claimed and \
                    mate['written'] >= (_seconds(row['started']) or now) - MATCH_SLACK:
                seen = mate
                claimed.add(mate['agent_id'])
        if seen and not row['agent']:
            store.db.execute("UPDATE subagents SET agent=?, transcript=?, state='running' WHERE call=?",
                             (seen['agent_id'], seen['transcript'], row['call']))
            row.update(agent=seen['agent_id'], transcript=seen['transcript'], state='running')
        if seen and not seen['running']:
            stopped = _iso(seen['written'])
            store.db.execute("UPDATE subagents SET state='ended', ended=?, why=? WHERE call=?",
                             (stopped, seen['why'], row['call']))
            ended.append(dict(row, ended=stopped, why=seen['why']))
        elif not seen and now - (_seconds(row['started']) or now) > RESERVE_SECONDS:
            store.db.execute("UPDATE subagents SET state='lost', ended=?, why=? WHERE call=?",
                             (_iso(now), 'no subagent appeared within %d s' % RESERVE_SECONDS, row['call']))
    mine = {r['call'] for r in store.db.execute(
        "SELECT call FROM subagents WHERE session=? AND state='starting'", (session,)).fetchall()}
    running = [r for r in found if r['running']]
    for call in mine - set(by_call):            # a place taken a moment ago, its .meta.json not written yet
        row = dict(store.db.execute('SELECT * FROM subagents WHERE call=?', (call,)).fetchone())
        running.append({'agent_id': None, 'call': call, 'description': row['description'], 'type': row['kind'],
                        'name': row['name'], 'written': _seconds(row['started']) or now, 'running': True,
                        'why': 'starting'})
    return running, ended


def _label(row):
    text = row.get('name') or row.get('description') or row.get('type') or 'subagent'
    return str(text)[:40]


def _record(store, session, kind, payload):
    from .teammates import _record as record
    record(store, session, kind, dict(payload, session=session, layer='subagents'))


def _measure(row):
    try:
        from .teammates import measure
        return measure(row['transcript']) if row.get('transcript') else None
    except Exception:  # noqa: BLE001 - a size is never worth the answer
        return None


# ---------------------------------------------------------------- the gate

def refusal_text(running, at_most):
    names = ', '.join(_label(r) for r in running[:SHOWN]) + (' and %d more' % (len(running) - SHOWN)
                                                              if len(running) > SHOWN else '')
    return ('Orbit: not started — %d subagents already run in this session (%s), at most %d at a time. Wait until one '
            'finishes, or do this step yourself in this conversation; a subagent is a new conversation that costs about '
            '%dk tokens before it does any work.' % (len(running), names, at_most, round(MEASURED_COST / 1000)))[:LINE_CHARS]


def started_text(count, at_most):
    return ('Orbit: subagent %d of at most %d at once in this session. Each is a new conversation (about %dk tokens before '
            'any work, measured): give subagents only what cannot be done here, and do the rest in this conversation.'
            % (count, at_most, round(MEASURED_COST / 1000)))[:LINE_CHARS]


def gate(home, host, payload, now=None):
    """The PreToolUse answer for an Agent call, or None. Never creates a ledger; silent where Orbit is off."""
    if host != 'claude' or not isinstance(payload, dict) or payload.get('tool_name') not in AGENT_TOOLS:
        return None
    session, call = payload.get('session_id'), payload.get('tool_use_id')
    transcript = payload.get('transcript_path')
    home = Path(home)
    if not session or not call or not (home / 'tasks.sqlite3').is_file():
        return None
    at_most = limit(home)[0]
    if not at_most:
        return None                                        # off
    from .store import Store
    store = Store(home)
    try:
        from .attach import hook_root
        if hook_root(store, payload.get('cwd') or os.getcwd(), session, 'pre-tool') is None:
            return None                                    # Orbit is off for this session
        now = time.time() if now is None else now
        tool_input = payload.get('tool_input') if isinstance(payload.get('tool_input'), dict) else {}
        store.db.execute('BEGIN IMMEDIATE')                # parallel Agent calls: one counts at a time
        try:
            if store.db.execute('SELECT 1 FROM subagents WHERE call=?', (call,)).fetchone():
                store.db.commit()
                return None                                # this call was answered already
            running, ended = _settle(store, session, transcript, now)
            allowed = len(running) < at_most
            store.db.execute('INSERT INTO subagents(call,session,agent,kind,name,description,state,transcript,started,'
                             'ended,why) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                             (call, session, None, str(tool_input.get('subagent_type') or '')[:80] or None,
                              str(tool_input.get('name') or '')[:80] or None,
                              str(tool_input.get('description') or '')[:200] or None,
                              'starting' if allowed else 'refused', None, _iso(now), None if allowed else _iso(now),
                              None if allowed else '%d running, at most %d' % (len(running), at_most)))
            store.db.execute('DELETE FROM subagents WHERE started < ?', (_iso(now - KEEP_SECONDS),))
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise
        for row in ended:
            _record(store, session, 'subagent_ended', {'agent_id': row['agent'], 'description': row['description'],
                                                       'why': row['why'], 'tokens': _measure(row),
                                                       'started': row['started'], 'ended': row['ended']})
        detail = {'description': tool_input.get('description'), 'type': tool_input.get('subagent_type'),
                  'name': tool_input.get('name'), 'running': [_label(r) for r in running], 'at_most': at_most,
                  'by_subagent': payload.get('agent_id')}
        if not allowed:
            _record(store, session, 'subagent_refused', detail)
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                           'permissionDecisionReason': refusal_text(running, at_most)}}
        _record(store, session, 'subagent_started', dict(detail, count=len(running) + 1))
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse',
                                       'additionalContext': started_text(len(running) + 1, at_most)}}
    finally:
        store.close()


def session_ended(store, session):
    """SessionEnd: the session's subagents end with it."""
    with store.db:
        store.db.execute("UPDATE subagents SET state='ended', ended=?, why='the session ended' "
                         "WHERE session=? AND state IN ('starting','running')", (_iso(time.time()), session))


# ---------------------------------------------------------------- telling the session

def guidance_text(at_most):
    return ('[orbit] Subagents: each is a new conversation (~%dk tokens before any work). Do the work in this '
            'conversation; start one only when it cannot be done here, at most %d at a time.'
            % (round(MEASURED_COST / 1000), at_most))


def guidance(store, payload, host):
    """The SessionStart line, or None: Claude Code only, where Orbit is on (its switch, or a session already bound to a
    task) and the limit is not off. Reads only: hook_root would bind the session, which is the task code's to decide."""
    if host != 'claude' or not isinstance(payload, dict) or payload.get('agent_id'):
        return None
    at_most = limit(store.home)[0]
    if not at_most:
        return None
    from .attach import delegated, normalise, resolve
    if normalise(os.environ.get('ORBIT') or os.environ.get('COSMOS_ORBIT')) != 'on' and delegated(host, payload):
        return None                                        # Orbit's own reviewer or worker run, as _handle skips it
    cwd = payload.get('cwd') or os.getcwd()
    session = payload.get('session_id')
    bound = store.session_task(session) if session else None
    if resolve(store, cwd)[0] != 'on' and not (bound and bound.get('task_id')):
        return None
    return guidance_text(at_most)


# ---------------------------------------------------------------- stopped at the usage limit

STOPPED_WITHIN = 24 * 3600    # a stop older than this is history, not work to resume
STOPPED_TELLS = 3             # prompts that carry one stop's list, at most
STOPPED_TAIL = 65536          # the end of a subagent's transcript, enough for its last line
STOPPED_SHOWN = 12            # subagents named in the list, at most
STOPPED_KEY = 'subagent-stopped:'  # + session: {stop id: prompts it was told at}


def _last_message(path):
    """The last user or assistant line of a transcript (other lines — queue operations, attachments — skipped)."""
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - STOPPED_TAIL))
            lines = handle.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get('type') in ('user', 'assistant'):
            return record
    return None


def stopped(transcript_path, now=None):
    """The session's subagents whose own transcript ends on the host's usage-limit line, newest stop first: agent id,
    name (a teammate) or description, when it stopped and that line's id. Not one whose name (a teammate) or
    description (an anonymous subagent) was started again after its stop: that newer agent took its work."""
    from .routes import LIMIT_ERRORS, _content_text, _stamp
    from .teammates import _name, subagents_dir
    now = time.time() if now is None else now
    try:
        files = [p for p in subagents_dir(transcript_path).iterdir()
                 if p.name.startswith('agent-') and p.name.endswith('.jsonl')]
    except OSError:
        return []
    every, found = [], []
    for path in files:
        try:
            written = path.stat().st_mtime
        except OSError:
            continue
        if now - written > STOPPED_WITHIN:
            continue
        stem = path.name[:-len('.jsonl')]
        meta = _json_file(path.with_name(stem + '.meta.json')) or {}
        row = {'agent_id': stem[len('agent-'):], 'name': _name(stem, meta), 'description': meta.get('description'),
               'type': meta.get('agentType'), 'written': written, 'transcript': str(path)}
        every.append(row)
        last = _last_message(path)
        if not (last and last.get('type') == 'assistant' and last.get('isApiErrorMessage')
                and last.get('error') in LIMIT_ERRORS):
            continue
        at = _stamp(last.get('timestamp')) or written
        if now - at > STOPPED_WITHIN:
            continue
        found.append(dict(row, stopped_at=at, stop=str(last.get('uuid') or '%s@%d' % (row['agent_id'], at)),
                          text=str(_content_text(last.get('message')) or last.get('error'))[:160]))

    def replaced(row):
        field = 'name' if row['name'] else 'description'
        return bool(row[field]) and any(
            other['agent_id'] != row['agent_id'] and other[field] == row[field] and other['written'] > row['stopped_at']
            and not (field == 'description' and other['name']) for other in every)

    found = [r for r in found if not replaced(r)]
    found.sort(key=lambda r: r['stopped_at'], reverse=True)
    return found


def _clock(stamp, now):
    import datetime
    when = datetime.datetime.fromtimestamp(stamp)
    same_day = when.date() == datetime.datetime.fromtimestamp(now).date()
    return when.strftime('%H:%M' if same_day else '%m-%d %H:%M')


def stopped_text(rows, at_most, now=None):
    now = time.time() if now is None else now
    count = len(rows)
    lines = ['[orbit] %d subagent%s of this session stopped at the usage limit and %s not run since:'
             % (count, '' if count == 1 else 's', 'has' if count == 1 else 'have')]
    for row in rows[:STOPPED_SHOWN]:
        label = row['name'] or '"%s"' % str(row['description'] or row['type'] or 'subagent')[:60]
        lines.append('- %s · agentId %s · stopped %s' % (label, row['agent_id'], _clock(row['stopped_at'], now)))
    if count > STOPPED_SHOWN:
        lines.append('- and %d more: `cosmos-orbit context subagents` lists them' % (count - STOPPED_SHOWN))
    lines.append(
        "A subagent runs on its session's account. Once this session answers again (another account, or the limit "
        'reset), resume each where it stopped instead of starting a new agent for its work: SendMessage({to: '
        '"<its agentId>", message: "The usage limit is lifted: continue from where you stopped."}). Use the agentId, '
        'not the name: after an account switch or a restart a name no longer reaches it ("No agent named … is '
        'reachable"), while the agentId resumes it with its whole conversation (measured 2026-11-24).'
        + (' At most %d run at a time: resume the rest as places free up.' % at_most if at_most else '')
        + ' If the user said to leave them stopped, leave them.')
    return '\n'.join(lines)


def stopped_reply(store, payload, host, now=None):
    """The prompt's note, or None: Claude Code only, where Orbit is on, when subagents of this session stopped at the
    usage limit and have not run since. A stop is told at STOPPED_TELLS prompts at most; it leaves the list by itself
    once its subagent runs again (its transcript no longer ends on the limit line)."""
    if host != 'claude' or not isinstance(payload, dict) or payload.get('agent_id'):
        return None
    session, transcript = payload.get('session_id'), payload.get('transcript_path')
    if not session or not transcript:
        return None
    now = time.time() if now is None else now
    rows = stopped(transcript, now)
    if not rows:
        return None
    from .attach import hook_root, resolve
    cwd = payload.get('cwd') or os.getcwd()
    if resolve(store, cwd)[0] != 'on' and hook_root(store, cwd, session, 'pre-tool') is None:
        return None                                    # Orbit is off for this session
    try:
        told = json.loads(store.preference(STOPPED_KEY + session) or '{}')
    except ValueError:
        told = {}
    told = told if isinstance(told, dict) else {}
    if not any(int(told.get(r['stop']) or 0) < STOPPED_TELLS for r in rows):
        return None                                    # told often enough; the user may have meant them to stay
    store.preference(STOPPED_KEY + session, json.dumps({r['stop']: int(told.get(r['stop']) or 0) + 1 for r in rows},
                                                       sort_keys=True))
    _record(store, session, 'subagents_stopped_told', {'agents': [
        {'agent_id': r['agent_id'], 'name': r['name'], 'description': r['description'],
         'stopped': _iso(r['stopped_at'])} for r in rows]})
    return stopped_text(rows, limit(store.home)[0], now)


# ---------------------------------------------------------------- showing

def view(store, value, session, how, transcript=None, now=None):
    """`cosmos-orbit context subagents [N|off|reset] [--session [ID]]`: the limit, and this session's subagents."""
    changed = None
    if value is not None:
        changed = {'subagents': set_limit(store, value)}
    at_most, source = limit(store.home)
    result = {'max': at_most, 'source': source, 'changed': changed, 'session': session, 'session_from': how,
              'measured_cost': '%d tokens for a subagent that only replied OK (2026-11-24)' % MEASURED_COST}
    if not session:
        return result
    if transcript is None:
        transcript = _session_transcript(store, session)
    now = time.time() if now is None else now
    store.db.execute('BEGIN IMMEDIATE')
    try:
        running, _ = _settle(store, session, transcript, now)
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
    rows = [dict(r) for r in store.db.execute('SELECT * FROM subagents WHERE session=? ORDER BY started DESC LIMIT 20',
                                              (session,))]
    result.update(running=[{'label': _label(r), 'agent_id': r.get('agent_id'), 'why': r['why'],
                            'minutes': int((now - r['written']) // 60) if r.get('written') else None} for r in running],
                  recent=[{k: r[k] for k in ('description', 'kind', 'name', 'state', 'started', 'ended', 'why')}
                          for r in rows],
                  started=sum(1 for r in rows if r['state'] != 'refused'),
                  refused=sum(1 for r in rows if r['state'] == 'refused'),
                  stopped_at_limit=[{'label': _label(r), 'agent_id': r['agent_id'], 'stopped': _iso(r['stopped_at'])}
                                    for r in (stopped(transcript, now) if transcript else [])])
    return result


def _session_transcript(store, session):
    """The transcript path the session's hooks last reported (verifier.TRANSCRIPT_KEY), or None."""
    from .verifier import TRANSCRIPT_KEY
    return store.preference(TRANSCRIPT_KEY + session)
