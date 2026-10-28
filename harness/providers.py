"""Adapters to installed, authenticated CLIs; never select a second provider implicitly."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid


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


class Provider:
    def __init__(self, name, home, model=None, timeout=180):
        if name not in ('codex', 'claude') or not shutil.which(name):
            raise ValueError('Install and authenticate the selected CLI: ' + name)
        self.name, self.home, self.model, self.timeout = name, Path(home), model, timeout

    def call(self, task_id, prompt, schema, session=None):
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
                       '--output-format', 'json', '--json-schema', json.dumps(schema)]
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
        if self.model:
            # Options precede the stdin prompt marker for Codex.
            if self.name == 'codex':
                command[-1:-1] = ['--model', self.model]
            else:
                command += ['--model', self.model]
        started = time.monotonic()
        invocation = directory / (call_id + '.invocation.json')
        version = subprocess.run([self.name, '--version'], capture_output=True, text=True, timeout=10)
        invocation.write_text(json.dumps({'argv': command, 'version': version.stdout.strip()}, indent=2), encoding='utf-8')
        os.chmod(invocation, 0o600)
        with log_path.open('w', encoding='utf-8') as stdout, error_path.open('w', encoding='utf-8') as stderr:
            os.chmod(log_path, 0o600)
            os.chmod(error_path, 0o600)
            process = subprocess.Popen(command, cwd=str(directory), stdin=subprocess.PIPE,
                                       stdout=stdout, stderr=stderr, text=True, encoding='utf-8',
                                       start_new_session=True)
            try:
                process.communicate(prompt, timeout=self.timeout)
            except BaseException:
                os.killpg(process.pid, signal.SIGTERM)
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
        if self.name == 'claude':
            envelope = json.loads(log_path.read_text(encoding='utf-8'))
            if envelope.get('is_error'):
                raise RuntimeError('Claude did not complete the call; inspect ' + str(log_path))
            session = envelope.get('session_id', session)
            answer = envelope.get('structured_output')
            if answer is None:
                answer = json.loads(envelope.get('result', '{}'))
            usage = envelope.get('usage', {})
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
        if not isinstance(answer, dict):
            raise ValueError('Provider response must be a structured object')
        return answer, {'session': session, 'provider': self.name,
                        'seconds': round(time.monotonic() - started, 3),
                        'usage': usage, 'log': str(log_path), 'invocation': str(invocation)}
