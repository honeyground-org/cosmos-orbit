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
        value = input('선택 / Select [1]: ').strip() or '1'
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1][1]
        print('목록의 번호를 입력하세요. / Enter a listed number.')


def engine_model():
    choices = [(('Codex CLI' if name == 'codex' else 'Claude Code'), name)
               for name in ('codex', 'claude') if shutil.which(name)]
    if not choices:
        raise ValueError('Install and authenticate Codex CLI or Claude Code first.')
    provider = choose('실행 엔진 / Engine', choices)
    mode = choose('모델 / Model', [('CLI 기본 선택 / CLI default', 'default'),
                                  ('모델 ID 직접 지정 / Enter model ID', 'custom')])
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
    print('\n  LOCAL HARNESS\n  목적 → 실행 → 검증 / Goal → Work → Verify')
    print('  Store: ' + str(Path(home or os.environ.get('HARNESS_HOME', '~/.local/share/local-harness')).expanduser()))
    try:
        action = choose('무엇을 할까요? / What would you like to do?', [
            ('새 작업 시작 / Start a task', 'start'),
            ('관찰 화면 열기 / Open dashboard', 'dashboard'),
            ('터미널 상태 보기 / Watch tasks', 'watch'),
            ('작업 처리기 실행 / Run worker', 'worker'),
            ('기존 작업 엔진·모델 전환 / Switch engine and model', 'switch'),
            ('종료 / Exit', 'exit')])
        if action == 'exit':
            return 0
        if action in ('dashboard', 'watch'):
            language = choose('화면 언어 / Display language', [('한국어', 'ko'), ('English', 'en')])
            return main(prefix + [action, '--lang', language])
        if action == 'worker':
            return main(prefix + ['worker'])
        task_id = None
        if action == 'switch':
            store = Store(home)
            try:
                tasks = store.list()
                if not tasks:
                    print('등록된 작업이 없습니다. / No registered tasks.')
                    return 0
                task_id = choose('작업 선택 / Task', [(t['id'] + ' · ' + t['provider'] + ' · ' + t['status'], t['id']) for t in tasks])
            finally:
                store.close()
        provider, model = engine_model()
        if action == 'switch':
            return main(prefix + ['switch', task_id, '--provider', provider] + (['--model', model] if model else []))
        while True:
            workspace = Path(input('작업 폴더 / Workspace [.]: ').strip() or '.').expanduser().resolve()
            if workspace.is_dir():
                break
            print('존재하는 폴더를 입력하세요. / Enter an existing directory.')
        prompt = ''
        while not prompt:
            prompt = input('작업 목적 / Task: ').strip()
        mode = choose('실행 방식 / Run mode', [('지금 실행 / Run now', 'now'),
                                               ('대기열에 등록 / Queue for worker', 'queue')])
        print('\n%s / %s\n%s\n%s' % (provider, model or 'CLI default', workspace, prompt))
        command = prefix + ['start', '--provider', provider, '--workspace', str(workspace)]
        if model:
            command += ['--model', model]
        if mode == 'queue':
            command += ['--queue']
        result = main(command + ['--', prompt])
        if mode == 'queue' and result == 0:
            print('대기열에 등록했습니다. 같은 저장소에서 harness worker를 실행하세요. / Queued: run harness worker with the same --home.')
        return result
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except (EOFError, KeyboardInterrupt):
        print('\n취소했습니다. / Cancelled.')
        return 130
