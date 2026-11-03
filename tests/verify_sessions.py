#!/usr/bin/env python3
"""Several sessions open in one project must not share a task by accident. Model-free, with controls.

Also runs every installed hook event through the real command line (a hook the CLI rejects fails silently
in the host: the PreCompact hook did exactly that until this check existed).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH'):
    os.environ.pop(name, None)

from harness import attach, continuity  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', '-C', str(root), 'init', '-q'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '--allow-empty', '-m', 'init'], check=True)
    return root


def prompt(store, root, session, text, source=None):
    return attach.handle(store, 'prompt', {'cwd': str(root), 'session_id': session, 'prompt': text}, 'claude')


def task_of(store, root, session):
    return attach.resolve_task(store, root, session)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-sessions-'))
    store = Store(base / 'home')
    root = make_repo(base, 'repo')
    attach.set_switch(store, 'on', root)

    print('① two live sessions in one project get separate tasks')
    prompt(store, root, 'A', 'Generate the product video with the new storyboard')
    a = task_of(store, root, 'A')
    prompt(store, root, 'B', 'Reduce local-brain indexing CPU when many sessions start')
    b = task_of(store, root, 'B')
    check('A and B have different tasks', a and b and a['id'] != b['id'], (a and a['id'], b and b['id']))
    prompt(store, root, 'A', 'Also add subtitles to the video')
    a = store.get(a['id'])
    b = store.get(b['id'])
    check("A's change lands in A's task only", a['data']['feedback'][-1]['text'] == 'Also add subtitles to the video'
          and all('subtitles' not in f['text'] for f in b['data'].get('feedback') or []))
    prompt(store, root, 'B', 'It must be event based')
    a = store.get(a['id'])
    check("B's change never reaches A (control for the old bug)", all('event based' not in f['text'] for f in a['data'].get('feedback') or []))
    check('no ping-pong rotations between A and B', 'session_rotated' not in [r['kind'] for r in store.events(a['id'])])

    print('② continuation rules: ended sessions, handoff invitation (one taker), /clear without invitation')
    start = attach.handle(store, 'session-start', {'cwd': str(root), 'session_id': 'A2', 'source': 'clear'}, 'claude')
    check("/clear without an invitation does not take another live session's task (control)", task_of(store, root, 'A2') is None and start is None)
    c = task_of(store, root, 'C')
    check('a new terminal while B is live does not inherit B (control)', c is None)
    attach.handle(store, 'session-end', {'cwd': str(root), 'session_id': 'B'}, 'claude')
    c = task_of(store, root, 'C')
    check('after SessionEnd of B, a new session continues B', c is not None and c['id'] == b['id'])
    prompt(store, root, 'D', 'Generate thumbnails for the video')
    d = task_of(store, root, 'D')
    with store.lock('task:' + d['id']):
        t = store.get(d['id'])
        t['data']['handoff_state'] = {'session': 'D', 'state': 'told', 'tokens': 210000, 'limit': 200000}
        store.save(t, t['status'], 'test_setup', {})
    e = attach.resolve_task(store, root, 'E', 'clear')
    check('a handoff invitation lets the next session continue while D is live', e is not None and e['id'] == d['id'])
    check('the invitation has one taker (control)', task_of(store, root, 'F') is None
          and store.get(d['id'])['data']['handoff_state'].get('adopted_by') == 'E')

    print('③ tasks created before session binding: the creator owns them')
    root2 = make_repo(base, 'repo2')
    attach.set_switch(store, 'on', root2)
    legacy = attach.create(store, root2, 'Legacy task from before binding', 'claude', None)
    with store.lock('task:' + legacy['id']):
        t = store.get(legacy['id'])
        t['data']['prompts'] = [{'at': '2026-11-03T00:00:00+00:00', 'text': 'Legacy task from before binding', 'session': 'OLD'}]
        store.save(t, t['status'], 'test_setup', {})
    check('another session does not inherit a legacy task while its creator may be live', task_of(store, root2, 'NEW') is None)
    check('the creator still finds it', task_of(store, root2, 'OLD')['id'] == legacy['id'])

    print('④ every installed hook event runs through the real command line (exit 0)')
    home = str(base / 'home')
    for host, events in attach.HOST_EVENTS.items():
        for _, name, _ in events:
            payload = json.dumps({'cwd': str(root), 'session_id': 'Z-' + name, 'prompt': 'x', 'source': 'startup', 'trigger': 'auto'})
            result = subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', home, 'hook', name, '--host', host],
                                    input=payload, capture_output=True, text=True)
            check('%s hook %s → exit 0, no usage error' % (host, name), result.returncode == 0 and 'usage:' not in result.stderr, result.stderr[-300:])
    check('Codex is not given a SessionEnd hook it does not have', 'SessionEnd' not in [e for e, _, _ in attach.HOST_EVENTS['codex']])
    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
