"""Interactive entry point. Explicit choices, no model calls during setup."""
import os
from pathlib import Path
import shutil
import sys


def choose(title, choices):
    print('\n' + title)
    for index, (label, _) in enumerate(choices, 1):
        print('  %d. %s' % (index, label))
    while True:
        value = input('Select [1]: ').strip() or '1'
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1][1]
        print('Enter a listed number.')


def engine_model():
    choices = [(('Codex CLI' if name == 'codex' else 'Claude Code'), name)
               for name in ('codex', 'claude') if shutil.which(name)]
    if not choices:
        raise ValueError('Install and authenticate Codex CLI or Claude Code first.')
    provider = choose('Engine', choices)
    mode = choose('Model', [('CLI default', 'default'),
                                  ('Enter model ID', 'custom')])
    model = None
    if mode == 'custom':
        while not model:
            model = input('Model ID: ').strip()
        if model.startswith('-') or any(c.isspace() for c in model) or len(model) > 200:
            raise ValueError('Invalid model identifier')
    return provider, model


def launch(home=None):
    from .cli import main
    from .store import Store
    if not sys.stdin.isatty():
        print('Interactive terminal required. Use harness --help for explicit commands.', file=sys.stderr)
        return 2
    prefix = ['--home', str(home)] if home else []
    print('\n  LOCAL HARNESS\n  Goal → Work → Verify')
    print('  Store: ' + str(Path(home or os.environ.get('HARNESS_HOME', '~/.local/share/local-harness')).expanduser()))
    try:
        action = choose('What would you like to do?', [
            ('Start a task', 'start'),
            ('Open dashboard', 'dashboard'),
            ('Watch tasks', 'watch'),
            ('Run worker', 'worker'),
            ('Switch engine and model', 'switch'),
            ('Exit', 'exit')])
        if action == 'exit':
            return 0
        if action in ('dashboard', 'watch'):
            return main(prefix + [action])
        if action == 'worker':
            return main(prefix + ['worker'])
        task_id = None
        if action == 'switch':
            store = Store(home)
            try:
                tasks = store.list()
                if not tasks:
                    print('No registered tasks.')
                    return 0
                task_id = choose('Task', [(t['id'] + ' · ' + t['provider'] + ' · ' + t['status'], t['id']) for t in tasks])
            finally:
                store.close()
        provider, model = engine_model()
        if action == 'switch':
            return main(prefix + ['switch', task_id, '--provider', provider] + (['--model', model] if model else []))
        budget_text = input('Handoff tokens [auto]: ').strip()
        budget = None
        if budget_text:
            if not budget_text.isdigit() or int(budget_text) < 1000:
                raise ValueError('Handoff threshold must be at least 1000 tokens')
            budget = int(budget_text)
        while True:
            workspace = Path(input('Workspace [.]: ').strip() or '.').expanduser().resolve()
            if workspace.is_dir():
                break
            print('Enter an existing directory.')
        prompt = ''
        while not prompt:
            prompt = input('Task: ').strip()
        mode = choose('Run mode', [('Run now', 'now'),
                                               ('Queue for worker', 'queue')])
        print('\n%s / %s\n%s\n%s' % (provider, model or 'CLI default', workspace, prompt))
        command = prefix + ['start', '--provider', provider, '--workspace', str(workspace)]
        if model:
            command += ['--model', model]
        if budget is not None:
            command += ['--context-budget', str(budget)]
        if mode == 'queue':
            command += ['--queue']
        result = main(command + ['--', prompt])
        if mode == 'queue' and result == 0:
            print('Queued: run harness worker with the same --home.')
        return result
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print('Cancelled.')
        return 130
