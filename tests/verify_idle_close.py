#!/usr/bin/env python3
"""Sessions nobody used for days close by themselves, in order, and say whether their work was finished (2026-11-24
user request, D22). Model-free, with controls. Exit 0 = pass; on the main before it every check fails, it does not crash.

① settings: on, 3 days by default; a duration, off and reset change it; a word that is none of them is refused
② a session with no prompt for 4 days at an empty input closes: handoff → memory → /exit, the task closes stopped
   with its open items counted; a verified one closes complete
③ left alone: 2 days (control), kept by the user, a turn on screen, text typed and not sent, a permission question
   (not an empty input), a review running, and everything while the setting is off
④ another live session on the same task: only the idle one ends, the task stays open
⑤ the preview closes nothing; the background round (patrol) runs the cleanup, at most every 10 minutes
⑥ the session list shows in progress / stopped (open items) / complete; picking a stopped one takes it back by handoff
"""
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH', 'CLAUDE_CODE_SESSION_ID', 'CLAUDECODE',
             'CODEX_THREAD_ID', 'TMUX', 'TMUX_PANE', 'CMUX_SURFACE_ID', 'CMUX_PANEL_ID', 'CMUX_WORKSPACE_ID'):
    os.environ.pop(name, None)
from harness import attach, patrol, restore, wake  # noqa: E402
from harness.store import Store  # noqa: E402
try:
    from harness import idle_close  # noqa: E402
except ImportError:
    idle_close = None

FAILED = []
RULE = '─' * 60
EMPTY = 'done.\n' + RULE + '\n❯ \n' + RULE + '\n  ? for shortcuts'
SCREENS = {'%1': EMPTY, '%2': EMPTY, '%3': '✻ Working… (12s · esc to interrupt)\n' + EMPTY,
           '%4': 'done.\n' + RULE + '\n❯ 아직 보내지 않은 글\n' + RULE, '%5': 'Do you want to proceed?\n❯ 1. Yes\n  2. No',
           '%6': EMPTY, '%7': EMPTY, '%8': EMPTY, '%9': EMPTY, '%10': EMPTY, '%11': EMPTY}
TYPED = []


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


def runner(argv):
    if 'capture-pane' in argv:
        return 0, SCREENS.get(argv[-1], '')
    return 0, ''


def spawn(terminal, steps, log, home=None, task=None, report=None):
    TYPED.append((terminal['target'], steps))
    return 1


def make_repo(base, name):
    root = base / name
    root.mkdir()
    for args in (['init', '-q'], ['commit', '-q', '--allow-empty', '-m', 'init']):
        subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), *args], check=True)
    return root


def session_on(store, base, name, session, pane, prompt, items=(), days=4.0, verified=False):
    """A live session in a tmux pane, its task, its last prompt `days` ago."""
    root = make_repo(base, name)
    attach.set_switch(store, 'on', root)
    attach.handle(store, 'session-start', {'cwd': str(root), 'session_id': session, 'source': 'startup'}, 'claude')
    prompt += ' (화면과 코드 모두 반영해 줘)'             # a task starts from a request of 20 characters or more
    attach.handle(store, 'prompt', {'cwd': str(root), 'session_id': session, 'prompt': prompt}, 'claude')
    tid = attach.resolve_task(store, root, session)['id']
    if items:
        attach.set_done(store, tid, list(items))
    age(store, tid, session, days, pane, verified)
    return root, tid


def age(store, tid, session, days, pane, verified=False):
    stamp = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    with store.lock('task:' + tid):
        task = store.get(tid)
        for p in task['data'].get('prompts') or []:
            if p.get('session') == session:
                p['at'] = stamp
        store.save(task, 'verified' if verified else task['status'], 'aged', {})
    wake.note(store, session, 'claude', 'stop', {'kind': 'tmux', 'cli': '/usr/bin/true', 'target': pane})


def run_once(store, at=None):
    return idle_close.sweep(store, runner=runner, at=at, force=True, spawn=spawn)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-idle-close-'))
    store = Store(base / 'home')
    try:
        run(base, store)
    except Exception as error:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAILED.append('crashed: ' + repr(error)[:200])
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'all checks passed') + ' · ' + str(base))
    return 1 if FAILED else 0


def run(base, store):
    print('① settings')
    view = idle_close.configure(store)
    check('built in: on, 3 days', view['on'] and view['days'] == 3 and view['source'] == 'built-in', view)
    check('a duration changes it ("5d", "12h")', idle_close.configure(store, '5d')['days'] == 5
          and idle_close.configure(store, '12h')['days'] == 0.5)
    check('off turns it off, reset returns to the default', idle_close.configure(store, 'off')['on'] is False
          and idle_close.configure(store, 'reset') == dict(view, changed=True))
    try:
        idle_close.configure(store, 'soon')
        refused = False
    except ValueError:
        refused = True
    check('control: a word that is not a duration is refused', refused)
    saved = json.loads((store.home / 'settings.json').read_text())
    check('stored in <home>/settings.json', 'idle_close' in saved, saved)

    print('② idle for 4 days at an empty input: closed in order')
    _, stopped = session_on(store, base, 'r1', 'S1', '%1', '결제 페이지에 쿠폰 입력칸을 추가해 줘',
                            ['D1 쿠폰 입력칸이 보인다', 'D2 쿠폰이 금액에 반영된다'])
    _, done = session_on(store, base, 'r2', 'S2', '%2', '헤더 로고를 새 SVG 로 바꿔 줘', ['D1 새 로고'], verified=True)
    _, recent = session_on(store, base, 'r6', 'S6', '%6', '푸터에 회사 주소를 넣어 줘', ['D1 주소'], days=2)
    _, kept = session_on(store, base, 'r7', 'S7', '%7', '검색창에 자동완성을 붙여 줘', ['D1 자동완성'])
    idle_close.keep(store, 'S7')
    _, busy = session_on(store, base, 'r3', 'S3', '%3', '로그인 화면 문구를 다듬어 줘', ['D1 문구'])
    _, typing = session_on(store, base, 'r4', 'S4', '%4', '알림 설정 화면을 만들어 줘', ['D1 화면'])
    _, asking = session_on(store, base, 'r5', 'S5', '%5', '프로필 사진 업로드를 고쳐 줘', ['D1 업로드'])
    _, reviewing = session_on(store, base, 'r8', 'S8', '%8', '주문 내역 정렬을 바꿔 줘', ['D1 정렬'])
    with store.lock('task:' + reviewing):
        t = store.get(reviewing)
        t['data']['verification_pending'] = True
        store.save(t, t['status'], 'claimed', {})
    preview = idle_close.sweep(store, runner=runner, apply=False)
    check('the preview names the two to close and closes nothing',
          sorted(d['session'] for d in preview if d['action'] == 'close') == ['S1', 'S2'] and not TYPED
          and store.get(stopped)['status'] != 'closed', [(d['session'], d['action'], d['why']) for d in preview])
    order = []
    import harness.continuity as continuity
    original = continuity.record_checkpoint
    with patch('harness.continuity.record_checkpoint', lambda *a, **k: (order.append('handoff'), original(*a, **k))[1]), \
            patch('harness.memory.enqueue_checkpoint', lambda s, t: order.append('memory')), \
            patch('harness.memory.flush', lambda s, t, lease_fds=(): [{'id': 'x', 'stored': True}]):
        with store.lock('task:' + stopped):
            t = store.get(stopped)
            t['data']['memory'] = {'mode': 'brain', 'remember': True, 'outbox': [], 'project_id': 'p'}
            store.save(t, t['status'], 'memory_configured', {})
        decisions = run_once(store)
    why = {d['session']: d['why'] for d in decisions}
    closed_s1, closed_s2 = store.get(stopped), store.get(done)
    rec1, rec2 = closed_s1['data'].get('closed') or {}, closed_s2['data'].get('closed') or {}
    check('the unfinished one closes stopped, with 2 open items', closed_s1['status'] == 'closed'
          and rec1.get('state') == 'stopped' and rec1.get('remaining') == 2 and rec1.get('reason') == 'idle_close', rec1)
    check('the verified one closes complete', closed_s2['status'] == 'closed' and rec2.get('state') == 'completed', rec2)
    check('in order: handoff, memory, then /exit', order[:2] == ['handoff', 'memory'] and TYPED and TYPED[0][0] == '%1', (order, TYPED[:1]))
    steps = dict((t, s) for t, s in TYPED)
    check('/exit is typed into exactly the two panes, nothing is emptied first (no blocked line there)',
          sorted(steps) == ['%1', '%2'] and all(('text', '/exit') in s and s[-1] == ('key', 'enter')
                                                 and not any(x[0] == 'clear' for x in s) for s in steps.values()), TYPED)
    check('a checkpoint says what is left', 'open items' in ((closed_s1['data'].get('checkpoints') or [{}])[-1].get('next') or ''))
    check('both sessions are ended', all((store.session_task(s) or {}).get('ended') for s in ('S1', 'S2')))

    print('③ left alone')
    for session, expected, label in (('S6', 'recent', 'no prompt for 2 days only (control)'), ('S7', 'kept', 'kept by the user'),
                                     ('S3', 'turn', 'a turn on its screen'), ('S4', 'unsent_input', 'text typed and not sent'),
                                     ('S5', 'not_at_input', 'a permission question on screen'), ('S8', 'reviewing', 'a review running')):
        check('%s: left (%s)' % (label, expected), why.get(session) == expected and not (store.session_task(session) or {}).get('ended'),
              why.get(session))
    idle_close.configure(store, 'off')
    _, off_task = session_on(store, base, 'r9', 'S9', '%9', '장바구니 수량 버튼을 고쳐 줘', ['D1 수량'])
    check('off: nothing is closed', run_once(store) == [] and store.get(off_task)['status'] != 'closed')
    idle_close.configure(store, 'on')

    print('④ another live session on the same task')
    TYPED.clear()
    root_s, shared = session_on(store, base, 'r10', 'S10', '%10', '대시보드 차트 색을 바꿔 줘', ['D1 차트 색'])
    store.bind_session('S11', shared, str(root_s))      # a second terminal working on the same task
    attach.handle(store, 'prompt', {'cwd': str(root_s), 'session_id': 'S11', 'prompt': '차트 범례 위치도 위로 옮겨 줘 (같은 작업)'}, 'claude')
    wake.note(store, 'S11', 'claude', 'stop', {'kind': 'tmux', 'cli': '/usr/bin/true', 'target': '%11'})
    bound = (store.session_task('S11') or {}).get('task_id')
    decisions = run_once(store)
    check('control: both sessions are on the task', bound == shared, bound)
    check('only the idle session ends; the task stays open', (store.session_task('S10') or {}).get('ended')
          and not (store.session_task('S11') or {}).get('ended') and store.get(shared)['status'] != 'closed',
          [(d['session'], d['action'], d['why']) for d in decisions if d['task'] == shared])

    print('⑤ the background round')
    _, by_round = session_on(store, base, 'r12', 'S12', '%9', '이미지 지연 로딩을 붙여 줘', ['D1 지연 로딩'])
    with store.db:
        store.db.execute("DELETE FROM preferences WHERE key=?", (idle_close.SWEEP_KEY,))
    with patch('harness.typist.spawn', spawn):
        first = patrol.sweep(store, runner=runner, desktop=lambda body: None)
        again_task = session_on(store, base, 'r13', 'S13', '%6', '404 페이지를 꾸며 줘', ['D1 404'])[1]
        second = patrol.sweep(store, runner=runner, desktop=lambda body: None)
    check('patrol closes the idle session', ('%s' % by_round, 'S12', 'idle_closed_stopped') in [(a, b, c) for a, b, c in first], first)
    check('…at most every 10 minutes (the next round leaves the new one)', store.get(again_task)['status'] != 'closed'
          and not any(c.startswith('idle_closed') for _, _, c in second), second)

    print('⑥ the session list: in progress / stopped / complete')
    for gone in ('S1', 'S2', 'S10', 'S12'):              # the typed /exit ends them: their SessionEnd hook
        wake.note(store, gone, 'claude', 'session-end', None, reason='prompt_input_exit')
    root_l = make_repo(base, 'r-list')
    attach.set_switch(store, 'on', root_l)
    picks = []
    reply = restore.on_prompt(store, {'session_id': 'L1', 'prompt': '세션 목록 보여줘', 'cwd': str(root_l)}, 'claude',
                              terminal={'kind': 'tmux', 'cli': '/usr/bin/true', 'target': '%20'}, run=lambda a: (picks.append(a), (0, ''))[1],
                              running=(set(), set()))
    text = (reply or {}).get('reason', '')
    check('three groups: 진행 중, 멈춤 with its open items, 완결', all(w in text for w in ('진행 중', '멈춤', '완결', '남은 일: 2')), text[:900])
    numbered = [line for line in text.splitlines() if line[:4].strip().isdigit()]
    state = json.loads(store.preference(restore.OFFER_KEY + 'L1'))
    pick = next(i for i, r in enumerate(state['rows']) if r.get('task') == stopped and r.get('state') == 'stopped') + 1
    answer = restore.on_prompt(store, {'session_id': 'L1', 'prompt': str(pick), 'cwd': str(root_l)}, 'claude',
                               terminal={'kind': 'tmux', 'cli': '/usr/bin/true', 'target': '%20'}, run=lambda a: (picks.append(a), (0, ''))[1])
    back = store.get(stopped)
    check('picking the stopped one takes it back for a handoff', back['status'] == 'working'
          and (back['data'].get('handoff_state') or {}).get('state') == 'told', (back['status'], back['data'].get('handoff_state')))
    check('…and opens a new window in its folder with the carry line', any('new-window' in a and any('carried over' in x for x in a)
                                                                           for a in picks), picks[-1:])
    check('…the user is told in their language', '핸드오프' in (answer or {}).get('reason', ''), answer)
    check('control: numbers were given to the rows', len(numbered) >= 3, numbered)


if __name__ == '__main__':
    sys.exit(main())
