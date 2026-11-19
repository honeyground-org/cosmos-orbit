#!/usr/bin/env python3
"""Model-free check of the mid-turn notice and the handoff ceiling (§handoff ceiling).

Why (measured 2026-11-18, task 631fe2b8b0cf): the handoff point was 300k and a session was handed over at 677k. One
65-minute turn grew it from 185k to 511k while Orbit measured only at prompts and Stops, and its checkpoint recorded the
stale 185k; after that a question, the review, an unsure prompt, prompts typed mid-turn, an unprocessed requirement change
at Stop and the review again each postponed the handoff, and nothing closed. Replayed here with synthetic transcripts:

  ① settings: session > environment > global > built in, each with its off switch, shown by `cosmos-orbit context`
  ② the tool-call hook measures during a turn (the real command line), once per crossing, silent where it must be
  ③ the Stop after the notice leads to the handoff on today's path (control: without the notice, a second checkpoint)
  ④ past the ceiling a question or an unsure message is handed over with the end of the last answer, word for word
  ⑤ past the ceiling a missing checkpoint and an unprocessed requirement change no longer hold the handoff
  ⑥ what stays: below the ceiling nothing changed; during a turn the prompt still waits (the 2026-11-05 rule)
  ⑦ the silent path's cost

Nothing types into a terminal, starts a model or touches the live Orbit home or host settings: the store,
CLAUDE_CONFIG_DIR and CODEX_HOME are temporary folders, the terminal and the typist are fakes.
"""
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
STRIP = ('ORBIT', 'COSMOS_ORBIT', 'ORBIT_OFFLOAD', 'ORBIT_HANDOFF_AT', 'ORBIT_HANDOFF_MIN_GROWTH', 'ORBIT_HANDOFF_MIDTURN',
         'ORBIT_HANDOFF_CEILING', 'HARNESS_HOME', 'TMUX', 'TMUX_PANE', 'CMUX_PANEL_ID', 'CMUX_SURFACE_ID',
         'CMUX_BUNDLED_CLI_PATH', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME')
for _name in STRIP:
    os.environ.pop(_name, None)  # a plain terminal; each check adds what it needs
CLI = [sys.executable, str(ROOT / 'bin' / 'cosmos-orbit')]
from harness import attach, continuity, supervision, typist  # noqa: E402
from harness.store import Store  # noqa: E402

FAILED = []
FAKE = {'kind': 'cmux', 'cli': '/bin/echo', 'target': 'PANEL-C'}
WORKING = 'Wrote mod.py; the tests for main() are next.'


def check(name, condition, detail=''):
    print(('  ok  ' if condition else '  FAIL') + ' ' + name + ((' — ' + str(detail)[:300]) if detail and not condition else ''))
    if not condition:
        FAILED.append(name)


class Loose(dict):
    """A reply read leniently: a missing key is an empty reply, so an older checkout fails check by check."""

    def __missing__(self, key):
        return Loose()


class Env:
    def __init__(self, base):
        self.base = base
        self.home = base / 'home'
        self.vars = dict(os.environ, CLAUDE_CONFIG_DIR=str(base / 'claude'), CODEX_HOME=str(base / 'codex'))

    def run(self, args, stdin=None, home=None, **extra):
        env = dict(self.vars)
        for key, value in extra.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(CLI + ['--home', str(home or self.home)] + list(args), input=stdin, capture_output=True,
                              text=True, cwd=str(self.base), env=env, timeout=120)

    def context(self, *args, **extra):
        result = self.run(['context'] + list(args), **extra)
        try:
            return json.loads(result.stdout, object_hook=Loose)
        except ValueError:
            return Loose(error=result.stderr.strip()[-300:], code=result.returncode)

    def hook(self, payload, host='claude', home=None, **extra):
        result = self.run(['hook', 'pre-tool', '--host', host], stdin=json.dumps(payload), home=home, **extra)
        try:
            out = json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError:
            out = {'unreadable': result.stdout}
        return out


def note(out):
    spec = (out or {}).get('hookSpecificOutput') or {}
    return spec.get('additionalContext') or spec.get('permissionDecisionReason') or ''


def make_repo(base, name):
    root = base / name
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'app.py').write_text('print("hello")\n')
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'add', '.'], check=True)
    subprocess.run(['git', '-c', 'user.email=t@t', '-c', 'user.name=t', '-C', str(root), 'commit', '-q', '-m', 'f'], check=True)
    return root


def say(path, tokens, text=None):
    """One assistant message of a Claude Code transcript: its size, and its words when given."""
    message = {'id': 'm%d' % tokens, 'model': 'claude-test', 'role': 'assistant',
               'usage': {'input_tokens': 5, 'cache_creation_input_tokens': 995, 'cache_read_input_tokens': tokens - 1000}}
    if text:
        message['content'] = [{'type': 'text', 'text': text}]
    with open(path, 'a') as stream:
        stream.write(json.dumps({'type': 'assistant', 'message': message}) + '\n')


def rollout(path, tokens):
    """One token count of a Codex rollout (window 258,400 as Codex reports it)."""
    path = Path(path)
    first = not path.exists() or path.stat().st_size == 0
    row = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
        'last_token_usage': {'total_tokens': tokens}, 'model_context_window': 258_400}}}
    with open(path, 'a') as stream:
        if first:
            stream.write(json.dumps({'type': 'session_meta', 'payload': {'id': 'x', 'source': 'cli'}}) + '\n')
        stream.write(json.dumps(row) + '\n')


def bash(command, pay, **more):
    return dict({'session_id': pay['session_id'], 'transcript_path': pay['transcript_path'], 'cwd': pay['cwd'],
                 'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_use_id': 'toolu_1',
                 'tool_input': {'command': command, 'description': 'x'}}, **more)


def kinds(store, tid):
    return [row['kind'] for row in store.events(tid)]


def end_turn(store, pay, host='claude'):
    """Stops until the Stop hook lets the turn end. Returns the replies."""
    replies = []
    for n in range(6):
        reply = attach.handle(store, 'stop', dict(pay, stop_hook_active=n > 0, last_assistant_message='Answered.'), host)
        replies.append(reply)
        if (reply or {}).get('decision') != 'block':
            return replies
    raise AssertionError('the Stop hook never let the turn end')


def start(store, base, name, host='claude', first=50_000):
    """A session with a task, its first turn ended. Returns (task id, payload, transcript, root)."""
    root = make_repo(base, name)
    attach.set_switch(store, 'on', root)
    path = base / (name + ('.rollout.jsonl' if host == 'codex' else '.jsonl'))
    (rollout if host == 'codex' else say)(path, first)
    pay = {'cwd': str(root), 'session_id': name, 'transcript_path': str(path)}
    attach.handle(store, 'session-start', dict(pay, source='startup'), host)
    attach.handle(store, 'prompt', dict(pay, prompt='Refactor app.py into a module with tests and keep the output identical'), host)
    tid = attach.current_task(store, root)['id']
    end_turn(store, pay, host)
    return tid, pay, path, root


def told_at(store, base, name, tokens):
    """A session told to hand off at `tokens` (point 300k), idle at its input line; set up in tell mode."""
    mode = continuity.handoff_mode(store)
    continuity.set_handoff_mode(store, 'tell')
    try:
        tid, pay, path, root = start(store, base, name)
        say(path, tokens)
        attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining tests'), 'claude')
        attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Half done.'), 'claude')
        continuity.record_checkpoint(store, tid, 'Write tests/test_mod.py', notes='mod.py extracted')
        attach.handle(store, 'stop', dict(pay, stop_hook_active=True, last_assistant_message='checkpoint saved'), 'claude')
    finally:
        continuity.set_handoff_mode(store, mode)
    return tid, pay, path, root


class Faked:
    """A terminal Orbit can type into, a typist that only records, a supervisor that is never launched."""

    def __enter__(self):
        self.typed, self.launched = [], []
        self.saved = typist.detect, typist.spawn, supervision.ensure
        typist.detect = lambda env=None: FAKE
        typist.spawn = lambda terminal, steps, log, home=None, task=None: self.typed.append((terminal, steps, task))
        supervision.ensure = lambda store, task, session: self.launched.append((task['id'], session))
        return self

    def __exit__(self, *exc):
        typist.detect, typist.spawn, supervision.ensure = self.saved


# ---------------------------------------------------------------- ①

def settings(env, base, store):
    print('① settings: one style with offload — session > environment > global > built in, each with an off switch')
    view = env.context()
    check('built in: the notice advises, the ceiling is 1.5x the point (claude 300k → 450k)',
          view['midturn']['here'] == 'advise' and view['midturn']['source'] == 'built-in'
          and view['ceiling']['here'] == '1.5x' and view['ceiling']['source'] == 'built-in'
          and view['ceiling']['tokens'] == 450_000, (view.get('midturn'), view.get('ceiling'), view.get('error')))
    out = env.context('ceiling', '500k', '--global')
    check('context ceiling 500k --global writes settings.json "handoff.ceiling"',
          out['ceiling']['here'] == '500k' and out['ceiling']['source'] == 'global'
          and json.loads((env.home / 'settings.json').read_text())['handoff'].get('ceiling') == 500_000, out.get('ceiling'))
    out = env.context('ceiling', '1.6x', CLAUDE_CODE_SESSION_ID='s-set')
    check('without a scope: this session (CLAUDE_CODE_SESSION_ID), like offload', out['changed'].get('scope') == 'session'
          and out['ceiling']['here'] == '1.6x' and out['ceiling']['source'] == 'session' and out['session']['id'] == 's-set',
          (out.get('changed'), out.get('ceiling')))
    out = env.context('ceiling', CLAUDE_CODE_SESSION_ID='s-set', ORBIT_HANDOFF_CEILING='off')
    check('the session beats ORBIT_HANDOFF_CEILING', out['ceiling']['here'] == '1.6x', out.get('ceiling'))
    out = env.context('ceiling', CLAUDE_CODE_SESSION_ID='s-other', ORBIT_HANDOFF_CEILING='off')
    check('… which beats the global value for another session', out['ceiling']['here'] == 'off'
          and out['ceiling']['source'] == 'environment', out.get('ceiling'))
    out = env.context('ceiling', 'reset', '--session', 's-set')
    check('reset --session ID: the global value applies again', out['ceiling']['here'] == '500k', out.get('ceiling'))
    out = env.context('ceiling', 'reset', '--global')
    check('reset --global: the built-in 1.5x applies again', out['ceiling']['here'] == '1.5x', out.get('ceiling'))
    out = env.context('midturn', 'off', '--global')
    check('context midturn off --global', out['midturn']['here'] == 'off' and out['midturn']['source'] == 'global', out.get('midturn'))
    out = env.context('midturn', 'block', '--session', 's-set')
    check('context midturn block --session ID (opt-in refusal)', out['midturn']['here'] == 'block'
          and out['midturn']['source'] == 'session', out.get('midturn'))
    out = env.context('midturn', 'on', '--global')
    check('on = advise', out['midturn']['here'] == 'advise', out.get('midturn'))
    env.context('midturn', 'reset', '--global')
    env.context('midturn', 'reset', '--session', 's-set')
    for bad in (('ceiling', '0.8x'), ('ceiling', 'lots'), ('ceiling', '5k'), ('midturn', 'loud')):
        result = env.run(['context', bad[0], bad[1], '--global'])
        check('rejected: context %s %s' % bad, result.returncode != 0 and 'Traceback' not in result.stderr, result.stderr[-200:])
    result = env.run(['context', 'ceiling', '1.6x'])
    check('no session here and no --global: refused, naming --global', result.returncode != 0 and '--global' in result.stderr,
          result.stderr[-200:])
    data = json.loads((env.home / 'settings.json').read_text())
    data['handoff'].update(midturn='loud', ceiling='lots')
    (env.home / 'settings.json').write_text(json.dumps(data))
    view = env.context()
    problems = view['settings_file']['problems']
    check('a hand-edited value that cannot be read is ignored and reported', view['midturn']['here'] == 'advise'
          and view['ceiling']['here'] == '1.5x' and any('handoff.midturn' in p for p in problems)
          and any('handoff.ceiling' in p for p in problems), problems)
    data['handoff'].pop('midturn')
    data['handoff'].pop('ceiling')
    (env.home / 'settings.json').write_text(json.dumps(data))
    status = json.loads(env.run(['settings']).stdout or '{}', object_hook=Loose)
    check('the one status view (cosmos-orbit settings) shows both', status['handoff']['midturn']['here'] == 'advise'
          and status['handoff']['ceiling']['here'] == '1.5x', status.get('handoff', {}).get('ceiling'))
    guide = env.run(['commands']).stdout
    check('cosmos-orbit commands names both switches', 'context midturn' in guide and 'context ceiling' in guide)


# ---------------------------------------------------------------- ②

def hook(env, base, store):
    print('② the tool-call hook measures during a turn: once per crossing, firmer at the ceiling, silent where it must be')
    tid, pay, path, root = start(store, base, 'm1')
    attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining tests'), 'claude')  # a turn runs
    say(path, 185_000)
    attach.handle(store, 'prompt', dict(pay, prompt='and the edge cases too'), 'claude')               # ledger: 185k
    revision = store.get(tid)['revision']
    say(path, 290_000)
    check('control: 290k, below the 300k point: silent', env.hook(bash('ls', pay)) is None)
    check('… and the ledger is untouched', store.get(tid)['revision'] == revision)
    say(path, 320_000)
    out = env.hook(bash('git status', pay))
    text = note(out)
    check('320k: the notice, as additionalContext (not a refusal)', bool(text)
          and 'permissionDecision' not in out['hookSpecificOutput'], out)
    check('… it names the size, the point, the ceiling, the checkpoint command and ending the turn',
          '320k' in text and '300k' in text and '450k' in text and ('checkpoint %s' % tid) in text
          and 'end the turn' in text and len(text) <= 900, text)
    data = store.get(tid)['data']
    check('the measurement is recorded mid-turn (ledger 185k → 320k), the handoff recommended for this session',
          data['context_live'].get('tokens') == 320_000 and (data.get('handoff_state') or {}).get('state') == 'recommended'
          and data['handoff_state'].get('session') == 'm1', (data['context_live'].get('tokens'), data.get('handoff_state')))
    check('… and the ledger says the notice was given', 'handoff_midturn' in kinds(store, tid), kinds(store, tid)[-4:])
    say(path, 330_000)
    check('330k: told once for this crossing: silent', env.hook(bash('ls', pay)) is None)
    say(path, 371_000)
    check('371k (50k later): told again', '371k' in note(env.hook(bash('ls', pay))))
    say(path, 455_000)
    text = note(env.hook(bash('ls', pay)))
    check('455k: past the ceiling, firmer (do not start another step), still advice', 'ceiling 450k' in text
          and 'Do not start another step' in text, text)
    say(path, 470_000)
    check('470k: told at the ceiling already: silent', env.hook(bash('ls', pay)) is None)
    check('the notices in the ledger: 320k, 371k, 455k', [e['payload']['tokens'] for e in store.events(tid)
                                                          if e['kind'] == 'handoff_midturn'] == [320_000, 371_000, 455_000])

    tid2, pay2, path2, root2 = start(store, base, 'm2')
    say(path2, 320_000)
    attach.handle(store, 'prompt', dict(pay2, prompt='keep going with the remaining tests'), 'claude')
    for label, payload, extra in (
            ("a subagent's call (agent_id)", bash('ls', pay2, agent_id='a1', agent_type='Explore'), {}),
            ('the notice off globally', bash('ls', pay2), {'_global_off': True}),
            ('ORBIT=off in the terminal', bash('ls', pay2), {'ORBIT': 'off'}),
            ('ORBIT_HANDOFF_AT=off (handoff off)', bash('ls', pay2), {'ORBIT_HANDOFF_AT': 'off'}),
            ('ORBIT_HANDOFF_MIDTURN=off', bash('ls', pay2), {'ORBIT_HANDOFF_MIDTURN': 'off'}),
            ('a session with no task', dict(bash('ls', pay2), session_id='nobody'), {}),
            ('an empty event', {}, {})):
        if extra.pop('_global_off', False):
            env.context('midturn', 'off', '--global')
        out = env.hook(payload, **extra)
        if label == 'the notice off globally':
            env.context('midturn', 'reset', '--global')
        check('silent: %s' % label, out is None, out)
    env.context('midturn', 'off', '--session', 'm2')
    check('silent: this session off beats ORBIT_HANDOFF_MIDTURN=advise',
          env.hook(bash('ls', pay2), ORBIT_HANDOFF_MIDTURN='advise') is None)
    env.context('midturn', 'reset', '--session', 'm2')
    attach.set_switch(store, 'off', root2)
    check('silent: Orbit switched off for the task\'s project', env.hook(bash('ls', pay2)) is None)
    attach.set_switch(store, 'on', root2)
    check('none of those gave or used up the notice', 'handoff_midturn' not in kinds(store, tid2))
    check('control: the same call now speaks', '320k' in note(env.hook(bash('ls', pay2))))
    missing = base / 'no-home'
    out = env.hook(bash('ls', pay2), home=missing)
    check('no ledger in the home: silent, and the home is not created', out is None and not missing.exists())

    cid, cpay, cpath, croot = start(store, base, 'c1', host='codex')
    rollout(cpath, 215_000)
    text = note(env.hook(bash('cargo build', cpay), host='codex'))
    check('Codex: measured from its rollout, its point capped at 80% of the window (207k)', '215k' in text
          and '207k' in text, text)

    tid3, pay3, path3, root3 = start(store, base, 'b1')
    env.context('midturn', 'block', '--session', 'b1')
    say(path3, 320_000)
    out = env.hook(bash('ls', pay3))
    check('block mode below the ceiling: advice, never a refusal', bool(note(out))
          and 'permissionDecision' not in out['hookSpecificOutput'], out)
    say(path3, 460_000)
    out = env.hook(bash('ls', pay3))
    spec = (out or {}).get('hookSpecificOutput') or {}
    check('block mode past the ceiling: the call is refused, naming the bypass',
          spec.get('permissionDecision') == 'deny' and 'ORBIT_HANDOFF_MIDTURN=off' in spec.get('permissionDecisionReason', ''), out)
    out = env.hook(bash('git diff', pay3))
    check('… every call, not only the first', ((out or {}).get('hookSpecificOutput') or {}).get('permissionDecision') == 'deny', out)
    for command in ("cosmos-orbit checkpoint %s --next 'x' --notes 'y'" % tid3, 'cosmos-orbit claim %s --summary done' % tid3,
                    "cosmos-orbit --home '/tmp/a b' evidence %s D1 --note x" % tid3,
                    "python3 -m harness.cli done %s 'D1 x'" % tid3, 'ORBIT_HANDOFF_MIDTURN=off ls -la'):
        out = env.hook(bash(command, pay3))
        check('never refused: %s' % command.split(' %s' % tid3)[0][:40],
              ((out or {}).get('hookSpecificOutput') or {}).get('permissionDecision') != 'deny', out)

    tid4, pay4, path4, root4 = start(store, base, 'o1')
    say(path4, 320_000)
    env.run(['offload', 'advise', '--global'])
    text = note(env.hook(bash('python3 -m pytest -q', pay4)))
    check('a suite past both points: offload\'s note and the notice in one reply', 'offload run' in text
          and 'handoff point 300k' in text, text)
    env.run(['offload', 'off', '--global'])


# ---------------------------------------------------------------- ③

def stop_after_notice(env, base, store):
    print('③ the Stop after the notice leads to the handoff on today\'s path')
    with Faked() as fake:
        continuity.set_handoff_mode(store, 'auto')
        try:
            tid, pay, path, root = start(store, base, 's1')
            attach.set_done(store, tid, ['D1 mod.py holds the logic', 'D2 tests cover main()'])
            say(path, 185_000)
            attach.handle(store, 'prompt', dict(pay, prompt='이어서 진행해'), 'claude')  # a go-ahead; the ledger says 185k
            say(path, 320_000)
            check('the notice mid-turn', '320k' in note(env.hook(bash('pytest -q tests/test_mod.py', pay))))
            result = env.run(['checkpoint', tid, '--next', 'Write tests for main()', '--notes', 'mod.py extracted'])
            row = (store.get(tid)['data'].get('checkpoints') or [{}])[-1]
            state = store.get(tid)['data'].get('handoff_state') or {}
            check('the checkpoint the session writes carries the size measured mid-turn (320k, not the stale 185k)',
                  result.returncode == 0 and row.get('tokens') == 320_000, (result.stderr[-200:], row.get('tokens')))
            check('… and makes the handoff ready (checkpointed)', state.get('state') == 'checkpointed', state)
            reply = attach.handle(store, 'stop', dict(pay, stop_hook_active=False,
                                                      last_assistant_message='mod.py extracted; tests for main() next.'), 'claude')
            state = store.get(tid)['data'].get('handoff_state') or {}
            check('the Stop does not ask for a second checkpoint: it tells, and the work goes on in a fresh context',
                  (reply or {}).get('decision') != 'block' and 'starts a fresh context by itself' in (reply or {}).get('systemMessage', '')
                  and state.get('state') == 'told', (reply, state))
            task = store.get(tid)
            check('the supervisor takes it from here (handoff_continues, and it was started)',
                  supervision.handoff_continues(store, task) and (tid, 's1') in fake.launched, fake.launched)
            started = continuity.continue_in_fresh_context(store, task, 's1', 'claude', FAKE, spawn=lambda *a, **k: None)
            check('… and the unattended handoff starts (state auto)', started
                  and store.get(tid)['data']['handoff_state']['state'] == 'auto', store.get(tid)['data']['handoff_state'])
            # control: the same turn without the notice (ORBIT_HANDOFF_MIDTURN=off) — the stale size, a second request
            tid0, pay0, path0, root0 = start(store, base, 's0')
            say(path0, 185_000)
            attach.handle(store, 'prompt', dict(pay0, prompt='keep going with the remaining tests'), 'claude')
            say(path0, 320_000)
            check('control: the notice off: silent', env.hook(bash('pytest -q', pay0), ORBIT_HANDOFF_MIDTURN='off') is None)
            env.run(['checkpoint', tid0, '--next', 'Write tests for main()', '--notes', 'mod.py extracted'])
            row0 = store.get(tid0)['data']['checkpoints'][-1]
            reply0 = attach.handle(store, 'stop', dict(pay0, stop_hook_active=False,
                                                       last_assistant_message='mod.py extracted; tests for main() next.'), 'claude')
            check('control: without it the checkpoint says 185k and the Stop asks for another one (the 2026-11-18 gap)',
                  row0.get('tokens') == 185_000 and (reply0 or {}).get('decision') == 'block'
                  and 'Record a checkpoint now' in (reply0 or {}).get('reason', ''), (row0.get('tokens'), reply0))
        finally:
            continuity.set_handoff_mode(store, 'tell')


# ---------------------------------------------------------------- ④

QUESTION = ('Why did the second check fail on the export path? ' * 34 + 'And which file holds the rule it reads, '
            'QUESTION-END-4431?')
ANSWER = 'ANSWER-HEAD-1122 ' + 'The export path reads the rule from config/rules.toml before the check. ' * 70 + 'ANSWER-TAIL-7781.'


def question(env, base, store):
    print('④ past the ceiling a question or an unsure message is handed over, with the end of the last answer')
    check('the question is a question, longer than the brief\'s 1200-character line',
          __import__('harness.requirements', fromlist=['classify']).classify(QUESTION) == 'question' and len(QUESTION) > 1500,
          len(QUESTION))
    with Faked() as fake:
        tid, pay, path, root = told_at(store, base, 'q1', 455_000)
        say(path, 456_000, ANSWER)
        continuity.set_handoff_mode(store, 'auto')
        try:
            reply = attach.handle(store, 'prompt', dict(pay, prompt=QUESTION), 'claude')
            carried = store.get(tid)['data'].get('carried_prompt') or {}
            check('456k (ceiling 450k): the question is blocked here and handed over', (reply or {}).get('decision') == 'block'
                  and len(fake.typed) == 1 and store.get(tid)['data']['handoff_state']['state'] == 'auto', (reply, len(fake.typed)))
            check('… the ledger carries it word for word, as a question', carried.get('text') == QUESTION
                  and carried.get('kind') == 'question' and carried.get('ceiling'), {k: carried.get(k) for k in ('kind', 'ceiling')})
            answer = carried.get('answer') or ''
            check('… with the end of the last answer (at most 3000 characters, its last words kept)',
                  answer.endswith('ANSWER-TAIL-7781.') and 'ANSWER-HEAD-1122' not in answer and len(answer) <= 3000, answer[-80:])
            start_out = attach.handle(store, 'session-start', {'cwd': str(root), 'session_id': 'q2', 'source': 'clear',
                                                               'transcript_path': str(base / 'q2.jsonl')}, 'claude')
            context = ((start_out or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
            check('the fresh session takes the task over', (store.session_task('q2') or {}).get('task_id') == tid)
            check('its brief says to answer the carried message', 'CARRIED OVER (the message the user sent' in context, context[-600:])
            check('the user\'s words reach it whole (all of the question, though the brief line holds 1200)',
                  QUESTION in context, len(context))
            check('… and the end of the answer it refers to', 'ANSWER-TAIL-7781.' in context)
            line = attach.handle(store, 'prompt', {'cwd': str(root), 'session_id': 'q2', 'prompt': typist.CARRY_LINE}, 'claude')
            text = ((line or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
            check('… once: the carry line does not repeat it', 'CARRIED OVER IN FULL' not in text, text[-300:])

            tid_u, pay_u, path_u, root_u = told_at(store, base, 'u1', 455_000)
            say(path_u, 456_000, 'I updated the export and its tests.')
            unsure = 'README도 업데이트하는 게 맞을까?'
            count = len(fake.typed)
            reply = attach.handle(store, 'prompt', dict(pay_u, prompt=unsure), 'claude')
            carried = store.get(tid_u)['data'].get('carried_prompt') or {}
            check('an unsure message past the ceiling is handed over too', (reply or {}).get('decision') == 'block'
                  and len(fake.typed) == count + 1 and carried.get('text') == unsure and carried.get('kind') == 'unsure'
                  and 'export and its tests' in (carried.get('answer') or ''), (reply, carried))

            for name, tokens, label in (('qb', 440_000, '440k, past the point but below the ceiling'),):
                tid_b, pay_b, path_b, _ = told_at(store, base, name, tokens)
                say(path_b, tokens + 1000, ANSWER)
                count = len(fake.typed)
                reply = attach.handle(store, 'prompt', dict(pay_b, prompt=QUESTION), 'claude')
                check('control: %s: the question is answered in place (as before), nothing typed' % label,
                      (reply or {}).get('decision') != 'block' and len(fake.typed) == count
                      and not store.get(tid_b)['data'].get('carried_prompt'), reply)
                end_turn(store, pay_b)
                reply = attach.handle(store, 'prompt', dict(pay_b, prompt=unsure), 'claude')
                check('control: %s: an unsure message too' % label, (reply or {}).get('decision') != 'block'
                      and len(fake.typed) == count, reply)
            tid_o, pay_o, path_o, _ = told_at(store, base, 'qo', 455_000)
            continuity.set_limit(store, 'ceiling', 'off', 'session', 'qo')
            say(path_o, 620_000, ANSWER)
            count = len(fake.typed)
            reply = attach.handle(store, 'prompt', dict(pay_o, prompt=QUESTION), 'claude')
            check('control: the ceiling off for this session: even at 620k the question is answered in place',
                  (reply or {}).get('decision') != 'block' and len(fake.typed) == count, reply)
            tid_r, pay_r, path_r, root_r = told_at(store, base, 'qr', 455_000)
            reply = attach.handle(store, 'prompt', dict(pay_r, prompt='Add CSV export for the report'), 'claude')
            carried = store.get(tid_r)['data'].get('carried_prompt') or {}
            check('a request past the ceiling: handed over as before, now also with the answer and its whole text',
                  (reply or {}).get('decision') == 'block' and carried.get('kind') == 'requirement' and carried.get('ceiling'), carried)
        finally:
            continuity.set_handoff_mode(store, 'tell')


# ---------------------------------------------------------------- ⑤

def gates(env, base, store):
    print('⑤ past the ceiling a missing checkpoint and an unprocessed requirement change no longer hold the handoff')
    with Faked() as fake:
        def asked(name, tokens):
            """A session asked for a checkpoint at `tokens` that never wrote one, idle at its input line."""
            continuity.set_handoff_mode(store, 'tell')
            tid, pay, path, root = start(store, base, name)
            say(path, tokens)
            attach.handle(store, 'prompt', dict(pay, prompt='keep going with the remaining tests'), 'claude')
            # ("Half done." reads as a completion claim, and that nudge would take the Stop first)
            attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message=WORKING), 'claude')
            attach.handle(store, 'stop', dict(pay, stop_hook_active=True, last_assistant_message=WORKING), 'claude')
            return tid, pay, path, root

        try:
            tid, pay, path, root = asked('a1', 400_000)
            check('setup: asked at 400k and no checkpoint came: the state stays "asked"',
                  store.get(tid)['data']['handoff_state']['state'] == 'asked', store.get(tid)['data']['handoff_state'])
            continuity.set_handoff_mode(store, 'auto')
            say(path, 405_000)
            count = len(fake.typed)
            reply = attach.handle(store, 'prompt', dict(pay, prompt='Add CSV export for the report'), 'claude')
            check('control: below the ceiling an "asked" handoff still waits for the checkpoint (as before)',
                  (reply or {}).get('decision') != 'block' and len(fake.typed) == count, reply)
            end_turn(store, pay)
            say(path, 460_000)
            reply = attach.handle(store, 'prompt', dict(pay, prompt='Add PDF export for the report'), 'claude')
            data = store.get(tid)['data']
            check('past the ceiling the request hands off at once, Orbit writing the checkpoint from the ledger',
                  (reply or {}).get('decision') == 'block' and len(fake.typed) == count + 1
                  and data['handoff_state']['state'] == 'auto' and data['checkpoints'][-1].get('by') == 'orbit'
                  and data['carried_prompt']['text'] == 'Add PDF export for the report', (reply, data['handoff_state']))

            tid_s, pay_s, path_s, _ = asked('a2', 400_000)
            say(path_s, 460_000)
            attach.handle(store, 'prompt', dict(pay_s, prompt='keep going with the remaining tests'), 'claude')
            reply = attach.handle(store, 'stop', dict(pay_s, stop_hook_active=False, last_assistant_message=WORKING), 'claude')
            state = store.get(tid_s)['data']['handoff_state']
            check('a Stop past the ceiling with an "asked" handoff: Orbit writes the checkpoint and tells',
                  state.get('state') == 'told' and store.get(tid_s)['data']['checkpoints'][-1].get('by') == 'orbit'
                  and 'Checkpoint saved' in (reply or {}).get('systemMessage', ''), (reply, state))
            tid_c, pay_c, path_c, _ = asked('a3', 400_000)
            say(path_c, 420_000)
            attach.handle(store, 'prompt', dict(pay_c, prompt='keep going with the remaining tests'), 'claude')
            reply = attach.handle(store, 'stop', dict(pay_c, stop_hook_active=False, last_assistant_message=WORKING), 'claude')
            check('control: the same Stop below the ceiling changes nothing', reply is None
                  and store.get(tid_c)['data']['handoff_state']['state'] == 'asked', (reply, store.get(tid_c)['data']['handoff_state']))

            def changed(name, tokens, mode):
                """Told at `tokens`; a go-ahead starts a turn (in tell mode, so it hands nothing off) and a request
                change is typed during it; then the turn's Stop."""
                continuity.set_handoff_mode(store, 'tell')
                tid, pay, path, root = told_at(store, base, name, tokens)
                attach.set_done(store, tid, ['D1 mod.py holds the logic'])
                attach.handle(store, 'prompt', dict(pay, prompt='이어서 진행해'), 'claude')
                continuity.set_handoff_mode(store, mode)
                inside = attach.handle(store, 'prompt', dict(pay, prompt='Also add CSV export with one row per item'), 'claude')
                reply = attach.handle(store, 'stop', dict(pay, stop_hook_active=False, last_assistant_message='Half done.'), 'claude')
                return tid, pay, root, inside, reply

            tid_q, pay_q, root_q, inside, reply = changed('r1', 460_000, 'auto')
            data = store.get(tid_q)['data']
            check('a request change typed mid-turn past the ceiling still waits for the turn (not blocked, nothing typed)',
                  (inside or {}).get('decision') != 'block' and 'handoff_deferred' in kinds(store, tid_q), kinds(store, tid_q)[-5:])
            check('the Stop past the ceiling does not send the session back to work for it',
                  (reply or {}).get('decision') != 'block' and 'requirement_to_fresh_context' in kinds(store, tid_q), reply)
            check('… the change stays pending, not asked: the fresh context revises the goals',
                  data.get('requirements_pending') and not data['requirements_pending'].get('asked'), data.get('requirements_pending'))
            task = store.get(tid_q)
            check('… and the supervisor hands the work over (work remains: the change)', supervision.handoff_continues(store, task))
            continuity.continue_in_fresh_context(store, task, 'r1', 'claude', FAKE, spawn=lambda *a, **k: None)
            fresh = base / 'r2.jsonl'
            say(fresh, 30_000)
            pay_f = {'cwd': str(root_q), 'session_id': 'r2', 'transcript_path': str(fresh)}
            out = attach.handle(store, 'session-start', dict(pay_f, source='clear'), 'claude')
            brief = ((out or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')
            check('the fresh session takes the task over, the change in its brief',
                  (store.session_task('r2') or {}).get('task_id') == tid_q and 'CSV export' in brief, brief[-500:])
            attach.handle(store, 'prompt', dict(pay_f, prompt=typist.CARRY_LINE), 'claude')
            reply_f = attach.handle(store, 'stop', dict(pay_f, stop_hook_active=False, last_assistant_message='Reading.'), 'claude')
            check('… and its first Stop asks for the revision there: the change is not lost',
                  (reply_f or {}).get('decision') == 'block' and 'changed the request' in reply_f.get('reason', ''), reply_f)

            tid_b, _, _, _, reply = changed('r3', 440_000, 'auto')
            check('control: below the ceiling the Stop still asks for the revision first (as before)',
                  (reply or {}).get('decision') == 'block' and 'changed the request' in reply.get('reason', ''), reply)
            tid_t, _, _, _, reply = changed('r4', 460_000, 'tell')
            check('control: past the ceiling but in tell mode (nobody can start it) it still asks (as before)',
                  (reply or {}).get('decision') == 'block' and 'changed the request' in reply.get('reason', ''), reply)
        finally:
            continuity.set_handoff_mode(store, 'tell')


# ---------------------------------------------------------------- ⑥

def stays(env, base, store):
    print('⑥ what stays: during a turn a prompt still waits past the ceiling (the 2026-11-05 rule)')
    with Faked() as fake:
        tid, pay, path, root = told_at(store, base, 'd1', 460_000)
        attach.handle(store, 'prompt', dict(pay, prompt='이어서 진행해'), 'claude')  # tell mode: a turn starts
        continuity.set_handoff_mode(store, 'auto')
        try:
            count = len(fake.typed)
            mid = attach.handle(store, 'prompt', dict(pay, prompt='What did the last test print?'), 'claude')
            events = [e for e in store.events(tid) if e['kind'] == 'handoff_deferred']
            check('a question typed during a turn past the ceiling: not blocked, nothing typed, deferred with the reason',
                  (mid or {}).get('decision') != 'block' and len(fake.typed) == count and events
                  and events[-1]['payload'].get('ceiling'), (mid, len(fake.typed) - count, events[-1:] if events else None))
            end_turn(store, pay)
            after = attach.handle(store, 'prompt', dict(pay, prompt='What did the last test print?'), 'claude')
            check('… the first prompt after the turn ends hands off (the question too)', (after or {}).get('decision') == 'block'
                  and len(fake.typed) == count + 1, after)
        finally:
            continuity.set_handoff_mode(store, 'tell')


# ---------------------------------------------------------------- ⑦

def cost(env, base, store):
    print('⑦ the silent path: no store below the point (medians over 10 runs of the real command line)')
    tid, pay, path, root = start(store, base, 't1')
    with open(path, 'a') as stream:                     # a long session's transcript: 3 MB of tool output
        for n in range(1500):
            stream.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'content': 'x' * 2000}]}}) + '\n')
    say(path, 120_000)
    timings = {}
    for label, payload in (('below the point, 3 MB transcript', bash('ls', pay)),
                           ('an empty event', {})):
        runs = []
        for _ in range(10):
            started = time.monotonic()
            out = env.hook(payload)
            runs.append((time.monotonic() - started) * 1000)
            assert out is None, out
        timings[label] = statistics.median(runs)
    say(path, 320_000)
    env.hook(bash('ls', pay))                            # told once
    say(path, 330_000)
    runs = []
    for _ in range(10):
        started = time.monotonic()
        env.hook(bash('ls', pay))
        runs.append((time.monotonic() - started) * 1000)
    timings['past the point, told already'] = statistics.median(runs)
    print('  info hook pre-tool median: ' + ', '.join('%s %.0f ms' % kv for kv in timings.items()))
    check('below the point the hook adds little to an empty event (< 40 ms median)',
          timings['below the point, 3 MB transcript'] - timings['an empty event'] < 40, timings)


def main():
    base = Path(tempfile.mkdtemp(prefix='orbit-ceiling-'))
    env = Env(base)
    store = Store(env.home)
    for section in (settings, hook, stop_after_notice, question, gates, stays, cost):
        try:
            section(env, base, store)
        except Exception:  # noqa: BLE001 - a section that cannot finish fails as one check; the next still runs
            import traceback
            check('%s: ran to the end' % section.__name__, False, traceback.format_exc()[-400:])
    store.close()
    shutil.rmtree(base, ignore_errors=True)
    print('\n' + ('FAIL ' + ', '.join(FAILED) if FAILED else 'PASS') + ' · ' + str(base))
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
