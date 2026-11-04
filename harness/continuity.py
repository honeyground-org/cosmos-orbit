"""Context measurement and mid-task handoff, for attached sessions and unattended work.

A long session re-reads its whole context on every turn. Measured on 99 real Claude Code sessions over
14 days, the median session started at 78k tokens and peaked at 506k. Replaying those sessions with a
fresh-context handoff at 200k saved 31-51% of cost-equivalent tokens (input x1, cache write x1.25, cache
read x0.1) across re-read assumptions of 15k-80k tokens per handoff; 150k thrashed when re-reading was
heavy. The defaults below follow that measurement (scripts/measure_context_growth.py repeats it locally).

Nothing here calls a model. A handoff is rebuilt from the ledger plus one short checkpoint the executor
writes about what the ledger cannot know: the approach in progress and the next concrete step.

A hook cannot clear its own session. So a handoff ends in one of three ways: the user types /clear (or /new),
the host compacts the session itself, or - with `cosmos-orbit context auto` in cmux or tmux - Orbit types it
(see typist.py). A compaction closes the open handoff and measures again from the compacted size, and an
open handoff expires: an invitation nobody took must never hand a task to an unrelated session later.
"""
import json
import os
from pathlib import Path
import re
import subprocess

from .store import now

DEFAULT_HANDOFF_AT = 200_000
MIN_GROWTH = 60_000          # never hand off a session that has not grown this much since it started
GLOBAL_KEY = 'handoff_at'
PROJECT_PREFIX = 'handoff_at:'
TAIL_BYTES = 4_000_000
RECORD_STEP = 10_000          # store a measurement when it moved this much (keeps the ledger readable)
INVITE_TTL_SECONDS = 12 * 3600  # an open handoff nobody took expires (observed: taken 18 hours later by unrelated work)
OPEN_STATES = ('recommended', 'asked', 'checkpointed', 'told', 'auto', 'auto_failed')
INVITING_STATES = ('checkpointed', 'told', 'auto', 'auto_failed')
MODE_KEY = 'handoff_mode'     # 'tell' (default): ask the user to type /clear · 'auto': type it where a typist exists


# ---------------------------------------------------------------- settings

def parse_tokens(value, strict=True):
    """'200000', '200k', '1.5m', 'on' (default) or 'off' (0)."""
    text = str(value).strip().lower().replace(',', '').replace('_', '')
    if text in ('off', 'no', 'false', '0'):
        return 0
    if text in ('on', 'yes', 'true', 'default'):
        return DEFAULT_HANDOFF_AT
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([km]?)', text)
    if not match:
        raise ValueError('Give a token count such as 200000, 200k or 1.5m, or on / off')
    tokens = int(float(match[1]) * {'': 1, 'k': 1000, 'm': 1_000_000}[match[2]])
    if strict and 0 < tokens < 20_000:
        raise ValueError('A handoff point below 20k tokens would hand off on almost every turn')
    return tokens


def resolve_threshold(store, root):
    """Winner first: ORBIT_HANDOFF_AT environment → project → global → built-in default. 0 means off."""
    env = os.environ.get('ORBIT_HANDOFF_AT')
    if env:
        try:
            return parse_tokens(env, strict=False), 'environment'
        except ValueError:
            pass
    value = store.preference(PROJECT_PREFIX + str(Path(root).resolve()))
    if value is not None:
        return int(value), 'project'
    value = store.preference(GLOBAL_KEY)
    if value is not None:
        return int(value), 'default'
    return DEFAULT_HANDOFF_AT, 'built-in'


def set_threshold(store, value, root=None, global_default=False):
    tokens = parse_tokens(value)
    if global_default:
        store.preference(GLOBAL_KEY, str(tokens))
        return {'scope': 'default', 'handoff_at': tokens}
    store.preference(PROJECT_PREFIX + str(Path(root).resolve()), str(tokens))
    return {'scope': 'project', 'root': str(Path(root).resolve()), 'handoff_at': tokens}


def handoff_mode(store):
    return 'auto' if store.preference(MODE_KEY) == 'auto' else 'tell'


def set_handoff_mode(store, mode):
    if mode not in ('auto', 'tell'):
        raise ValueError('The handoff mode is auto or tell')
    store.preference(MODE_KEY, mode)
    return mode


def invitation_open(state, at=None):
    """A checkpointed handoff a fresh session may still take: not compacted, not adopted, not expired."""
    if (state or {}).get('state') not in INVITING_STATES:
        return False
    from datetime import datetime, timezone
    stamp = state.get('auto_at') or state.get('told_at') or state.get('checkpoint_at') or state.get('at')
    if not stamp:
        return True  # a record from before handoffs carried times: never expires, as before
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    current = at or datetime.now(timezone.utc)
    return (current - then).total_seconds() <= INVITE_TTL_SECONDS


def close_on_compaction(data, session):
    """The host compacted the session itself: that was the handoff. Close it and measure again from here."""
    state = data.get('handoff_state') or {}
    if state.get('session') == session and state.get('state') in OPEN_STATES:
        state.update(state='compacted', compacted_at=now())
        return True
    return False


def threshold_view(store, root):
    tokens, source = resolve_threshold(store, root)
    projects = {row[0][len(PROJECT_PREFIX):]: int(row[1]) for row in
                store.db.execute('SELECT key, value FROM preferences WHERE key LIKE ?', (PROJECT_PREFIX + '%',))}
    default = store.preference(GLOBAL_KEY)
    return {'here': {'handoff_at': tokens, 'source': source, 'off': tokens == 0},
            'default': int(default) if default is not None else DEFAULT_HANDOFF_AT,
            'projects': projects, 'min_growth': min_growth(),
            'environment': os.environ.get('ORBIT_HANDOFF_AT'),
            'handoff_mode': handoff_mode(store), 'typist': (__import__('harness.typist', fromlist=['detect']).detect() or {}).get('kind')}


def min_growth():
    try:
        return max(0, int(os.environ.get('ORBIT_HANDOFF_MIN_GROWTH', MIN_GROWTH)))
    except ValueError:
        return MIN_GROWTH


def limit_for_session(threshold, start):
    """The point at which this session should hand off; 0 when handoff is off."""
    if not threshold:
        return 0
    return max(threshold, (start or 0) + min_growth())


# ---------------------------------------------------------------- measurement

def tail_lines(path, limit=TAIL_BYTES):
    with open(path, 'rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - limit))
        data = stream.read()
    lines = data.decode('utf-8', 'replace').splitlines()
    return lines[1:] if size > limit else lines


def measure_transcript(path):
    """Latest context size in a Claude Code transcript or a Codex rollout file; None when unknown."""
    try:
        lines = tail_lines(path)
    except OSError:
        return None
    for line in reversed(lines):
        if '"usage"' in line and '"assistant"' in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') != 'assistant' or event.get('isSidechain'):
                continue
            message = event.get('message') or {}
            usage = message.get('usage') or {}
            tokens = sum(usage.get(k) or 0 for k in ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens'))
            if tokens > 0:
                return {'tokens': tokens, 'source': 'claude_transcript', 'model': message.get('model'), 'window': None}
        elif '"token_count"' in line:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            payload = event.get('payload') or {}
            if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                continue
            info = payload.get('info') or {}
            tokens = (info.get('last_token_usage') or {}).get('total_tokens')
            if isinstance(tokens, int) and tokens > 0:
                return {'tokens': tokens, 'source': 'codex_rollout', 'model': None, 'window': info.get('model_context_window')}
    return None


def measure(payload, host):
    path = payload.get('transcript_path')
    if path and Path(path).is_file():
        found = measure_transcript(path)
        if found:
            return found
    if host == 'codex' and payload.get('session_id'):
        from .context import codex_measurement
        found = codex_measurement(payload['session_id'])
        if found.get('tokens'):
            return dict(found, source='codex_session', model=None)
    return None


# ---------------------------------------------------------------- attached sessions

def observe(store, task, payload, host, event, source=None):
    """Record the session's context size, session changes and the handoff point. Returns the fresh task."""
    measured = measure(payload, host)
    session = payload.get('session_id')
    root = Path(task['workspace'])
    threshold, threshold_source = resolve_threshold(store, root)
    with store.lock('task:' + task['id']):
        task = store.get(task['id'])
        data = task['data']
        live = data.setdefault('context_live', {})
        log = data.setdefault('session_log', [])
        kind, payload_out = None, {}
        if session and live.get('session') and session != live['session']:
            if log and log[-1].get('session') == live['session']:
                log[-1].update(ended=now(), peak=live.get('peak'))
            payload_out = {'from': live['session'], 'to': session, 'from_peak': live.get('peak'),
                           'reason': source or event, 'checkpoint': bool(_checkpoint_since(data, (log[-1] if log else {}).get('started')))}
            kind = 'session_rotated'
            live.clear()
        if session and not live.get('session'):
            live.update(session=session, started=now(), start=None, peak=0)
            log.append({'session': session, 'started': live['started'], 'start': None, 'peak': 0, 'reason': source or event})
            del log[:-20]
            kind = kind or 'session_observed'
        if measured:
            tokens = measured['tokens']
            previous = live.get('tokens')
            if previous and tokens < previous * 0.6 and kind != 'session_rotated':
                kind, payload_out = 'compacted', {'trigger': 'observed', 'from': previous, 'to': tokens}
                live['start'] = tokens
                data['context_dropped'] = True
                close_on_compaction(data, live.get('session'))
            if live.get('start') is None:
                live['start'] = tokens
                if log and log[-1].get('session') == live.get('session'):
                    log[-1]['start'] = tokens
            moved = previous is None or abs(tokens - previous) >= RECORD_STEP
            live.update(tokens=tokens, at=now(), source=measured['source'], window=measured.get('window'),
                        model=measured.get('model') or live.get('model'), peak=max(live.get('peak') or 0, tokens))
            if log and log[-1].get('session') == live.get('session'):
                log[-1]['peak'] = live['peak']
            kind = kind or ('context_measured' if moved else None)
        live.update(threshold=threshold, threshold_source=threshold_source,
                    limit=limit_for_session(threshold, live.get('start')))
        state = data.get('handoff_state') or {}
        if (live.get('limit') and live.get('tokens') and live['tokens'] >= live['limit']
                and (state.get('session') != live.get('session') or state.get('state') == 'compacted')
                and task['status'] not in ('stalled', 'interrupted')):
            data['handoff_state'] = {'session': live.get('session'), 'state': 'recommended', 'tokens': live['tokens'],
                                     'limit': live['limit'], 'at': now()}
            kind, payload_out = 'handoff_recommended', {'tokens': live['tokens'], 'limit': live['limit'],
                                                        'threshold_source': threshold_source}
        if kind:
            store.save(task, task['status'], kind, dict(payload_out, tokens=live.get('tokens'), session=live.get('session'), layer='context'))
    return task


def _checkpoint_since(data, stamp):
    rows = data.get('checkpoints') or []
    if not stamp:
        return rows[-1] if rows else None
    return next((row for row in reversed(rows) if row.get('at', '') >= stamp), None)


def record_checkpoint(store, task_id, next_step, notes='', files=(), by=None, kind='executor'):
    """The executor's working-memory snapshot: what the ledger cannot know. Bounded; no file contents."""
    next_step = (next_step or '').strip()
    if not next_step:
        raise ValueError('Give the next concrete step with --next')
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        data = task['data']
        root = Path(task['workspace']).resolve()
        listed = []
        for name in list(files)[:20]:
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError('Checkpoint files must be inside the workspace: ' + name)
            listed.append({'path': str(target.relative_to(root)), 'exists': target.is_file()})
        changed = []
        base = (data.get('development') or {}).get('base')
        if base:
            try:
                result = subprocess.run(['git', '-C', str(root), 'diff', '--name-only', base, '--'], capture_output=True, text=True, timeout=10)
                untracked = subprocess.run(['git', '-C', str(root), 'ls-files', '--others', '--exclude-standard'], capture_output=True, text=True, timeout=10)
                changed = sorted({p for p in (result.stdout + untracked.stdout).splitlines() if p})[:40]
            except (OSError, subprocess.TimeoutExpired):
                changed = []
        live = data.get('context_live') or {}
        row = {'at': now(), 'by': by or data.get('host') or task['provider'], 'kind': kind,
               'next': next_step[:1200], 'notes': (notes or '').strip()[:2000], 'files': listed,
               'changed': changed, 'tokens': live.get('tokens'), 'session': live.get('session'),
               'turn': data.get('turns', 0)}
        rows = data.setdefault('checkpoints', [])
        rows.append(row)
        del rows[:-10]
        state = data.get('handoff_state') or {}
        if state.get('state') in ('recommended', 'asked'):
            state['state'] = 'checkpointed'
            state['checkpoint_at'] = row['at']
        store.save(task, task['status'], 'checkpoint_recorded',
                   {'kind': kind, 'tokens': row['tokens'], 'files': len(listed), 'changed': len(changed), 'layer': 'context'})
    try:
        from .attach import write_handoff
        write_handoff(store, store.get(task_id), store.home / 'runs' / task_id / 'handoff.md')
    except (OSError, ValueError, RuntimeError):
        pass
    return row


def auto_checkpoint(store, task, trigger):
    """Before native compaction: keep a ledger-built checkpoint when the executor wrote none this session."""
    data = task['data']
    live = data.get('context_live') or {}
    if _checkpoint_since(data, live.get('started')):
        return None
    executor = data.get('executor_result') or {}
    remaining = [r for r in executor.get('remaining') or [] if r.strip()]
    prompts = data.get('prompts') or []
    next_step = ('; '.join(remaining) if remaining else
                 'Continue the latest request: ' + (prompts[-1]['text'] if prompts else data.get('goal') or task['prompt']))
    return record_checkpoint(store, task['id'], next_step,
                             notes='Written by Orbit from the ledger before native compaction (%s); the executor wrote no checkpoint this session.' % trigger,
                             by='orbit', kind='auto')


def auto_handoff(store, task, session, text, kind, host, closed=False, terminal=None, spawn=None):
    """Auto mode: the user's next request after a checkpointed handoff starts the fresh context by itself.

    The request is carried in the ledger, the prompt is blocked here, and a detached typist types /clear and one
    fixed line; the fresh session takes the task over and finds the request in its brief. Only Claude Code (its
    prompt hook can block a prompt), only in cmux or tmux (something can type), only a request or a go-ahead: a
    question about the last answer needs that answer, so it is answered in place and the next request hands off.
    Returns the hook reply, or None to let the prompt through."""
    if closed or host != 'claude' or kind not in ('requirement', 'trivial') or handoff_mode(store) != 'auto':
        return None
    state = task['data'].get('handoff_state') or {}
    if state.get('session') != session or state.get('state') not in ('checkpointed', 'told'):
        return None
    from . import typist
    terminal = typist.detect() if terminal is None else terminal
    if not terminal:
        return None
    with store.lock('task:' + task['id']):
        task = store.get(task['id'])
        data = task['data']
        state = data.get('handoff_state') or {}
        if state.get('session') != session or state.get('state') not in ('checkpointed', 'told'):
            return None
        data['carried_prompt'] = {'text': text[:4000], 'at': now(), 'from_session': session}
        state.update(state='auto', auto_at=now(), panel=terminal['target'])  # only the session in this pane takes it
        store.save(task, task['status'], 'handoff_auto', {'terminal': terminal['kind'], 'layer': 'context'})
    (spawn or typist.spawn)(terminal, typist.handoff_steps(host, typist.clear_presses(text)), store.home / 'hooks.log',
                            home=store.home, task=task['id'])
    live = data.get('context_live') or {}
    return {'decision': 'block', 'reason': (
        'Orbit: handing off to a fresh context (context %s tokens >= handoff point %s). Your request is carried over '
        'and runs there by itself; there is nothing to retype.' % (_k(live.get('tokens')), _k(state.get('limit'))))}


def auto_failed(store, task_id, reason):
    """The typist could not hand off: fall back to telling. The request stays carried for a manual /clear."""
    try:
        with store.lock('task:' + task_id):
            task = store.get(task_id)
            state = task['data'].get('handoff_state') or {}
            if state.get('state') == 'auto':
                state.update(state='auto_failed', failed_at=now())
                store.save(task, task['status'], 'handoff_auto_failed', {'reason': str(reason)[:200], 'layer': 'context'})
    except (RuntimeError, ValueError):
        pass


def mark_carried_served(store, task, session):
    """The fresh session has received the carried request in its brief: no later session receives it again."""
    carried = task['data'].get('carried_prompt') or {}
    if not carried.get('text') or not session or session == carried.get('from_session') or carried.get('served_to'):
        return
    try:
        with store.lock('task:' + task['id']):
            current = store.get(task['id'])
            row = current['data'].get('carried_prompt') or {}
            if row.get('at') == carried.get('at') and not row.get('served_to'):
                row['served_to'] = session
                store.save(current, current['status'], 'carried_prompt_served', {'session': session, 'layer': 'context'})
    except (RuntimeError, ValueError):
        pass


def handoff_lines(data, prefix, tid, closed=False):
    """Brief lines about context and continuation; the numbers stay out of the brief digest."""
    lines = []
    live = data.get('context_live') or {}
    state = data.get('handoff_state') or {}
    log = data.get('session_log') or []
    current = live.get('session')
    if not closed and len(log) >= 2 and log[-1].get('session') == current:
        previous = log[-2]
        lines.append('CONTINUED in a fresh context (previous session peaked at %s tokens). Start from the checkpoint; '
                     'open only the files you need; do not repeat completed work.' % _k(previous.get('peak')))
    checkpoint = (data.get('checkpoints') or [None])[-1]
    if checkpoint and not closed:
        line = 'CHECKPOINT (%s · %s tokens · %s): next: %s' % (_local(checkpoint['at']), _k(checkpoint.get('tokens')),
                                                            checkpoint.get('by'), _one(checkpoint['next'], 400))
        if checkpoint.get('notes'):
            line += ' · notes: ' + _one(checkpoint['notes'], 500)
        if checkpoint.get('files'):
            line += ' · files: ' + ', '.join(f['path'] for f in checkpoint['files'][:10])
        lines.append(line)
    if state.get('session') == current and current:
        if state.get('state') in ('recommended', 'asked'):
            lines.append("CONTEXT %s tokens ≥ handoff point %s. At the next natural boundary record a checkpoint: "
                         "%s checkpoint %s --next '…' --notes '…' [--file path]. A fresh session then continues from the ledger "
                         "instead of re-reading this context." % (_k(live.get('tokens')), _k(state.get('limit')), prefix, tid))
        elif state.get('state') == 'auto_failed':
            lines.append('HANDOFF READY, but the automatic /clear did not take effect: ask the user to type /clear; '
                         'the request they made is carried over to the fresh session.')
        elif state.get('state') in ('checkpointed', 'told'):
            lines.append('HANDOFF READY: checkpoint saved. Orbit cannot clear this session itself: the user types /clear '
                         '(Claude Code) or /new (Codex) and the fresh session takes this task over; otherwise the next '
                         'automatic compaction closes the handoff.')
    carried = data.get('carried_prompt') or {}
    if carried.get('text') and not closed and current and current != carried.get('from_session') \
            and carried.get('served_to') in (None, current):
        lines.append('CARRIED OVER (the request the user made just before this fresh context; do it now): '
                     + _one(carried['text'], 1200))
    return lines


def context_view(data):
    live = data.get('context_live') or {}
    return {'tokens': live.get('tokens'), 'start': live.get('start'), 'peak': live.get('peak'),
            'limit': live.get('limit'), 'threshold': live.get('threshold'), 'threshold_source': live.get('threshold_source'),
            'session': live.get('session'), 'at': live.get('at'), 'model': live.get('model'), 'window': live.get('window')}


def _k(value):
    if not isinstance(value, (int, float)) or not value:
        return '?'
    return '%dk' % round(value / 1000)


def _one(text, limit):
    return re.sub(r'\s+', ' ', str(text or '')).strip()[:limit]


def _local(stamp):
    import datetime
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).astimezone().strftime('%H:%M')
    except (ValueError, TypeError):
        return str(stamp or '')[:16]


# ---------------------------------------------------------------- unattended sessions

def rollover_due(store, task, key):
    """True when the owned executor session should restart from a ledger handoff before the next turn."""
    data = task['data']
    options = data.get('options') or {}
    if options.get('auto_handoff') is False:
        return False
    threshold = options.get('context_budget')
    if threshold is None:
        source = (data.get('development') or {}).get('source', task['workspace'])
        threshold, _ = resolve_threshold(store, source)
    tokens = (data.get('context_measurements') or {}).get(key, {}).get('tokens')
    if not threshold or not isinstance(tokens, int) or not data.get('sessions', {}).get(key):
        return False
    start = (data.get('context_start') or {}).get(key)
    return tokens >= limit_for_session(threshold, start)
