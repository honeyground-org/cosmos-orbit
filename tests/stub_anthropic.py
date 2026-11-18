#!/usr/bin/env python3
"""A local stand-in for an Anthropic-compatible endpoint (Ollama's cloud, say), for checking a route end to end.

    python3 tests/stub_anthropic.py PORT LOG [TOKEN [MARKER,MARKER…]]

Answers POST /v1/messages (streamed or not) with one fixed text and appends one JSON line per request to LOG: the
path, the model asked for, whether the bearer token was the one expected (the token itself is never written) and
which MARKERs the request carries.
Anything else answers 404. It serves until killed.

With STUB_REFUSE=STATUS in its environment it refuses every request with that HTTP status instead, the way a
service refuses once a free period is over: the body says STUB_REFUSE_TEXT (by default Ollama's words for a model
that needs a paid plan), shaped as an Anthropic error unless STUB_REFUSE_SHAPE=plain asks for {"error": TEXT}.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = 'ROUTE-STUB-ANSWER'

REFUSE_TEXT = 'this model requires a subscription, upgrade for access: https://ollama.com/upgrade (ref: stub)'


def refusal():
    """(status, body) when the stub is told to refuse, else None."""
    status = os.environ.get('STUB_REFUSE')
    if not status:
        return None
    text = os.environ.get('STUB_REFUSE_TEXT') or REFUSE_TEXT
    if os.environ.get('STUB_REFUSE_SHAPE') == 'plain':
        return int(status), {'error': text}
    return int(status), {'type': 'error', 'error': {'type': 'permission_error', 'message': text}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self, body):
        auth = self.headers.get('Authorization') or ''
        expected = 'Bearer ' + (self.server.expected_token or '')
        with open(self.server.log, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'path': self.path, 'model': body.get('model'), 'stream': bool(body.get('stream')),
                                     'token_ok': bool(self.server.expected_token) and auth == expected,
                                     'x_api_key_sent': bool(self.headers.get('x-api-key')),
                                     'authorization_kind': (auth.split(' ')[0] if auth else None),
                                     'authorization_is_expected': auth == expected,
                                     'x_api_key_ok': bool(self.server.expected_token)
                                     and self.headers.get('x-api-key') == self.server.expected_token,
                                     'markers': [m for m in self.server.markers if m in json.dumps(body, ensure_ascii=False)]})
                         + '\n')

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}
        self._record(body)
        refused = refusal()
        if refused:
            data = json.dumps(refused[1]).encode()
            self.send_response(refused[0])
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if not self.path.split('?')[0].endswith('/v1/messages'):
            self.send_response(404)
            self.end_headers()
            return
        model = body.get('model') or 'stub'
        message = {'id': 'msg_stub', 'type': 'message', 'role': 'assistant', 'model': model, 'stop_reason': 'end_turn',
                   'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 1}}
        if not body.get('stream'):
            payload = json.dumps(dict(message, content=[{'type': 'text', 'text': ANSWER}])).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        events = [
            ('message_start', {'type': 'message_start', 'message': dict(message, content=[], stop_reason=None)}),
            ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
            ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': ANSWER}}),
            ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
            ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                               'usage': {'output_tokens': 1}}),
            ('message_stop', {'type': 'message_stop'}),
        ]
        for name, data in events:
            self.wfile.write(('event: %s\ndata: %s\n\n' % (name, json.dumps(data))).encode())
            self.wfile.flush()


def main():
    port, log = int(sys.argv[1]), sys.argv[2]
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.log = log
    server.expected_token = sys.argv[3] if len(sys.argv) > 3 else None
    server.markers = [m for m in (sys.argv[4] if len(sys.argv) > 4 else '').split(',') if m]   # words to look for in each request
    server.serve_forever()


if __name__ == '__main__':
    main()
