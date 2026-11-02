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
    parser = argparse.ArgumentParser(prog='cosmos-orbit', description='Persistent, provider-neutral local task runtime (pilot)')
    parser.add_argument('--home', help='Private task store (default: ~/.local/share/local-harness)')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('commands', help='Show the quick command reference without starting services or models')
    sub.add_parser('doctor', help='Show installed CLIs; never installs or changes provider settings')
    sub.add_parser('list', help='Read current tasks without a model call')
    plan_cmd = sub.add_parser('plan', help='Generate a separate execution plan for an existing development task')
    plan_cmd.add_argument('id')
    plan_cmd.add_argument('--auto', action='store_true', help='Continue automatically after planning; no review hold')
    plan_cmd.add_argument('--public-web', action='store_true', help='Authorise read-only public-web research during execution')
    assess_cmd = sub.add_parser('assess', help='Evaluate existing development evidence and continue feasible work')
    assess_cmd.add_argument('id')
    approve_cmd = sub.add_parser('approve', help='Approve a reviewed plan and queue execution')
    approve_cmd.add_argument('id')
    result_cmd = sub.add_parser('result', help='Read or publish a task result in both terminal and browser')
    result_cmd.add_argument('id')
    result_cmd.add_argument('--file', help='UTF-8 result path relative to the task workspace; refresh after later turns')
    result_cmd.add_argument('--title', help='User-facing task title')
    result_cmd.add_argument('--summary', help='User-facing result summary')
    config = sub.add_parser('config', help='Choose a default provider once; task-level overrides remain possible')
    config.add_argument('--provider', choices=('codex', 'claude'))
    config.add_argument('--brain-command', metavar='ARGV_JSON', help='Optional stdio Brain MCP command')
    memory_cmd = sub.add_parser('memory', help='Inspect or retry optional memory integration')
    memory_cmd.add_argument('id')
    memory_cmd.add_argument('--flush', action='store_true', help='Retry queued Brain writes now')
    switch = sub.add_parser('switch', help='Queue engine/model selection at the next safe boundary')
    switch.add_argument('id')
    switch.add_argument('--provider', required=True, choices=('codex', 'claude'))
    switch.add_argument('--model', help='Model ID; omit to use the selected CLI default')
    start = sub.add_parser('start', help='Interpret an ordinary request and run a local artifact task')
    start.add_argument('prompt', nargs='?', help='Ordinary user request, or - to read stdin; optional with a handoff')
    handoff_input = start.add_mutually_exclusive_group()
    handoff_input.add_argument('--handoff-file', help='Import an explicit UTF-8 handoff document into a new managed task')
    handoff_input.add_argument('--handoff-text', help='Import pasted handoff context into a new managed task')
    start.add_argument('--memory', choices=('off', 'native', 'brain', 'both'), default='off')
    start.add_argument('--native-memory-dir', action='append', default=[], help='Explicit native Markdown memory directory')
    start.add_argument('--remember', action='store_true', help='Queue historical verification checkpoints for Brain')
    start.add_argument('--project-id', help='Stable memory scope shared across checkouts')
    start.add_argument('--plan-first', action='store_true', help='Generate a plan and wait for review before native execution')
    start.add_argument('--public-web', action='store_true', help='Authorise public web research in native development execution')
    start.add_argument('--development', action='store_true', help='Edit an isolated Git clone with native tools')
    start.add_argument('--check', action='append', default=[], metavar='ARGV_JSON',
                       help='Authorise a fixed verification command as a JSON argument array; repeatable')
    start.add_argument('--workspace', default='.')
    start.add_argument('--provider', choices=('codex', 'claude'))
    start.add_argument('--peer', action='append', default=[], metavar='NAME=PROVIDER',
                       help='Optional task-owned adviser session; repeat for multiple peers')
    execution = start.add_mutually_exclusive_group()
    execution.add_argument('--queue', action='store_true', help='Persist for a worker instead of running now')
    execution.add_argument('--plan-only', action='store_true')
    resume = sub.add_parser('resume', help='Recover from the durable checkpoint')
    resume.add_argument('id')
    feedback = sub.add_parser('feedback', help='Apply natural-language correction and reopen the task')
    feedback.add_argument('id')
    feedback.add_argument('text')
    feedback.add_argument('--provider', choices=('codex', 'claude'))
    feedback.add_argument('--queue', action='store_true')
    for command in (start, resume, feedback):
        command.add_argument('--max-attempts', type=int)
        command.add_argument('--model', help='Optional model on the selected CLI; no cross-provider fallback')
        command.add_argument('--timeout', type=int, help='Deprecated compatibility option; elapsed time never stops a task')
        command.add_argument('--context-budget', type=int,
                             help='Handoff before the next call at this measured context size; default Claude 500k, Codex min(500k, 80%% window)')
    for name in ('status', 'events', 'messages', 'handoff', 'pause'):
        child = sub.add_parser(name)
        child.add_argument('id')
    recovery = sub.add_parser('recover', help='Abandon a pending proposal while preserving all current files')
    recovery.add_argument('id')
    recovery.add_argument('--keep-current-files', action='store_true', required=True)
    worker = sub.add_parser('worker', help='Run queued tasks without requiring another user prompt')
    worker.add_argument('--once', action='store_true')
    worker.add_argument('--interval', type=float, default=3)
    watch = sub.add_parser('watch', help='Live, model-free terminal monitor')
    watch.add_argument('--once', action='store_true')
    watch.add_argument('--task')
    watch.add_argument('--interval', type=float, default=2)
    watch.add_argument('--lang', choices=('en',), default='en')
    dashboard = sub.add_parser('dashboard', help='Local session monitor and task interventions')
    dashboard.add_argument('--port', type=int, default=0)
    dashboard.add_argument('--no-open', action='store_true')
    dashboard.add_argument('--lang', choices=('en',), default='en')
    args = parser.parse_args(argv)
    if args.command is None:
        from .launcher import launch
        return launch(args.home)
    if args.command == 'commands':
        from .commands import terminal_guide
        print(terminal_guide())
        return 0
    if args.command == 'doctor':
        print(json.dumps(doctor(), ensure_ascii=False, indent=2))
        return 0
    store = Store(args.home)
    notify = lambda phase, task_id: print(task_id + ' ' + phase, file=sys.stderr, flush=True)
    engine = Engine(store, notify=notify)
    options = {key: getattr(args, key, None) for key in ('model', 'timeout', 'max_attempts', 'context_budget')}
    try:
        if args.command == 'watch':
            from .observer import watch as observe
            observe(store, args.interval, args.once, args.task, args.lang)
            return 0
        elif args.command == 'dashboard':
            from .observer import serve
            serve(store.home, args.port, not args.no_open, args.lang)
            return 0
        elif args.command == 'plan':
            with store.lock('task:' + args.id):
                task = store.get(args.id)
                if not task['data'].get('development'):
                    raise ValueError('Execution plans currently require a development task')
                task['data']['plan_requested'] = True
                task['data']['review_plan'] = not args.auto
                if args.public_web:
                    task['data']['options']['public_web'] = True
                store.save(task, 'ready', 'execution_plan_requested', {'layer': 'control', 'note': 'Plan requested'})
            task = engine.run(args.id)
            if args.auto and task['status'] == 'ready':
                task = engine.run(args.id)
            result = show(task)
        elif args.command == 'assess':
            with store.lock('task:' + args.id):
                task = store.get(args.id)
                from .manager import request_assessment
                request_assessment(store, task)
            result = show(engine.run(args.id))
        elif args.command == 'approve':
            from .planning import approve
            with store.lock('task:' + args.id):
                task = store.get(args.id)
                approve(store, task)
            result = show(task)
        elif args.command == 'result':
            with store.lock('task:' + args.id):
                task = store.get(args.id)
                if args.file is not None or args.title is not None or args.summary is not None:
                    presentation = task['data'].setdefault('presentation', {})
                    for key, value, limit in [('title', args.title, 240), ('summary', args.summary, 2000), ('result_file', args.file, 1000)]:
                        if value is not None:
                            if not value.strip() or len(value) > limit:
                                raise ValueError('Invalid result ' + key)
                            presentation[key] = value
                    from .results import capture
                    capture(task)
                    if presentation.get('report_error'):
                        raise ValueError(presentation['report_error'])
                    store.save(task, task['status'], 'result_published', {'note': 'Result available in terminal and browser', 'layer': 'evidence', 'direction': 'in'})
                p = task['data'].get('presentation', {})
                print(p.get('title') or task['prompt'])
                print('Status: ' + task['status'])
                print(p.get('summary') or task['data'].get('last_summary', ''))
                if task['data'].get('reason'):
                    print('Remaining: ' + task['data']['reason'])
                if p.get('report'):
                    print('\n' + p.get('report_source', 'Saved result') + '\n' + p['report'])
                elif p.get('report_error'):
                    print('Result unavailable: ' + p['report_error'])
            return 0
        elif args.command == 'switch':
            result = {'queued': store.route(args.id, args.provider, args.model),
                      'note': 'Worker or resume applies this selection at a safe boundary. Model availability is checked by the CLI on its next call.'}
        elif args.command == 'start':
            if args.plan_first and args.plan_only:
                raise ValueError('Choose --plan-first for a generated plan or --plan-only for a model-free development hold')
            if (args.plan_first or args.public_web) and not args.development:
                raise ValueError('--plan-first and --public-web currently require --development')
            root = Path(args.workspace).expanduser().resolve()
            if not root.is_dir():
                raise ValueError('Workspace must already exist')
            prompt = sys.stdin.read(24001) if args.prompt == '-' else args.prompt
            imported = None
            if args.handoff_file is not None or args.handoff_text is not None:
                from .intake import read_handoff, import_request
                text = read_handoff(args.handoff_file) if args.handoff_file is not None else args.handoff_text
                prompt, imported = import_request(prompt, text, str(Path(args.handoff_file).expanduser().resolve()) if args.handoff_file else 'pasted text')
            prompt = prompt or ''
            if not prompt.strip() or len(prompt) > 24000:
                raise ValueError('Request must be nonempty and at most 24000 characters')
            initial_options = dict(model=None, timeout=None, max_attempts=3, context_budget=None)
            initial_options.update({k: v for k, v in options.items() if v is not None})
            if not 1 <= initial_options['max_attempts'] <= 10 or (initial_options['context_budget'] is not None and initial_options['context_budget'] < 1000):
                raise ValueError('Invalid attempt or context budget')
            provider = args.provider or store.preference('provider')
            if provider is None:
                installed = [name for name in ('codex', 'claude') if shutil.which(name)]
                if len(installed) == 1:
                    provider = installed[0]
                else:
                    raise ValueError('Choose once with config --provider codex|claude, or pass --provider for this task')
            peers = {}
            for item in args.peer:
                name, sep, peer_provider = item.partition('=')
                if not sep or not name or len(name) > 32 or not name.replace('-', '').replace('_', '').isalnum() or peer_provider not in ('codex', 'claude') or name in peers:
                    raise ValueError('Peer must be a unique NAME=codex or NAME=claude binding')
                peers[name] = {'provider': peer_provider}
            from .memory import configure as configure_memory
            memory_config = {'workspace': str(root), 'data': {}}
            configure_memory(store, memory_config, args.memory, directories=args.native_memory_dir,
                             remember=args.remember, project=args.project_id)
            if args.development:
                if peers:
                    raise ValueError('Development adviser sessions are not supported yet')
                from .development import create
                task = create(store, root, provider, prompt, args.check)
            else:
                if args.check:
                    raise ValueError('--check requires --development')
                task = store.create(root, provider, prompt)
            task['data']['memory'] = memory_config['data']['memory']
            task['data']['options'] = dict(initial_options, public_web=args.public_web)
            task['data']['plan_requested'] = bool(args.development and not args.plan_only)
            task['data']['review_plan'] = args.plan_first
            task['data']['peers'] = peers
            if imported:
                task['data']['imported_handoff'] = imported
            store.save(task, 'received', 'options_recorded', task['data']['options'])
            print('task ' + task['id'], file=sys.stderr, flush=True)
            if not args.queue:
                task = engine.run(task['id'], plan_only=args.plan_only)
                if args.development and not args.plan_only and not args.plan_first and task['status'] == 'ready':
                    task = engine.run(task['id'])
            result = show(task)
        elif args.command == 'resume':
            with store.lock('task:' + args.id):
                task = store.get(args.id)
                if task['data'].get('execution_plan', {}).get('state') == 'awaiting_review':
                    from .planning import approve
                    approve(store, task)
                elif task['data'].get('development', {}).get('hold'):
                    task['data']['development']['hold'] = False
                    store.save(task, 'ready', 'development_execution_requested')
            store.set_paused(args.id, False)
            engine.configure(args.id, options)
            task = engine.run(args.id)
            result = show(task)
        elif args.command == 'feedback':
            engine.configure(args.id, options)
            task = engine.feedback(args.id, args.text, args.provider)
            if not args.queue:
                try:
                    task = engine.run(args.id)
                except RuntimeError as error:
                    if 'owns this task' not in str(error):
                        raise
                    # The active executor will consume the persisted correction at a boundary.
                    task = store.get(args.id)
            result = show(task)
            result['queued_feedback'] = store.inbox(args.id)
            result['messages'] = store.messages(args.id)
        elif args.command == 'status':
            task = store.get(args.id)
            result = show(task)
            result['queued_feedback'] = store.inbox(args.id)
            result['messages'] = store.messages(args.id)
            if task['data'].get('development'):
                from .development import evidence_current
                result['evidence_current'] = evidence_current(task)
                if task['status'] == 'needs_review' and not result['evidence_current']:
                    result['status'] = 'stale'
            if task['data']['contract']:
                current = verify(task['workspace'], task['data']['contract'])
                result['current_evidence'] = current
                result['evidence_current'] = all(c['passed'] for c in current)
                result['recorded_status'] = result['status']
                if result['status'] in ('completed', 'already_satisfied') and not result['evidence_current']:
                    result['status'] = 'stale'
            from .observer import snapshot
            projected = snapshot(store, args.id)['tasks'][0]
            result['operation'] = projected['operation']
            result['process_health'] = projected['process_health']
            result['runtime'] = projected['runtime']
            result['observed_status'] = projected['status']
        elif args.command == 'events':
            result = store.events(args.id)
        elif args.command == 'messages':
            store.get(args.id)
            result = store.messages(args.id)
        elif args.command == 'handoff':
            result = show(engine.handoff(args.id))
        elif args.command == 'pause':
            store.set_paused(args.id, True)
            result = {'id': args.id, 'pause_requested': True,
                      'note': 'The executor stops at its next safe boundary; an in-flight model call may finish.'}
        elif args.command == 'recover':
            result = show(engine.recover(args.id))
        elif args.command == 'list':
            result = store.list()
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
                if not isinstance(command, list) or not command or any(not isinstance(v, str) or not v for v in command):
                    raise ValueError('Brain command must be a JSON argument array')
                store.preference('brain_command', json.dumps(command))
            result = {'provider': store.preference('provider', args.provider),
                      'brain_command': store.preference('brain_command')}
        else:
            if args.interval < 1:
                raise ValueError('Worker interval must be at least one second')
            with store.lock('worker'):
                while True:
                    for item in store.list():
                        if store.paused(item['id']):
                            continue
                        memory_task = store.get(item['id'])
                        if any(m['status'] != 'stored' and m['attempts'] < 5 and m['retry_at'] <= time.time()
                               for m in memory_task['data'].get('memory', {}).get('outbox', [])):
                            try:
                                from .memory import flush
                                with store.lock('task:' + item['id']) as lease:
                                    flush(store, store.get(item['id']), (lease,))
                            except RuntimeError:
                                pass  # Active executors own their task state.
                        if store.paused(item['id']):
                            continue
                        if item['status'] == 'interrupted' and store.get(item['id'])['data']['pending'] is not None:
                            continue  # A conflicted batch needs explicit resume or recovery.
                        if store.inbox(item['id']) or store.routing(item['id']) or item['status'] in ('received', 'ready', 'planning', 'running', 'applying', 'verifying', 'evaluating', 'repairing', 'waiting_peer', 'paused'):
                            try:
                                result = engine.run(item['id'])
                                if result['revision'] != item['revision']:
                                    print(json.dumps(show(result), ensure_ascii=False), flush=True)
                            except RuntimeError as error:
                                print(str(error), file=sys.stderr)
                    if args.once:
                        break
                    time.sleep(args.interval)
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get('status') in ('blocked', 'interrupted', 'needs_input', 'stale'):
            return 2
        return 0
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Stopped; durable task state is retained.', file=sys.stderr)
        return 130
    finally:
        store.close()


if __name__ == '__main__':
    sys.exit(main())
