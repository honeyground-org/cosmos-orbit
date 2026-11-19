"""Measure the conversation during a turn, at its tool calls, and tell the session when it is past its handoff point.

Measured 2026-11-18 (task 631fe2b8b0cf): Orbit measured a session only at prompt and Stop hooks, so one 65-minute turn
took the conversation from 185k (15:55) to 511k (17:00), past the 300k handoff point, without a single measurement, and
the checkpoint written in that turn recorded the stale 185k. The PreToolUse hook (matcher Bash, the one offload added)
now reads the size from the transcript at each shell call. Past the point it records the measurement in the ledger as a
prompt or a Stop would (a checkpoint written next carries it, and the Stop that follows finds the handoff ready instead
of asking for a second checkpoint) and tells the model: finish the step in hand, record a checkpoint, end the turn.

Once per crossing, then again every RENOTIFY tokens of growth, and once more, firmer, at the ceiling
(continuity.ceiling_tokens). One notice under a long turn's tool output is easy to lose; one on every call costs a
paragraph per call and teaches the model to skip it. At 50k, the 2026-11-18 turn would have heard it at 300k, 350k and
400k, and firmer at 450k and 500k: five notices in 65 minutes. `cosmos-orbit context midturn block` (opt-in) refuses
every shell call past the ceiling except Orbit's own commands; ORBIT_HANDOFF_MIDTURN=off before a command lets it run.

Cheapest test first, as offload's hook: a subagent's call, a home with no ledger, a conversation below the lowest
handoff point the settings file allows - silent without opening the store. Settings: continuity.limits_effective.
"""
import json
import os
from pathlib import Path
import re

RENOTIFY = 50_000      # tokens of growth after which the notice is given again
TAIL_FIRST = 262_144   # the newest assistant message ends the transcript; the whole 4 MB tail is read only if not here
MESSAGE_CHARS = 900
MARKER = 'midturn:'    # preference per session: the last notice (tokens, level, ceiling, time)
ENV = 'ORBIT_HANDOFF_MIDTURN'
# Orbit's own commands, which record the work and end it, always run - in block mode past the ceiling too.
OWN = re.compile(r'(?:\bcosmos-orbit|(?:^|[\s;&|(/])harness|\bharness\.cli)(?:\s+--home\s+(?:\'[^\']*\'|"[^"]*"|\S+))?'
                 r'\s+(?:checkpoint|claim|evidence|done|status|events|context|offload|commands|pause|detach)\b')
BYPASS = re.compile(r'(?:^|[\s;&|(])' + ENV + r'=(?:off|0|no|false)\b')


def _command(payload):
    tool_input = payload.get('tool_input')
    command = tool_input.get('command') if isinstance(tool_input, dict) else None
    if isinstance(command, list):
        command = ' '.join(str(part) for part in command)
    return command[:20000] if isinstance(command, str) else ''


def blockable(command):
    """A call block mode may refuse: not one of Orbit's own commands, not run with the bypass prefix."""
    return not (OWN.search(command or '') or BYPASS.search(command or ''))


def _measure(payload, host, session):
    from .continuity import measure_transcript
    path = payload.get('transcript_path')
    if path and os.path.isfile(path):
        found = measure_transcript(path, TAIL_FIRST) or measure_transcript(path)
        if found:
            return found
    if host == 'codex' and session:
        from .context import codex_measurement
        found = codex_measurement(session)
        if found.get('tokens'):
            return dict(found, source='codex_session', model=None)
    return None


def floor(home, host, environ, window=None):
    """The lowest handoff point any session of this host can have here, read from ORBIT_HANDOFF_AT and the settings
    file alone (no store): a conversation below it is below its own point. 0: handoff is off everywhere."""
    from .continuity import _hosts, _projects, _section, _tokens, host_cap, host_default, parse_tokens
    value = environ.get('ORBIT_HANDOFF_AT')
    if value:
        try:
            return host_cap(parse_tokens(value, strict=False), host, window)
        except ValueError:
            pass                                          # resolve_threshold ignores it too
    try:
        data = json.loads((Path(home) / 'settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        data = {}
    section = _section(data if isinstance(data, dict) else {})
    general = next((v for v in (_tokens(_hosts(section).get(host)), _tokens(section.get('at'))) if v is not None),
                   host_default(host))
    candidates = [v for v in (_tokens(p) for p in _projects(section).values()) if v is not None] + [general]
    points = [host_cap(v, host, window) for v in candidates if v]
    return min(points) if points else 0


def _marker(store, session):
    try:
        marker = json.loads(store.preference(MARKER + session) or 'null')
    except ValueError:
        return None
    return marker if isinstance(marker, dict) and isinstance(marker.get('tokens'), int) else None


def _due(marker, tokens, ceiling, compacted_at=None):
    """A notice is due: the first past the point, after the conversation was compacted, RENOTIFY tokens after the
    last one, or the first past the ceiling."""
    if not marker:
        return True
    if compacted_at and compacted_at > (marker.get('at') or ''):
        return True
    if tokens < marker['tokens'] * 0.6:
        return True                                       # shrunk as by a compaction (observe's own rule)
    if tokens >= marker['tokens'] + RENOTIFY:
        return True
    return bool(ceiling) and tokens >= ceiling and marker.get('level') != 'ceiling'


def decide(home, host, payload, environ=None):
    """The hook's answer for one tool call, or None (silence). Never creates a task or a home."""
    env = os.environ if environ is None else environ
    if not isinstance(payload, dict) or payload.get('agent_id'):
        return None                                       # 1. a subagent's call: its context is not the conversation
    session = payload.get('session_id')
    home = Path(home)
    if not session or not (home / 'tasks.sqlite3').is_file():
        return None                                       # 2. no session, or no ledger here (and none is created)
    measured = _measure(payload, host, session)
    tokens = (measured or {}).get('tokens')
    if not tokens:
        return None
    lowest = floor(home, host, env, measured.get('window'))
    if not lowest or tokens < lowest:
        return None                                       # 3. below every point a session here can have
    from .store import Store
    store = Store(home)
    try:
        return _speak(store, host, payload, env, session, tokens, measured)
    finally:
        store.close()


def _speak(store, host, payload, env, session, tokens, measured):
    from . import continuity
    from .attach import CLOSED, delegated, resolve
    from .store import now
    binding = store.session_task(session)
    if not binding or not binding.get('task_id'):
        return None                                       # 4. the session has no task
    limits = continuity.limits_effective(store, session, env)
    mode = limits['midturn']
    if mode == 'off':
        return None                                       # 5. the notice is off for this session
    command = _command(payload)
    may_block = mode == 'block' and blockable(command)
    marker = _marker(store, session)
    if marker and marker['tokens'] <= tokens < marker['tokens'] + RENOTIFY:
        past = bool(marker.get('ceiling')) and tokens >= marker['ceiling']
        if not past or (marker.get('level') == 'ceiling' and not may_block):
            return None                                   # 6. told for this stretch already (a drop is looked at below)
    try:
        task = store.get(binding['task_id'])
    except ValueError:
        return None
    if task['data'].get('mode') != 'attached' or task['status'] in CLOSED:
        return None
    if resolve(store, task['workspace'])[0] != 'on' or delegated(host, payload, env):
        return None                                       # 7. Orbit is off for its project, or a delegated codex exec
    state = task['data'].get('handoff_state') or {}
    if state.get('session') == session and state.get('state') in ('auto', 'adopted'):
        return None                                       # 8. the handoff is already under way

    def judged(task):
        live = task['data'].get('context_live') or {}
        limit = continuity.prospective_limit(store, task, session, host, tokens, measured.get('window'))
        ceiling = continuity.ceiling_tokens(limit, limits['ceiling'])
        level = 'ceiling' if ceiling and tokens >= ceiling else 'point'
        compacted = live.get('compacted_at') if live.get('session') == session else None
        return limit, ceiling, level, _due(marker, tokens, ceiling, compacted)

    limit, ceiling, level, due = judged(task)
    block = may_block and level == 'ceiling'
    if not limit or tokens < limit or (not due and not block):
        return None                                       # 9. below this session's own point, or told: nothing written
    try:
        task = continuity.observe(store, task, payload, host, 'pre-tool')  # recorded as a prompt or a Stop would
        limit, ceiling, level, due = judged(task)
        block = may_block and level == 'ceiling'
    except (RuntimeError, ValueError):
        pass                                              # a review holds the task: the notice still goes
    if not limit or tokens < limit or (not due and not block):
        return None
    text = message(store, task, tokens, limit, ceiling, level, block)
    if due:
        store.preference(MARKER + session, json.dumps({'tokens': tokens, 'level': level, 'limit': limit,
                                                       'ceiling': ceiling, 'at': now()}))
        with store.db:
            store._event(task['id'], 'handoff_midturn', {'tokens': tokens, 'limit': limit, 'ceiling': ceiling,
                                                         'level': level, 'mode': mode, 'blocked': block,
                                                         'session': session, 'source': measured.get('source'),
                                                         'layer': 'context'})
    if block:
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                       'permissionDecisionReason': text}}
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': text}}


def _k(tokens):
    return '%dk' % round(tokens / 1000)


def message(store, task, tokens, limit, ceiling, level, block):
    """At most MESSAGE_CHARS characters: the size, the point and the ceiling, the checkpoint command, end the turn."""
    from .attach import orbit_command
    for orbit in (orbit_command(store), 'cosmos-orbit'):
        checkpoint = ("%s checkpoint %s --next '<the next concrete step>' --notes '<approach and decisions the ledger "
                      "does not hold>'" % (orbit, task['id']))
        if level == 'ceiling':
            text = ('Orbit: this conversation is %s tokens, past its handoff ceiling %s (handoff point %s), measured at '
                    'this tool call. Do not start another step. Record a checkpoint now (%s), then end the turn: past '
                    'the ceiling Orbit hands the work to a fresh context at the first opportunity, whatever the user '
                    'types next.' % (_k(tokens), _k(ceiling), _k(limit), checkpoint))
            if block:
                text += (" Blocked (cosmos-orbit context midturn block): past the ceiling only Orbit's own commands run; "
                         'to run this one anyway, prefix %s=off.' % ENV)
        else:
            text = ('Orbit: this conversation is %s tokens, past its handoff point %s (%s), measured at this tool call. '
                    'Finish only the step in hand, then record a checkpoint (%s) and end the turn, so a fresh context '
                    'continues from the checkpoint and the ledger instead of re-reading this one.'
                    % (_k(tokens), _k(limit), ('ceiling %s' % _k(ceiling)) if ceiling else 'no ceiling', checkpoint))
        if len(text) <= MESSAGE_CHARS:
            return text
    return text[:MESSAGE_CHARS]


def merge(first, second):
    """One PreToolUse reply from two (offload's and this one's): a refusal wins and carries both texts."""
    if not first or not second:
        return first or second
    a, b = first.get('hookSpecificOutput') or {}, second.get('hookSpecificOutput') or {}
    texts = [x.get('permissionDecisionReason') or x.get('additionalContext') for x in (a, b)]
    text = '\n'.join(t for t in texts if t)
    if a.get('permissionDecision') == 'deny' or b.get('permissionDecision') == 'deny':
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                       'permissionDecisionReason': text}}
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'additionalContext': text}}
