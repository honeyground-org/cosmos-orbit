"""Bounded stdio MCP client. No host configuration discovery or shell invocation."""
import json
import os
import selectors
import signal
import subprocess
import tempfile
import time


class ToolError(RuntimeError):
    """A failed tool call with its structured details preserved for the caller."""
    def __init__(self, value):
        self.value = value
        super().__init__('MCP tool failed: ' + json.dumps(value, ensure_ascii=False)[:1000])


class Client:
    def __init__(self, argv, cwd, timeout=10, env=None, lease_fds=()):
        if (not isinstance(argv, list) or not argv or len(argv) > 100
                or any(not isinstance(v, str) or not v or '\0' in v for v in argv)):
            raise ValueError('MCP command must be a nonempty JSON argument array')
        self.deadline = time.monotonic() + timeout
        self.buffer = b''
        self.sequence = 0
        self.stderr = tempfile.TemporaryFile()
        self.selector = selectors.DefaultSelector()
        self.process = None
        try:
            self.process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr, start_new_session=True, pass_fds=lease_fds)
            os.set_blocking(self.process.stdin.fileno(), False)
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            hello = self.request('initialize', {'protocolVersion': '2024-11-05',
                'capabilities': {}, 'clientInfo': {'name': 'cosmos-orbit', 'version': '0.1.0'}})
            if not isinstance(hello.get('serverInfo'), dict):
                raise ValueError('MCP initialization omitted server identity')
            self.server = hello['serverInfo']
            self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
            listing = self.request('tools/list', {})
            self.tools = {item['name']: item for item in listing.get('tools', [])
                          if isinstance(item, dict) and isinstance(item.get('name'), str)}
        except BaseException:
            self.close()
            raise

    def send(self, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8') + b'\n'
        if len(raw) > 65536:
            raise ValueError('MCP request exceeds 64 KB')
        with selectors.DefaultSelector() as writable:
            writable.register(self.process.stdin, selectors.EVENT_WRITE)
            while raw:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise TimeoutError('MCP write deadline exceeded')
                try:
                    sent = os.write(self.process.stdin.fileno(), raw)
                    raw = raw[sent:]
                except BlockingIOError:
                    continue

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        received = 0
        while True:
            while b'\n' in self.buffer:
                line, self.buffer = self.buffer.split(b'\n', 1)
                if not line.strip():
                    continue
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError('Invalid MCP response')
                # Server-initiated sampling/elicitation is not an execution capability.
                if 'method' in message:
                    if 'id' in message:
                        self.send({'jsonrpc': '2.0', 'id': message['id'], 'error': {
                            'code': -32601, 'message': 'Server requests are not supported'}})
                    continue
                if message.get('id') != request_id:
                    continue
                if message.get('error'):
                    raise RuntimeError('MCP request failed: ' + str(message['error'])[:1000])
                if not isinstance(message.get('result'), dict):
                    raise ValueError('MCP result must be an object')
                return message['result']
            remaining = self.deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError('MCP request deadline exceeded')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('MCP server closed its output')
            received += len(chunk)
            if received > 2 * 1024 * 1024:
                raise ValueError('MCP response exceeds 2 MB')
            self.buffer += chunk

    def call(self, name, arguments):
        if name not in self.tools:
            raise ValueError('MCP tool is unavailable: ' + name)
        result = self.request('tools/call', {'name': name, 'arguments': arguments})
        value = result.get('structuredContent')
        if isinstance(value, dict):
            if result.get('isError'):
                raise ToolError(value)
            return value
        content = result.get('content', [])
        if not isinstance(content, list) or any(not isinstance(c, dict) for c in content):
            raise ValueError('MCP tool content must be an array of objects')
        parts = [c.get('text', '') for c in content if c.get('type') == 'text']
        if any(not isinstance(part, str) for part in parts):
            raise ValueError('MCP text content must be a string')
        text = '\n'.join(parts)
        try:
            value = json.loads(text)
        except ValueError:
            value = text
        if result.get('isError'):
            raise ToolError(value)
        return value

    def close(self):
        if self.process is not None:
            if self.process.stdin:
                self.process.stdin.close()
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait()
                except ProcessLookupError:
                    pass
            if self.process.stdout:
                self.process.stdout.close()
        self.selector.close()
        self.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
