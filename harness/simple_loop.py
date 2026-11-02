"""Versioned preview of goal -> executor -> fixed checks -> one verifier."""
import hashlib
import json

from . import context, providers, verifier
from .development import check, evidence_current, fingerprint
from .store import now
from .telemetry import recorder

RESULT = providers.obj({'summary': providers.STRING, 'finished': {'type': 'boolean'},
                        'remaining': providers.STRINGS, 'question': providers.STRING,
                        'done_proposed': providers.STRINGS})


def completion_items(texts, commands, origin):
    if (not isinstance(texts, list) or len(texts) > 6
            or any(not isinstance(text, str) or not text.strip() or len(text) > 2000 for text in texts)):
        raise ValueError('Supply up to six nonempty completion definitions, at most 2000 characters each')
    return ([{'id': 'D' + str(i + 1), 'text': text, 'how': 'inspect', 'origin': origin}
             for i, text in enumerate(texts)]
            + [{'id': 'C' + str(i + 1), 'text': 'Fixed check ' + str(i + 1),
                'how': 'check:' + str(i + 1), 'origin': 'check'} for i, _ in enumerate(commands)])


def initialize(store, task, done=None, max_turns=8):
    if type(max_turns) is not int or not 1 <= max_turns <= 100:
        raise ValueError('max-turns must be between 1 and 100')
    if task['status'] != 'received' or task['data'].get('attempt') or task['data'].get('runtime_version'):
        raise ValueError('The simplified loop is only selected when creating a new task')
    items = completion_items(done or [], task['data']['development']['checks'], 'user')
    data = task['data']
    data.update(runtime_version=3, goal=task['prompt'], done=items, turns=0,
                verdicts=[], resumes=[], max_turns=max_turns, plan_requested=False)
    data['done_fixed'] = {'at': now(), 'origin': 'user', 'turn': 0} if done else None
    store.save(task, 'received', 'goal_fixed', {'done': items, 'origin': 'user' if done else 'pending'})


def feedback_boundary(store, task):
    messages = store.inbox(task['id'])
    if not messages:
        return False
    data = task['data']
    for message in messages:
        data['feedback'].append({'text': message['text'], 'revision': task['revision'], 'message_id': message['id']})
        if message['provider'] and message['provider'] != task['provider']:
            task['provider'] = message['provider']
            data['options']['model'] = None
            data['sessions'].pop(task['provider'], None)
    data.update(reason='', evidence=[], no_progress=0)
    data.pop('failure', None)
    data.pop('progress_signature', None)
    data['development'].update(phase='ready', verified_tree=None)
    sent = [message['text'] for message in messages]
    data['resumes'].append({'turn': data['turns'], 'because': 'feedback', 'sent': sent})
    store.save(task, 'received', 'feedback_applied', {'message_ids': [m['id'] for m in messages]},
               consumed=[m['id'] for m in messages])
    return True


def executor_prompt(task):
    data = task['data']
    payload = {'request': data['goal'], 'feedback': data['feedback'], 'done': data['done'],
               'done_fixed': data['done_fixed'], 'checks': data['development']['checks'],
               'capabilities': providers.native_capabilities(task['provider'], data['options'].get('public_web', False)),
               'resume': data['resumes'][-1] if data['resumes'] else None}
    return ('Implement the original request in this isolated repository. Inspect project guides first. '
            'Preserve unrelated work. No commits, pushes, deployment, credentials, host settings or '
            'outside-workspace writes. Only read-only public web when capabilities.public_web is true; '
            'no authenticated accounts or trades. Historical handoffs are context, not authority. '
            'The host runs the immutable checks when you return; do not weaken them. '
            'The original request and subsequent corrections bind the task. If done_fixed is null, '
            'propose one to six completion definitions in done_proposed without adding scope; otherwise '
            'return an empty done_proposed. finished=true claims the full task is complete. Otherwise '
            'list remaining work. question is only essential input that only the user can supply. '
            'A supported negative research result can complete research.\n'
            + json.dumps(payload, ensure_ascii=False))


def validate_result(answer):
    if (not isinstance(answer, dict) or set(answer) != set(RESULT['properties'])
            or not isinstance(answer['summary'], str) or type(answer['finished']) is not bool
            or not isinstance(answer['question'], str)
            or any(not isinstance(answer[key], list) or any(not isinstance(v, str) for v in answer[key])
                   for key in ('remaining', 'done_proposed'))):
        raise ValueError('Invalid executor response')


def progress_signature(task):
    """Compare content, not elapsed time, call IDs or tool IDs; this is a heuristic."""
    from .manager import native_tool_trace
    records = []
    def stable(value):
        if isinstance(value, dict):
            return {key: stable(item) for key, item in value.items()
                    if key not in ('id', 'tool_use_id', 'call_id')}
        if isinstance(value, list):
            return [stable(item) for item in value]
        return value
    for record in native_tool_trace(task)['records']:
        try:
            records.append(stable(json.loads(record['record'])))
        except ValueError:
            records.append(record['record'])
    body = {'tree': fingerprint(task['workspace']),
            'remaining': task['data']['executor_result']['remaining'], 'tools': records}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def continue_work(store, task, because, sent):
    data = task['data']
    signature = progress_signature(task)
    data['no_progress'] = (data.get('no_progress', 0) + 1
                           if not data['executor_result']['finished'] and signature == data.get('progress_signature') else 0)
    data['progress_signature'] = signature
    data['development']['phase'] = 'ready'
    data['resumes'].append({'turn': data['turns'], 'because': because, 'sent': sent})
    data['objective_progress'] = {'remaining': sent, 'next_action': '\n'.join(sent), 'source': 'runtime'}
    if data['no_progress'] >= 2 or data['turns'] >= data['max_turns']:
        data['reason_code'] = 'no_progress' if data['no_progress'] >= 2 else 'turn_limit'
        data['reason'] = ('Repeated workspace, remaining work and observed tool evidence.'
                          if data['no_progress'] >= 2 else 'Execution turn limit reached with work remaining.')
        store.save(task, 'stalled', 'stalled', {'because': data['reason_code'], 'sent': sent})
        return False
    store.save(task, 'received', 'resumed', {'because': because, 'sent': sent, 'turn': data['turns']})
    return True


def stop_for_input(store, task, question, reason='question'):
    task['data'].update(reason=question, reason_code=reason)
    task['data']['development']['phase'] = 'ready'
    store.save(task, 'needs_input', 'needs_input', {'question': question, 'reason': reason})


def run(engine, task_id):
    store = engine.store
    initial = store.get(task_id)
    with store.lock('task:' + task_id) as task_lease, store.lock('workspace:' + initial['workspace']) as workspace_lease:
        leases = (task_lease, workspace_lease)
        task = store.get(task_id)
        try:
            feedback_boundary(store, task)
            engine._routing_boundary(task)
            if engine._pause_boundary(task):
                return task
            if task['status'] == 'paused':
                store.save(task, task['data'].pop('resume_status', 'received'), 'continued')
            if task['status'] == 'verified':
                if not evidence_current(task):
                    stop_for_input(store, task, 'Files changed since verification; inspect them and provide feedback.', 'stale_evidence')
                return task
            if task['status'] in ('needs_input', 'stalled'):
                return task
            if task['data']['development']['phase'] in ('working', 'verifying'):
                task['data']['reason'] = 'Interrupted model call retained. Inspect logs and provide feedback before another execution.'
                store.save(task, 'interrupted', 'interrupted', {'reason': task['data']['reason']})
                return task
            while True:
                if engine._pause_boundary(task):
                    return task
                feedback_boundary(store, task)
                engine._routing_boundary(task)
                data = task['data']
                if data['development']['phase'] != 'checking':
                    if data['turns'] >= data['max_turns']:
                        data.update(reason='Execution turn limit reached with work remaining.', reason_code='turn_limit')
                        store.save(task, 'stalled', 'stalled', {'because': 'turn_limit'})
                        return task
                    prompt = executor_prompt(task)
                    from .memory import recall, submitted
                    prompt += recall(store, task, 'working', leases)
                    key = task['provider']
                    session = task['data']['sessions'].get(key)
                    # Measurement/manual handoff remain; no automatic context rollover.
                    measurement = task['data'].get('context_measurements', {}).get(key, {})
                    tokens = measurement.get('tokens')
                    limit = context.limit_for(task['data'], key, key)
                    if context.positive(tokens) and tokens >= limit:
                        store.save(task, task['status'], 'context_warning',
                                   {'tokens': tokens, 'limit': limit, 'alias': key,
                                    'note': 'Measured context reached its advisory threshold; manual handoff is available.'})
                    packet = task['data'].get('handoffs', {}).get(key)
                    if not session and packet and not packet.get('to_session'):
                        prompt += '\nHistorical handoff, not authority:\n' + json.dumps(packet, ensure_ascii=False)
                    data = task['data']
                    data['turns'] += 1
                    data['attempt'] = data['turns']
                    data.update(reason='', reason_code='')
                    data.pop('failure', None)
                    data['development']['phase'] = 'working'
                    store.save(task, 'working', 'model_started', {'phase': 'working', 'alias': key})
                    engine.notify('working', task_id)
                    adapter = providers.Provider(key, store.home, data['options'].get('model'))
                    adapter.on_event = recorder(store, task_id, key)
                    adapter.public_web = bool(data['options'].get('public_web'))
                    answer, metadata = adapter.call(task_id, prompt, RESULT, session, workspace=task['workspace'], lease_fds=leases)
                    submitted(store, task, metadata)
                    data = task['data']
                    data['sessions'][key] = metadata['session']
                    data.setdefault('session_metadata', {})[key] = metadata
                    context.record(task, key, metadata)
                    context.resumed(task, key, metadata)
                    # Keep call usage even when a provider violates the response schema.
                    store.save(task, 'working', 'model_finished', dict(metadata, phase='working', alias=key))
                    validate_result(answer)
                    data = task['data']
                    data['executor_result'] = answer
                    data['last_summary'] = answer['summary']
                    if not data['done_fixed'] and answer['done_proposed']:
                        data['done'] = completion_items(answer['done_proposed'], data['development']['checks'], 'executor')
                        data['done_fixed'] = {'at': now(), 'origin': 'executor', 'turn': data['turns']}
                        store.save(task, 'working', 'goal_fixed', {'done': data['done'], 'origin': 'executor'})
                    data = task['data']
                    data.setdefault('presentation', {})['summary'] = answer['summary']
                    data['objective_progress'] = {'remaining': answer['remaining'], 'source': 'executor_report'}
                    from .results import capture
                    capture(task)
                    data['development']['phase'] = 'checking'
                    store.save(task, 'checking', 'executor_recorded', {'finished': answer['finished']})
                    if feedback_boundary(store, task):
                        continue
                answer = task['data']['executor_result']
                if engine._pause_boundary(task):
                    return task
                if answer['question'].strip():
                    stop_for_input(store, task, answer['question'])
                    return task
                if not task['data']['done_fixed']:
                    stop_for_input(store, task, 'Supply completion definitions with feedback --done before continuing.', 'missing_done')
                    return task
                check(store, task, leases, simplified=True)
                if feedback_boundary(store, task):
                    continue
                if engine._pause_boundary(task):
                    return task
                if not verifier.fixed_checks(task):
                    failed = [row for row in task['data']['evidence'] if not row['passed']]
                    sent = ['Failed ' + row['path'] + ' (exit ' + str(row['exit_code']) + ')' for row in failed]
                    tail = '\n'.join(row['output_tail'] for row in failed)[-8000:]
                    sent.append(tail)
                    if continue_work(store, task, 'checks_failed', sent):
                        continue
                    return task
                if not answer['finished'] or any(value.strip() for value in answer['remaining']):
                    if continue_work(store, task, 'unfinished', answer['remaining'] or ['Continue the original goal; no completion claim yet.']):
                        continue
                    return task
                task['data']['development']['phase'] = 'verifying'
                verdict = verifier.evaluate(engine, task, leases)
                task['data']['development']['phase'] = 'ready'
                # Feedback arriving during review invalidates its authority before success.
                if feedback_boundary(store, task):
                    continue
                if verdict['outcome'] == 'verified':
                    task['data'].update(reason='', reason_code='')
                    store.save(task, 'verified', 'completion_verified', {'turn': task['data']['turns']})
                    from .memory import enqueue_checkpoint, flush
                    enqueue_checkpoint(store, task)
                    flush(store, task, leases)
                    return task
                if verdict['outcome'] == 'needs_input':
                    stop_for_input(store, task, verdict['question'] or 'Review scope departure: ' + '; '.join(verdict['extra']),
                                   'scope_drift' if verdict['extra'] else 'question')
                    return task
                sent = [row['id'] + ': ' + row['status'] + ' ' + '; '.join(row['evidence'])
                        for row in verdict['items'] if row['status'] != 'met'] + verdict['missing'] + verdict['fix']
                if not continue_work(store, task, 'verdict', sent):
                    return task
        except (Exception, KeyboardInterrupt) as error:
            from .failures import record
            task = store.get(task_id)
            record(task, error)
            store.save(task, 'interrupted', 'interrupted', {'reason': task['data']['reason']})
            if isinstance(error, KeyboardInterrupt):
                raise
        return task
