"""Model-free projections of runtime-owned tasks and sessions."""
import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import time
import unicodedata
import webbrowser

from .store import Store, now
from .commands import browser_guide
from .context import limit_for
from .workspace import verify

ACTIVE = {'planning', 'running', 'applying', 'verifying', 'repairing'}
ATTENTION = {'needs_review', 'blocked', 'interrupted', 'needs_input', 'stale', 'unattended'}
LABELS = {
    'en': {'received': 'Queued', 'ready': 'Ready', 'planning': 'Planning', 'running': 'Working',
           'applying': 'Applying', 'verifying': 'Verifying', 'repairing': 'Repairing',
           'waiting_peer': 'Waiting for peer', 'completed': 'Verified', 'already_satisfied': 'Already satisfied',
           'needs_review': 'Review changes', 'needs_input': 'Input needed', 'blocked': 'Blocked', 'interrupted': 'Interrupted',
           'stale': 'Evidence changed', 'paused': 'Paused', 'pause_pending': 'Pause requested',
           'unattended': 'Executor absent', 'idle': 'Idle', 'unbound': 'Not started',
           'title': 'COSMOS ORBIT / OBSERVER', 'scope': 'Runtime-owned sessions only; external sessions are not scanned',
           'worker': 'Worker', 'on': 'online', 'off': 'offline', 'task': 'TASK', 'state': 'STATE',
           'provider': 'AI', 'goal': 'GOAL', 'attention': 'need attention', 'active': 'executing',
           'empty': 'No tasks in this store. Start a task with cosmos-orbit start.',
           'changes': 'RECENT CHANGES', 'stop': 'Ctrl-C closes the monitor; tasks keep running',
           'pending': 'Queued feedback', 'sessions': 'SESSIONS', 'reason': 'Reason'},

}


def age_seconds(stamp):
    try:
        return max(0, int((datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(stamp)).total_seconds()))
    except (ValueError, TypeError):
        return 0


def event_view(row):
    data = json.loads(row['payload'])
    note = data.get('note') or data.get('reason') or data.get('text') or data.get('question') or data.get('phase') or ''
    if row['kind'] == 'action_prepared':
        note = ', '.join(c['path'] for c in data.get('changes', []))
    if row['kind'] == 'verified':
        checks = data.get('checks', [])
        note = '%d / %d' % (sum(bool(c.get('passed')) for c in checks), len(checks))
    return {'seq': row['seq'], 'task_id': row['task_id'], 'kind': row['kind'], 'at': row['at'],
            'note': str(note)[:600], 'age': age_seconds(row['at']),
            'layer': data.get('layer'), 'direction': data.get('direction'),
            'provider': data.get('provider'), 'alias': data.get('alias'),
            'tool': data.get('tool'), 'call_id': data.get('call_id'),
            'exit_code': data.get('exit_code'), 'failed': data.get('failed', False),
            'tool_id': data.get('tool_id')}


def model_options():
    """Read display-only suggestions without auth, network requests or model execution."""
    choices = {'codex': [], 'claude': [
        {'id': 'sonnet', 'label': 'Sonnet — everyday coding', 'source': 'CLI alias'},
        {'id': 'opus', 'label': 'Opus — complex reasoning', 'source': 'CLI alias'},
        {'id': 'haiku', 'label': 'Haiku — lightweight tasks', 'source': 'CLI alias'}]}
    cache = Path(os.environ.get('CODEX_HOME') or str(Path.home() / '.codex')) / 'models_cache.json'
    try:
        with cache.open(encoding='utf-8') as handle:
            raw = handle.read(2_000_001)
        if len(raw) > 2_000_000:
            return choices
        payload = json.loads(raw)
        models = payload.get('models', []) if isinstance(payload, dict) else []
        if not isinstance(models, list):
            return choices
        for model in models[:100]:
            if not isinstance(model, dict) or model.get('visibility') != 'list':
                continue
            name = model.get('slug')
            if isinstance(name, str) and 0 < len(name) <= 200:
                title = model.get('display_name')
                choices['codex'].append({'id': name, 'label': title[:200] if isinstance(title, str) else name,
                                         'source': 'Local CLI cache'})
    except (OSError, ValueError, UnicodeError):
        pass
    return choices


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
            if state == 'needs_review' and data.get('development'):
                try:
                    from .development import evidence_current
                    if not evidence_current(task):
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
                                 'requested_model': data.get('session_metadata', {}).get(alias, {}).get('requested_model',
                                     data.get('peers', {}).get(name, {}).get('model') if peer else
                                     data.get('options', {}).get('model') if alias == task['provider'] else None),
                                 'reported_models': data.get('session_metadata', {}).get(alias, {}).get('reported_models', []),
                                 'context_tokens': data.get('context_measurements', {}).get(alias, {}).get('tokens'),
                                 'context_limit': limit_for(data, alias, provider),
                                 'context_source': data.get('context_measurements', {}).get(alias, {}).get('source', 'unavailable'),
                                 'handoff_count': data.get('handoff_counts', {}).get(alias, 0)})
            rows = store.db.execute('SELECT * FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 24', (task['id'],)).fetchall()
            events = [event_view(r) for r in rows]
            event_count = store.db.execute('SELECT COUNT(*) FROM events WHERE task_id=?', (task['id'],)).fetchone()[0]
            response_rows = store.db.execute("SELECT * FROM events WHERE task_id=? AND kind='provider_message' ORDER BY seq DESC LIMIT 6", (task['id'],)).fetchall()
            layer_activity = {}
            for layer, kinds_for_layer in {
                'control': ('received', 'execution_plan_ready', 'feedback_applied', 'routing_applied', 'continuation_scheduled'),
                'execution': ('model_started', 'model_finished', 'provider_message'),
                'context': ('memory_context_prepared', 'memory_context_submitted', 'session_checkpoint', 'automatic_checkpoint'),
                'evidence': ('verified', 'result_published', 'baseline_recorded')}.items():
                placeholders = ','.join('?' for _ in kinds_for_layer)
                row = store.db.execute("SELECT * FROM events WHERE task_id=? AND (json_extract(payload,'$.layer')=? OR kind IN (" + placeholders + ")) ORDER BY seq DESC LIMIT 1", (task['id'], layer, *kinds_for_layer)).fetchone()
                layer_activity[layer] = event_view(row) if row else None
            call_state = store.db.execute("SELECT kind,at FROM events WHERE task_id=? AND kind IN ('model_started','model_finished','interrupted') ORDER BY seq DESC LIMIT 1", (task['id'],)).fetchone()
            runtime = None
            if busy and call_state and call_state['kind'] == 'model_started':
                elapsed = age_seconds(call_state['at'])
                timeout = data.get('options', {}).get('timeout', 420)
                runtime = {'started': call_state['at'], 'elapsed_seconds': elapsed,
                           'timeout_seconds': timeout, 'remaining_seconds': max(0, timeout - elapsed)}
            contract = data.get('contract') or {}
            call_rows = store.db.execute("SELECT payload FROM events WHERE task_id=? AND kind IN ('model_finished','peer_response_received')", (task['id'],)).fetchall()
            calls = [json.loads(r['payload']) for r in call_rows]
            kinds = {r[0] for r in store.db.execute('SELECT DISTINCT kind FROM events WHERE task_id=?', (task['id'],))}
            visited = [0]
            if 'contract_created' in kinds:
                visited.append(1)
            if any(c.get('phase') == 'running' for c in calls):
                visited.append(2)
            if 'action_applied' in kinds:
                visited.append(3)
            if 'verified' in kinds:
                visited.append(4)
            usage_input = sum(c.get('usage', {}).get('input_tokens', 0) for c in calls)
            usage_output = sum(c.get('usage', {}).get('output_tokens', 0) for c in calls)
            stage = {'received': 0, 'planning': 1, 'ready': 2, 'running': 2, 'waiting_peer': 2,
                     'applying': 3, 'verifying': 4, 'repairing': 2, 'completed': 5,
                     'already_satisfied': 5, 'stale': 4, 'needs_review': 4}.get(task['status'], 1 if not contract else 2)
            if state == 'stale':
                stage = 4
            if data.get('pending'):
                stage = 3
            if data.get('development') and state in ('blocked', 'interrupted', 'unattended', 'paused', 'pause_pending'):
                stage = 1 if data.get('plan_requested') else {'verifying': 4, 'applying': 3}.get(data['development'].get('phase'), 2)
            started = datetime.datetime.fromisoformat(task['created'])
            ended = datetime.datetime.now(datetime.timezone.utc) if busy else datetime.datetime.fromisoformat(task['updated'])
            tasks.append({'id': task['id'], 'provider': task['provider'], 'status': state,
                          'stage': stage, 'visited': visited, 'requested_model': data.get('options', {}).get('model'),
                          'routing': store.routing(task['id']), 'last_summary': data.get('last_summary', ''),
                          'files': data.get('development', {}).get('changed_paths', contract.get('files', [])),
                          'development': data.get('development'), 'execution_plan': data.get('execution_plan'),
                          'objective_progress': data.get('objective_progress'),
                          'memory': {'mode': data.get('memory', {}).get('mode', 'off'),
                                     'project_id': data.get('memory', {}).get('project_id'),
                                     'seconds': (data.get('memory', {}).get('latest') or {}).get('seconds'),
                                     'notes': (data.get('memory', {}).get('latest') or {}).get('notes', []),
                                     'outbox': [{'id': m['id'], 'status': m['status'], 'attempts': m['attempts'],
                                                 'error': m.get('error'), 'ack': m.get('ack')}
                                                for m in data.get('memory', {}).get('outbox', [])]},
                          'metrics': {'calls': len(calls), 'model_seconds': round(sum(c.get('seconds', 0) for c in calls), 1),
                                      'input_tokens': usage_input, 'output_tokens': usage_output,
                                      'elapsed_seconds': max(0, round((ended-started).total_seconds()))},
                          'recorded_status': task['status'], 'revision': task['revision'],
                          'goal': data.get('presentation', {}).get('title') or contract.get('goal') or task['prompt'], 'request': data.get('mission_goal', task['prompt']),
                          'project': str(Path(data.get('development', {}).get('source', task['workspace'])).name),
                          'presentation': {k: str(v)[:16000] for k, v in data.get('presentation', {}).items()
                                           if k in ('title', 'summary', 'report', 'report_source', 'report_error')},
                          'workspace': task['workspace'], 'updated': task['updated'], 'runtime': runtime,
                          'age': age_seconds(task['updated']), 'busy': busy, 'pause_requested': paused,
                          'reason': issue or data.get('reason', ''), 'questions': contract.get('questions', []),
                          'constraints': contract.get('constraints', []), 'steps': contract.get('steps', []),
                          'attempt': data.get('attempt', 0), 'budget': data.get('options', {}).get('max_attempts', 3),
                          'checks': checks, 'sessions': sessions, 'feedback': store.inbox(task['id']),
                          'messages': [{k: m[k] for k in ('id', 'target', 'question', 'reply', 'status', 'attempts', 'retry_at', 'error', 'updated')}
                                       for m in messages[-30:]], 'events': events, 'event_count': event_count,
                          'responses': [event_view(r) for r in reversed(response_rows)],
                          'layer_activity': layer_activity,
                          'attention': state in ATTENTION})
        rows = store.db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 20').fetchall()
        global_events = [event_view(r) for r in rows]
        cursor = store.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
    finally:
        store.db.rollback()
    tasks.sort(key=lambda t: t['updated'], reverse=True)
    tasks.sort(key=lambda t: (not t['attention'], not t['busy']))
    from .operations import describe
    worker_running = store.locked('worker')
    for task in tasks:
        task['operation'] = describe(task, worker_running)
    health = None
    try:
        # Read-only engine/session readiness (local --version and session record only; no model calls).
        # Adds session['health'] to each session and returns the project-level summary.
        from .health import project_health
        health = project_health(store.home, tasks)
    except ImportError:
        pass
    except (OSError, ValueError, TypeError, KeyError) as error:
        health = {'checked_at': now(), 'engines': {}, 'policy': 'unavailable', 'error': str(error)}
    return {'at': now(), 'scope': 'managed', 'home': str(store.home), 'cursor': cursor,
            'worker_running': worker_running, 'tasks': tasks, 'total': len(summaries),
            'health': health,
            'events': global_events, 'labels': LABELS, 'model_options': model_options()}


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
        operation = task['operation']
        rows.append('  ' + operation['phase'] + ' · ' + operation['title'])
        rows.append('  ' + clean(operation['reason']))
        rows.append('  다음: ' + clean(operation['next_action']))
        if task['reason'] or task['questions']:
            rows.append('  ' + fit(task['reason'] or task['questions'][0], max(20, width - 2)))
        for session in task['sessions']:
            rows.append('    ' + fit(session['name'], 18) + fit(t[session['state']], 12)
                        + clean(session['id'] or '—'))
            rows.append('      ' + clean(session['provider']) + ' | requested: ' + clean(session['requested_model'] or 'CLI default')
                        + ' | reported: ' + clean(', '.join(session['reported_models']) or 'unreported'))
            rows.append('      context: ' + str(session['context_tokens'] if session['context_tokens'] is not None else '?')
                        + ' / ' + str(session['context_limit']) + ' | handoffs: ' + str(session['handoff_count']))
        if task['routing']:
            rows.append('    → ' + clean(task['routing']['provider']) + ' / ' + clean(task['routing']['model'] or 'CLI default'))
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
            if self.path.startswith('/events?') and self.valid_token():
                from urllib.parse import urlsplit, parse_qs
                store = Store(home)
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    task_id = query.get('task_id', [''])[0]
                    before = int(query.get('before', ['0'])[0])
                    if before <= 0:
                        raise ValueError('Invalid event cursor')
                    store.get(task_id)
                    rows = store.db.execute('SELECT * FROM events WHERE task_id=? AND seq<? ORDER BY seq DESC LIMIT 100', (task_id, before)).fetchall()
                    self.respond(200, json.dumps({'events': [event_view(row) for row in rows]}, ensure_ascii=False))
                except (ValueError, KeyError):
                    self.respond(400, json.dumps({'error': '작업 또는 기록 위치를 확인할 수 없습니다.'}, ensure_ascii=False))
                finally:
                    store.close()
            elif self.path == '/':
                self.respond(200, template.replace('__TOKEN__', token).replace('__LANG__', lang).replace('__COMMAND_GUIDE__', browser_guide()), 'text/html')
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
            if self.path not in ('/feedback', '/pause', '/continue', '/switch', '/start', '/approve'):
                self.respond(404, '{}')
                return
            store = Store(home)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 32000:
                    raise ValueError('Invalid body size')
                body = json.loads(self.rfile.read(size))
                if self.path == '/start':
                    prompt, root, provider = body.get('prompt'), body.get('workspace'), body.get('provider')
                    checks = body.get('checks')
                    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 24000
                            or not isinstance(root, str) or provider not in ('codex', 'claude')
                            or not isinstance(checks, list) or not checks or len(checks) > 12
                            or type(body.get('public_web', False)) is not bool
                            or type(body.get('review_plan', False)) is not bool):
                        raise ValueError('Provide a request, repository, provider and verification command arrays')
                    if not shutil.which(provider):
                        raise ValueError('Selected CLI is not installed')
                    from .development import create
                    task = create(store, root, provider, prompt, [json.dumps(c) for c in checks])
                    task['data'].setdefault('options', {}).update(timeout=420, max_attempts=3, public_web=body.get('public_web', False))
                    task['data']['plan_requested'] = True
                    task['data']['review_plan'] = bool(body.get('review_plan', False))
                    task['data']['presentation'] = {'title': prompt.strip().splitlines()[0][:240]}
                    store.save(task, 'received', 'execution_plan_requested', {'layer': 'control', 'note': 'New task queued for planning'})
                    self.respond(200, json.dumps({'task_id': task['id'], 'worker_running': store.locked('worker')}))
                    return
                task_id = body.get('task_id')
                if not isinstance(task_id, str):
                    raise ValueError('Task ID required')
                # Hold the write reservation through revision validation and enqueue/control.
                store.db.execute('BEGIN IMMEDIATE')
                task = store.get(task_id)
                if self.path in ('/feedback', '/approve') and body.get('revision') != task['revision']:
                    self.respond(409, json.dumps({'error': 'Task changed; refresh and review before submitting'}))
                    return
                if self.path == '/feedback':
                    text = body.get('text')
                    if not isinstance(text, str) or not text.strip() or len(text) > 24000:
                        raise ValueError('Nonempty feedback required')
                    message_id = store.enqueue(task_id, text, None)
                    self.respond(200, json.dumps({'queued': message_id, 'worker_running': store.locked('worker')}))
                elif self.path == '/approve':
                    from .planning import approve
                    approve(store, task)
                    self.respond(200, json.dumps({'queued': True, 'worker_running': store.locked('worker')}))
                elif self.path == '/switch':
                    request = store.route(task_id, body.get('provider'), body.get('model') or None)
                    self.respond(200, json.dumps({'queued': request, 'worker_running': store.locked('worker')}))
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
