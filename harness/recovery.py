"""Durable native-call receipts and bounded retries across worker restarts."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import time
import sys
from zoneinfo import ZoneInfo


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def process_identity(pid):
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=,pgid='],
                                capture_output=True, text=True, timeout=2, env=dict(os.environ, LC_ALL='C'))
        parts = result.stdout.split()
        if len(parts) == 6:
            return {'pid': pid, 'started': ' '.join(parts[:5]), 'pgid': int(parts[5])}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    try:
        return {'pid': pid, 'started': None, 'pgid': os.getpgid(pid)}
    except ProcessLookupError:
        return None


def process_alive(record):
    identity = record.get('process')
    if not identity:
        return False
    current = process_identity(identity['pid'])
    if current:
        same = current['pgid'] == identity['pgid'] and (not current['started'] or not identity['started'] or current['started'] == identity['started'])
        if not same:
            return False
        try:
            state = subprocess.run(['ps', '-p', str(identity['pid']), '-o', 'stat='],
                                   capture_output=True, text=True, timeout=2, env=dict(os.environ, LC_ALL='C')).stdout.strip()
            if state and not state.startswith('Z'):
                return True
        except (OSError, subprocess.TimeoutExpired):
            return True
    try:
        result = subprocess.run(['ps', '-axo', 'pgid=,stat='], capture_output=True, text=True, timeout=2, env=dict(os.environ, LC_ALL='C'))
    except (OSError, subprocess.TimeoutExpired):
        return True  # A missing diagnostic never authorises duplicate execution.
    if result.returncode:
        return True
    return any(len(parts) == 2 and parts[0] == str(identity['pgid']) and not parts[1].startswith('Z')
               for parts in (line.split() for line in result.stdout.splitlines()))


def owned_record(store, task):
    pointer = task['data'].get('active_call')
    if not pointer:
        return None
    path = Path(pointer['receipt'])
    path.resolve().relative_to((store.home / 'runs' / task['id']).resolve())
    record = json.loads(path.read_text())
    if record['call_id'] != pointer['call_id'] or record['task_id'] != task['id']:
        raise ValueError('Native receipt does not belong to this task')
    return record


def track(store, task, phase):
    def changed(record):
        task['data']['active_call'] = {key: record[key] for key in ('call_id', 'receipt', 'session', 'status')}
        task['data']['active_call']['phase'] = phase
        store.save(task, task['status'], 'native_call_' + record['status'],
                   {'call_id': record['call_id'], 'phase': phase, 'session': record['session'],
                    'pid': (record.get('process') or {}).get('pid')})
    return changed


def retry_time(message, now=None):
    now = time.time() if now is None else now
    match = re.search(r'resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)', message, re.I)
    if match:
        try:
            hour = int(match[1]) % 12 + (12 if match[3].lower() == 'pm' else 0)
            zone = ZoneInfo(match[4])
            current = datetime.datetime.fromtimestamp(now, zone)
            target = current.replace(hour=hour, minute=int(match[2] or 0), second=0, microsecond=0)
            if target <= current:
                target += datetime.timedelta(days=1)
            return target.timestamp() + 5
        except (ValueError, KeyError):
            pass
    delay = re.search(r'(?:retry|try again)\s+(?:after|in)\s+(\d+)\s*(?:seconds?|s)\b', message, re.I)
    return now + max(5, int(delay[1])) if delay else now + 300


class ProviderFailure(RuntimeError):
    def __init__(self, message, kind='process_error', retry_at=None, metadata=None):
        super().__init__(message)
        self.kind, self.retry_at, self.metadata = kind, retry_at, metadata


def failure(message, metadata=None):
    lowered = message.lower()
    if any(word in lowered for word in ('5-hour limit', 'rate limit', 'usage limit', 'overloaded', 'capacity', '429')):
        return ProviderFailure(message, 'provider_wait', retry_time(message), metadata)
    if any(word in lowered for word in ('connection', 'network', 'timed out', 'temporarily unavailable', 'stream disconnected',
                                     'transport error', 'broken pipe', '502', '503')):
        return ProviderFailure(message, 'connection_wait', time.time() + 30, metadata)
    return ProviderFailure(message, metadata=metadata)


def defer(store, task, error):
    data = task['data']
    retries = data.get('retry_count', 0) + 1
    data.update(retry_count=retries, reason=str(error), reason_code=error.kind)
    if error.retry_at and retries <= 5:
        data['retry_at'] = error.retry_at
        data['recoverable'] = True
        store.save(task, 'received', 'retry_scheduled', {'retry_at': error.retry_at, 'reason': error.kind})
    else:
        data['recoverable'] = False
        store.save(task, 'needs_input', 'needs_input', {'reason': 'provider_failure', 'question': str(error)})


def due(task):
    return task['data'].get('retry_at', 0) <= time.time()


def record_metadata(store, task, metadata, phase, recovered=False):
    from . import context
    key = task['provider'] + (':verifier' if phase == 'verifying' else '')
    call_id = metadata.get('call_id')
    if call_id and call_id in task['data'].get('recorded_calls', []):
        return
    data = task['data']
    if metadata.get('session'):
        data['sessions'][key] = metadata['session']
    data.setdefault('session_metadata', {})[key] = metadata
    context.record(task, key, metadata)
    if call_id:
        data.setdefault('recorded_calls', []).append(call_id)
    store.save(task, task['status'], 'model_finished',
               dict(metadata, phase=phase, alias=key, recovered=recovered))


def resume_session(record):
    session = record.get('session')
    path = Path(record['log'])
    if path.exists():
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if event.get('type') == 'thread.started':
                        session = event.get('thread_id', session)
                except ValueError:
                    continue
    return session


def ensure_worker(store, idle_exit=None):
    """Starting the app resumes authorised queued work; read-only observers never call this.

    idle_exit (seconds): a transient worker for one pending review exits after that quiet period."""
    if store.locked('worker'):
        return None
    log = store.home / 'worker.log'
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = package_root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    with log.open('ab') as output:
        os.chmod(log, 0o600)
        command = [sys.executable, '-m', 'harness.cli', '--home', str(store.home), 'worker']
        if idle_exit:
            command += ['--idle-exit', str(int(idle_exit))]
        process = subprocess.Popen(command,
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   cwd=store.home, env=env, start_new_session=True)
    for _ in range(100):
        if store.locked('worker'):
            return process.pid
        if process.poll() is not None:
            raise ValueError('Worker failed to start; inspect ' + str(log))
        time.sleep(.02)
    raise ValueError('Worker startup is not yet confirmed; inspect ' + str(log))
