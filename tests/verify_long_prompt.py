#!/usr/bin/env python3
"""A prompt longer than the ledger limit must still open a task from the prompt hook instead of failing.

Synthetic data only; no model call. Exit 0 = pass.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harness import attach  # noqa: E402
from harness.store import Store  # noqa: E402


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-long-prompt-'))
    root = base / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    store = Store(base / 'home')
    os.environ.pop('ORBIT', None)
    os.environ.pop('COSMOS_ORBIT', None)
    attach.set_switch(store, 'on', global_default=True)
    text = 'Please add a function that adds two numbers and write a test for it. ' * 500  # about 35000 characters
    assert len(text) > 24000
    attach.handle(store, 'prompt', {'cwd': str(root), 'session_id': 's-long', 'prompt': text}, 'claude')
    log = store.home / 'hooks.log'
    logged = log.read_text(encoding='utf-8') if log.exists() else ''
    task_id = store.attached(str(attach.project_root(root)[0]))
    failed = []
    if 'Supply a request' in logged:
        failed.append('hook logged a length error: ' + logged.strip()[-200:])
    if not task_id:
        failed.append('no task was attached for a long prompt')
    else:
        prompt = store.get(task_id)['data']['prompts'][0]['text']
        if not prompt.startswith('Please add a function'):
            failed.append('the task does not keep the start of the request')
    for item in failed:
        print('  FAIL ' + item)
    if not failed:
        print('  ok  a long prompt opens a task')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
