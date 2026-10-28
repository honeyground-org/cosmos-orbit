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


def show(task):
    data = task['data']
    return {'id': task['id'], 'status': task['status'], 'revision': task['revision'],
            'provider': task['provider'], 'workspace': task['workspace'],
            'request': task['prompt'], 'contract': data['contract'],
            'attempts': data['attempt'], 'evidence': data['evidence'],
            'reason': data['reason'], 'feedback': data['feedback'],
            'sessions': data['sessions'], 'context': data.get('context_measurements', {}),
            'context_limits': {key: limit_for(data, key, data.get('peers', {}).get(key[5:], {}).get('provider')
                                             if key.startswith('peer:') else key.split(':')[0])
                               for key in set(data['sessions']) | set(data.get('handoffs', {}))},
            'handoffs': data.get('handoff_counts', {}), 'updated': task['updated']}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Persistent, provider-neutral local task runtime (pilot)')
    parser.add_argument('--home', help='Private task store (default: ~/.local/share/local-harness)')
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('doctor', help='Show installed CLIs; never installs or changes provider settings')
    sub.add_parser('list', help='Read current tasks without a model call')
    config = sub.add_parser('config', help='Choose a default provider once; task-level overrides remain possible')
    config.add_argument('--provider', choices=('codex', 'claude'))
    switch = sub.add_parser('switch', help='Queue engine/model selection at the next safe boundary')
    switch.add_argument('id')
    switch.add_argument('--provider', required=True, choices=('codex', 'claude'))
    switch.add_argument('--model', help='Model ID; omit to use the selected CLI default')
    start = sub.add_parser('start', help='Interpret an ordinary request and run a local artifact task')
    start.add_argument('prompt', help='Ordinary user request, or - to read stdin')
    start.add_argument('--workspace', default='.')
    start.add_argument('--provider', choices=('codex', 'claude'))
    start.add_argument('--peer', action='append', default=[], metavar='NAME=PROVIDER',
                       help='Optional task-owned adviser session; repeat for multiple peers')
    start.add_argument('--queue', action='store_true', help='Persist for a worker instead of running now')
    start.add_argument('--plan-only', action='store_true')
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
        command.add_argument('--timeout', type=int)
        command.add_argument('--context-budget', type=int,
                             help='Handoff before the next call at this measured context size; default Claude 500k, Codex min(500k, 80% window)')
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
        elif args.command == 'switch':
            result = {'queued': store.route(args.id, args.provider, args.model),
                      'note': 'Worker or resume applies this selection at a safe boundary. Model availability is checked by the CLI on its next call.'}
        elif args.command == 'start':
            root = Path(args.workspace).expanduser().resolve()
            if not root.is_dir():
                raise ValueError('Workspace must already exist')
            prompt = sys.stdin.read() if args.prompt == '-' else args.prompt
            if not prompt.strip() or len(prompt) > 24000:
                raise ValueError('Request must be nonempty and at most 24000 characters')
            initial_options = dict(model=None, timeout=180, max_attempts=3, context_budget=None)
            initial_options.update({k: v for k, v in options.items() if v is not None})
            if not 1 <= initial_options['max_attempts'] <= 10 or not 1 <= initial_options['timeout'] <= 3600 or (initial_options['context_budget'] is not None and initial_options['context_budget'] < 1000):
                raise ValueError('Invalid attempt, timeout or context budget')
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
            task = store.create(root, provider, prompt)
            task['data']['options'] = initial_options
            task['data']['peers'] = peers
            store.save(task, 'received', 'options_recorded', task['data']['options'])
            print('task ' + task['id'], file=sys.stderr, flush=True)
            if not args.queue:
                task = engine.run(task['id'], plan_only=args.plan_only)
            result = show(task)
        elif args.command == 'resume':
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
            if task['data']['contract']:
                current = verify(task['workspace'], task['data']['contract'])
                result['current_evidence'] = current
                result['evidence_current'] = all(c['passed'] for c in current)
                result['recorded_status'] = result['status']
                if result['status'] in ('completed', 'already_satisfied') and not result['evidence_current']:
                    result['status'] = 'stale'
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
        elif args.command == 'config':
            result = {'provider': store.preference('provider', args.provider)}
        else:
            if args.interval < 1:
                raise ValueError('Worker interval must be at least one second')
            with store.lock('worker'):
                while True:
                    for item in store.list():
                        if store.paused(item['id']):
                            continue
                        if item['status'] == 'interrupted' and store.get(item['id'])['data']['pending'] is not None:
                            continue  # A conflicted batch needs explicit resume or recovery.
                        if store.inbox(item['id']) or store.routing(item['id']) or item['status'] in ('received', 'ready', 'planning', 'running', 'applying', 'verifying', 'repairing', 'waiting_peer', 'paused'):
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
