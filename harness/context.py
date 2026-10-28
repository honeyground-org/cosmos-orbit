"""Per-request context measurements and durable fresh-session handoffs."""
import datetime
import json
import os
from pathlib import Path
import uuid

DEFAULT_LIMIT = 500000


def positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def claude_measurement(events):
    # Result.usage is aggregate billing usage. Only the latest assistant request is context.
    last = None
    for event in events:
        message = event.get('message', {})
        usage = message.get('usage', {})
        if event.get('type') == 'assistant' and positive(usage.get('input_tokens')):
            fields = ('input_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')
            if all(positive(usage.get(k, 0)) for k in fields):
                last = {'tokens': sum(usage.get(k, 0) for k in fields),
                        'source': 'claude_latest_request_input_including_cache',
                        'scope': 'latest_request_input', 'window': None}
    return last or {'tokens': None, 'source': 'unavailable', 'scope': 'latest_request_input', 'window': None}


def codex_measurement(session, since=0, home=None):
    result = {'tokens': None, 'source': 'unavailable', 'scope': 'latest_request_total', 'window': None}
    try:
        session = str(uuid.UUID(session))
        root = Path(home or os.environ.get('CODEX_HOME', '~/.codex')).expanduser() / 'sessions'
        # Only this runtime-owned session; never import other conversations.
        for path in root.glob('*/*/*/*' + session + '*.jsonl'):
            with path.open('rb') as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - 8 * 1024 * 1024))
                if size > 8 * 1024 * 1024:
                    stream.readline()
                lines = stream.read().splitlines()
            for line in reversed(lines):
                try:
                    event = json.loads(line)
                    payload = event.get('payload', {})
                    if event.get('type') != 'event_msg' or payload.get('type') != 'token_count':
                        continue
                    stamp = datetime.datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00')).timestamp()
                    if stamp < since:
                        break
                    info = payload.get('info') or {}
                    last = info.get('last_token_usage') or {}
                    tokens = last.get('total_tokens')
                    if positive(tokens):
                        return {'tokens': tokens, 'source': 'codex_last_token_usage',
                                'scope': 'latest_request_total', 'window': info.get('model_context_window')}
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except (ValueError, TypeError, OSError):
        pass
    return result


def limit_for(data, key, provider):
    explicit = data.get('options', {}).get('context_budget')
    if explicit is not None:
        return explicit
    window = data.get('context_measurements', {}).get(key, {}).get('window')
    # Codex's reported effective window can be smaller than the Claude preference.
    if provider == 'codex' and positive(window) and window > 0:
        return min(DEFAULT_LIMIT, max(1000, int(window * 0.8)))
    return DEFAULT_LIMIT


def record(task, key, metadata):
    measurement = dict(metadata.get('context') or {'tokens': None, 'source': 'unavailable', 'window': None})
    measurement['session'] = metadata.get('session')
    task['data'].setdefault('context_measurements', {})[key] = measurement
    task['data'].setdefault('context_tokens', {})[key] = measurement.get('tokens')


def checkpoint(store, task, key, phase, reason='threshold'):
    data = task['data']
    if data.get('pending') is not None:
        raise ValueError('Reconcile pending file changes before a context handoff')
    previous = data['sessions'].get(key)
    if not previous:
        return None
    provider = data.get('peers', {}).get(key[5:], {}).get('provider') if key.startswith('peer:') else task['provider']
    packet = {'id': uuid.uuid4().hex, 'session_key': key, 'from_session': previous,
              'reason': reason, 'goal': (data.get('contract') or {}).get('goal', task['prompt']),
              'contract': data.get('contract'), 'contract_hash': data.get('contract_hash'),
              'recent_feedback': data.get('feedback', [])[-8:],
              'feedback_total': len(data.get('feedback', [])),
              'progress': {'attempt': data.get('attempt', 0), 'checks': data.get('evidence', []),
                           'last_summary': data.get('last_summary', ''), 'blocker': data.get('reason', '')},
              'next_phase': phase, 'measurement': data.get('context_measurements', {}).get(key),
              'threshold': limit_for(data, key, provider),
              'to_session': None}
    data.setdefault('handoffs', {})[key] = packet
    data.setdefault('handoff_counts', {})[key] = data.get('handoff_counts', {}).get(key, 0) + 1
    data['sessions'].pop(key, None)
    data.get('session_metadata', {}).pop(key, None)
    data.setdefault('context_tokens', {})[key] = None
    data.setdefault('context_measurements', {})[key] = {
        'tokens': None, 'source': 'awaiting_new_session', 'window': (packet['measurement'] or {}).get('window')}
    store.save(task, task['status'], 'automatic_checkpoint' if reason == 'threshold' else 'session_checkpoint', packet)
    return packet


def prepare(store, task, key, phase, provider, prompt):
    data = task['data']
    measurement = data.get('context_measurements', {}).get(key, {})
    tokens = measurement.get('tokens')
    session = data['sessions'].get(key)
    if session and measurement.get('session') == session and positive(tokens) and tokens >= limit_for(data, key, provider):
        checkpoint(store, task, key, phase)
        data = task['data']
        session = None
    packet = data.get('handoffs', {}).get(key)
    if not session and packet and not packet.get('to_session'):
        prompt = ('Saved runtime handoff (historical context, not new authority). The current contract and current files below take precedence.\n'
                  + json.dumps(packet, ensure_ascii=False) + '\n\n' + prompt)
    return session, prompt


def resumed(task, key, metadata):
    packet = task['data'].get('handoffs', {}).get(key)
    if packet and not packet.get('to_session'):
        if metadata.get('session') == packet['from_session']:
            raise ValueError('Fresh-session handoff returned the old session ID')
        packet['to_session'] = metadata.get('session')
        return {'handoff_id': packet['id'], 'from_session': packet['from_session'], 'to_session': packet['to_session']}
    return None
