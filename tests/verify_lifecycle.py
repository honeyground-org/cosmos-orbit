#!/usr/bin/env python3
"""A task's life after its work (2026-11-05 user request ⑨). Model-free, with controls.

① verified, then the same session goes on: the task reopens (working, goals to revise), not a new task
② verified or left alone for 12 hours: the task closes; its sessions end; a new terminal does not take it
③ a closed task comes back only when asked: list --closed, continue <id>; a related new request gets one line
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH', 'CLAUDE_CODE_SESSION_ID',
             'CODEX_THREAD_ID', 'TMUX', 'TMUX_PANE', 'CMUX_SURFACE_ID', 'CMUX_PANEL_ID', 'CMUX_WORKSPACE_ID'):
    os.environ.pop(name, None)

from harness import attach  # noqa: E402
from harness.store import Store  # noqa: E402

try:
    from harness import lifecycle  # noqa: E402
except ImportError:                # before the change: every check below fails instead of crashing
    lifecycle = None

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


def hook(store, root, event, session, **payload):
    return attach.handle(store, event, dict(payload, cwd=str(root), session_id=session), 'claude')


def verify(store, task_id):
    """What a passing review leaves behind (the verifier's own save)."""
    with store.lock('task:' + task_id):
        task = store.get(task_id)
        task['data']['verification_pending'] = False
        task['data']['reason_code'] = ''
        store.save(task, 'verified', 'completion_verified', {'turn': task['data'].get('turns', 0)})


def age(store, task_id, hours, sessions=()):
    """Make the task (and the last hook of its sessions) `hours` old, as if nothing had happened since."""
    stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).isoformat()
    with store.db:
        store.db.execute('UPDATE tasks SET updated=? WHERE id=?', (stamp, task_id))
        for session in sessions:
            store.db.execute('UPDATE terminals SET at=? WHERE session=?', (stamp, session))
            store.db.execute('UPDATE session_tasks SET at=? WHERE session=?', (stamp, session))
        store.db.execute("DELETE FROM preferences WHERE key='lifecycle_swept_at'")  # the next hook sweeps the ledger


def bound(store, session):
    return (store.session_task(session) or {}).get('task_id')


def cli(home, *args, env=None):
    return subprocess.run([sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(home)] + list(args),
                          capture_output=True, text=True, env=dict(os.environ, **(env or {})))


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-lifecycle-'))
    store = Store(base / 'home')
    root = make_repo(base, 'repo')
    attach.set_switch(store, 'on', root)

    print('① verified, then the same session goes on: the task reopens instead of a new one')
    hook(store, root, 'session-start', 'A', source='startup')
    hook(store, root, 'prompt', 'A', prompt='Build the CSV export for the monthly report page')
    task = attach.resolve_task(store, root, 'A')
    attach.set_done(store, task['id'], ['D1 the export downloads a CSV with every row of the report'])
    verify(store, task['id'])
    hook(store, root, 'prompt', 'A', prompt='좋아요, 고마워요')
    check('a thank-you after verification changes nothing (control)',
          store.get(task['id'])['status'] == 'verified' and bound(store, 'A') == task['id'])
    reply = hook(store, root, 'prompt', 'A', prompt='Now add an Excel export next to the CSV button')
    after = store.get(task['id'])
    check("the session's next substantial request reopens the same task (no new task)",
          bound(store, 'A') == task['id'] and after['status'] == 'working', (bound(store, 'A'), after['status']))
    pending = after['data'].get('requirements_pending') or {}
    check('…with its goals to revise: the request is pending', 'Excel export' in pending.get('text', ''), pending)
    brief = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
    check('…and the brief says it was reopened', 'REOPENED' in brief, brief[:300])
    check('…the ledger records the reopening', 'task_reopened' in [r['kind'] for r in store.events(task['id'])])

    root_b = make_repo(base, 'repo-new-terminal')
    attach.set_switch(store, 'on', root_b)
    hook(store, root_b, 'session-start', 'B1', source='startup')
    hook(store, root_b, 'prompt', 'B1', prompt='Build the CSV export for the monthly report page')
    vb = attach.resolve_task(store, root_b, 'B1')
    attach.set_done(store, vb['id'], ['D1 the export downloads a CSV'])
    verify(store, vb['id'])
    hook(store, root_b, 'session-end', 'B1')
    hook(store, root_b, 'session-start', 'B2', source='startup')
    hook(store, root_b, 'prompt', 'B2', prompt='Write the onboarding email sequence for new partners')
    check('a new terminal after verification starts its own task (control: only the session that did the work reopens)',
          bound(store, 'B2') not in (None, vb['id']) and store.get(vb['id'])['status'] == 'verified',
          (bound(store, 'B2'), store.get(vb['id'])['status']))

    print('② untouched for 12 hours: it closes (verified and unfinished alike)')
    root_c = make_repo(base, 'repo-close')
    attach.set_switch(store, 'on', root_c)
    hook(store, root_c, 'session-start', 'C', source='startup')
    hook(store, root_c, 'prompt', 'C', prompt='Build the CSV export for the monthly report page')
    vc = attach.resolve_task(store, root_c, 'C')
    attach.set_done(store, vc['id'], ['D1 the export downloads a CSV'])
    verify(store, vc['id'])
    age(store, vc['id'], 13, ['C'])
    reply = hook(store, root_c, 'prompt', 'C', prompt='Now add an Excel export next to the CSV button')
    old = store.get(vc['id'])
    check('verified and untouched for 13 hours: closed (reason verified) before the next prompt reads it',
          old['status'] == 'closed' and (old['data'].get('closed') or {}).get('reason') == 'verified',
          (old['status'], old['data'].get('closed')))
    check("…so the session's request a day later starts a new task", bound(store, 'C') not in (None, vc['id']),
          bound(store, 'C'))
    brief = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
    check('…whose brief names the closed task in one line, as a suggestion (not linked)',
          'CLOSED EARLIER HERE' in brief and vc['id'] in brief and 'continue ' + vc['id'] in brief, brief[-500:])

    root_d = make_repo(base, 'repo-idle')
    attach.set_switch(store, 'on', root_d)
    hook(store, root_d, 'session-start', 'D', source='startup')
    hook(store, root_d, 'prompt', 'D', prompt='Migrate the billing worker to the new queue library')
    idle = attach.resolve_task(store, root_d, 'D')
    hook(store, root_d, 'session-start', 'D-live', source='startup')
    hook(store, root_d, 'prompt', 'D-live', prompt='Refactor the invoice PDF renderer for the new template')
    live = attach.resolve_task(store, root_d, 'D-live')
    age(store, idle['id'], 20, ['D'])                    # D sat open (Codex sends no SessionEnd), nothing for 20 h
    age(store, live['id'], 20)                           # the task record is old, but its session is in use
    hook(store, root_d, 'stop', 'D-live', stop_hook_active=True, last_assistant_message='ok')
    swept = store.get(idle['id'])
    check('an unfinished task whose session went quiet for 20 hours closes (reason idle) on any hook',
          swept['status'] == 'closed' and (swept['data'].get('closed') or {}).get('reason') == 'idle',
          (swept['status'], swept['data'].get('closed')))
    check('…its session is ended, so no new terminal takes it as "nobody on it"',
          all(r['ended'] for r in store.task_sessions(idle['id'])), store.task_sessions(idle['id']))
    check('…its ledger is kept (goal, prompts, events)',
          swept['prompt'].startswith('Migrate the billing') and 'task_closed' in [r['kind'] for r in store.events(idle['id'])])
    check('a task whose session hooked just now stays open although its record is 20 hours old (control)',
          store.get(live['id'])['status'] == 'working', store.get(live['id'])['status'])
    hook(store, root_d, 'session-start', 'D-new', source='startup')
    check('a new terminal in that folder does not take the closed task', bound(store, 'D-new') != idle['id'], bound(store, 'D-new'))

    root_e = make_repo(base, 'repo-recent')
    attach.set_switch(store, 'on', root_e)
    hook(store, root_e, 'session-start', 'E', source='startup')
    hook(store, root_e, 'prompt', 'E', prompt='Migrate the billing worker to the new queue library')
    recent = attach.resolve_task(store, root_e, 'E')
    age(store, recent['id'], 11, ['E'])
    hook(store, root_e, 'stop', 'other-session', stop_hook_active=True, last_assistant_message='ok')
    check('11 hours of quiet: still open (control for the 12-hour line)', store.get(recent['id'])['status'] == 'working')
    with store.lock('task:' + recent['id']):
        t = store.get(recent['id'])
        t['data']['verification_pending'] = True
        store.save(t, t['status'], 'test_setup', {})
    age(store, recent['id'], 30, ['E'])
    lifecycle and lifecycle.sweep(store)
    check("a task waiting for Orbit's own review does not close (control: the wait is Orbit's, not the user's)",
          store.get(recent['id'])['status'] == 'working')

    root_r = make_repo(base, 'repo-come-back')
    attach.set_switch(store, 'on', root_r)
    hook(store, root_r, 'session-start', 'R', source='startup')
    hook(store, root_r, 'prompt', 'R', prompt='Port the scheduler to the new cron library and keep the old jobs')
    rt = attach.resolve_task(store, root_r, 'R')
    age(store, rt['id'], 17, ['R'])                      # the computer was off overnight (2026-11-06)
    hook(store, root_r, 'prompt', 'R-other', prompt='이어서 진행해')
    check('another session saying "이어서 진행해" does not reopen it (control)', store.get(rt['id'])['status'] == 'closed')
    hook(store, root_r, 'prompt', 'R', prompt='이어서 진행해')
    back_r = store.get(rt['id'])
    check('the session that worked on it says "이어서 진행해" the next morning: the same task reopens (working, bound)',
          back_r['status'] == 'working' and bound(store, 'R') == rt['id'], (back_r['status'], bound(store, 'R')))
    root_u = make_repo(base, 'repo-come-back-new-work')
    attach.set_switch(store, 'on', root_u)
    hook(store, root_u, 'session-start', 'U', source='startup')
    hook(store, root_u, 'prompt', 'U', prompt='Port the scheduler to the new cron library and keep the old jobs')
    ut = attach.resolve_task(store, root_u, 'U')
    age(store, ut['id'], 17, ['U'])
    hook(store, root_u, 'prompt', 'U', prompt='새 랜딩 페이지 작업을 시작해서 진행해')
    check('…a short new request that merely ends in "진행해" does not reopen it (control)',
          store.get(ut['id'])['status'] == 'closed', store.get(ut['id'])['status'])
    hook(store, root_u, 'prompt', 'U', prompt='Write the release notes for the mobile app version 3.2')
    check('…while the same session asking for unrelated work starts a new task (control)',
          store.get(ut['id'])['status'] == 'closed' and bound(store, 'U') not in (None, ut['id']), (store.get(ut['id'])['status'], bound(store, 'U')))

    print('③ a closed task comes back only when asked')
    listed = cli(base / 'home', 'list', '--closed', '--workspace', str(root_d))
    rows = json.loads(listed.stdout) if listed.returncode == 0 else []
    check('list --closed shows it with its goal, when and why it closed, and how to continue',
          any(r['id'] == idle['id'] and r['reason'] == 'idle' and r['closed_at'] and 'continue ' + idle['id'] in r['continue']
              for r in rows), (listed.returncode, listed.stdout[:300], listed.stderr[-300:]))
    check('…only the folder asked for (control)', all(r['workspace'] == str(root_d.resolve()) for r in rows), rows)
    hook(store, root_d, 'prompt', 'D-new', prompt='Continue the billing worker migration to the new queue library')
    started = bound(store, 'D-new')
    result = cli(base / 'home', 'continue', idle['id'], env={'CLAUDE_CODE_SESSION_ID': 'D-new'})
    back = store.get(idle['id'])
    check('continue <id> in a session: that session works on it again, working',
          result.returncode == 0 and bound(store, 'D-new') == idle['id'] and back['status'] == 'working',
          (result.returncode, result.stderr[-300:], bound(store, 'D-new'), back['status']))
    check('…the task it had just started for that request closes, and the request moves onto the task taken back',
          started and store.get(started)['status'] == 'closed'
          and any('billing worker migration' in p['text'] for p in back['data']['prompts']),
          (started and store.get(started)['status'], [p['text'][:40] for p in back['data']['prompts']]))
    check('…the ledger records who took it back', any(r['kind'] == 'task_reopened' and r['payload'].get('by') == 'continue'
                                                      for r in store.events(idle['id'])))
    hook(store, root_d, 'prompt', 'D-new', prompt='Also keep the retry policy of the old queue')
    check("…and the session's next request lands on it", any('retry policy' in p['text'] for p in store.get(idle['id'])['data']['prompts']))

    root_f = make_repo(base, 'repo-continue-fresh')
    attach.set_switch(store, 'on', root_f)
    hook(store, root_f, 'session-start', 'F', source='startup')
    hook(store, root_f, 'prompt', 'F', prompt='Translate the help center articles into Japanese')
    ft = attach.resolve_task(store, root_f, 'F')
    age(store, ft['id'], 30, ['F'])
    lifecycle and lifecycle.sweep(store)
    result = cli(base / 'home', 'continue', ft['id'])
    hook(store, root_f, 'session-start', 'F-first', source='startup')
    hook(store, root_f, 'session-start', 'F-second', source='startup')
    check('continue <id> from a plain shell: the next new session in that folder takes it',
          result.returncode == 0 and bound(store, 'F-first') == ft['id'] and store.get(ft['id'])['status'] == 'working',
          (result.returncode, result.stderr[-300:], bound(store, 'F-first'), store.get(ft['id'])['status']))
    check('…exactly one takes it (control)', bound(store, 'F-second') != ft['id'], bound(store, 'F-second'))

    root_g = make_repo(base, 'repo-unrelated')
    attach.set_switch(store, 'on', root_g)
    hook(store, root_g, 'session-start', 'G', source='startup')
    hook(store, root_g, 'prompt', 'G', prompt='Migrate the billing worker to the new queue library')
    gt = attach.resolve_task(store, root_g, 'G')
    age(store, gt['id'], 30, ['G'])
    lifecycle and lifecycle.sweep(store)
    reply = hook(store, root_g, 'prompt', 'G2', prompt='Design a dark mode palette for the marketing site')
    brief = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
    check('an unrelated request in a new terminal gets no suggestion (control)', 'CLOSED EARLIER HERE' not in brief, brief[-300:])
    reply = hook(store, root_g, 'prompt', 'G3', prompt='Finish migrating the billing worker to the new queue')
    brief = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
    check('a related request in a new terminal gets the one-line suggestion, and is not linked',
          'CLOSED EARLIER HERE' in brief and gt['id'] in brief and store.get(gt['id'])['status'] == 'closed'
          and bound(store, 'G3') != gt['id'], brief[-300:])

    root_k = make_repo(base, 'repo-korean')
    attach.set_switch(store, 'on', root_k)
    hook(store, root_k, 'session-start', 'K', source='startup')
    hook(store, root_k, 'prompt', 'K', prompt='로그인 페이지를 이메일과 비밀번호 입력으로 만들어줘')
    kt = attach.resolve_task(store, root_k, 'K')
    age(store, kt['id'], 30, ['K'])
    lifecycle and lifecycle.sweep(store)
    reply = hook(store, root_k, 'prompt', 'K2', prompt='어제 하던 로그인 페이지 작업을 마저 끝내자')
    brief = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
    check('Korean: a related request (particles differ) gets the suggestion', 'CLOSED EARLIER HERE' in brief and kt['id'] in brief, brief[-300:])
    reply = hook(store, root_k, 'prompt', 'K3', prompt='월간 매출 보고서를 엑셀로 내보내는 기능을 추가해줘')
    brief = ((reply or {}).get('hookSpecificOutput') or {}).get('additionalContext') or ''
    check('Korean: an unrelated request gets none (control)', 'CLOSED EARLIER HERE' not in brief, brief[-300:])

    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
