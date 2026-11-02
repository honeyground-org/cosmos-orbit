"""Adapters to installed, authenticated CLIs; never select a second provider implicitly."""
import json
from collections import deque
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from .context import claude_measurement, codex_measurement
from .recovery import atomic_json, process_identity, failure


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


STRING = {'type': 'string'}
STRINGS = {'type': 'array', 'items': STRING}


def doctor():
    output = {}
    for name in ('codex', 'claude'):
        executable = shutil.which(name)
        item = {'installed': bool(executable), 'path': executable, 'authenticated': 'not checked'}
        if executable:
            result = subprocess.run([executable, '--version'], capture_output=True, text=True, timeout=10)
            item['version'] = result.stdout.strip()
        output[name] = item
    return output


def native_capabilities(provider, public_web=False):
    """Describe the actual native adapter, not the host CLI's full capabilities."""
    return {'file_read_write': True, 'shell': True,
            'public_web': bool(public_web), 'host_fixed_checks': True,
            'additional_host_commands': False,
            'scope': 'isolated workspace; no commits, deployment or external writes',
            'verification': 'Orbit runs the fixed checks after the executor returns. '
                            'Their recorded exit codes and output are authoritative for those checks.'}


class Provider:
    def __init__(self, name, home, model=None, timeout=None):
        if name not in ('codex', 'claude') or not shutil.which(name):
            raise ValueError('Install and authenticate the selected CLI: ' + name)
        self.name, self.home, self.model, self.timeout = name, Path(home), model, None

    def call(self, task_id, prompt, schema, session=None, workspace=None, lease_fds=()):
        read_only = bool(getattr(self, 'read_only', False))
        directory = self.home / 'runs' / task_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        call_id = uuid.uuid4().hex
        schema_path = directory / (call_id + '.schema.json')
        output_path = directory / (call_id + '.answer.json')
        log_path = directory / (call_id + '.events.jsonl')
        error_path = directory / (call_id + '.stderr')
        schema_path.write_text(json.dumps(schema), encoding='utf-8')
        os.chmod(schema_path, 0o600)
        if self.name == 'claude':
            command = ['claude', '-p', '--tools', '', '--strict-mcp-config', '--setting-sources', '',
                       '--output-format', 'stream-json', '--verbose', '--json-schema', json.dumps(schema)]
            if session:
                command += ['--resume', session]
            else:
                session = str(uuid.uuid4())
                command += ['--session-id', session]
        else:
            command = ['codex', 'exec'] + (['resume', session] if session else [])
            command += ['--ignore-user-config', '--skip-git-repo-check', '--json',
                        '--output-schema', str(schema_path), '-o', str(output_path),
                        '-c', 'sandbox_mode="read-only"', '-c', 'approval_policy="never"',
                        '-c', 'web_search="disabled"']
            for feature in ('shell_tool', 'unified_exec', 'apps', 'browser_use', 'computer_use', 'multi_agent'):
                command += ['--disable', feature]
            command += ['-']
        if workspace is not None:
            if self.name == 'claude':
                if read_only:
                    command[command.index('--tools') + 1] = 'Read,Glob,Grep,Bash'
                    command += ['--restricted']
                else:
                    # Keep the executor's native settings, skills, MCP and memory behaviour.
                    index = command.index('--tools')
                    del command[index:index + 2]
                    command.remove('--strict-mcp-config')
                    index = command.index('--setting-sources')
                    del command[index:index + 2]
                sandbox = {'enabled': True, 'failIfUnavailable': True,
                           'allowUnsandboxedCommands': False,
                           'filesystem': {'denyWrite': [str(self.home)] if read_only else []},
                           'network': {'allowedDomains': [], 'deniedDomains': [] if getattr(self, 'public_web', False) else ['*']}}
                if read_only:
                    sandbox['filesystem']['denyWrite'].append(str(Path(workspace).resolve()))
                command += ['--settings', json.dumps({'sandbox': sandbox}),
                            '--permission-mode', 'acceptEdits', '--permission-prompts', 'none']
            else:
                if not read_only:
                    command.remove('--ignore-user-config')
                    command[command.index('sandbox_mode="read-only"')] = 'sandbox_mode="workspace-write"'
                # Native shell and patch tools operate inside the provider sandbox.
                for feature in ('shell_tool', 'unified_exec'):
                    index = command.index(feature)
                    del command[index - 1:index + 1]
                command[-1:-1] = ['-c', 'sandbox_workspace_write.network_access=false']
        if workspace is not None and getattr(self, 'public_web', False):
            if self.name == 'codex':
                command[command.index('web_search="disabled"')] = 'web_search="live"'
                command[command.index('sandbox_workspace_write.network_access=false')] = 'sandbox_workspace_write.network_access=true'
            else:
                if '--tools' in command:
                    command[command.index('--tools') + 1] += ',WebSearch,WebFetch'
        if self.model:
            # Options precede the stdin prompt marker for Codex.
            if self.name == 'codex':
                command[-1:-1] = ['--model', self.model]
            else:
                command += ['--model', self.model]
        started = time.monotonic()
        started_wall = time.time()
        invocation = directory / (call_id + '.invocation.json')
        try:
            version = subprocess.run([self.name, '--version'], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            version = 'unavailable'
        invocation.write_text(json.dumps({'argv': command, 'version': version}, indent=2), encoding='utf-8')
        os.chmod(invocation, 0o600)
        receipt = directory / (call_id + '.receipt.json')
        record = {'task_id': task_id, 'call_id': call_id, 'receipt': str(receipt),
                  'provider': self.name, 'requested_model': self.model, 'session': session,
                  'status': 'prepared', 'started_wall': started_wall, 'process': None,
                  'log': str(log_path), 'error_log': str(error_path), 'answer_path': str(output_path),
                  'invocation': str(invocation), 'exit_code': None, 'seconds': None}
        def persist(status):
            record['status'] = status
            atomic_json(receipt, record)
            callback = getattr(self, 'on_call', None)
            if callback:
                callback(record)
        persist('prepared')
        with log_path.open('w', encoding='utf-8') as stdout, error_path.open('w', encoding='utf-8') as stderr:
            os.chmod(log_path, 0o600)
            os.chmod(error_path, 0o600)
            runner = [sys.executable, str(Path(__file__).with_name('native_runner.py')), str(receipt), *command]
            process = subprocess.Popen(runner, cwd=str(workspace or directory), stdin=subprocess.PIPE,
                                       stdout=stdout, stderr=stderr, text=True, encoding='utf-8',
                                       start_new_session=True, pass_fds=lease_fds,
                                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1') if read_only else None)
            record['process'] = process_identity(process.pid)
            callback = getattr(self, 'on_call', None)
            if callback:
                callback(dict(record, status='running'))
            try:
                callback = getattr(self, 'on_event', None)
                if callback is None:
                    process.communicate(prompt)
                else:
                    from .telemetry import provider_event
                    from .process_health import Monitor
                    monitor = Monitor(process, log_path, error_path, callback, call_id)
                    monitor.sample(force=True)
                    with log_path.open(encoding='utf-8', errors='replace') as stream:
                        pending, dropping = '', False
                        def drain():
                            nonlocal pending, dropping
                            # Bound parsing; full original provider output remains in the log.
                            chunk = stream.read(262144)
                            while chunk:
                                for part in chunk.splitlines(keepends=True):
                                    if not dropping:
                                        pending += part
                                        if len(pending) > 1048576:
                                            pending, dropping = '', True
                                    if part.endswith('\n'):
                                        if not dropping:
                                            try:
                                                event = json.loads(pending)
                                            except ValueError:
                                                event = None
                                            if isinstance(event, dict):
                                                for kind, payload in provider_event(event):
                                                    callback(kind, dict(payload, call_id=call_id))
                                        pending, dropping = '', False
                                chunk = stream.read(262144)
                        first = True
                        while True:
                            monitor.sample()
                            try:
                                process.communicate(prompt if first else None, timeout=.25)
                                drain()
                                monitor.sample(force=True)
                                break
                            except subprocess.TimeoutExpired:
                                first = False
                                drain()
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise
        record = json.loads(receipt.read_text())
        callback = getattr(self, 'on_call', None)
        if callback:
            callback(record)
        return read_result(record)


def read_result(record):
    """Parse only a terminal native response; partial file edits never imply completion."""
    events = deque(maxlen=128)
    provider, session = record['provider'], record.get('session')
    reported = []
    path = Path(record['log'])
    if path.exists():
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict):
                        if event.get('type') == 'thread.started':
                            session = event.get('thread_id', session)
                        if isinstance(event.get('model'), str) and event['model'] not in reported:
                            reported.append(event['model'])
                        if event.get('type') in ('thread.started', 'result', 'turn.completed', 'turn.failed', 'error', 'connection_error'):
                            events.append(event)
                        elif event.get('type') == 'assistant' and isinstance(event.get('message'), dict):
                            events.append({'type': 'assistant', 'message': {'usage': event['message'].get('usage', {})}})
                except ValueError:
                    continue
    for event in events:
        if event.get('type') == 'thread.started':
            session = event.get('thread_id', session)
    usage, models, answer = {}, [], None
    terminal = next((event for event in reversed(events) if event.get('type') in ('result', 'turn.completed', 'turn.failed')), {})
    usage = terminal.get('usage', {})
    models = list(terminal.get('modelUsage', {}))
    if provider == 'claude':
        session = terminal.get('session_id', session)
        measurement = claude_measurement(events)
    else:
        measurement = codex_measurement(session, record.get('started_wall', 0))
        models = reported
    metadata = {'call_id': record['call_id'], 'session': session, 'provider': provider,
                'requested_model': record.get('requested_model'), 'reported_models': models,
                'context': measurement, 'seconds': record.get('seconds'), 'usage': usage,
                'log': record['log'], 'invocation': record['invocation']}
    error_events = [str(event.get('error') or event.get('message') or event.get('result') or '')
                    for event in events if event.get('type') in ('error', 'connection_error', 'turn.failed')
                    or event.get('type') == 'result' and event.get('is_error')]
    terminal_failed = terminal.get('type') == 'turn.failed' or terminal.get('is_error')
    if record.get('exit_code') not in (None, 0) or terminal_failed or not terminal and error_events:
        message = '\n'.join(error_events)
        if not message:
            error_path = Path(record['error_log'])
            message = record.get('launch_error') or (error_path.read_text(errors='replace')[-4000:] if error_path.exists() else '')
        if isinstance(record.get('exit_code'), int) and record['exit_code'] < 0:
            from .recovery import ProviderFailure
            raise ProviderFailure(message or 'Native process interrupted by a signal.', 'connection_wait', time.time() + 30, metadata)
        raise failure(message or 'Native CLI exited without a successful response.', metadata)
    if not terminal:
        raise failure('Native call ended before its terminal response was recorded.', metadata)
    try:
        if provider == 'claude':
            answer = terminal.get('structured_output')
            if answer is None:
                answer = json.loads(terminal.get('result', '{}'))
        else:
            answer = json.loads(Path(record['answer_path']).read_text(encoding='utf-8'))
        if not isinstance(answer, dict):
            raise ValueError('Response must be a JSON object')
    except (ValueError, OSError) as error:
        raise failure('Native response could not be read: ' + str(error), metadata) from error
    return answer, metadata
