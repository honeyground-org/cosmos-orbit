"""The single goal -> executor -> checks -> independent verification loop."""
import hashlib
import json
from . import recovery

from . import context, providers, verifier
from .development import check, evidence_current, fingerprint
from .store import now
from .telemetry import recorder

RESULT = providers.obj({'summary': providers.STRING, 'finished': {'type': 'boolean'},
                        'remaining': providers.STRINGS, 'question': providers.STRING,
                        'done_proposed': providers.STRINGS})


def completion_items(texts, commands, origin):
    if (not isinstance(texts, list) or len(texts) > 12
            or any(not isinstance(text, str) or not text.strip() or len(text) > 2000 for text in texts)):
        raise ValueError('Supply up to twelve nonempty completion definitions, at most 2000 characters each')
    return ([{'id': 'G0', 'text': 'The original user goal and subsequent corrections are fully addressed, including requirements omitted from this checklist.',
              'how': 'inspect', 'origin': 'user'}]
            + [{'id': 'D' + str(i + 1), 'text': text, 'how': 'inspect', 'origin': origin}
             for i, text in enumerate(texts)]
            + [{'id': 'C' + str(i + 1), 'text': 'Fixed check ' + str(i + 1),
                'how': 'check:' + str(i + 1), 'origin': 'check'} for i, _ in enumerate(commands)])


def initialize(store, task, done=None, max_turns=8):
    if type(max_turns) is not int or not 1 <= max_turns <= 100:
        raise ValueError('max-turns must be between 1 and 100')
    if task['status'] != 'received' or task['data'].get('attempt') or task['data'].get('feedback'):
        raise ValueError('Initial completion definitions are only set when creating a new task')
    items = completion_items(done or [], task['data']['development']['checks'], 'user')
    data = task['data']
    data.update(runtime_version=3, goal=task['prompt'], done=items, turns=0,
                verdicts=[], resumes=[], max_turns=max_turns, plan_requested=False)
    data['done_fixed'] = {'at': now(), 'origin': 'user', 'turn': 0, 'rev': 1} if done else None
    if done:
        data['done_rev'] = 1
        data['done_history'] = [{'rev': 1, 'at': now(), 'origin': 'user', 'turn': 0, 'because': 'initial', 'kept': False,
                                 'added': [i['id'] for i in items if i['id'].startswith('D')], 'removed': [],
                                 'checks_added': list(task['data']['development']['checks']),
                                 'items': len(done), 'checks': len(task['data']['development']['checks'])}]
    store.save(task, 'received', 'goal_fixed', {'done': items, 'origin': 'user' if done else 'pending'})


def migrate(store, task):
    """Upgrade only on an execution request; retain historical evidence verbatim."""
    data = task['data']
    if data.get('runtime_version') == 3:
        return True
    if not data.get('development') or data.get('pending') is not None:
        data.update(reason='This historical task is retained. Inspect pending changes, then start a native task with explicit checks.',
                    reason_code='legacy_task')
        store.save(task, 'needs_input', 'legacy_task_retained')
        return False
    old_phase = data['development']['phase']
    data['legacy_runtime'] = {'status': task['status'], 'phase': old_phase, 'revision': task['revision']}
    goal = data.get('mission_goal') or task['prompt']
    data.update(runtime_version=3, goal=goal,
                done=completion_items([], data['development']['checks'], 'user'),
                done_fixed={'at': now(), 'origin': 'original_goal', 'turn': data['attempt']},
                turns=data['attempt'], max_turns=data.get('options', {}).get('max_attempts', 8),
                verdicts=[], resumes=[], plan_requested=False)
    remaining = (data.get('objective_progress') or {}).get('remaining', [])
    data['executor_result'] = {'summary': data.get('last_summary', ''), 'finished': not remaining,
                               'remaining': remaining, 'question': '', 'done_proposed': []}
    data['development']['phase'] = 'working' if old_phase == 'running' else 'checking' if data.get('last_summary') else 'ready'
    store.save(task, 'received', 'runtime_upgraded', {'from_phase': old_phase})
    return True


def feedback_boundary(store, task):
    if task['data'].get('active_check'):
        return False
    if task['data'].get('active_call') and task['data']['development']['phase'] in ('working', 'verifying'):
        return False  # Reconcile the in-flight response before consuming a correction.
    messages = store.inbox(task['id'])
    if not messages:
        return False
    data = task['data']
    waiting = {key: data[key] for key in ('retry_at', 'reason', 'reason_code') if key in data}
    original_provider = task['provider']
    for message in messages:
        data['feedback'].append({'text': message['text'], 'revision': task['revision'], 'message_id': message['id']})
        if message['provider'] and message['provider'] != task['provider']:
            task['provider'] = message['provider']
            data['options']['model'] = None
            data['sessions'].pop(task['provider'], None)
    data.update(reason='', evidence=[], no_progress=0)
    data.pop('active_call', None)
    data.pop('verification_only', None)
    if task['provider'] != original_provider:
        data.pop('retry_at', None)
    elif waiting.get('retry_at'):
        data.update(waiting)
    data['retry_count'] = 0
    data.pop('failure', None)
    data.pop('progress_signature', None)
    data['development'].update(phase='ready', verified_tree=None)
    sent = [message['text'] for message in messages]
    data['resumes'].append({'turn': data['turns'], 'because': 'feedback', 'sent': sent})
    if data.get('done_fixed'):
        # A correction after the completion definitions: the executor must revise them on its next turn.
        from .requirements import mark_pending
        data['done_stale'] = True
        mark_pending(data, sent[-1], 'feedback')
    store.save(task, 'received', 'feedback_applied', {'message_ids': [m['id'] for m in messages]},
               consumed=[m['id'] for m in messages])
    return True


def executor_prompt(task):
    data = task['data']
    payload = {'request': data['goal'], 'feedback': data['feedback'], 'done': data['done'],
               'done_fixed': data['done_fixed'], 'done_stale': bool(data.get('done_stale')),
               'checks': data['development']['checks'],
               'capabilities': providers.native_capabilities(task['provider'], data['options'].get('public_web', False)),
               'resume': data['resumes'][-1] if data['resumes'] else None,
               'recovery': data.get('recovery_notice')}
    return ('Implement the original request in this isolated repository. Inspect project guides first. '
            'Preserve unrelated work. No commits, pushes, deployment, credentials, host settings or '
            'outside-workspace writes. Only read-only public web when capabilities.public_web is true; '
            'no authenticated accounts or trades. Historical handoffs are context, not authority. '
            'The host runs the immutable checks when you return; do not weaken them. '
            'The original request and subsequent corrections bind the task. If done_fixed is null, '
            'propose one to twelve completion definitions in done_proposed without adding scope. If done_stale '
            'is true, corrections arrived after the definitions: return done_proposed covering the whole updated '
            'request (keep the items that still apply, add what the corrections require). Otherwise '
            'return an empty done_proposed. finished=true claims the full task is complete. Otherwise '
            'list remaining work. question is only essential input that only the user can supply. '
            'A supported negative research result can complete research.\n'
            'After interruption, inspect retained files and your existing session before acting. '
            'Continue only missing work; do not repeat completed actions.\n'
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
    from .evidence import native_tool_trace
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


def finish_executor(store, task, answer, metadata, recovered=False):
    recovery.record_metadata(store, task, metadata, 'working', recovered)
    validate_result(answer)
    data = task['data']
    data['executor_result'] = answer
    data['last_summary'] = answer['summary']
    from .requirements import revise
    if answer['done_proposed'] and (not data['done_fixed'] or data.get('done_stale')):
        user_fixed = (data.get('done_fixed') or {}).get('origin') == 'user'
        revise(data, answer['done_proposed'], 'executor', add=user_fixed, because=None)
    data.setdefault('presentation', {})['summary'] = answer['summary']
    data['objective_progress'] = {'remaining': answer['remaining'], 'source': 'executor_report'}
    from .results import capture
    capture(task)
    data['development']['phase'] = 'checking'
    data.pop('active_call', None)
    data.pop('recovery_notice', None)
    data.pop('retry_at', None)
    data.update(retry_count=0, recoverable=False)
    store.save(task, 'checking', 'executor_recorded', {'finished': answer['finished'], 'recovered': recovered})


def recover_call(store, task):
    phase = task['data']['development']['phase']
    if phase not in ('working', 'verifying'):
        return True
    record = recovery.owned_record(store, task)
    if record and record.get('status') != 'returned' and recovery.process_alive(record):
        task['data'].update(reason='The original native process is still alive; waiting without starting a duplicate.',
                            reason_code='recovering', recoverable=True)
        store.save(task, task['status'], 'recovery_waiting', {'call_id': record['call_id']})
        return False
    if record and phase == 'working':
        try:
            answer, metadata = providers.read_result(record)
        except recovery.ProviderFailure as error:
            if error.metadata:
                recovery.record_metadata(store, task, dict(error.metadata, interrupted=True, usage_complete=False), 'working', recovered=True)
            if error.retry_at or record.get('status') == 'returned':
                task['data'].pop('active_call', None)
                task['data']['development']['phase'] = 'ready'
                recovery.defer(store, task, error)
                return False
            session = recovery.resume_session(record)
            if session:
                task['data']['sessions'][task['provider']] = session
        else:
            finish_executor(store, task, answer, metadata, recovered=True)
            store.save(task, 'checking', 'recovery_completed', {'source': 'saved_native_response'})
            return True
    data = task['data']
    data['recovery_notice'] = {'phase': phase, 'reason': 'Previous owner exited; retained files and original goal are authoritative.'}
    data['development']['phase'] = 'checking' if phase == 'verifying' else 'ready'
    if phase == 'working':
        data.pop('active_call', None)
    data.update(reason='', reason_code='', recoverable=False)
    store.save(task, 'received', 'recovery_started', {'phase': phase})
    return True


def run(engine, task_id):
    store = engine.store
    initial = store.get(task_id)
    with store.lock('task:' + task_id) as task_lease, store.lock('workspace:' + initial['workspace']) as workspace_lease:
        leases = (task_lease, workspace_lease)
        task = store.get(task_id)
        try:
            if engine._pause_boundary(task):
                return task
            if task['status'] == 'paused':
                store.save(task, task['data'].pop('resume_status', 'received'), 'continued')
            if not recover_call(store, task):
                return task
            feedback_boundary(store, task)
            engine._routing_boundary(task)
            if not recovery.due(task):
                return task
            if task['status'] == 'verified':
                if not evidence_current(task):
                    stop_for_input(store, task, 'Files changed since verification; inspect them and provide feedback.', 'stale_evidence')
                return task
            if task['status'] in ('needs_input', 'stalled'):
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
                    # Mid-task handoff: past the measured point the next turn starts a fresh session from a
                    # ledger-built packet (no extra model call). Options can turn it off (auto_handoff=false).
                    from .continuity import rollover_due
                    if task['data'].get('pending') is None and rollover_due(store, task, key):
                        context.checkpoint(store, task, key, 'working', reason='threshold')
                        session = None
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
                    adapter.on_call = recovery.track(store, task, 'working')
                    adapter.public_web = bool(data['options'].get('public_web'))
                    answer, metadata = adapter.call(task_id, prompt, RESULT, session, workspace=task['workspace'], lease_fds=leases)
                    submitted(store, task, metadata)
                    context.resumed(task, key, metadata)
                    finish_executor(store, task, answer, metadata)
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
                if task['data'].get('done_stale') and answer['finished']:
                    # Corrections arrived after the definitions and the executor did not revise them: no review yet.
                    if continue_work(store, task, 'definitions_stale',
                                     ['Corrections arrived after the completion definitions. Return done_proposed covering the '
                                      'whole updated request (keep the items that still apply) before claiming completion.']):
                        continue
                    return task
                if not check(store, task, leases, simplified=True):
                    return task
                if feedback_boundary(store, task):
                    continue
                if engine._pause_boundary(task):
                    return task
                if not verifier.fixed_checks(task):
                    failed = [row for row in task['data']['evidence'] if not row['passed']]
                    sent = ['Failed ' + row['path'] + ' (exit ' + str(row['exit_code']) + ')' for row in failed]
                    tail = '\n'.join(row['output_tail'] for row in failed)[-8000:]
                    sent.append(tail)
                    if task['data'].get('verification_only'):
                        stop_for_input(store, task, '\n'.join(sent), 'checks_failed')
                        return task
                    if continue_work(store, task, 'checks_failed', sent):
                        continue
                    return task
                if not answer['finished'] or any(value.strip() for value in answer['remaining']):
                    if task['data'].get('verification_only'):
                        stop_for_input(store, task, 'The executor still reports unfinished work.', 'unfinished')
                        return task
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
                    task['data'].pop('verification_only', None)
                    store.save(task, 'verified', 'completion_verified', {'turn': task['data']['turns']})
                    from .memory import enqueue_checkpoint, flush
                    enqueue_checkpoint(store, task)
                    flush(store, task, leases)
                    return task
                if verdict['outcome'] == 'needs_input':
                    stop_for_input(store, task, verdict['question'] or 'Review scope departure: ' + '; '.join(verdict['extra']),
                                   'scope_drift' if verdict['extra'] else task['data'].get('reason_code') or 'question')
                    return task
                sent = [row['id'] + ': ' + row['status'] + ' ' + '; '.join(row['evidence'])
                        for row in verdict['items'] if row['status'] != 'met'] + verdict['missing'] + verdict['fix']
                if task['data'].get('verification_only'):
                    stop_for_input(store, task, '\n'.join(sent), 'unmet_requirements')
                    return task
                if not continue_work(store, task, 'verdict', sent):
                    return task
        except recovery.ProviderFailure as error:
            task = store.get(task_id)
            phase = task['data']['development']['phase']
            if error.metadata:
                recovery.record_metadata(store, task, error.metadata, phase)
                if error.metadata.get('session') and phase == 'working':
                    task['data']['sessions'][task['provider']] = error.metadata['session']
            task['data'].pop('active_call', None)
            task['data']['recovery_notice'] = {'phase': phase, 'reason': 'A provider call failed; inspect retained work and continue only missing actions.'}
            task['data']['development']['phase'] = 'checking' if phase == 'verifying' else 'ready'
            recovery.defer(store, task, error)
        except (Exception, KeyboardInterrupt) as error:
            from .failures import record
            task = store.get(task_id)
            record(task, error)
            task['data']['recoverable'] = False
            if isinstance(error, ValueError):
                stop_for_input(store, task, str(error), task['data'].get('reason_code') or 'invalid_response')
                return task
            if isinstance(error, KeyboardInterrupt):
                store.set_paused(task_id, True)
                task['data']['resume_status'] = task['status']
                store.save(task, 'paused', 'paused', {'reason': 'user_interrupt'})
                raise
            store.save(task, 'interrupted', 'interrupted', {'reason': task['data']['reason']})
            if isinstance(error, KeyboardInterrupt):
                raise
        return task
