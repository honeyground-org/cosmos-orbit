#!/usr/bin/env python3
"""A Codex notice is not a CLI error, and a failed turn is one. Model-free, with controls.

Why this exists (2026-11-11): a week of Orbit ledgers held 60 `provider_issue` events ("CLI reported an error").
All 60 were the same banner, an `error` item a wrapper adds to every Codex call
(`--dangerously-bypass-hook-trust` is enabled …), and every one of those calls exited 0. The flow showed
"CLI issue reported" twice at the start of each review. Meanwhile `turn.failed`, the event Codex sends when a turn
does fail, was not read at all.
"""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
from harness.telemetry import provider_event  # noqa: E402

FAILED = []


def check(name, ok, detail=''):
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + detail) if detail and not ok else ''))
    if not ok:
        FAILED.append(name)


def kinds(event):
    return [k for k, _ in provider_event(event)]


BANNER = {'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'error', 'message':
          '`--dangerously-bypass-hook-trust` is enabled. Enabled hooks may run without review for this invocation.'}}

# The banner, exactly as recorded: a notice, never an issue, and its text is kept.
got = provider_event(BANNER)
check('the hook-trust banner is not a CLI error', 'provider_issue' not in kinds(BANNER), str(got))
check('the banner is kept as a notice with its text',
      len(got) == 1 and got[0][0] == 'provider_notice' and 'dangerously-bypass-hook-trust' in got[0][1]['note'], str(got))
check('a started error item adds nothing (one notice per item)',
      kinds({'type': 'item.started', 'item': {'id': 'e', 'type': 'error', 'message': 'x'}}) == [])
long = provider_event({'type': 'item.completed', 'item': {'type': 'error', 'message': 'y' * 900}})
check('a notice longer than 200 characters is cut',
      [k for k, _ in long] == ['provider_notice'] and long[0][1]['note'] == 'CLI notice: ' + 'y' * 200, str(long)[:200])

# Real failures: a failed turn and a stream-level error are issues.
check('turn.failed is a CLI error',
      kinds({'type': 'turn.failed', 'error': {'message': 'usage limit reached'}}) == ['provider_issue'])
check('a top-level error is a CLI error', kinds({'type': 'error', 'message': 'stream disconnected'}) == ['provider_issue'])
check('connection_error is a CLI error', kinds({'type': 'connection_error'}) == ['provider_issue'])

# Controls: the tool lifecycle and public messages are unchanged.
check('control: a command start is recorded',
      kinds({'type': 'item.started', 'item': {'id': 'c', 'type': 'command_execution'}}) == ['provider_tool_started'])
done = provider_event({'type': 'item.completed', 'item': {'id': 'c', 'type': 'command_execution', 'exit_code': 0}})
check('control: a command end is recorded with its exit code',
      [k for k, _ in done] == ['provider_tool_finished'] and done[0][1]['exit_code'] == 0, str(done))
check('control: an agent message is recorded',
      kinds({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'hi'}}) == ['provider_message'])
check('control: a turn that completes adds nothing', kinds({'type': 'turn.completed', 'usage': {}}) == [])

# Replay a recorded call's stream shape: two banners, then work. No issue anywhere.
stream = [{'type': 'thread.started'}, {'type': 'turn.started'},
          dict(BANNER), dict(BANNER, item=dict(BANNER['item'], id='item_1')),
          {'type': 'item.started', 'item': {'id': 'c', 'type': 'command_execution'}},
          {'type': 'item.completed', 'item': {'id': 'c', 'type': 'command_execution', 'exit_code': 0}},
          {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
          {'type': 'turn.completed', 'usage': {}}]
replay = [k for e in stream for k in kinds(json.loads(json.dumps(e)))]
check('a recorded review stream has no CLI error', 'provider_issue' not in replay, str(replay))
check('the same stream keeps both notices', replay.count('provider_notice') == 2, str(replay))

print('\n%d failed' % len(FAILED) if FAILED else '\nall passed')
sys.exit(1 if FAILED else 0)
