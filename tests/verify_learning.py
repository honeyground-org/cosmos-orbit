#!/usr/bin/env python3
"""Orbit decides only the clear cases; the executor decides the rest, and that decision teaches this installation.
Model-free, with controls.

  ① a question with no open task starts nothing (control: before, any prompt of 20+ characters became a task)
  ② an 'unsure' prompt opens a draft: the executor confirms it with `done`, or it closes by itself at the end of
     the turn when nothing changed; files changed without a definition → one request, never two
  ③ learning: after the executor's decisions, the same ending is classified on its own — in this store only
     (control: a second installation, with no labels, still asks), and no learned phrase enters the code
  ④ inside an open task an 'unsure' prompt is a possible change: gated at claim, no Stop block
     (control: a clear request blocks once); `done --keep` records that it was not a change
  ⑤ a session keeps its open task when its shell moves to another repository (control for the defect that filed
     two prompts into another task); after the task closes, new work starts where the session now is
  ⑥ a new task carries the user's earlier prompts from the host transcript, shown in the brief and given to the
     reviewer
"""
import hashlib
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

from harness import attach, requirements  # noqa: E402
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


def say(store, root, session, text, **extra):
    return attach.handle(store, 'prompt', dict({'cwd': str(root), 'session_id': session, 'prompt': text}, **extra), 'claude')


def stop(store, root, session):
    return attach.handle(store, 'stop', {'cwd': str(root), 'session_id': session, 'stop_hook_active': False,
                                         'last_assistant_message': 'Here is the answer.'}, 'claude')


def package_digest():
    digest = hashlib.sha256()
    for path in sorted((ROOT / 'harness').rglob('*.py')):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-verify-learning-'))
    store = Store(base / 'home')
    code_before = package_digest()

    print('① a question with no open task starts nothing')
    q_root = make_repo(base, 'repo-question')
    attach.set_switch(store, 'on', q_root)
    question = '잘 수정 업데이트 된 것인지 확인해 볼 수 있는 상태인지?'
    say(store, q_root, 'Q', question)
    check('a status question (20+ characters) creates no task', attach.resolve_task(store, q_root, 'Q') is None)
    check('control: the old rule (any 20+ character prompt) would have created one', len(question) >= attach.MIN_TASK_PROMPT)
    say(store, q_root, 'Q', 'Did you update the README with the new commands?')
    check('an English status question creates no task either', attach.resolve_task(store, q_root, 'Q') is None)

    print('② an unsure prompt opens a draft the executor confirms, or that closes by itself')
    d_root = make_repo(base, 'repo-draft')
    attach.set_switch(store, 'on', d_root)
    unsure = 'README 의 설치 안내 부분 업데이트?'
    check('the prompt is unsure', requirements.classify(unsure) == 'unsure', requirements.classify(unsure))
    brief = say(store, d_root, 'D1', unsure)
    draft = attach.resolve_task(store, d_root, 'D1')
    check('a draft task is opened and the brief says how to confirm it',
          draft and draft['data'].get('draft') and 'DRAFT:' in brief['hookSpecificOutput']['additionalContext'])
    check('the turn ends with nothing changed and no definition → no block', stop(store, d_root, 'D1') is None)
    closed = store.get(draft['id'])
    check('…and the draft closed itself as not a request', closed['status'] == 'stalled'
          and closed['data'].get('reason_code') == 'not_a_request', (closed['status'], closed['data'].get('reason_code')))
    say(store, d_root, 'D2', 'API 문서 예시 코드 부분 업데이트?')
    confirm = attach.resolve_task(store, d_root, 'D2')
    attach.set_done(store, confirm['id'], ['D1 the API examples show the new parameters'])
    confirm = store.get(confirm['id'])
    check('the executor confirms a draft by defining the completion', not confirm['data'].get('draft') and confirm['status'] == 'working')
    check('…and the confirmed task continues at the end of the turn', (stop(store, d_root, 'D2') or {}).get('decision') == 'block'
          and store.get(confirm['id'])['status'] == 'working')
    c_root = make_repo(base, 'repo-changed')
    attach.set_switch(store, 'on', c_root)
    say(store, c_root, 'D3', 'CHANGELOG 의 이번 릴리스 항목 추가?')
    changed = attach.resolve_task(store, c_root, 'D3')
    (c_root / 'CHANGELOG.md').write_text('# 1.2\n', encoding='utf-8')
    first = stop(store, c_root, 'D3')
    second = stop(store, c_root, 'D3')
    check('files changed without a definition → one request to define it', first and first.get('decision') == 'block'
          and 'done ' + changed['id'] in first['reason'], first)
    check('…never two (control for a loop)', second is None and store.get(changed['id'])['status'] == 'working')

    print('③ the executor\'s decisions teach this installation, and only this one')
    l_root = make_repo(base, 'repo-learn')
    attach.set_switch(store, 'on', l_root)
    ending = '배포노트갱신?'
    for i, prefix in enumerate(('서버 설정 문서 업데이트', '모바일 가이드 문서 업데이트')):
        text = prefix + ' ' + ending
        check('before learning: %r is unsure' % text, requirements.classify(text, attach.learned(store)) == 'unsure'
              if i == 0 else True)
        say(store, l_root, 'L%d' % i, text)
        stop(store, l_root, 'L%d' % i)                     # the executor only answered: the draft closes as a question
    table = attach.learned(store)
    check('two decisions are recorded for that ending', (table.get(requirements.ending_key('x ' + ending)) or {}).get('no_change') == 2, table)
    later = '결제 모듈 안내 문서 업데이트 ' + ending
    check('after learning: the same ending is a question on this installation',
          requirements.classify(later, attach.learned(store)) == 'question')
    say(store, l_root, 'L9', later)
    check('…so it starts nothing', attach.resolve_task(store, l_root, 'L9') is None)
    other = Store(base / 'another-installation')
    check('control: another installation has learned nothing and still asks', requirements.classify(later, attach.learned(other)) == 'unsure')
    check('the learned endings live in the local store, not in the code', package_digest() == code_before
          and ending not in ''.join(p.read_text(encoding='utf-8') for p in (ROOT / 'harness').rglob('*.py')))
    events = [e['kind'] for e in store.events(confirm['id'])]
    check('each decision is in the ledger (prompt_labeled)', 'prompt_labeled' in events, events[-5:])

    print('④ inside an open task an unsure prompt is a possible change: gated at claim, no Stop block')
    t_root = make_repo(base, 'repo-task')
    attach.set_switch(store, 'on', t_root)
    say(store, t_root, 'T', 'Add CSV export to the monthly report tool please')
    task = attach.resolve_task(store, t_root, 'T')
    attach.set_done(store, task['id'], ['D1 CSV export works'])
    brief = say(store, t_root, 'T', '엑셀 내보내기 부분 반영?')
    task = store.get(task['id'])
    check('recorded as a possible change', (task['data'].get('requirements_pending') or {}).get('unsure') is True
          and 'POSSIBLE CHANGE' in brief['hookSpecificOutput']['additionalContext'])
    check('no Stop block for a possible change', stop(store, t_root, 'T') is None)
    try:
        attach.claim(store, task['id'], summary='done')
        gated = False
    except ValueError:
        gated = True
    check('…but the claim waits for the executor\'s decision', gated)
    attach.set_done(store, task['id'], [], keep=True)
    labels = [json.loads(e['payload']) if isinstance(e.get('payload'), str) else e.get('payload') for e in store.events(task['id']) if e['kind'] == 'prompt_labeled']
    check('done --keep records that it was not a change', labels and labels[-1].get('label') == 'no_change', labels)
    say(store, t_root, 'T', 'Also add a JSON export next to the CSV one')
    block = stop(store, t_root, 'T')
    check('control: a clear request still asks once at Stop', block and block.get('decision') == 'block')

    print('⑤ a session keeps its open task when its shell moves')
    a_root, b_root = make_repo(base, 'repo-a'), make_repo(base, 'repo-b')
    attach.set_switch(store, 'on', a_root)
    attach.set_switch(store, 'on', b_root)
    say(store, a_root, 'S', 'Build the invoice exporter with tests please')
    mine = attach.resolve_task(store, a_root, 'S')
    say(store, b_root, 'X', 'Write the onboarding guide for new staff please')          # another session's task in repo-b
    theirs = attach.resolve_task(store, b_root, 'X')
    say(store, b_root, 'S', 'Also handle negative amounts in the exporter')             # S's shell is now in repo-b
    check("the prompt from the moved shell lands in the session's own task", attach.resolve_task(store, b_root, 'S')['id'] == mine['id']
          and any('negative amounts' in f['text'] for f in store.get(mine['id'])['data'].get('feedback') or []))
    check("…and never in the other task of that repository (control for the defect)",
          all('negative amounts' not in f['text'] for f in store.get(theirs['id'])['data'].get('feedback') or []))
    attach.detach(store, task_id=mine['id'])
    say(store, b_root, 'S', 'Now write the release checklist for repo b please')
    fresh = attach.resolve_task(store, b_root, 'S')
    check('after its task closes, new work starts where the session now is', fresh and fresh['id'] != mine['id']
          and Path(fresh['workspace']).resolve() == b_root.resolve())

    print('⑥ a new task carries the earlier prompts of the session')
    x_root = make_repo(base, 'repo-context')
    attach.set_switch(store, 'on', x_root)
    transcript = base / 'session.jsonl'
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': 'Why does the nightly export fail on empty months?'}},
            {'type': 'assistant', 'message': {'role': 'assistant', 'content': []}},
            {'type': 'user', 'message': {'role': 'user', 'content': '<task-notification>x</task-notification>'}},
            {'type': 'user', 'message': {'role': 'user', 'content': 'Fix it so empty months export a header row only'}}]
    transcript.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    brief = say(store, x_root, 'C', 'Fix it so empty months export a header row only', transcript_path=str(transcript))
    task = attach.resolve_task(store, x_root, 'C')
    check('the earlier prompt is kept as context (host notifications and the prompt itself are not)',
          task['data'].get('context_prompts') == ['Why does the nightly export fail on empty months?'], task['data'].get('context_prompts'))
    check('the brief shows it', 'CONTEXT' in brief['hookSpecificOutput']['additionalContext'])
    source = (ROOT / 'harness' / 'verifier.py').read_text(encoding='utf-8')
    check('the reviewer receives it in the binding', "'context': data.get('context_prompts'" in source)

    store.close()
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
