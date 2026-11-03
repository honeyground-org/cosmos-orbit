"""Goal ownership, user controls and the single recoverable native execution loop."""
from pathlib import Path
from . import context, workspace


class Engine:
    def __init__(self, store, model=None, timeout=None, notify=None):
        self.store, self.model, self.timeout = store, model, timeout
        self.notify = notify or (lambda *_: None)

    def _pause_boundary(self, task):
        if not self.store.paused(task['id']):
            return False
        if task['status'] != 'paused':
            task['data']['resume_status'] = task['status']
            self.store.save(task, 'paused', 'paused_at_boundary')
        return True

    def _routing_boundary(self, task):
        request = self.store.routing(task['id'])
        if not request or task['data']['pending'] is not None:
            return
        if task['data'].get('active_check'):
            return
        if task['data'].get('active_call') and task['data']['development']['phase'] in ('working', 'verifying'):
            return
        old = {'provider': task['provider'], 'model': task['data'].get('options', {}).get('model')}
        task['provider'] = request['provider']
        task['data'].setdefault('options', {})['model'] = request['model']
        task['data']['sessions'].pop(request['provider'], None)
        task['data'].setdefault('session_metadata', {}).pop(request['provider'], None)
        task['data'].get('context_tokens', {}).pop(request['provider'], None)
        task['data'].get('context_measurements', {}).pop(request['provider'], None)
        if old != {'provider': request['provider'], 'model': request['model']}:
            task['data'].pop('retry_at', None)
            task['data'].update(reason='', reason_code='', retry_count=0)
        self.store.save(task, task['status'], 'routing_applied', {'from': old, 'to': request}, routing_id=request['id'])

    def feedback(self, task_id, text, provider=None):
        if not text.strip() or len(text) > 24000:
            raise ValueError('Feedback must be nonempty and at most 24000 characters')
        self.store.enqueue(task_id, text, provider)
        try:
            with self.store.lock('task:' + task_id):
                task = self.store.get(task_id)
                from .simple_loop import migrate
                if not migrate(self.store, task):
                    return task
                self._feedback_boundary(task)
        except RuntimeError as error:
            if 'owns this task' not in str(error):
                raise
        return self.store.get(task_id)

    def handoff(self, task_id):
        task = self.store.get(task_id)
        with self.store.lock('task:' + task_id):
            task = self.store.get(task_id)
            for key in list(task['data']['sessions']):
                context.checkpoint(self.store, task, key, task['status'], reason='manual')
        return task

    def recover(self, task_id):
        """Explicitly abandon a conflicted pending proposal; never overwrite current files."""
        with self.store.lock('task:' + task_id):
            task = self.store.get(task_id)
            with self.store.lock('workspace:' + task['workspace']):
                pending = task['data']['pending']
                if pending is None:
                    raise ValueError('No pending file action to recover')
                current = []
                for edit in pending:
                    try:
                        raw = workspace.read_file(task['workspace'], edit['path'])
                        sha = workspace.digest(raw) if raw is not None else ''
                        classification = 'after' if sha == edit['after_sha'] else 'before' if sha == edit['before_sha'] else 'external'
                        current.append({'path': edit['path'], 'sha': sha, 'classification': classification,
                                        'before_sha': edit['before_sha'], 'after_sha': edit['after_sha']})
                    except (OSError, ValueError) as error:
                        current.append({'path': edit['path'], 'error': str(error)})
                task['data']['pending'] = None
                task['data']['recovery_context'] = current
                task['data']['reason'] = 'Pending proposal abandoned; all current files preserved. Provide feedback to replan.'
                self.store.save(task, 'needs_input', 'pending_abandoned', {'proposal': pending, 'current': current})
                if task['data'].get('runtime_version') == 3:
                    self._feedback_boundary(task)
        return task

    def configure(self, task_id, options):
        options = {k: v for k, v in options.items() if v is not None}
        if not options:
            return
        if ('max_attempts' in options and not 1 <= options['max_attempts'] <= 10
                or 'context_budget' in options and options['context_budget'] < 1000):
            raise ValueError('Invalid attempt or context budget')
        with self.store.lock('task:' + task_id):
            task = self.store.get(task_id)
            if task['data'].get('development') and task['data'].get('runtime_version') != 3:
                from .simple_loop import migrate
                migrate(self.store, task)
            if task['data'].get('runtime_version') == 3:
                from .simple_loop import completion_items
                if 'max_attempts' in options:
                    raise ValueError('Simplified tasks use --max-turns')
                if 'max_turns' in options:
                    limit = options['max_turns']
                    if type(limit) is not int or not 1 <= limit <= 100:
                        raise ValueError('max-turns must be between 1 and 100')
                    task['data']['max_turns'] = limit
                if 'done' in options:
                    items = completion_items(options['done'], task['data']['development']['checks'], 'user')
                    if not options['done']:
                        raise ValueError('Completion definitions cannot be cleared')
                    from .store import now
                    task['data']['done'] = items
                    task['data']['done_fixed'] = {'at': now(), 'origin': 'user', 'turn': task['data']['turns']}
                    task['status'] = 'needs_input'
                    task['data'].update(reason='Completion definitions changed; provide feedback to continue.',
                                        reason_code='done_changed')
                    task['data']['development']['phase'] = 'ready'
                if (task['status'] == 'stalled' and task['data'].get('reason_code') == 'turn_limit'
                        and task['data']['max_turns'] > task['data']['turns']):
                    task['status'] = 'received'
                    task['data'].update(reason='', reason_code='')
            elif 'max_turns' in options or 'done' in options:
                raise ValueError('--done and --max-turns apply to simplified tasks only')
            task['data'].setdefault('options', {}).update(options)
            event = 'goal_fixed' if task['data'].get('runtime_version') == 3 and 'done' in options else 'options_updated'
            payload = dict(options, done=task['data']['done'], origin='user') if event == 'goal_fixed' else options
            self.store.save(task, task['status'], event, payload)

    def run(self, task_id, max_attempts=None, plan_only=False):
        from .simple_loop import migrate, run
        if plan_only:
            raise ValueError('The separate planner is removed. Create a queued task to inspect it without execution.')
        with self.store.lock('task:' + task_id):
            task = self.store.get(task_id)
            if task['data'].get('mode') == 'attached':
                attached = True
            else:
                attached = False
                if not migrate(self.store, task):
                    return task
        if attached:
            from .attach import run_verification
            return run_verification(self, task_id)
        if max_attempts is not None:
            self.configure(task_id, {'max_turns': max_attempts})
        return run(self, task_id)

    def _feedback_boundary(self, task):
        from .simple_loop import feedback_boundary
        return feedback_boundary(self.store, task)
