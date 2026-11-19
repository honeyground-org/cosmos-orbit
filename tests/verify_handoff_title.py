#!/usr/bin/env python3
"""Model-free check that a session's name follows it through a handoff (harness/title.py).

Why (measured 2026-11-19): a handoff starts a new Claude Code session whose first prompt is the line Orbit submits, and
Claude Code names a session after its first prompt. 11 of the 14 latest sessions of one folder were called
'Orbit 이전 요청 이어서', 'Orbit 이어서 진행', '이어서 진행' on their terminal tabs, whatever the pane was called before.

  ① what a transcript says about its session's name (the user's title, the generated one, how the session began)
  ② through a handoff: the hook of the carry line answers with the leaving session's name (the real command line too)
  ③ whose name: the user's own first, a carried one again at the next handoff, never one made from Orbit's line
  ④ what stays: any other prompt, a session named already, Codex, the line in the wrong session, and the off switch

Nothing types into a terminal, starts a model or touches the live Orbit home or host settings (the helpers of
verify_handoff_ceiling.py: temporary store and host folders, a fake terminal and typist).
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from verify_handoff_ceiling import (Env, Faked, FAILED, check, end_turn, kinds, say, start, told_at,  # noqa: E402
                                    attach, continuity, typist)
from harness.store import Store  # noqa: E402
try:
    from harness import title
except ImportError:              # a checkout from before this change: every check below fails, one by one
    title = None

os.environ.pop('ORBIT_HANDOFF_TITLE', None)
REQUEST = 'Now add a command line entry point for the module, with tests'
ORBIT_MADE = 'Orbit 이전 요청 이어서'      # what Claude Code generates from the carry line


def rows(path, *entries):
    """Append transcript rows: ('user', text) · ('custom', title) · ('ai', title)."""
    with open(path, 'a') as stream:
        for kind, text in entries:
            row = {'user': {'type': 'user', 'message': {'role': 'user', 'content': text}},
                   'custom': {'type': 'custom-title', 'customTitle': text, 'sessionId': 's'},
                   'ai': {'type': 'ai-title', 'aiTitle': text, 'sessionId': 's'}}[kind]
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def spec(reply):
    return (reply or {}).get('hookSpecificOutput') or {}


def hand_over(store, pay, fake):
    """Auto mode: the idle session's next request is blocked there and handed over. Returns the reply."""
    continuity.set_handoff_mode(store, 'auto')
    try:
        count = len(fake.typed)
        reply = attach.handle(store, 'prompt', dict(pay, prompt=REQUEST), 'claude')
        return reply if len(fake.typed) == count + 1 else None
    finally:
        continuity.set_handoff_mode(store, 'tell')


def fresh(store, base, root, name, *entries, host='claude', line=None):
    """The session after /clear takes the task over; then the carry line arrives. Returns (payload, the line's reply)."""
    path = base / (name + '.jsonl')
    rows(path, ('user', typist.CARRY_LINE), *entries)
    pay = {'cwd': str(root), 'session_id': name, 'transcript_path': str(path)}
    attach.handle(store, 'session-start', dict(pay, source='clear'), host)
    return pay, attach.handle(store, 'prompt', dict(pay, prompt=typist.CARRY_LINE if line is None else line), host)


def tell(store, tid, pay, path, tokens):
    """A fresh session answers the carry line (40k), grows past its handoff point and is told (tell mode): idle,
    checkpoint saved."""
    say(path, 40_000, 'Continuing with the carried request.')
    end_turn(store, pay)
    say(path, tokens)
    attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining tests'), 'claude')
    attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Half done.'), 'claude')
    continuity.record_checkpoint(store, tid, 'Write tests/test_cli.py', notes='cli.py started')
    attach.handle(store, 'stop', dict(pay, stop_hook_active=True, last_assistant_message='checkpoint saved'), 'claude')


# ---------------------------------------------------------------- ①

def reading(env, base, store):
    print('① what a transcript says about its session\'s name')
    path = base / 'r1.jsonl'
    rows(path, ('user', '<local-command-stdout>ok</local-command-stdout>'), ('user', 'Refactor app.py into a module'),
         ('ai', 'Refactor app into a module'))
    info = title.read(path)
    check('a generated title, a session the user began: that title is its name',
          info == {'custom': None, 'generated': 'Refactor app into a module', 'carried': False}
          and title.name_of(info) == 'Refactor app into a module', info)
    rows(path, ('custom', 'ai-camera-demo'), ('ai', 'Module refactor and tests'), ('custom', 'camera  demo\n(ios)'))
    info = title.read(path)
    check('the user\'s title wins over a later generated one; the last of each counts; one clean line',
          info.get('custom') == 'camera demo (ios)' and info.get('generated') == 'Module refactor and tests'
          and title.name_of(info) == 'camera demo (ios)', info)
    typed, wrapped = base / 'r2.jsonl', base / 'r3.jsonl'
    rows(typed, ('user', typist.CARRY_LINE), ('ai', ORBIT_MADE))
    rows(wrapped, ('user', 'The cosmos-orbit-handoff plugin sent a message: ' + typist.CARRY_LINE), ('ai', '이어서 진행'))
    check('a session that began with the carry line (typed, or submitted by the mod): its generated title is no name',
          title.read(typed).get('carried') and title.read(wrapped).get('carried')
          and title.name_of(title.read(typed)) is None and title.name_of(title.read(wrapped)) is None,
          (title.read(typed), title.read(wrapped)))
    rows(typed, ('custom', 'ai-camera-demo'))
    check('… once it has a title of its own, that is its name', title.name_of(title.read(typed)) == 'ai-camera-demo')
    check('a missing or unreadable transcript: nothing, no error',
          title.read(base / 'none.jsonl') == {} and title.read(None) == {} and title.name_of({}) is None)
    big = base / 'r4.jsonl'
    rows(big, ('user', 'Refactor app.py into a module'), ('custom', 'old name'))
    for n in range(40):
        say(big, 1000 + n, 'filler ' * 20)
    rows(big, ('custom', 'new name'))
    saved = title.WHOLE, title.HEAD, title.TAIL
    title.WHOLE, title.HEAD, title.TAIL = 2000, 400, 700
    try:
        info = title.read(big)
    finally:
        title.WHOLE, title.HEAD, title.TAIL = saved
    check('a transcript too large to read whole (%d bytes, limit set to 2000): its start and its end are read'
          % big.stat().st_size, info == {'custom': 'new name', 'generated': None, 'carried': False}, info)
    check('a title is cut to fit a tab (120 characters)', len(title.clean('가' * 300)) == 120 and title.clean(' \n ') is None)


# ---------------------------------------------------------------- ②

def through(env, base, store):
    print('② through a handoff: the hook of the carry line answers with the leaving session\'s name')
    with Faked() as fake:
        tid, pay, path, root = told_at(store, base, 'a1', 320_000)
        rows(path, ('user', 'Refactor app.py into a module with tests'), ('ai', 'Refactor app into a module'))
        blocked = hand_over(store, pay, fake)
        check('320k (point 300k), auto mode: the next request is handed over', (blocked or {}).get('decision') == 'block', blocked)
        wrong = attach.handle(store, 'prompt', dict(pay, prompt=typist.CARRY_LINE), 'claude')
        check('the carry line in the session being handed off is blocked and names nothing',
              (wrong or {}).get('decision') == 'block' and 'sessionTitle' not in spec(wrong), wrong)
        pay2, line = fresh(store, base, root, 'a2')
        check('the fresh session (a new session id) takes the task over', (store.session_task('a2') or {}).get('task_id') == tid)
        check('the reply to the carry line names the session as the one it follows was named',
              spec(line).get('sessionTitle') == 'Refactor app into a module'
              and spec(line).get('hookEventName') == 'UserPromptSubmit', spec(line))
        check('… and adds nothing else to the reply', set(spec(line)) == {'hookEventName', 'sessionTitle'}, spec(line))
        event = [e for e in store.events(tid) if e['kind'] == 'handoff_title_kept']
        check('… and the ledger says so (which session, from which, what name)', len(event) == 1
              and event[0]['payload'] == {'session': 'a2', 'from': 'a1', 'title': 'Refactor app into a module'},
              [e['payload'] for e in event])
        note = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'a note for the session'}}
        both = title.keep(store, dict(pay2, prompt=typist.CARRY_LINE), note, 'claude')
        check('a reply that already carries context keeps it beside the name',
              spec(both) == {'hookEventName': 'UserPromptSubmit', 'additionalContext': 'a note for the session',
                             'sessionTitle': 'Refactor app into a module'} and 'sessionTitle' not in spec(note), spec(both))
        again = attach.handle(store, 'prompt', dict(pay2, prompt='and document the flags'), 'claude')
        check('the next prompt of the fresh session names nothing', 'sessionTitle' not in spec(again), spec(again))

        tid, pay, path, root = told_at(store, base, 'b1', 320_000)
        rows(path, ('user', 'Refactor app.py into a module with tests'), ('ai', 'Refactor app into a module'))
        hand_over(store, pay, fake)
        path2 = base / 'b2.jsonl'
        rows(path2, ('user', typist.CARRY_LINE))
        pay2 = {'cwd': str(root), 'session_id': 'b2', 'transcript_path': str(path2)}
        attach.handle(store, 'session-start', dict(pay2, source='clear'), 'claude')
        result = env.run(['hook', 'prompt', '--host', 'claude'], stdin=json.dumps(dict(pay2, prompt=typist.CARRY_LINE,
                                                                                   hook_event_name='UserPromptSubmit')))
        try:
            out = json.loads(result.stdout)
        except ValueError:
            out = {}
        check('the real command line (cosmos-orbit hook prompt --host claude): exit 0, the name in its JSON',
              result.returncode == 0 and spec(out).get('sessionTitle') == 'Refactor app into a module',
              (result.returncode, result.stdout[-300:], result.stderr[-200:]))


# ---------------------------------------------------------------- ③

def whose(env, base, store):
    print('③ whose name: the user\'s own first, a carried one again at the next handoff, never one made from Orbit\'s line')
    with Faked() as fake:
        tid, pay, path, root = told_at(store, base, 'c1', 320_000)
        rows(path, ('user', 'Refactor app.py into a module with tests'), ('ai', 'Refactor app into a module'),
             ('custom', 'ai-camera-demo'), ('ai', 'Module refactor and tests'))
        hand_over(store, pay, fake)
        pay2, line = fresh(store, base, root, 'c2')
        check('the leaving session was renamed by the user (/rename): that name is carried, not the generated one',
              spec(line).get('sessionTitle') == 'ai-camera-demo', spec(line))
        # Claude Code keeps the hook's title as the session's own (measured on 2.1.328: a custom-title record)
        rows(pay2['transcript_path'], ('custom', 'ai-camera-demo'))
        tell(store, tid, pay2, pay2['transcript_path'], 330_000)
        handed = hand_over(store, pay2, fake)
        pay3, line3 = fresh(store, base, root, 'c3')
        check('the next handoff (c2 → c3) carries the same name again', (handed or {}).get('decision') == 'block'
              and spec(line3).get('sessionTitle') == 'ai-camera-demo', (handed, spec(line3)))
        rows(pay3['transcript_path'], ('custom', 'ai-camera-demo'), ('custom', 'camera v2'))
        tell(store, tid, pay3, pay3['transcript_path'], 340_000)
        hand_over(store, pay3, fake)
        _, line4 = fresh(store, base, root, 'c4')
        check('a rename in a carried session (c3) is what the one after it gets', spec(line4).get('sessionTitle') == 'camera v2',
              spec(line4))

        tid, pay, path, root = told_at(store, base, 'e1', 320_000)
        rows(path, ('user', 'Refactor app.py into a module with tests'), ('ai', 'Refactor app into a module'))
        hand_over(store, pay, fake)
        saved = continuity.BUILT_IN_LIMITS['title']
        continuity.BUILT_IN_LIMITS['title'] = 'off'      # e2: a session from before this change
        try:
            pay2, line = fresh(store, base, root, 'e2', ('ai', ORBIT_MADE))
        finally:
            continuity.BUILT_IN_LIMITS['title'] = saved
        check('control: without the setting the fresh session is not named (Claude Code then calls it %r)' % ORBIT_MADE,
              'sessionTitle' not in spec(line), spec(line))
        tell(store, tid, pay2, pay2['transcript_path'], 330_000)
        hand_over(store, pay2, fake)
        _, line3 = fresh(store, base, root, 'e3')
        check('a leaving session named only from Orbit\'s line: the name of the session before it is carried instead',
              spec(line3).get('sessionTitle') == 'Refactor app into a module', spec(line3))
        event = [e['payload'] for e in store.events(tid) if e['kind'] == 'handoff_title_kept']
        check('… and the ledger names where it came from (e1, not e2)', event and event[-1].get('from') == 'e1', event)

        tid, pay, path, root = told_at(store, base, 'f1', 320_000)
        hand_over(store, pay, fake)
        before = len(store.events(tid))
        _, line = fresh(store, base, root, 'f2')
        check('no session of the task has a name: nothing is set, the reply is as before, no event',
              'sessionTitle' not in spec(line) and 'handoff_title_kept' not in kinds(store, tid)[before:], spec(line))


# ---------------------------------------------------------------- ④

def stays(env, base, store):
    print('④ what stays: any other prompt, a session named already, Codex, and the off switch at every scope')
    with Faked() as fake:
        def handed(name):
            tid, pay, path, root = told_at(store, base, name, 320_000)
            rows(path, ('user', 'Refactor app.py into a module with tests'), ('ai', 'Refactor app into a module'))
            hand_over(store, pay, fake)
            return tid, root

        tid, root = handed('g1')
        _, line = fresh(store, base, root, 'g2', line='What was the last thing done here?')
        check('the fresh session\'s first prompt is the user\'s own, not the carry line: Claude Code names it, Orbit does not',
              'sessionTitle' not in spec(line), spec(line))
        tid, root = handed('h1')
        _, line = fresh(store, base, root, 'h2', ('custom', 'my own name'))
        check('the fresh session has a name already: it stays', 'sessionTitle' not in spec(line), spec(line))
        tid, root = handed('i1')
        pay2, line = fresh(store, base, root, 'i2', host='codex')
        check('Codex: no title in the reply (its hooks take none)', 'sessionTitle' not in spec(line)
              and title.keep(store, dict(pay2, prompt=typist.CARRY_LINE), None, 'codex') is None, spec(line))

        view = env.context()
        check('cosmos-orbit context shows the setting: keep, built in, and how to set it',
              view['title']['here'] == 'keep' and view['title']['source'] == 'built-in'
              and 'context title keep|off|reset' in str(view['title']['set']), view['title'])
        tid, root = handed('j1')
        os.environ['ORBIT_HANDOFF_TITLE'] = 'off'
        try:
            _, line = fresh(store, base, root, 'j2')
        finally:
            os.environ.pop('ORBIT_HANDOFF_TITLE')
        check('ORBIT_HANDOFF_TITLE=off (one terminal): not named', 'sessionTitle' not in spec(line), spec(line))
        tid, root = handed('k1')
        done = env.run(['context', 'title', 'off', '--global'])
        view = env.context()
        _, line = fresh(store, base, root, 'k2')
        check('cosmos-orbit context title off --global: stored, shown, not named', done.returncode == 0
              and view['title']['here'] == 'off' and view['title']['source'] == 'global' and 'sessionTitle' not in spec(line),
              (done.stderr[-200:], view['title'], spec(line)))
        tid, root = handed('l1')
        one = env.run(['context', 'title', 'keep', '--session', 'l2'])
        pay2, line = fresh(store, base, root, 'l2')
        check('… a session set to keep wins over the global off', one.returncode == 0
              and spec(line).get('sessionTitle') == 'Refactor app into a module', (one.stderr[-200:], spec(line)))
        bad = env.run(['context', 'title', 'sometimes', '--global'])
        check('a value that is not keep or off is refused', bad.returncode != 0 and 'keep' in bad.stderr, bad.stderr[-200:])
        reset = env.run(['context', 'title', 'reset', '--global'])
        check('reset --global: built in again', reset.returncode == 0 and env.context()['title']['here'] == 'keep',
              reset.stderr[-200:])
        check('a hand-edited value the file cannot hold is named, not followed',
              any('handoff.title' in p for p in continuity.limit_problems({'title': 'maybe'}))
              and continuity.limits_effective(store, None, {}, {'handoff': {'title': 'maybe'}})['title'] == 'keep')


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-title-'))
    env = Env(base)
    store = Store(env.home)
    for section in (reading, through, whose, stays):
        try:
            if title is None:
                raise ImportError('harness/title.py is not in this checkout')
            section(env, base, store)
        except Exception:  # noqa: BLE001 - a section that cannot finish fails as one check; the next still runs
            import traceback
            check('%s: ran to the end' % section.__name__, False, traceback.format_exc()[-500:])
    store.close()
    shutil.rmtree(base, ignore_errors=True)
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
