"""Model-free projections of runtime-owned tasks and sessions."""
import datetime
import json
from pathlib import Path
import secrets
import shutil
import sys
import time
import unicodedata
import webbrowser

from .store import Store, now
from .workspace import verify

ACTIVE = {'planning', 'running', 'applying', 'verifying', 'repairing'}
ATTENTION = {'blocked', 'interrupted', 'needs_input', 'stale', 'unattended'}
LABELS = {
    'en': {'received': 'Queued', 'ready': 'Ready', 'planning': 'Planning', 'running': 'Working',
           'applying': 'Applying', 'verifying': 'Verifying', 'repairing': 'Repairing',
           'waiting_peer': 'Waiting for peer', 'completed': 'Verified', 'already_satisfied': 'Already satisfied',
           'needs_input': 'Input needed', 'blocked': 'Blocked', 'interrupted': 'Interrupted',
           'stale': 'Evidence changed', 'paused': 'Paused', 'pause_pending': 'Pause requested',
           'unattended': 'Executor absent', 'idle': 'Idle', 'unbound': 'Not started',
           'title': 'HARNESS / OBSERVER', 'scope': 'Runtime-owned sessions only; external sessions are not scanned',
           'worker': 'Worker', 'on': 'online', 'off': 'offline', 'task': 'TASK', 'state': 'STATE',
           'provider': 'AI', 'goal': 'GOAL', 'attention': 'need attention', 'active': 'executing',
           'empty': 'No tasks in this store. Start a task with harness start.',
           'changes': 'RECENT CHANGES', 'stop': 'Ctrl-C closes the monitor; tasks keep running',
           'pending': 'Queued feedback', 'sessions': 'SESSIONS', 'reason': 'Reason'},
    'ko': {'received': '접수·대기', 'ready': '실행 준비', 'planning': '계획 중', 'running': '작업 중',
           'applying': '변경 적용', 'verifying': '검증 중', 'repairing': '수정 중',
           'waiting_peer': '상대 답변 대기', 'completed': '검증 완료', 'already_satisfied': '기존 조건 충족',
           'needs_input': '입력 필요', 'blocked': '막힘', 'interrupted': '중단됨',
           'stale': '검증 근거 변경', 'paused': '일시정지', 'pause_pending': '정지 요청됨',
           'unattended': '실행기 없음', 'idle': '대기', 'unbound': '시작 전',
           'title': 'HARNESS / OBSERVER', 'scope': '하네스 관리 세션만 표시 · 외부 세션은 조회하지 않음',
           'worker': '워커', 'on': '실행 중', 'off': '미실행', 'task': '작업', 'state': '상태',
           'provider': 'AI', 'goal': '목적', 'attention': '개입 필요', 'active': '실행 중',
           'empty': '이 저장소에는 작업이 없습니다. harness start로 시작하세요.',
           'changes': '최근 상태 변화', 'stop': 'Ctrl-C: 관찰 화면만 종료 · 실행 중인 작업은 유지',
           'pending': '피드백 대기', 'sessions': '세션', 'reason': '이유'},
}


def age_seconds(stamp):
    try:
        return max(0, int((datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(stamp)).total_seconds()))
    except (ValueError, TypeError):
        return 0


def event_view(row):
    data = json.loads(row['payload'])
    note = data.get('reason') or data.get('text') or data.get('question') or data.get('phase') or ''
    if row['kind'] == 'action_prepared':
        note = ', '.join(c['path'] for c in data.get('changes', []))
    if row['kind'] == 'verified':
        checks = data.get('checks', [])
        note = '%d / %d' % (sum(bool(c.get('passed')) for c in checks), len(checks))
    return {'seq': row['seq'], 'task_id': row['task_id'], 'kind': row['kind'], 'at': row['at'],
            'note': str(note)[:600], 'age': age_seconds(row['at'])}


def snapshot(store, task_id=None):
    """Observe persisted facts and lock ownership; never invoke a model or repair files."""
    tasks = []
    summaries = store.list()
    selected = [r for r in summaries if r['id'] == task_id] if task_id else summaries[:100]
    if task_id and not selected:
        raise ValueError('Unknown task: ' + task_id)
    store.db.execute('BEGIN')
    try:
        for summary in selected:
            task = store.get(summary['id'])
            data, state = task['data'], task['status']
            busy = store.locked('task:' + task['id'])
            paused = store.paused(task['id'])
            checks = data.get('evidence', [])
            issue = ''
            if state in ('completed', 'already_satisfied') and data.get('contract'):
                try:
                    checks = verify(task['workspace'], data['contract'])
                    if not all(c.get('passed') for c in checks):
                        state = 'stale'
                except (OSError, ValueError) as error:
                    state, issue = 'stale', str(error)
            if state in ACTIVE and not busy:
                state = 'unattended'
            if paused and state not in ('completed', 'already_satisfied', 'stale'):
                state = 'pause_pending' if busy and task['status'] != 'paused' else 'paused'
            messages = store.messages(task['id'])
            sessions = []
            bindings = dict(data.get('sessions', {}))
            if not any(k == task['provider'] or k.startswith(task['provider'] + ':') for k in bindings):
                bindings[task['provider']] = None
            for name in data.get('peers', {}):
                bindings.setdefault('peer:' + name, None)
            for alias, session_id in bindings.items():
                peer = alias.startswith('peer:')
                name = alias[5:] if peer else alias
                provider = data.get('peers', {}).get(name, {}).get('provider', 'unknown') if peer else alias.split(':')[0]
                in_flight = any(m['target'] == name and m['status'] == 'in_flight' for m in messages) if peer else task['status'] in ('planning', 'running') and alias == task['provider']
                session_state = 'running' if in_flight and busy else 'idle' if session_id else 'unbound'
                sessions.append({'name': name, 'provider': provider, 'id': session_id, 'peer': peer,
                                 'state': session_state, 'managed': True,
                                 'context_tokens': data.get('context_tokens', {}).get(alias)})
            rows = store.db.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 24', (task['id'],)).fetchall()
            events = [event_view(r) for r in rows]
            contract = data.get('contract') or {}
            tasks.append({'id': task['id'], 'provider': task['provider'], 'status': state,
                          'recorded_status': task['status'], 'revision': task['revision'],
                          'goal': contract.get('goal') or task['prompt'], 'request': task['prompt'],
                          'workspace': task['workspace'], 'updated': task['updated'],
                          'age': age_seconds(task['updated']), 'busy': busy, 'pause_requested': paused,
                          'reason': issue or data.get('reason', ''), 'questions': contract.get('questions', []),
                          'constraints': contract.get('constraints', []), 'steps': contract.get('steps', []),
                          'attempt': data.get('attempt', 0), 'budget': data.get('options', {}).get('max_attempts', 3),
                          'checks': checks, 'sessions': sessions, 'feedback': store.inbox(task['id']),
                          'messages': [{k: m[k] for k in ('id', 'target', 'question', 'reply', 'status', 'attempts', 'retry_at', 'error', 'updated')}
                                       for m in messages[-30:]], 'events': events,
                          'attention': state in ATTENTION})
        rows = store.db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 20').fetchall()
        global_events = [event_view(r) for r in rows]
        cursor = store.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
    finally:
        store.db.rollback()
    tasks.sort(key=lambda t: t['updated'], reverse=True)
    tasks.sort(key=lambda t: (not t['attention'], not t['busy']))
    return {'at': now(), 'scope': 'managed', 'home': str(store.home), 'cursor': cursor,
            'worker_running': store.locked('worker'), 'tasks': tasks, 'total': len(summaries),
            'events': global_events, 'labels': LABELS}


def clean(text):
    return ''.join(c if c.isprintable() else ' ' for c in str(text))


def fit(text, width):
    output, used = '', 0
    for char in clean(text):
        size = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
        if used + size > width:
            break
        output += char
        used += size
    return output + ' ' * max(0, width - used)


def terminal(data, lang='en', width=110):
    t = LABELS[lang]
    rows = [t['title'] + '  |  ' + t['worker'] + ': ' + t['on' if data['worker_running'] else 'off'],
            t['scope'], '%s %d  ·  %s %d  ·  %d / %d' %
            (t['active'], sum(x['busy'] for x in data['tasks']), t['attention'],
             sum(x['attention'] for x in data['tasks']), len(data['tasks']), data['total']), '']
    goal_width = max(12, width - 48)
    rows.append(fit(t['task'], 14) + fit(t['state'], 22) + fit(t['provider'], 12) + t['goal'])
    rows.append('─' * min(width, 150))
    for task in data['tasks'][:18]:
        marker = '! ' if task['attention'] else '> ' if task['busy'] else '  '
        rows.append(marker + fit(task['id'][:10], 12) + fit(t.get(task['status'], task['status']), 22)
                    + fit(task['provider'], 12) + fit(task['goal'], goal_width))
        if task['reason'] or task['questions']:
            rows.append('  ' + fit(task['reason'] or task['questions'][0], max(20, width - 2)))
        for session in task['sessions']:
            rows.append('    ' + fit(session['name'], 18) + fit(t[session['state']], 12)
                        + clean(session['id'] or '—'))
        if task['feedback']:
            rows.append('    %s: %d' % (t['pending'], len(task['feedback'])))
    if not data['tasks']:
        rows.append(t['empty'])
    rows.extend(['', t['changes']])
    for event in data['events'][:6]:
        rows.append(fit(event['at'][11:19], 10) + fit(event['task_id'][:10], 12)
                    + fit(event['kind'], 29) + fit(event['note'], max(10, width - 51)))
    rows.extend(['', t['stop']])
    return '\n'.join(rows)


def watch(store, interval=2, once=False, task_id=None, lang='en'):
    if not 0.5 <= interval <= 60:
        raise ValueError('Monitor interval must be between 0.5 and 60 seconds')
    interactive = sys.stdout.isatty() and not once
    previous = None
    try:
        if interactive:
            print('\033[?1049h\033[?25l', end='', flush=True)
        while True:
            data = snapshot(store, task_id)
            rendered = terminal(data, lang, shutil.get_terminal_size((110, 30)).columns)
            if rendered != previous:
                if interactive:
                    print('\033[H\033[2J', end='')
                print(rendered, flush=True)
                previous = rendered
            if once:
                break
            time.sleep(interval)
    finally:
        if interactive:
            print('\033[?25h\033[?1049l', end='', flush=True)


def serve(home, port=0, open_browser=True, lang='en'):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    if not 0 <= port <= 65535:
        raise ValueError('Invalid port')
    token = secrets.token_urlsafe(32)
    template = Path(__file__).with_name('observer.html').read_text(encoding='utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, code, content, kind='application/json'):
            raw = content.encode('utf-8')
            self.send_response(code)
            for key, value in {'Content-Type': kind + '; charset=utf-8', 'Content-Length': str(len(raw)),
                               'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY',
                               'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
                               'Cross-Origin-Resource-Policy': 'same-origin',
                               'Content-Security-Policy': "default-src 'none'; script-src 'nonce-%s'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % token}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(raw)

        def valid_token(self):
            provided = self.headers.get('X-Harness-Token', '')
            return provided.isascii() and secrets.compare_digest(provided, token)

        def do_GET(self):
            if self.headers.get('Host') != authority:
                self.respond(403, '{}')
                return
            if self.path == '/':
                self.respond(200, template.replace('__TOKEN__', token).replace('__LANG__', lang), 'text/html')
            elif self.path == '/state' and self.valid_token():
                store = Store(home)
                try:
                    self.respond(200, json.dumps(snapshot(store), ensure_ascii=False))
                finally:
                    store.close()
            else:
                self.respond(404, '{}')

        def do_POST(self):
            if self.headers.get('Host') != authority or self.headers.get('Origin') != origin or not self.valid_token():
                self.respond(403, '{}')
                return
            if self.path not in ('/feedback', '/pause', '/continue'):
                self.respond(404, '{}')
                return
            store = Store(home)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32000:
                    raise ValueError('Invalid body size')
                body = json.loads(self.rfile.read(size))
                task_id = body.get('task_id')
                if not isinstance(task_id, str):
                    raise ValueError('Task ID required')
                # Hold the write reservation through revision validation and enqueue/control.
                store.db.execute('BEGIN IMMEDIATE')
                task = store.get(task_id)
                if self.path == '/feedback' and body.get('revision') != task['revision']:
                    self.respond(409, json.dumps({'error': 'Task changed; refresh and review before submitting'}))
                    return
                if self.path == '/feedback':
                    text = body.get('text')
                    if not isinstance(text, str) or not text.strip() or len(text) > 24000:
                        raise ValueError('Nonempty feedback required')
                    message_id = store.enqueue(task_id, text, None)
                    self.respond(200, json.dumps({'queued': message_id, 'worker_running': store.locked('worker')}))
                else:
                    store.set_paused(task_id, self.path == '/pause')
                    self.respond(200, json.dumps({'saved': True, 'worker_running': store.locked('worker')}))
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                self.respond(400, json.dumps({'error': str(error)}))
            finally:
                store.close()

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    authority = '127.0.0.1:%d' % server.server_port
    origin = 'http://' + authority
    print(origin, flush=True)
    if open_browser:
        webbrowser.open(origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
