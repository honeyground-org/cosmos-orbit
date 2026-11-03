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
import time

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


def invite(store, task, session):
    with store.lock('task:' + task['id']):
        t = store.get(task['id'])
        t['data']['handoff_state'] = {'session': session, 'state': 'told', 'tokens': 210000, 'limit': 200000}
        store.save(t, t['status'], 'test_setup', {})


def transcript(base, name, entries):
    path = base / (name + '.jsonl')
    path.write_text(''.join(json.dumps(e) + '\n' for e in entries), encoding='utf-8')
    return str(path)


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
    check('C, open since before B ended, still does not inherit B (control: it was seen without a task)',
          task_of(store, root, 'C') is None)
    c2 = task_of(store, root, 'C2')
    check('after SessionEnd of B, a new session continues B', c2 is not None and c2['id'] == b['id'])
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

    # found by the independent review: a session that ended and then resumes is live again
    root3 = make_repo(base, 'repo-resume')
    attach.set_switch(store, 'on', root3)
    prompt(store, root3, 'R', 'Build the resume-safe exporter please')
    r_task = task_of(store, root3, 'R')
    attach.handle(store, 'session-end', {'cwd': str(root3), 'session_id': 'R'}, 'claude')
    prompt(store, root3, 'R', 'Also export to CSV')                     # `claude --resume` keeps the session id
    check('a resumed session keeps its task', task_of(store, root3, 'R')['id'] == r_task['id'])
    check('…and is live again: a new session does not take it (control for the review finding)', task_of(store, root3, 'S') is None)
    # one taker even when two new sessions race for the invitation
    with store.lock('task:' + r_task['id']):
        t = store.get(r_task['id'])
        t['data']['handoff_state'] = {'session': 'R', 'state': 'told', 'tokens': 210000, 'limit': 200000}
        store.save(t, t['status'], 'test_setup', {})
    with store.lock('task:' + r_task['id']):
        busy = attach.resolve_task(store, root3, 'T1')                    # the task is locked by someone else
    check('a locked invitation is never adopted without the lock', busy is None and store.session_task('T1') is None)
    first = attach.resolve_task(store, root3, 'T2')
    second = attach.resolve_task(store, root3, 'T3')
    check('the invitation has exactly one taker', first is not None and first['id'] == r_task['id'] and second is None)

    # found by the fourth independent review: an open session without a task took a later handoff invitation
    root4 = make_repo(base, 'repo-open')
    attach.set_switch(store, 'on', root4)
    attach.handle(store, 'session-start', {'cwd': str(root4), 'session_id': 'OB', 'source': 'startup'}, 'claude')
    prompt(store, root4, 'OA', 'Generate the product video with the new storyboard')
    oa = task_of(store, root4, 'OA')
    invite(store, oa, 'OA')
    prompt(store, root4, 'OB', 'Reduce indexing CPU when many sessions start')
    ob = task_of(store, root4, 'OB')
    check("an open session without a task never takes another session's invitation (review counterexample)",
          ob is not None and ob['id'] != oa['id'] and store.get(oa['id'])['data']['handoff_state'].get('state') == 'told')
    fresh = attach.resolve_task(store, root4, 'OA2', 'clear')
    check('…while the new session the invitation was for still takes it (control)', fresh is not None and fresh['id'] == oa['id'])

    # a session already in conversation when Orbit first sees it (the switch turned on mid-session)
    prompt(store, root4, 'OC', 'Render the storyboard thumbnails in a grid')
    oc = task_of(store, root4, 'OC')
    invite(store, oc, 'OC')
    talked = transcript(base, 'talked', [{'type': 'user', 'sessionId': 'MID', 'message': {'role': 'user', 'content': 'hi'}},
                                         {'type': 'assistant', 'sessionId': 'MID', 'message': {'role': 'assistant', 'content': []}}])
    mid = attach.handle(store, 'prompt', {'cwd': str(root4), 'session_id': 'MID', 'prompt': 'Translate the README into Spanish',
                                          'transcript_path': talked}, 'claude')
    check('a session already in conversation when first seen does not take an invitation',
          task_of(store, root4, 'MID')['id'] != oc['id'] and store.get(oc['id'])['data']['handoff_state'].get('state') == 'told')
    codex_fresh = transcript(base, 'codex-new', [{'type': 'session_meta', 'payload': {'id': 'CX'}},
                                                 {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': []}}])
    cx = attach.resolve_task(store, root4, 'CX', None, codex_fresh)
    check('a Codex /new session (rollout with no reply yet) takes it (control)', cx is not None and cx['id'] == oc['id'])

    # a conversation resumed under a new session id keeps its own task
    resumed = transcript(base, 'resumed', [{'type': 'user', 'sessionId': 'OA2', 'message': {'role': 'user', 'content': 'x'}},
                                           {'type': 'assistant', 'sessionId': 'OA2', 'message': {'role': 'assistant', 'content': []}}])
    again = attach.resolve_task(store, root4, 'OA3', 'resume', resumed)
    check('a conversation resumed under a new id finds its own task', again is not None and again['id'] == oa['id'])
    stranger = transcript(base, 'stranger', [{'type': 'assistant', 'sessionId': 'NEVER-SEEN', 'message': {'role': 'assistant', 'content': []}}])
    other = attach.resolve_task(store, root4, 'OA4', 'resume', stranger)
    check('…and an unrelated resumed conversation does not (control)', other is None)

    # Codex reports no SessionEnd: a second handoff must still work (the first adoption ends the inviter)
    root5 = make_repo(base, 'repo-chain')
    attach.set_switch(store, 'on', root5)
    attach.handle(store, 'prompt', {'cwd': str(root5), 'session_id': 'X1', 'prompt': 'Port the exporter to the new API'}, 'codex')
    x1 = task_of(store, root5, 'X1')
    invite(store, x1, 'X1')
    x2 = attach.resolve_task(store, root5, 'X2', 'startup')
    invite(store, x2, 'X2')
    x3 = attach.resolve_task(store, root5, 'X3', 'startup')
    check('Codex: a second handoff in a row is taken by the next new session', x2 and x3 and x2['id'] == x1['id'] == x3['id'])

    # found by the fifth independent review: several new sessions at once after SessionEnd → exactly one takes it
    for label, setup in (('after SessionEnd', 'end'), ('with a handoff invitation', 'invite')):
        rootp = make_repo(base, 'repo-race-' + setup)
        attach.set_switch(store, 'on', rootp)
        owner = 'P0-' + setup
        prompt(store, rootp, owner, 'Write the race-safe importer please')
        p0 = task_of(store, rootp, owner)
        if setup == 'end':
            attach.handle(store, 'session-end', {'cwd': str(rootp), 'session_id': owner}, 'claude')
        else:
            invite(store, p0, owner)
        at = time.time() + 1.5
        script = ('import sys,time,json; sys.path.insert(0, %r); from harness import attach; from harness.store import Store;'
                  's=Store(%r); time.sleep(max(0, %r - time.time())); t=attach.resolve_task(s, %r, sys.argv[1], "startup");'
                  'print(json.dumps(t and t["id"]))') % (str(ROOT), str(base / 'home'), at, str(rootp))
        procs = [subprocess.Popen([sys.executable, '-c', script, 'N%d-%s' % (i, setup)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(10)]
        outs = [p.communicate()[0].strip() for p in procs]
        took = [o for o in outs if o and o != 'null']
        live = [r['session'] for r in store.task_sessions(p0['id']) if not r['ended']]
        check('ten new sessions at once %s → exactly one takes the task' % label, len(took) == 1 and len(live) == 1, (outs, live))

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
    old2 = attach.create(store, root2, 'Second legacy task, not the latest', 'claude', None)
    newer = attach.create(store, root2, 'A newer task by someone else', 'claude', 'OTHER')
    with store.lock('task:' + old2['id']):
        t = store.get(old2['id'])
        t['data']['session_ids'] = ['MEMBER']
        store.save(t, t['status'], 'test_setup', {})
    with store.db:
        store.db.execute('DELETE FROM session_tasks WHERE task_id=?', (old2['id'],))
    found = task_of(store, root2, 'MEMBER')
    check('a legacy member finds its own task even when it is not the latest', found is not None and found['id'] == old2['id'],
          found and found['id'])

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
