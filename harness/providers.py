"""Adapters to installed, authenticated CLIs; never select a second provider implicitly."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid

from .context import claude_measurement, codex_measurement


def obj(properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


STRING = {'type': 'string'}
STRINGS = {'type': 'array', 'items': STRING}
CHECK = obj({'path': STRING, 'kind': {'type': 'string', 'enum': ['contains', 'not_contains', 'equals', 'json_equals']},
             'expected': STRING, 'pointer': STRING, 'reason': STRING})
PLAN = obj({'goal': STRING, 'constraints': STRINGS, 'local_write': {'type': 'boolean'},
            'permission_basis': STRING, 'questions': STRINGS, 'steps': STRINGS, 'files': STRINGS,
            'checks': {'type': 'array', 'items': CHECK}})
CHANGE = obj({'path': STRING, 'before_sha': STRING, 'content': STRING})
EXECUTE = obj({'summary': STRING, 'changes': {'type': 'array', 'items': CHANGE}, 'blocker': STRING,
               'messages': {'type': 'array', 'items': obj({'to': STRING, 'text': STRING})}})
REPLY = obj({'accepted': {'type': 'boolean'}, 'reply': STRING})


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
    return {'file_read_write': True, 'shell': provider == 'codex',
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
                command[command.index('--tools') + 1] = 'Read,Edit,Write,Glob,Grep'
                command += ['--restricted', '--safe-mode', '--disable-slash-commands',
                            '--permission-mode', 'acceptEdits', '--permission-prompts', 'none']
            else:
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
        version = subprocess.run([self.name, '--version'], capture_output=True, text=True, timeout=10)
        invocation.write_text(json.dumps({'argv': command, 'version': version.stdout.strip()}, indent=2), encoding='utf-8')
        os.chmod(invocation, 0o600)
        with log_path.open('w', encoding='utf-8') as stdout, error_path.open('w', encoding='utf-8') as stderr:
            os.chmod(log_path, 0o600)
            os.chmod(error_path, 0o600)
            process = subprocess.Popen(command, cwd=str(workspace or directory), stdin=subprocess.PIPE,
                                       stdout=stdout, stderr=stderr, text=True, encoding='utf-8',
                                       start_new_session=True, pass_fds=lease_fds)
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
        if process.returncode:
            raise RuntimeError('Selected CLI failed (exit %s). Inspect %s and %s' %
                               (process.returncode, log_path, error_path))
        usage = {}
        reported_models = []
        if self.name == 'claude':
            events = [json.loads(line) for line in log_path.read_text(encoding='utf-8').splitlines() if line.strip()]
            envelope = next((e for e in reversed(events) if e.get('type') == 'result'), {})
            if not envelope:
                raise ValueError('Claude stream has no result envelope')
            context = claude_measurement(events)
            if envelope.get('is_error'):
                raise RuntimeError('Claude did not complete the call; inspect ' + str(log_path))
            session = envelope.get('session_id', session)
            answer = envelope.get('structured_output')
            if answer is None:
                answer = json.loads(envelope.get('result', '{}'))
            usage = envelope.get('usage', {})
            reported_models = list(envelope.get('modelUsage', {}))
        else:
            answer = json.loads(output_path.read_text(encoding='utf-8'))
            for line in log_path.read_text(encoding='utf-8').splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'thread.started':
                    session = event.get('thread_id', session)
                if event.get('type') == 'turn.completed':
                    usage = event.get('usage', {})
                if isinstance(event.get('model'), str) and event['model'] not in reported_models:
                    reported_models.append(event['model'])
            context = codex_measurement(session, started_wall)
        if not isinstance(answer, dict):
            raise ValueError('Provider response must be a structured object')
        return answer, {'session': session, 'provider': self.name,
                        'requested_model': self.model, 'reported_models': reported_models, 'context': context,
                        'seconds': round(time.monotonic() - started, 3),
                        'usage': usage, 'log': str(log_path), 'invocation': str(invocation)}
