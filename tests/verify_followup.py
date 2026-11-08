#!/usr/bin/env python3
"""Model-free check of follow-up background: one line at a stop, a card on request, and asking never moves the task.

The release-blocking rule (PLAN_FOLLOWUP §6 ②): "팔로업 N", "이건 알아 N", "닫기" and mode words change neither the
pending question, nor the completion items, nor the review state, and start no work. Every rule has a control.
"""
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for name in ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH'):
    os.environ.pop(name, None)

for _marker in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID'):  # these runs stand for sessions a person opened,
    os.environ.pop(_marker, None)                                    # not ones an agent delegated (attach.delegated)
from harness import attach, followup  # noqa: E402
from harness.store import Store  # noqa: E402
from verify_living_checklist import check, FAILED, make_repo  # noqa: E402

CONCEPTS = [followup.parse_concept('로그인 시 자동 실행 | macOS가 로그인 때 프로그램을 켜 주는 방식 | Orbit 실행을 launchd에 맡김 | A는 매번 켤 필요가 없다'),
            followup.parse_concept('관리자 병합 | 보호 규칙을 건너뛰고 병합하는 권한 | PR #26 병합'),
            followup.parse_concept('원본과 색인 | SQLite가 원본, Qdrant는 찾기용 사본')]


def frozen(task):
    """What asking for background must never change."""
    data = task['data']
    return json.dumps({'status': task['status'], 'done': data.get('done'), 'done_rev': data.get('done_rev'),
                       'pending': data.get('requirements_pending'), 'vp': data.get('verification_pending'),
                       'question': (data.get('executor_result') or {}).get('question'),
                       'prompts': len(data.get('prompts') or []), 'feedback': len(data.get('feedback') or []),
                       'turns': data.get('turns'), 'resumes': len(data.get('resumes') or [])}, sort_keys=True, ensure_ascii=False)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-followup-'))
    store = Store(base / 'home')
    run(base, store)
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'all checks passed') + ' · ' + str(base))
    return 1 if FAILED else 0


def attached(store, base, name, session):
    root = make_repo(base, name)
    attach.set_switch(store, 'on', root)
    pay = {'cwd': str(root), 'session_id': session}
    attach.handle(store, 'session-start', dict(pay, source='startup'), 'claude')
    attach.handle(store, 'prompt', dict(pay, prompt='로그인할 때 Orbit이 자동으로 실행되도록 설정을 바꿔 주세요'), 'claude')
    task = attach.current_task(store, root)
    attach.set_done(store, task['id'], ['로그인 시 Orbit이 자동 실행된다'])
    return root, pay, task['id']


def stop(store, pay):
    return attach.handle(store, 'stop', dict(pay, last_assistant_message='어느 쪽으로 할까요?'), 'claude') or {}


def prompt(store, pay, text):
    return attach.handle(store, 'prompt', dict(pay, prompt=text), 'claude') or {}


def run(base, store):
    print('① a question stop shows one line, once')
    root, pay, tid = attached(store, base, 'repo-q', 's1')
    attach.claim(store, tid, 'A/B 중 선택 필요', question='A. 로그인 시 시작  B. 직접 실행 — 어느 쪽?', concepts=CONCEPTS)
    first = stop(store, pay)
    line = first.get('systemMessage') or ''
    check('the stop says 배경 · ① … with the way to ask', '배경 · ① 로그인 시 자동 실행' in line and '"팔로업 1"' in line, first)
    check('…one line, at most three concepts', line.count('\n') <= line.count('Orbit') and line.split('배경')[-1].count('④') == 0)
    check('control: the next stop of the same question repeats nothing', '배경' not in (stop(store, pay).get('systemMessage') or ''))

    print('② asking for background never moves the task (release-blocking)')
    before = frozen(store.get(tid))
    for text in ('팔로업', '팔로업 1', 'followup 2', '이건 알아 1', '팔로업 복원 1', '닫기', '팔로업 항상', '팔로업 자동'):
        reply = prompt(store, pay, text)
        after = frozen(store.get(tid))
        check('"%s" is answered by Orbit itself and blocked' % text, reply.get('decision') == 'block' and reply.get('reason'), reply)
        check('…and the task is exactly as it was' + (' ("%s")' % text), after == before, (before, after))
    card = prompt(store, pay, '팔로업 1')['reason']
    check('the card: name, 뜻, 이번 작업, 영향, and the question still waits',
          card.startswith('① 로그인 시 자동 실행') and '뜻: macOS' in card and '이번 작업: Orbit' in card and '영향: A는' in card
          and '기다리는 질문은 그대로' in card, card)
    check('a card is short (at most 7 lines with its controls)', len(card.splitlines()) <= 7, card)
    two = prompt(store, pay, '팔로업 2')['reason']
    check('a concept without where/impact leaves those lines out', '이번 작업: PR #26' in two and '영향' not in two, two)
    codex = attach.handle(store, 'prompt', dict(pay, prompt='팔로업 1'), 'codex') or {}
    check('the same in a Codex session (same hook reply)', codex.get('decision') == 'block'
          and codex.get('reason', '').startswith('①') and frozen(store.get(tid)) == before, codex)
    answer = prompt(store, pay, 'A로 해 주세요')
    check('control: a real answer goes on as the user\'s words and resumes the work',
          store.get(tid)['status'] == 'working' and answer.get('decision') != 'block', store.get(tid)['status'])

    print('③ modes and hidden concepts')
    root, pay, tid = attached(store, base, 'repo-modes', 's2')
    attach.claim(store, tid, '선택 필요', question='어느 쪽?', concepts=CONCEPTS)
    followup.set_mode(store, 'off')
    check('off: nothing by itself', '배경' not in (stop(store, pay).get('systemMessage') or ''))
    check('…but asking still works in off', prompt(store, pay, '팔로업 1').get('reason', '').startswith('①'))
    followup.set_mode(store, 'auto')
    attach.claim(store, tid, '선택 필요', question='다시: 어느 쪽?', concepts=CONCEPTS)
    prompt(store, pay, '이건 알아 4')        # card 4 = 로그인 시 자동 실행 of this stop
    line = stop(store, pay).get('systemMessage') or ''
    check('auto: a hidden concept is left out of the line', '배경' in line and '로그인 시 자동 실행' not in line and '관리자 병합' in line, line)
    followup.set_mode(store, 'always', store.get(tid))
    attach.claim(store, tid, '선택 필요', question='한 번 더: 어느 쪽?', concepts=CONCEPTS)
    line = stop(store, pay).get('systemMessage') or ''
    check('always (this task only): hidden concepts show too', '로그인 시 자동 실행' in line, line)
    check('…the task setting wins over the default, which stays auto',
          followup.mode(store) == 'auto' and followup.mode(store, store.get(tid)) == 'always')
    prompt(store, pay, '팔로업 복원 로그인 시 자동 실행')
    check('restore by name shows it again', '로그인 시 자동 실행' not in followup.hidden(store))

    print('④ completion: shown once the review verifies, not at the claim')
    root, pay, tid = attached(store, base, 'repo-done', 's3')
    attach.claim(store, tid, '설정 완료', concepts=CONCEPTS[:1])
    check('control: while it is only claimed (checking), no line', '배경' not in (stop(store, pay).get('systemMessage') or ''))
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['verification_pending'] = False
        store.save(task, 'verified', 'test_verified', {})
    line = stop(store, pay).get('systemMessage') or ''
    check('verified: the line shows at the next stop', '배경 · ① 로그인 시 자동 실행' in line, line)

    print('⑤ nothing changes when there is no background')
    root, pay, tid = attached(store, base, 'repo-none', 's4')
    attach.claim(store, tid, '선택 필요', question='어느 쪽?')
    check('a claim without concepts: no line', '배경' not in (stop(store, pay).get('systemMessage') or ''))
    before = frozen(store.get(tid))
    reply = prompt(store, pay, '닫기')
    check('control: a bare "닫기" with no background shown is the user\'s own words',
          reply.get('decision') != 'block' and store.get(tid)['status'] == 'working', (reply, before))
    for bad in ('이름만', ' | 설명', 'a|b|c|d|e'):
        try:
            followup.parse_concept(bad)
            ok = False
        except ValueError:
            ok = True
        check('a malformed concept is refused: %r' % bad, ok)

    print('⑦ Astra review regressions (REPORT_F2)')
    root, pay, tid = attached(store, base, 'repo-r1', 's6')
    followup.set_mode(store, 'off')
    attach.claim(store, tid, '선택 필요', question='어느 쪽?', concepts=CONCEPTS[:1])
    stop(store, pay)
    before = frozen(store.get(tid))
    prompt(store, pay, '팔로업 1')
    closed_reply = prompt(store, pay, '닫기')
    check('off → a card asked for by hand → 닫기 is ours, and the task does not resume',
          closed_reply.get('decision') == 'block' and frozen(store.get(tid)) == before, (closed_reply, store.get(tid)['status']))
    again = prompt(store, pay, '닫기')
    check('control: a second 닫기 (nothing open) is the user\'s own words', again.get('decision') != 'block')
    followup.set_mode(store, 'auto')

    root, pay, tid = attached(store, base, 'repo-r3', 's7')
    attach.claim(store, tid, '선택 필요', question='어느 쪽?', concepts=CONCEPTS[:1])
    stop(store, pay)
    prompt(store, pay, 'B로 해 주세요')
    later = prompt(store, pay, 'close')
    check('after the user answered, a later "close" is not swallowed', later.get('decision') != 'block')

    root, pay, tid = attached(store, base, 'repo-r4', 's8')
    attach.claim(store, tid, '질문 A', question='A 질문?', concepts=CONCEPTS[:2])
    attach.claim(store, tid, '질문 B', question='B 질문?')
    check('a new question without concepts shows none of the last one\'s', '배경' not in (stop(store, pay).get('systemMessage') or ''))
    listed = prompt(store, pay, '팔로업').get('reason', '')
    check('…and the list is this stop\'s (empty), not A\'s', '관리자 병합' not in listed and '배경 카드가 없' in listed, listed)

    with store.lock('task:' + tid):
        task = store.get(tid)
        for i in range(63):
            followup.record(task['data'], [followup.parse_concept('c%d | plain %d' % (i, i))], 'question', 'q')
        store.save(task, task['status'], 'test_setup', {})
    numbers = [c['n'] for c in store.get(tid)['data']['followup']['cards']]
    check('card numbers only grow (63 records, 60 kept, all distinct)', len(numbers) == len(set(numbers)) == 60
          and numbers[-1] == max(numbers), numbers[-5:])

    messy = followup.parse_concept('긴\n이름 | 첫 문장.\n둘째   문장. | /Users/sean/Development/projects/cosmos-orbit/harness/followup.py:120 | ' + '가' * 400)
    check('fields become one line, at most 160 characters', '\n' not in json.dumps(messy, ensure_ascii=False).replace('\\n', '')
          and all('\n' not in v for v in messy.values()) and len(messy['impact']) <= 160, messy)
    root, pay, tid = attached(store, base, 'repo-r6', 's9')
    attach.claim(store, tid, '선택', question='어느 쪽?', concepts=[messy])
    shown = prompt(store, pay, '팔로업 %d' % store.get(tid)['data']['followup']['cards'][-1]['n']).get('reason', '')
    check('a long path is shortened in the card, with the way to see it whole', '…/followup.py:120' in shown and '근거' in shown
          and '/Users/sean' not in shown, shown)
    whole = prompt(store, pay, '팔로업 %d 근거' % store.get(tid)['data']['followup']['cards'][-1]['n']).get('reason', '')
    check('…"팔로업 N 근거" shows the whole path', '/Users/sean/Development/projects/cosmos-orbit/harness/followup.py:120' in whole, whole)
    deep = '/Users/sean/Development/projects/' + 'very-long-folder-name/' * 8 + 'target_file.py:207'
    kept = followup.parse_concept('긴 근거 | 뜻 한 문장. 그리고 덧붙임. 또 덧붙임. | %s | 영향 하나.' % deep)
    check('the source keeps the whole reference (over 160 characters)', kept['source'].endswith('target_file.py:207')
          and len(kept['source']) > 160, len(kept['source']))
    check('each card field is one sentence', kept['plain'] == '뜻 한 문장. …' and kept['impact'] == '영향 하나.', kept)
    attach.claim(store, tid, '선택', question='다른 질문?', concepts=[kept])
    n_kept = store.get(tid)['data']['followup']['cards'][-1]['n']
    whole = prompt(store, pay, '팔로업 %d 근거' % n_kept).get('reason', '')
    check('…and "근거" shows it whole', deep in whole, whole[:200])
    sentences = prompt(store, pay, '팔로업 %d' % n_kept).get('reason', '').split('\n')[1:4]
    check('the card body has at most three sentences: one per line, the long path shortened',
          len(sentences) <= 3 and not any(re.search(r'[.!?]\s+\S', l.replace(' …', '')) for l in sentences)
          and '…/target_file.py:207' in sentences[1], sentences)
    earlier = prompt(store, pay, '팔로업 %d' % (n_kept - 1)).get('reason', '')
    check('an earlier stop\'s card says which stop it belongs to', '(앞선 멈춤: 어느 쪽?)' in earlier, earlier)
    current = prompt(store, pay, '팔로업 %d' % n_kept).get('reason', '')
    check('control: the current stop\'s card does not', '앞선 멈춤' not in current, current)
    mode_reply = prompt(store, pay, '팔로업 항상').get('reason', '')
    check('the "this task only" hint is a command that really sets this task only', '--here --task ' + tid in mode_reply, mode_reply)
    followup.set_mode(store, 'auto')

    print('⑧ a review that ends after the last stop carries the line in its notice')
    root, pay, tid = attached(store, base, 'repo-async', 's10')
    attach.claim(store, tid, '설정 완료', concepts=CONCEPTS[:1])
    stop(store, pay)                                        # the turn ends while the review still runs
    with store.lock('task:' + tid):
        task = store.get(tid)
        task['data']['verification_pending'] = False
        store.save(task, 'verified', 'test_verified', {})
    from harness import supervision, wake
    sent = []
    original = (wake.resolve, wake.notify_command)
    wake.resolve = lambda store, session=None, **k: ({'kind': 'tmux', 'target': '%1'}, 'test')
    wake.notify_command = lambda terminal, text: ['notify', text]
    try:
        told = supervision.tell(store, store.get(tid), 's10', runner=lambda argv: (sent.append(argv[-1]) or 0, ''))
    finally:
        wake.resolve, wake.notify_command = original
    check('the verified notice says 배경 · ① …', told and sent and '배경 · ① 로그인 시 자동 실행' in sent[-1], sent)
    check('…and the next stop does not repeat it', '배경' not in (stop(store, pay).get('systemMessage') or ''))

    print('⑥ the CLI')
    root, pay, tid = attached(store, base, 'repo-cli', 's5')
    cli = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit'), '--home', str(store.home)]
    ran = subprocess.run(cli + ['claim', tid, '--summary', '선택 필요', '--question', '어느 쪽?',
                                '--concept', '로그인 시 자동 실행 | 로그인 때 켜 주는 방식'], capture_output=True, text=True)
    check('claim --concept records the card (exit 2 = waiting for the user, by convention)', ran.returncode == 2 and (store.get(tid)['data'].get('followup') or {}).get('cards'),
          ran.stderr[-300:])
    shown = subprocess.run(cli + ['followup', '1', '--task', tid], capture_output=True, text=True)
    check('followup N prints the card', shown.returncode == 0 and shown.stdout.startswith('① 로그인 시 자동 실행'), shown.stdout)
    bad = subprocess.run(cli + ['claim', tid, '--summary', 'x', '--concept', '이름만'], capture_output=True, text=True)
    check('control: a malformed --concept refuses the claim', bad.returncode != 0 and 'plain words' in bad.stderr)


if __name__ == '__main__':
    sys.exit(main())
