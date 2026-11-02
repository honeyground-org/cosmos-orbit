"""Natural-language tasks, durable feedback and an optional serial worker."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

from .engine import Engine
from .context import limit_for
from .providers import doctor
from .store import Store
from .workspace import verify
from . import failures


def show(task):
    data = task['data']
    return {'id': task['id'], 'status': task['status'], 'revision': task['revision'],
            'provider': task['provider'], 'workspace': task['workspace'],
            'request': task['prompt'], 'contract': data['contract'],
            'development': data.get('development'), 'memory': data.get('memory'),
            'runtime_version': data.get('runtime_version', 2), 'goal': data.get('goal'),
            'done': data.get('done'), 'done_fixed': data.get('done_fixed'), 'turns': data.get('turns'),
            'max_turns': data.get('max_turns'), 'verdicts': data.get('verdicts'), 'resumes': data.get('resumes'),
            'reason_code': data.get('reason_code'),
            'retry_at': data.get('retry_at'), 'active_call': data.get('active_call'),
            'recoverable': data.get('recoverable', False),
            'presentation': data.get('presentation'), 'execution_plan': data.get('execution_plan'),
            'objective_progress': data.get('objective_progress'), 'evaluation': data.get('evaluation'),
            'attempts': data['attempt'], 'evidence': data['evidence'],
            'reason': failures.describe(data), 'failure': failures.details(data), 'feedback': data['feedback'],
            'sessions': data['sessions'], 'context': data.get('context_measurements', {}),
            'context_limits': {key: limit_for(data, key, data.get('peers', {}).get(key[5:], {}).get('provider')
                                             if key.startswith('peer:') else key.split(':')[0])
                               for key in set(data['sessions']) | set(data.get('handoffs', {}))},
            'handoffs': data.get('handoff_counts', {}), 'imported_handoff': data.get('imported_handoff'), 'updated': task['updated']}



def main(argv=None):
    parser = argparse.ArgumentParser(prog='cosmos-orbit', description='Keep a native CLI task on goal, recover interruptions and verify its conclusion')
    parser.add_argument('--home', help='Private task store')
    sub = parser.add_subparsers(dest='command')
    for name in ('commands', 'doctor', 'list'):
        sub.add_parser(name)
    start = sub.add_parser('start', help='Start a native task in an isolated Git clone')
    start.add_argument('prompt', nargs='?')
    start.add_argument('--workspace', default='.')
    start.add_argument('--provider', choices=('codex', 'claude'))
    start.add_argument('--check', action='append', default=[], metavar='ARGV_JSON')
    start.add_argument('--done', action='append')
    start.add_argument('--queue', action='store_true')
    start.add_argument('--public-web', action='store_true')
    start.add_argument('--development', action='store_true', help=argparse.SUPPRESS)
    start.add_argument('--simplified', action='store_true', help=argparse.SUPPRESS)
    start.add_argument('--memory', choices=('off', 'native', 'brain', 'both'), default='off')
    start.add_argument('--native-memory-dir', action='append', default=[])
    start.add_argument('--remember', action='store_true')
    start.add_argument('--project-id')
    handoff = start.add_mutually_exclusive_group()
    handoff.add_argument('--handoff-file')
    handoff.add_argument('--handoff-text')
    resume = sub.add_parser('resume', help='Resume a paused task or recover its saved execution')
    resume.add_argument('id')
    feedback = sub.add_parser('feedback', help='Add a correction to the original goal')
    feedback.add_argument('id')
    feedback.add_argument('text')
    feedback.add_argument('--provider', choices=('codex', 'claude'))
    feedback.add_argument('--done', action='append')
    feedback.add_argument('--queue', action='store_true')
    for command in (start, resume, feedback):
        command.add_argument('--model')
        command.add_argument('--max-turns', type=int)
        command.add_argument('--context-budget', type=int, help='Advisory context threshold; never stops a task')
    for name in ('status', 'events', 'pause', 'handoff', 'verify', 'messages'):
        sub.add_parser(name).add_argument('id')
    result = sub.add_parser('result')
    result.add_argument('id')
    result.add_argument('--file')
    result.add_argument('--title')
    result.add_argument('--summary')
    switch = sub.add_parser('switch')
    switch.add_argument('id')
    switch.add_argument('--provider', required=True, choices=('codex', 'claude'))
    switch.add_argument('--model')
    worker = sub.add_parser('worker', help='Run queued work and recover interrupted owned calls')
    worker.add_argument('--once', action='store_true')
    worker.add_argument('--background', action='store_true', help='Keep authorised work running after this terminal closes')
    worker.add_argument('--interval', type=float, default=3)
    watch = sub.add_parser('watch')
    watch.add_argument('--once', action='store_true')
    watch.add_argument('--task')
    watch.add_argument('--interval', type=float, default=2)
    watch.add_argument('--lang', choices=('en', 'ja', 'de', 'es', 'fr', 'ko'), default='en')
    dashboard = sub.add_parser('dashboard')
    dashboard.add_argument('--port', type=int, default=0)
    dashboard.add_argument('--no-open', action='store_true')
    dashboard.add_argument('--lang', choices=('en', 'ja', 'de', 'es', 'fr', 'ko'), default='en')
    config = sub.add_parser('config')
    config.add_argument('--provider', choices=('codex', 'claude'))
    config.add_argument('--brain-command')
    memory = sub.add_parser('memory')
    memory.add_argument('id')
    memory.add_argument('--flush', action='store_true')
    recover = sub.add_parser('recover', help='Preserve files and abandon a historical pending file proposal')
    recover.add_argument('id')
    recover.add_argument('--keep-current-files', required=True, action='store_true')
    args = parser.parse_args(argv)
    if args.command is None:
        from .launcher import launch
        return launch(args.home)
    if args.command == 'commands':
        from .commands import terminal_guide
        print(terminal_guide())
        return 0
    if args.command == 'doctor':
        print(json.dumps(doctor(), indent=2))
        return 0
    store = Store(args.home)
    engine = Engine(store, notify=lambda phase, task_id: print(task_id + ' ' + phase, file=sys.stderr, flush=True))
    options = {key: getattr(args, key, None) for key in ('model', 'max_turns', 'context_budget', 'done')}
    def run_owned(task_id):
        try:
            return engine.run(task_id)
        except RuntimeError as error:
            if 'owns this task' not in str(error):
                raise
            from .recovery import ensure_worker
            ensure_worker(store)
            return store.get(task_id)
    try:
        if args.command == 'start':
            from .development import create
            from .simple_loop import completion_items
            completion_items(args.done or [], [], 'user')
            limit = args.max_turns if args.max_turns is not None else 8
            if not 1 <= limit <= 100:
                raise ValueError('max-turns must be between 1 and 100')
            if args.context_budget is not None and args.context_budget < 1000:
                raise ValueError('The advisory context threshold must be at least 1000 tokens')
            prompt = sys.stdin.read(24001) if args.prompt == '-' else args.prompt
            imported = None
            if args.handoff_file is not None or args.handoff_text is not None:
                from .intake import read_handoff, import_request
                text = read_handoff(args.handoff_file) if args.handoff_file is not None else args.handoff_text
                prompt, imported = import_request(prompt, text, args.handoff_file or 'pasted text')
            if not prompt or not prompt.strip() or len(prompt) > 24000:
                raise ValueError('Supply a request of 1–24000 characters')
            provider = args.provider or store.preference('provider')
            if provider is None:
                installed = [name for name in ('codex', 'claude') if shutil.which(name)]
                if len(installed) != 1:
                    raise ValueError('Choose --provider codex|claude')
                provider = installed[0]
            from .memory import configure
            memory_config = {'workspace': str(Path(args.workspace).expanduser().resolve()), 'data': {}}
            configure(store, memory_config, args.memory, directories=args.native_memory_dir,
                      remember=args.remember, project=args.project_id)
            task = create(store, args.workspace, provider, prompt, args.check, args.done, limit)
            task['data']['options'] = {'model': args.model, 'public_web': args.public_web,
                                      'context_budget': args.context_budget}
            task['data']['memory'] = memory_config['data']['memory']
            if imported:
                task['data']['imported_handoff'] = imported
            store.save(task, 'received', 'options_recorded', task['data']['options'])
            print('task ' + task['id'], file=sys.stderr, flush=True)
            result = show(task if args.queue else run_owned(task['id']))
        elif args.command == 'resume':
            engine.configure(args.id, options)
            store.set_paused(args.id, False)
            try:
                with store.lock('task:' + args.id):
                    task = store.get(args.id)
                    if task['status'] == 'needs_input' and task['data'].get('reason_code') in ('provider_wait', 'connection_wait', 'process_error'):
                        task['data'].pop('retry_at', None)
                        task['data'].update(retry_count=0, reason='', reason_code='')
                        store.save(task, 'received', 'retry_requested')
            except RuntimeError as error:
                if 'owns this task' not in str(error):
                    raise
            result = show(run_owned(args.id))
        elif args.command == 'feedback':
            engine.configure(args.id, options)
            task = engine.feedback(args.id, args.text, args.provider)
            if not args.queue:
                task = run_owned(args.id)
            result = show(task)
            result['queued_feedback'] = store.inbox(args.id)
        elif args.command == 'verify':
            from .verifier import request_verification
            with store.lock('task:' + args.id):
                task = store.get(args.id)
                from .simple_loop import migrate
                if migrate(store, task):
                    request_verification(store, task)
            result = show(run_owned(args.id))
        elif args.command == 'status':
            task = store.get(args.id)
            result = show(task)
            from .observer import snapshot
            projected = snapshot(store, args.id)['tasks'][0]
            result.update(operation=projected['operation'], observed_status=projected['status'],
                          process_health=projected['process_health'], runtime=projected['runtime'],
                          queued_feedback=store.inbox(args.id))
            if task['data'].get('development'):
                from .development import evidence_current
                result['evidence_current'] = evidence_current(task)
        elif args.command == 'events':
            result = store.events(args.id)
        elif args.command == 'messages':
            result = store.messages(args.id)
        elif args.command == 'list':
            result = store.list()
        elif args.command == 'pause':
            store.set_paused(args.id, True)
            result = {'id': args.id, 'pause_requested': True}
        elif args.command == 'switch':
            result = {'queued': store.route(args.id, args.provider, args.model)}
        elif args.command == 'handoff':
            result = show(engine.handoff(args.id))
        elif args.command == 'recover':
            result = show(engine.recover(args.id))
        elif args.command == 'result':
            with store.lock('task:' + args.id):
                task = store.get(args.id)
                presentation = task['data'].setdefault('presentation', {})
                changed = False
                for key, value, limit in [('result_file', args.file, 1000), ('title', args.title, 240), ('summary', args.summary, 2000)]:
                    if value is not None:
                        if not value.strip() or len(value) > limit:
                            raise ValueError('Invalid result ' + key)
                        presentation[key], changed = value, True
                if changed:
                    from .results import capture
                    capture(task)
                    if presentation.get('report_error'):
                        raise ValueError(presentation['report_error'])
                    store.save(task, task['status'], 'result_published')
                print(presentation.get('title') or task['prompt'])
                print('Status: ' + task['status'])
                print(presentation.get('report') or presentation.get('summary') or task['data'].get('last_summary', ''))
                if task['data'].get('reason'):
                    print(task['data']['reason'])
            return 0
        elif args.command == 'memory':
            from .memory import flush
            with store.lock('task:' + args.id) as lease:
                task = store.get(args.id)
                if args.flush:
                    for item in task['data'].get('memory', {}).get('outbox', []):
                        if item['status'] != 'stored':
                            item['retry_at'], item['attempts'] = 0, 0
                    store.save(task, task['status'], 'memory_retry_requested')
                    flush(store, task, (lease,))
                result = task['data'].get('memory', {'mode': 'off'})
        elif args.command == 'config':
            if args.brain_command is not None:
                command = json.loads(args.brain_command)
                if not isinstance(command, list) or not command or any(not isinstance(item, str) or not item for item in command):
                    raise ValueError('Brain command must be a nonempty JSON argument array')
                store.preference('brain_command', json.dumps(command))
            result = {'provider': store.preference('provider', args.provider), 'brain_command': store.preference('brain_command')}
        elif args.command == 'dashboard':
            from .observer import serve
            serve(store.home, args.port, not args.no_open, args.lang)
            return 0
        elif args.command == 'watch':
            from .observer import watch
            watch(store, args.interval, args.once, args.task, args.lang)
            return 0
        else:
            from .recovery import due
            from .memory import flush
            if args.interval < 1:
                raise ValueError('Worker interval must be at least one second')
            if args.background:
                from .recovery import ensure_worker
                print(json.dumps({'worker_pid': ensure_worker(store), 'running': True}))
                return 0
            with store.lock('worker'):
                while True:
                    for item in store.list():
                        task = store.get(item['id'])
                        controls_pending = store.inbox(item['id']) or store.routing(item['id'])
                        if store.paused(item['id']) or not due(task) and not controls_pending:
                            continue
                        pending_memory = any(row['status'] != 'stored' and row['attempts'] < 5 and row['retry_at'] <= time.time()
                                             for row in task['data'].get('memory', {}).get('outbox', []))
                        if pending_memory:
                            try:
                                with store.lock('task:' + item['id']) as lease:
                                    flush(store, store.get(item['id']), (lease,))
                            except RuntimeError:
                                pass
                        active = task['status'] in ('received', 'ready', 'running', 'working', 'checking', 'verifying', 'evaluating', 'paused')
                        if active or task['data'].get('recoverable') or store.inbox(item['id']) or store.routing(item['id']):
                            try:
                                updated = engine.run(item['id'])
                                if updated['revision'] != item['revision']:
                                    print(json.dumps(show(updated), ensure_ascii=False), flush=True)
                            except RuntimeError as error:
                                if 'owns this task' not in str(error):
                                    print(str(error), file=sys.stderr)
                    if args.once:
                        break
                    time.sleep(args.interval)
            return 0
        if (args.command in ('start', 'resume', 'feedback') and not getattr(args, 'queue', False)
                and isinstance(result, dict) and result.get('retry_at') and result.get('status') == 'received'):
            from .recovery import ensure_worker
            result['background_worker_pid'] = ensure_worker(store)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and result.get('status') in ('needs_input', 'stalled', 'interrupted', 'stale', 'blocked') else 0
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Paused; saved execution will continue when resumed.', file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == '__main__':
    sys.exit(main())
