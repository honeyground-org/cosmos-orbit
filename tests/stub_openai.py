#!/usr/bin/env python3
"""A local stand-in for an OpenAI Responses-compatible endpoint, for checking a Codex route end to end.

    python3 tests/stub_openai.py PORT LOG TOKEN

Answers POST …/responses (streamed) with one fixed text and appends one JSON line per request to LOG: the method,
the path, the model asked for and whether the bearer token was the one expected (the token itself is never
written). GET …/models answers an empty list; anything else answers 404. It serves until killed.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = 'ROUTE-STUB-ANSWER'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _record(self, method, body):
        auth = self.headers.get('Authorization') or ''
        expected = 'Bearer ' + (self.server.expected_token or '')
        with open(self.server.log, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps({'method': method, 'path': self.path, 'model': body.get('model'),
                                     'token_ok': bool(self.server.expected_token) and auth == expected}) + '\n')

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._record('GET', {})
        if self.path.split('?')[0].endswith('/models'):
            self._json(200, {'object': 'list', 'data': [], 'models': []})
        else:
            self._json(404, {'error': {'message': 'not found'}})

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}
        self._record('POST', body)
        if not self.path.split('?')[0].endswith('/responses'):
            self._json(404, {'error': {'message': 'not found'}})
            return
        model = body.get('model') or 'stub'
        item = {'type': 'message', 'role': 'assistant', 'id': 'msg_stub', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': ANSWER, 'annotations': []}]}
        usage = {'input_tokens': 1, 'input_tokens_details': {'cached_tokens': 0}, 'output_tokens': 1,
                 'output_tokens_details': {'reasoning_tokens': 0}, 'total_tokens': 2}
        response = {'id': 'resp_stub', 'object': 'response', 'model': model, 'status': 'in_progress', 'output': []}
        events = [
            ('response.created', {'type': 'response.created', 'response': response}),
            ('response.output_item.added', {'type': 'response.output_item.added', 'output_index': 0,
                                            'item': dict(item, status='in_progress', content=[])}),
            ('response.output_text.delta', {'type': 'response.output_text.delta', 'item_id': 'msg_stub',
                                            'output_index': 0, 'content_index': 0, 'delta': ANSWER}),
            ('response.output_item.done', {'type': 'response.output_item.done', 'output_index': 0, 'item': item}),
            ('response.completed', {'type': 'response.completed',
                                    'response': dict(response, status='completed', output=[item], usage=usage)}),
        ]
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        for name, data in events:
            self.wfile.write(('event: %s\ndata: %s\n\n' % (name, json.dumps(data))).encode())
            self.wfile.flush()


def main():
    port, log = int(sys.argv[1]), sys.argv[2]
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.log = log
    server.expected_token = sys.argv[3] if len(sys.argv) > 3 else None
    server.serve_forever()


if __name__ == '__main__':
    main()
