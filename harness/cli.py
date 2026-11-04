"""Natural-language tasks, durable feedback and an optional serial worker."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

from .engine import Engine
from .context import limit_for
from .providers import doctor
from .store import Store, now
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
            'mode': data.get('mode', 'managed'), 'host': data.get('host'),
            'done_evidence': data.get('done_evidence'), 'prompts': data.get('prompts'), 'briefs': data.get('briefs'),
            'verification_pending': data.get('verification_pending', False),
            'done_rev': data.get('done_rev'), 'done_history': data.get('done_history'),
            'requirements_pending': data.get('requirements_pending'),
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
        command.add_argument('--context-budget', type=int, help='Mid-task handoff point in tokens (default: cosmos-orbit context); never stops a task')
    for name in ('status', 'events', 'pause', 'verify', 'messages'):
        sub.add_parser(name).add_argument('id')
    handoff_cmd = sub.add_parser('handoff', help='Preserve goal, corrections, evidence and verdicts; --write renders the ledger as Markdown')
    handoff_cmd.add_argument('id')
    handoff_cmd.add_argument('--write', metavar='PATH')
    # Attach Orbit to the user's own CLI session: settings, host hooks and the ledger commands the executor calls.
    for name, text in (('on', 'Turn Orbit on for this project (or --global default)'), ('off', 'Turn Orbit off for this project (or --global default)')):
        switch_cmd = sub.add_parser(name, help=text)
        switch_cmd.add_argument('path', nargs='?')
        switch_cmd.add_argument('--global', dest='global_default', action='store_true')
    sub.add_parser('settings', help='Hook installation, default and per-project switches').add_argument('path', nargs='?')
    hooks_cmd = sub.add_parser('hooks', help='install | uninstall | status the four hooks (SessionStart/UserPromptSubmit/Stop/PreCompact) in Claude Code and Codex')
    hooks_cmd.add_argument('action', choices=('install', 'uninstall', 'status'))
    hooks_cmd.add_argument('--host', action='append', choices=('claude', 'codex'))
    attach_cmd = sub.add_parser('attach', help='Create the ledger for a request in your current checkout (no clone, no model call)')
    attach_cmd.add_argument('prompt', nargs='?')
    attach_cmd.add_argument('--workspace', default='.')
    attach_cmd.add_argument('--host', choices=('codex', 'claude'))
    attach_cmd.add_argument('--check', action='append', default=[], metavar='ARGV_JSON')
    attach_cmd.add_argument('--done', action='append')
    attach_cmd.add_argument('--max-turns', type=int, default=8)
    done_cmd = sub.add_parser('done', help='Write a revision of the result goals (D items) and verification goals (fixed checks)')
    done_cmd.add_argument('id')
    done_cmd.add_argument('text', nargs='*')
    done_cmd.add_argument('--add', action='store_true', help='Append to the current items instead of replacing them')
    done_cmd.add_argument('--keep', action='store_true', help='Confirm the current items still cover the changed request')
    done_cmd.add_argument('--check', dest='checks', action='append', default=[], metavar='ARGV_JSON',
                          help='Add a fixed verification command (append-only; existing checks never change)')
    done_cmd.add_argument('--user', action='store_true', help='Record as fixed by the user')
    evidence_cmd = sub.add_parser('evidence', help='Record evidence for one completion item: a note, a command actually run, or a file fingerprint')
    evidence_cmd.add_argument('id')
    evidence_cmd.add_argument('item')
    evidence_cmd.add_argument('--note')
    evidence_cmd.add_argument('--command', dest='argv_json', metavar='ARGV_JSON')
    evidence_cmd.add_argument('--file')
    claim_cmd = sub.add_parser('claim', help='Claim completion (fixed checks run now; one independent review follows) or report remaining work')
    claim_cmd.add_argument('id')
    claim_cmd.add_argument('--summary', default='')
    claim_cmd.add_argument('--remaining', action='append', default=[])
    claim_cmd.add_argument('--question', default='')
    claim_cmd.add_argument('--wait', action='store_true', help='Run the independent review now instead of in the background worker')
    brief_cmd = sub.add_parser('brief', help='Print the brief the hooks inject (current checkout or a task ID)')
    brief_cmd.add_argument('id', nargs='?')
    detach_cmd = sub.add_parser('detach', help='Close the current attached task without verification; the ledger stays')
    detach_cmd.add_argument('id', nargs='?')
    detach_cmd.add_argument('--workspace', default='.')
    sub.add_parser('lessons', help='Corrections repeated across tasks (guard-rule candidates)').add_argument('--min', type=int, default=2)
    evolve_cmd = sub.add_parser('evolve', help='Self-evolution: local learning, and (maintainers) repeated defects fixed behind gates')
    evolve_cmd.add_argument('--reset-learned', action='store_true', help='Forget the locally learned prompt endings')
    evolve_cmd.add_argument('--mode', choices=('local', 'propose', 'merge'), help='local (default) · propose: gated fix kept as a local branch · merge: gated fix pushed and installed')
    evolve_cmd.add_argument('--repo', help='A clone of the upstream repository for propose and merge')
    evolve_cmd.add_argument('--run', action='store_true', help='Run one evolution attempt now')
    evolve_cmd.add_argument('--candidate', help='The candidate key to attempt (default: the most frequent due one)')
    checkpoint_cmd = sub.add_parser('checkpoint', help='Record the next step and working notes so a fresh session can continue')
    checkpoint_cmd.add_argument('id')
    checkpoint_cmd.add_argument('--next', dest='next_step', required=True)
    checkpoint_cmd.add_argument('--notes', default='')
    checkpoint_cmd.add_argument('--file', dest='files', action='append', default=[])
    context_cmd = sub.add_parser('context', help='Show or set the mid-task handoff point: on | off | 200k [PATH] [--global]')
    context_cmd.add_argument('value', nargs='?')
    context_cmd.add_argument('path', nargs='?')
    context_cmd.add_argument('--global', dest='global_default', action='store_true')
    hook_cmd = sub.add_parser('hook', help=argparse.SUPPRESS)
    hook_cmd.add_argument('event', choices=('session-start', 'prompt', 'stop', 'pre-compact', 'session-end'))
    hook_cmd.add_argument('--host', choices=('claude', 'codex'), default='claude')
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
    worker.add_argument('--idle-exit', type=int, help='Exit after this many quiet seconds (transient worker for one pending review)')
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
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        raw = list(sys.argv[1:] if argv is None else argv)
        if exit_.code not in (0, None) and 'hook' in raw:
            # A hook the CLI rejects fails in the host with only a usage line; record it so it can be seen and fixed.
            home = raw[raw.index('--home') + 1] if '--home' in raw[:-1] else os.environ.get('HARNESS_HOME', '~/.local/share/local-harness')
            try:
                log = Path(home).expanduser() / 'hooks.log'
                with log.open('a', encoding='utf-8') as handle:
                    handle.write(now() + ' hook usage error: ' + ' '.join(a for a in raw if not a.startswith('/'))[:300] + '\n')
            except OSError:
                pass
        raise
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
    if args.command == 'hook':
        # Host hook: stdin carries the event JSON; stdout carries one JSON object or nothing. Exit 0 always.
        from . import attach
        try:
            raw = sys.stdin.read(400000) if not sys.stdin.isatty() else ''
            payload = json.loads(raw) if raw.strip() else {}
            if not isinstance(payload, dict):
                payload = {}
        except ValueError:
            payload = {}
        try:
            output = attach.handle(store, args.event, payload, args.host)
            if output:
                print(json.dumps(output, ensure_ascii=False))
        finally:
            store.close()
        return 0
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
            from .continuity import resolve_threshold
            handoff_at = args.context_budget
            if handoff_at is None:
                handoff_at = resolve_threshold(store, memory_config['workspace'])[0] or None
            task['data']['options'] = {'model': args.model, 'public_web': args.public_web,
                                      'context_budget': handoff_at, 'auto_handoff': bool(handoff_at)}
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
        elif args.command == 'feedback' and store.get(args.id)['data'].get('mode') == 'attached':
            from .attach import cli_feedback
            if args.done:
                engine.configure(args.id, {'done': args.done})
            task = cli_feedback(store, args.id, args.text)
            result = show(task)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
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
                if task['data'].get('mode') == 'attached':
                    if not (task['data'].get('executor_result') or {}).get('finished'):
                        raise ValueError('Nothing claimed yet; run claim first')
                    task['data'].update(verification_pending=True, reason='', reason_code='')
                    task['data'].pop('checks_done_for', None)
                    task['data']['development']['phase'] = 'checking'
                    store.save(task, 'checking', 'verification_requested', {'turn': task['data'].get('turns', 0)})
                else:
                    from .simple_loop import migrate
                    if migrate(store, task):
                        request_verification(store, task)
            result = show(run_owned(args.id))
        elif args.command in ('on', 'off'):
            from .attach import set_switch, settings_view
            result = dict(set_switch(store, args.command, args.path, args.global_default), settings=settings_view(store, args.path))
        elif args.command == 'settings':
            from .attach import settings_view
            result = settings_view(store, args.path)
        elif args.command == 'hooks':
            from .attach import install_hooks, uninstall_hooks, hook_status
            hosts = args.host or [name for name in ('claude', 'codex') if shutil.which(name)] or ['claude']
            if args.action == 'install':
                result = {'installed': [install_hooks(host, args.home) for host in hosts],
                          'note': 'Hooks are inert until a project (or the default) is on: cosmos-orbit on [--global]'}
            elif args.action == 'uninstall':
                result = {'uninstalled': [uninstall_hooks(host) for host in hosts]}
            else:
                result = {host: hook_status(host) for host in hosts}
        elif args.command == 'attach':
            from .attach import create, brief_text
            from .simple_loop import completion_items
            completion_items(args.done or [], [], 'user')
            prompt = sys.stdin.read(24001) if args.prompt == '-' else args.prompt
            host = args.host or store.preference('provider')
            if host is None:
                installed = [name for name in ('codex', 'claude') if shutil.which(name)]
                if len(installed) != 1:
                    raise ValueError('Choose --host codex|claude')
                host = installed[0]
            task = create(store, args.workspace, prompt, host, None, args.check, args.done, args.max_turns)
            print(brief_text(store, task, 'attach'), file=sys.stderr)
            result = show(task)
        elif args.command == 'done':
            from .attach import set_done
            task = set_done(store, args.id, args.text, 'user' if args.user else 'executor', args.add, args.keep, args.checks)
            result = show(task)
            result['done_rev'] = task['data'].get('done_rev')
            result['revision'] = (task['data'].get('done_history') or [None])[-1]
        elif args.command == 'evidence':
            from .attach import record_evidence
            entry = record_evidence(store, args.id, args.item, args.note, args.argv_json, args.file)
            result = {'id': args.id, 'item': args.item, 'recorded': entry}
        elif args.command == 'claim':
            from .attach import claim, run_verification
            from .recovery import ensure_worker
            task = claim(store, args.id, args.summary, args.remaining, args.question)
            if task['data'].get('verification_pending'):
                task = run_verification(engine, args.id, checks_only=not args.wait)
                if task['data'].get('verification_pending') and not args.wait:
                    ensure_worker(store, idle_exit=90)
                    print('Fixed checks passed; the independent review runs in the background worker.', file=sys.stderr)
            result = show(task)
        elif args.command == 'brief':
            from .attach import brief_text, current_task, project_root
            task = store.get(args.id) if args.id else current_task(store, project_root()[0])
            if task is None:
                raise ValueError('No attached task in this checkout')
            print(brief_text(store, task, 'brief'))
            return 0
        elif args.command == 'detach':
            from .attach import detach
            result = detach(store, args.workspace, args.id)
        elif args.command == 'lessons':
            from .attach import lessons
            result = lessons(store, args.min)
        elif args.command == 'evolve':
            from . import evolve
            from .attach import LEARNED_KEY
            if args.reset_learned:
                store.preference(LEARNED_KEY, '{}')
            if args.mode or args.repo:
                evolve.configure(store, args.mode, args.repo)
            result = evolve.run(store, args.candidate) if args.run else evolve.view(store)
        elif args.command == 'checkpoint':
            from .continuity import record_checkpoint
            result = {'id': args.id, 'checkpoint': record_checkpoint(store, args.id, args.next_step, args.notes, args.files),
                      'handoff_file': str(store.home / 'runs' / args.id / 'handoff.md')}
        elif args.command == 'context':
            from .attach import project_root, current_task
            from . import continuity
            if args.value is not None and args.path is None and ('/' in args.value or Path(args.value).expanduser().is_dir()):
                args.path, args.value = args.value, None  # `cosmos-orbit context PATH` shows that project
            root = project_root(args.path)[0]
            if args.value is not None:
                changed = continuity.set_threshold(store, args.value, root, args.global_default)
            else:
                changed = None
            task = current_task(store, root)
            result = dict(continuity.threshold_view(store, root), root=str(root), changed=changed,
                          task=task['id'] if task else None,
                          measured=continuity.context_view(task['data']) if task else None,
                          checkpoints=len(task['data'].get('checkpoints') or []) if task else 0)
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
            task = store.get(args.id)
            if task['data'].get('mode') != 'attached':
                task = engine.handoff(args.id)
            result = show(task)
            if args.write:
                from .attach import write_handoff
                result['handoff_file'] = write_handoff(store, task, args.write)
            elif task['data'].get('mode') == 'attached':
                from .attach import handoff_markdown
                print(handoff_markdown(store, task))
                return 0
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
                last_active = time.time()
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
                        if task['data'].get('mode') == 'attached':
                            active = bool(task['data'].get('verification_pending')) and task['status'] not in ('verified', 'stalled', 'interrupted', 'needs_input')
                            if not active:
                                continue
                        if active or task['data'].get('recoverable') or store.inbox(item['id']) or store.routing(item['id']):
                            last_active = time.time()
                            try:
                                updated = engine.run(item['id'])
                                if updated['revision'] != item['revision']:
                                    print(json.dumps(show(updated), ensure_ascii=False), flush=True)
                            except RuntimeError as error:
                                if 'owns this task' not in str(error):
                                    print(str(error), file=sys.stderr)
                    if args.once or (args.idle_exit and time.time() - last_active > args.idle_exit):
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
