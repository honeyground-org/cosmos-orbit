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

    print('③-b two open handoffs in one project: the pane decides which one a fresh session takes (seen 2026-11-04)')
    from harness import typist
    from harness.store import now
    real_detect = typist.detect

    def in_pane(target):
        typist.detect = (lambda env=None: None) if target is None else \
            (lambda env=None: {'kind': 'tmux', 'cli': '/usr/bin/true', 'target': target})

    def start(session, source):
        attach.handle(store, 'session-start', {'cwd': str(root3), 'session_id': session, 'source': source}, 'claude')
        return task_of(store, root3, session)

    root3 = make_repo(base, 'repo-panes')
    attach.set_switch(store, 'on', root3)
    try:
        in_pane('%PANE-Y')
        start('Y', 'startup')
        prompt(store, root3, 'Y', 'Refactor the importer and keep the output identical')
        mine = task_of(store, root3, 'Y')
        in_pane('%PANE-X')
        start('X', 'startup')
        prompt(store, root3, 'X', 'Render the product video with the new storyboard')
        other = task_of(store, root3, 'X')
        check('setup: two sessions, two panes, two tasks', mine and other and mine['id'] != other['id'])
        for task, state in ((mine, {'session': 'Y', 'state': 'auto', 'panel': '%PANE-Y', 'at': '2026-11-04T05:34:22+00:00', 'auto_at': now()}),
                            (other, {'session': 'X', 'state': 'told', 'at': now(), 'told_at': now()})):  # the other one is newer
            with store.lock('task:' + task['id']):
                t = store.get(task['id'])
                t['data']['handoff_state'] = dict(state, tokens=360000, limit=350000)
                store.save(t, t['status'], 'test_setup', {})
        in_pane('%PANE-Y')
        took = start('Z', 'clear')                       # Orbit typed /clear in pane Y
        check("the fresh session in the pane where Orbit typed /clear takes that pane's task, not the newer handoff "
              'of a session still working in another pane', took and took['id'] == mine['id'], took and took['id'])
        check("…and the other pane's handoff stays open for its own session (control)",
              store.get(other['id'])['data']['handoff_state']['state'] == 'told')
        in_pane(None)
        elsewhere = start('W', 'startup')                # a plain terminal while X still holds pane X
        check('a fresh session elsewhere does not take a handoff whose session still holds its pane',
              not elsewhere or elsewhere['id'] != other['id'], elsewhere and elsewhere['id'])
        attach.handle(store, 'session-end', {'cwd': str(root3), 'session_id': 'X'}, 'claude')
        later = start('V', 'startup')
        check('once that session has ended, a fresh session anywhere takes its handoff (control: not a ban)',
              later and later['id'] == other['id'], later and later['id'])
    finally:
        typist.detect = real_detect

    print('③-c the handoff follows the pane, not the folder: the shell had cd\'d into a worktree (seen 2026-11-04 17:40)')
    launch = make_repo(base, 'repo-launch')
    wt = launch / 'wt'
    subprocess.run(['git', '-C', str(launch), 'worktree', 'add', '-q', '--detach', str(wt)], check=True, capture_output=True)
    for folder in (launch, wt):
        attach.set_switch(store, 'on', folder)

    def begin(session, source, cwd):
        out = attach.handle(store, 'session-start', {'cwd': str(cwd), 'session_id': session, 'source': source}, 'claude')
        return attach.resolve_task(store, cwd, session), json.dumps(out or {}, ensure_ascii=False)

    def arm(task, **state):
        with store.lock('task:' + task['id']):
            t = store.get(task['id'])
            t['data']['handoff_state'] = dict(state, tokens=404042, limit=350000)
            if state.get('state') == 'auto':
                t['data']['carried_prompt'] = {'text': 'Yes, merge it and install', 'at': now(), 'from_session': state['session']}
            store.save(t, t['status'], 'test_setup', {})

    def end(session, cwd):
        attach.handle(store, 'session-end', {'cwd': str(cwd), 'session_id': session}, 'claude')

    try:
        in_pane('%PANE-E')
        begin('E1', 'startup', launch)
        prompt(store, wt, 'E1', 'Merge the pull request and reinstall from main')   # the agent cd'd into the worktree
        moved = attach.resolve_task(store, wt, 'E1')
        check('setup: the task lives in the worktree the shell moved to',
              moved and Path(moved['workspace']).resolve() == wt.resolve(), moved and moved['workspace'])
        arm(moved, session='E1', state='auto', panel='%PANE-E', at=now(), auto_at=now())
        end('E1', wt)                                     # Orbit's /clear ends the old session before the fresh one starts
        in_pane('%PANE-OTHER')
        stranger, _ = begin('E-other', 'clear', launch)
        check('a /clear in another pane does not take it (control)', not stranger or stranger['id'] != moved['id'],
              stranger and stranger['id'])
        in_pane('%PANE-E')
        took, brief = begin('E2', 'clear', launch)        # the fresh session starts where the host was launched
        check('the fresh session in the pane where Orbit typed /clear takes the task although it starts one folder up',
              took and took['id'] == moved['id'], took and took['id'])
        check('…and its first brief carries the request the user made', 'Yes, merge it and install' in brief, brief[:300])

        in_pane('%PANE-T')
        begin('TOLD1', 'startup', launch)
        prompt(store, wt, 'TOLD1', 'Write the release note for the merged change')
        told = attach.resolve_task(store, wt, 'TOLD1')
        with store.lock('task:' + told['id']):
            t = store.get(told['id'])
            t['data']['context_live'] = dict(t['data'].get('context_live') or {}, session='TOLD1', tokens=404042)
            t['data']['handoff_state'] = {'session': 'TOLD1', 'state': 'checkpointed', 'tokens': 404042, 'limit': 350000,
                                          'at': now(), 'checkpoint_at': now()}
            store.save(t, t['status'], 'test_setup', {})
        attach.context_decision(store, store.get(told['id']))       # the Stop hook tells the user to /clear
        recorded = store.get(told['id'])['data']['handoff_state']
        check('telling the user records the pane the handoff was told in', recorded.get('state') == 'told'
              and recorded.get('pane') == '%PANE-T', recorded)
        end('TOLD1', wt)
        relaunch, _ = begin('TOLD0', 'startup', launch)      # a new launch in that pane, not a /clear
        check('a new launch in that pane does not take another folder\'s handoff (control: only /clear crosses folders)',
              not relaunch or relaunch['id'] != told['id'], relaunch and relaunch['id'])
        in_pane('%PANE-T2')
        elsewhere, _ = begin('TOLD-else', 'clear', launch)
        check('a /clear in another pane does not take another folder\'s told handoff (control)',
              not elsewhere or elsewhere['id'] != told['id'], elsewhere and elsewhere['id'])
        in_pane('%PANE-T')
        took2, _ = begin('TOLD2', 'clear', launch)
        check('the user\'s own /clear in the told pane takes it although the fresh session starts one folder up',
              took2 and took2['id'] == told['id'], took2 and took2['id'])

        print('③-d when the hook misses anyway, the typist hands the task to the session that appeared in its pane')
        from harness import wake
        in_pane('%PANE-G')
        begin('G1', 'startup', launch)
        prompt(store, wt, 'G1', 'Rebuild the demo page and check it')
        g = attach.resolve_task(store, wt, 'G1')
        arm(g, session='G1', state='auto', panel='%PANE-G', at=now(), auto_at=now())
        end('G1', wt)
        wake.note(store, 'G2', 'claude', 'session-start', {'kind': 'tmux', 'target': '%PANE-G'})
        store.bind_session('G2', attach.NO_TASK, launch)  # its hook decided there was nothing to take (today's outcome)
        wake.note(store, 'G-other', 'claude', 'session-start', {'kind': 'tmux', 'target': '%PANE-NOT-G'})
        ran = []
        typist.run({'kind': 'tmux', 'cli': '/usr/bin/true', 'target': '%PANE-G'},
                   [('adopted', 1.0), ('text', typist.CARRY_LINE), ('key', 'enter')],
                   runner=lambda argv: ran.append(argv) or 0, sleep=lambda s: None,
                   adopted=lambda: continuity.adopt_in_panel(store, g['id'], '%PANE-G'))
        check('the typist hands the task to the fresh session in its pane, then types the carry line',
              (store.session_task('G2') or {}).get('task_id') == g['id'] and any(typist.CARRY_LINE in a for a in ran),
              (store.session_task('G2'), ran))
        check('…the other pane\'s session is left alone (control)', (store.session_task('G-other') or {}).get('task_id') != g['id'])
        again, _ = begin('G2', 'clear', launch)
        check('the fresh session then finds that task on its next event', again and again['id'] == g['id'], again and again['id'])

        begin('K1', 'startup', launch)
        prompt(store, wt, 'K1', 'Translate the README into Japanese')
        k = attach.resolve_task(store, wt, 'K1')
        arm(k, session='K1', state='auto', panel='%PANE-K', at=now(), auto_at=now())
        wake.note(store, 'K2', 'claude', 'prompt', {'kind': 'tmux', 'target': '%PANE-K'})
        store.bind_session('K2', g['id'], launch)        # a session that already works on a task of its own
        check('a session with a task of its own is never taken from it (control)',
              continuity.adopt_in_panel(store, k['id'], '%PANE-K') is False and store.session_task('K2')['task_id'] == g['id'])
        wake.note(store, 'K3', 'claude', 'session-start', {'kind': 'tmux', 'target': '%PANE-K3'})
        check('the typist hands over only in the pane it typed /clear in (control)',
              continuity.adopt_in_panel(store, k['id'], '%PANE-K3') is False)
        with store.lock('task:' + k['id']):
            t = store.get(k['id'])
            t['data']['handoff_state']['panel'] = '%PANE-K4'
            store.save(t, t['status'], 'test_setup', {})
        wake.note(store, 'K4', 'claude', 'session-start', {'kind': 'tmux', 'target': '%PANE-K4'})
        check('a session whose own hook has not finished yet gets the grace period first (control)',
              continuity.adopt_in_panel(store, k['id'], '%PANE-K4') is False and store.session_task('K4') is None)
        check('…and is handed the task once the grace has passed',
              continuity.adopt_in_panel(store, k['id'], '%PANE-K4', grace=0) is True
              and (store.session_task('K4') or {}).get('task_id') == k['id'])

        print("③-e after a failed auto handoff the user's own /clear in that pane takes it across folders (review counterexample)")
        in_pane('%PANE-AF')
        begin('AF1', 'startup', launch)
        prompt(store, wt, 'AF1', 'Package the build and upload the artifact')
        failed = attach.resolve_task(store, wt, 'AF1')
        arm(failed, session='AF1', state='auto_failed', panel='%PANE-AF', at=now(), auto_at=now(), failed_at=now())
        end('AF1', wt)                                    # the user types /clear: the old session ends first
        in_pane('%PANE-AF-OTHER')
        other_af, _ = begin('AF-other', 'clear', launch)
        check('a /clear in another pane does not take it (control)', not other_af or other_af['id'] != failed['id'],
              other_af and other_af['id'])
        in_pane('%PANE-AF')
        took_af, _ = begin('AF2', 'clear', launch)
        check("the user's /clear in the pane Orbit had typed into takes it although it starts one folder up",
              took_af and took_af['id'] == failed['id'], took_af and took_af['id'])
    finally:
        typist.detect = real_detect

    print("③-f a task whose session was closed long ago is not handed to the next day's new terminal (2026-11-05)")
    # Replayed on 1bc4852: the session was closed (SessionEnd); 20 hours later a new terminal in that folder took its
    # task, and its unrelated request was recorded as a correction of it. The invitation's 12-hour expiry did not
    # help: a closed session's task reached the new terminal as "the folder's latest task, nobody on it".
    import datetime

    def closed_ago(session, hours):
        stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).isoformat()
        with store.db:
            store.db.execute('UPDATE session_tasks SET ended=? WHERE session=? AND ended IS NOT NULL', (stamp, session))

    def start(root_, session, source='startup'):
        attach.handle(store, 'session-start', {'cwd': str(root_), 'session_id': session, 'source': source}, 'claude')

    root_x = make_repo(base, 'repo-closed-long-ago')
    attach.set_switch(store, 'on', root_x)
    start(root_x, 'CL-A')
    prompt(store, root_x, 'CL-A', 'Refactor the report module and keep the output identical')
    old = attach.resolve_task(store, root_x, 'CL-A')
    attach.handle(store, 'session-end', {'cwd': str(root_x), 'session_id': 'CL-A'}, 'claude')
    closed_ago('CL-A', 20)
    start(root_x, 'CL-B')
    prompt(store, root_x, 'CL-B', 'Build a login page with email and password fields')
    new = attach.resolve_task(store, root_x, 'CL-B')
    old = store.get(old['id'])
    check('a new terminal 20 hours after the session was closed starts its own task',
          new is not None and new['id'] != old['id'], (old['id'], new and new['id']))
    check('…its request is not recorded in the closed task',
          all('login page' not in (p.get('text') or '') for p in old['data'].get('prompts') or []), old['data'].get('prompts'))
    check('…the closed task keeps its ledger (not closed, says why it was not continued)',
          old['status'] not in attach.CLOSED and 'continuation_declined' in [r['kind'] for r in store.events(old['id'])],
          (old['status'], [r['kind'] for r in store.events(old['id'])][-4:]))
    start(root_x, 'CL-A', source='resume')
    check('…the closed session itself resumed (claude --resume) still has its task (control)',
          (attach.resolve_task(store, root_x, 'CL-A') or {}).get('id') == old['id'])
    root_y = make_repo(base, 'repo-closed-just-now')
    attach.set_switch(store, 'on', root_y)
    start(root_y, 'CR-A')
    prompt(store, root_y, 'CR-A', 'Refactor the export module and keep the output identical')
    recent = attach.resolve_task(store, root_y, 'CR-A')
    attach.handle(store, 'session-end', {'cwd': str(root_y), 'session_id': 'CR-A'}, 'claude')
    closed_ago('CR-A', 0.1)
    start(root_y, 'CR-B')
    check('a new terminal minutes after the session was closed continues its task (control)',
          (attach.resolve_task(store, root_y, 'CR-B') or {}).get('id') == recent['id'])

    print('③-g move-session: a session bound to the wrong task moves onto its own (the 2026-11-05 OFFSCRIPT binding)')
    # The shape of the incident: the session worked on the harness task, then (after a /clear in the same pane) the
    # next session's unrelated product requests were recorded as corrections of the harness task.
    root_m = make_repo(base, 'repo-move')
    attach.set_switch(store, 'on', root_m)
    start(root_m, 'MV-A')
    prompt(store, root_m, 'MV-A', 'Fix the handoff flow so that no session is ever lost')
    harness_task = attach.resolve_task(store, root_m, 'MV-A')
    attach.set_done(store, harness_task['id'], ['D1 a fresh session takes the handoff exactly once'])
    prompt(store, root_m, 'MV-A', 'Also keep the flow exact for Codex sessions')
    end_turn = lambda s: attach.handle(store, 'stop', {'cwd': str(root_m), 'session_id': s, 'stop_hook_active': True,
                                                         'last_assistant_message': 'ok'}, 'claude')
    end_turn('MV-A')
    boundary = attach.now()
    prompt(store, root_m, 'MV-B', 'Build the AI content page with social posts and thumbnails')
    prompt(store, root_m, 'MV-B', 'Add a calendar view to the content page')
    from harness import continuity as continuity_
    continuity_.record_checkpoint(store, harness_task['id'], 'Wire the calendar view', notes='content page done', by='claude')
    # Bind MV-B as the idle takeover did, and record its prompts on the harness task (what the defect produced).
    store.bind_session('MV-B', harness_task['id'], root_m)
    for text in ('Build the AI content page with social posts and thumbnails', 'Add a calendar view to the content page'):
        attach.record_prompt(store, store.get(harness_task['id']), text, 'MV-B')
    with store.lock('task:' + harness_task['id']):
        t = store.get(harness_task['id'])
        t['data']['checkpoints'][-1]['session'] = 'MV-B'  # written in MV-B's context
        store.save(t, t['status'], 'test_setup', {})
    before = store.get(harness_task['id'])
    plan = attach.move_session(store, 'MV-B', since=boundary)
    after_plan = store.get(harness_task['id'])
    check('the plan names the requests to move and changes nothing',
          plan['prompts'] == 2 and not plan['applied'] and after_plan['revision'] == before['revision']
          and (store.session_task('MV-B') or {}).get('task_id') == harness_task['id'], plan)
    moved = attach.move_session(store, 'MV-B', since=boundary, apply=True)
    old_t, new_t = store.get(harness_task['id']), store.get(moved['to'])
    check('applied: the session works on its own new task, whose request is its first one',
          (store.session_task('MV-B') or {}).get('task_id') == new_t['id'] != old_t['id']
          and new_t['prompt'].startswith('Build the AI content page'), (store.session_task('MV-B'), new_t['prompt'][:60]))
    pending_m = old_t['data'].get('requirements_pending') or {}
    check('…its requests and corrections left the old task; its pending change is its own request again, counted once',
          all('content page' not in p['text'] for p in old_t['data']['prompts'])
          and all('content page' not in f['text'] for f in old_t['data'].get('feedback') or [])
          and 'Codex sessions' in pending_m.get('text', '') and pending_m.get('count') == 1,
          ([p['text'][:30] for p in old_t['data']['prompts']], pending_m))
    check("…the old task keeps its own requests from before the boundary (control) and a record of what moved",
          any('Codex sessions' in f['text'] for f in old_t['data'].get('feedback') or [])
          and len(old_t['data'].get('moved_prompts') or []) == 2 and 'prompts_moved_out' in [r['kind'] for r in store.events(old_t['id'])])
    check('…the checkpoint written in that session moved with it',
          [c['next'] for c in new_t['data'].get('checkpoints') or []] == ['Wire the calendar view']
          and all(c['next'] != 'Wire the calendar view' for c in old_t['data'].get('checkpoints') or []))
    check("…the session's next request lands on the new task",
          prompt(store, root_m, 'MV-B', 'Show the calendar by week') is not None
          and any('by week' in p['text'] for p in store.get(new_t['id'])['data']['prompts'])
          and all('by week' not in p['text'] for p in store.get(old_t['id'])['data']['prompts']))
    try:
        attach.move_session(store, 'MV-A', since=attach.now())
        refused = None
    except ValueError as error:
        refused = str(error)
    check('nothing after the boundary: refused, nothing changes (control)', refused and 'Nothing to move' in refused, refused)
    # The incident itself: every pending request came from the sessions that move (MV-C before its /clear, MV-D after).
    root_n = make_repo(base, 'repo-move-all')
    attach.set_switch(store, 'on', root_n)
    start(root_n, 'MV-H')
    prompt(store, root_n, 'MV-H', 'Fix the handoff flow so that no session is ever lost')
    task_n = attach.resolve_task(store, root_n, 'MV-H')
    attach.set_done(store, task_n['id'], ['D1 a fresh session takes the handoff exactly once'])
    for sess, text in (('MV-C', 'Build the AI content page with social posts'), ('MV-D', 'Add a calendar view to the content page')):
        store.bind_session(sess, task_n['id'], root_n)
        attach.record_prompt(store, store.get(task_n['id']), text, sess)
    attach.handle(store, 'session-end', {'cwd': str(root_n), 'session_id': 'MV-C'}, 'claude')
    ended_c = store.session_task('MV-C')['ended']
    moved_n = attach.move_session(store, 'MV-D', also=['MV-C'], apply=True)
    old_n = store.get(task_n['id'])
    check('every pending request moved: the old task waits on nothing (the OFFSCRIPT case)',
          moved_n['prompts'] == 2 and not old_n['data'].get('requirements_pending'), old_n['data'].get('requirements_pending'))
    row_c = store.session_task('MV-C')
    check('…the session before the /clear (--also) now belongs to the new task and stays ended (a later --resume finds it)',
          row_c['task_id'] == moved_n['to'] and row_c['ended'] == ended_c, row_c)
    home_m = str(base / 'home')
    result = subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', home_m, 'move-session', 'MV-A'],
                            capture_output=True, text=True)
    check('the command line prints the plan and exits 0 (dry run)', result.returncode == 0 and '"applied": false' in result.stdout,
          (result.returncode, result.stderr[-300:]))

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
