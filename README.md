# Local Harness — executable pilot

A local task runtime for ordinary-language requests. Use **Codex alone or Claude alone**;
neither provider has a fixed role and the other subscription is not required. Existing CLI
authentication is reused. The runtime does not install hooks or modify host configuration.

This first vertical slice performs bounded UTF-8 text/JSON artifact work. It is a running
program with SQLite state, a serial queue worker, provider adapters, file reconciliation and
a deterministic feedback loop. It is not a skill bundle or a workflow document interpreter.

## Run

Requirements: macOS or Linux, Python 3.9+, and one installed/authenticated CLI.

```sh
bin/harness doctor
bin/harness config --provider codex  # optional: choose once; a single installed CLI is auto-selected
bin/harness start --provider codex --workspace /path/to/workspace \
  'Create settings.json with language set to ko and retries set to 3.'
# Or use --provider claude. No second provider is called automatically.
bin/harness status TASK_ID
bin/harness feedback TASK_ID 'Change retries to 5; keep the language unchanged.'
bin/harness handoff TASK_ID
bin/harness resume TASK_ID
bin/harness events TASK_ID
bin/harness recover TASK_ID --keep-current-files  # only for a conflicted pending action
```

After configuring a default provider, omit `--provider` from ordinary requests.
`--plan-only` records an interpreted goal, constraints, scope and acceptance checks without
editing workspace files. `--provider` selects an installed provider; `--model` is optional.
The configured role split is not prescribed. A feedback turn can specify `--provider` to
switch provider while retaining the original request, corrections and contract history.

```sh
bin/harness start --provider claude --workspace /path/to/workspace --queue \
  'Create a short onboarding checklist in onboarding.md.'
bin/harness worker                 # continue queued work, Ctrl-C to stop the worker
bin/harness worker --once          # process the current queue once
bin/harness list                   # no model call
```

The worker is an ordinary foreground process that can be supervised by the user's service
manager. No service is installed automatically. An idle worker makes no model calls.

## Automatic communication between sessions

Optionally bind named advisers to a task. Names are user-selected, not fixed product roles;
both sessions can use the same provider, or different providers when available.

```sh
bin/harness start --provider codex --peer reviewer=claude --workspace /path/to/workspace \
  'Ask reviewer how to verify the JSON types, then create settings.json with enabled true and language ko.'
bin/harness messages TASK_ID
```

When the executing model requests a consultation, the runtime persists the question, wakes its
own named peer session, records the reply and automatically resumes the original session with it.
No user prompt to read a mailbox is needed. Messages move through `queued`, `in_flight`,
`answered` and `consumed`; `in_flight` is not proof of receipt or completion. Peer replies are
advice, not user permissions, and cannot change the fixed acceptance contract.

Delivery failures wait 30 seconds before retry, with at most two delivery attempts. A running
worker handles retries; foreground users can `resume` later. A bounded three consultation rounds
and duplicate-question checks prevent endless acknowledgement loops. A crash can repeat a
read-only model consultation, so this is not an exactly-once inference or billing guarantee.

This pilot wakes **runtime-owned sessions**. It does not attach to arbitrary already-open CLI
terminals or inject keystrokes. Existing-session adapters need explicit capability negotiation.
Without configured peers, no other session or provider is invoked.

## What the runtime owns

1. The selected AI interprets the request and follow-up corrections into a versioned contract.
2. The runtime validates the scope and requires a meaningful quote supporting local write intent.
   This is provenance for an AI interpretation, not a deterministic proof of natural-language consent.
3. The AI proposes full file contents; it does not write the target workspace itself.
4. The runtime checks paths and original hashes, persists the pending action, and replaces files.
5. Fixed text/JSON checks run locally. Failed results are fed into another execution turn;
   the executor cannot replace the acceptance criteria to make its work pass.
6. After three applied attempts by default, unresolved work becomes blocked. Authentication,
   malformed responses and process failures become interrupted, not completed.
7. User feedback reopens the task, preserves its history, and creates a new contract revision.

Feedback submitted while a model is running is stored in a durable inbox. The runtime consumes it
at the next safe boundary, discarding an un-applied model proposal when superseded. A file batch
already being applied is reconciled before the new instruction; feedback does not automatically
undo completed writes. `status` exposes corrections still queued.

`handoff` clears runtime-owned session bindings. The next model call reconstructs its context
from the durable goal, constraints, current files and verification results. It does not replay
the entire conversation. Otherwise each task reuses one session per selected provider across
planning, execution and repair. `--context-budget` sets a checkpoint trigger based on the CLI's
reported input tokens (including cached input); the default 64,000 is a configurable guardrail,
not a measured optimum or an exact live context-window reading. It never attaches a second writer
to an existing user-owned live session.

## State and recovery

Private state defaults to `~/.local/share/local-harness`; use `--home PATH` or `HARNESS_HOME`
to isolate it. The SQLite database, event history and provider logs include task text and
artifact contents. Do not publish that directory. State is kept outside the code repository.

A task lock and workspace lock prevent concurrent runtime writers. Before/after hashes let
an interrupted file batch resume without rewriting an already applied file. An unexpected
external edit stops recovery for inspection. A batch is not a cross-file atomic transaction;
its pending action is retained so a partial batch can be reconciled. Original contents remain
in the stored action history for manual recovery. External processes are not locked by this runtime.

`status` rechecks the contract and reports `stale` when a previously completed result no longer
passes. `resume` then records `stale` and stops; it does not revert user edits to an older goal.
Use feedback to request new work. If checks already hold before any change, the distinct
`already_satisfied` state reports that no work was performed, rather than claiming an implementation.
Historical completion evidence is
retained; it is not a claim that files can never change afterwards.

For an interrupted batch with conflicting external changes, `recover --keep-current-files` abandons
the pending proposal and preserves every current file. The proposal and original contents remain
in the event journal. Submit feedback to replan; no automatic rollback overwrites external work.

Attempt, timeout and context-budget options are persisted per task. Explicit options on a later
`resume` or `feedback` update that task; omitted options retain their stored values.

## Pilot boundaries

- Checks currently support `contains`, `not_contains`, `equals` and `json_equals` (JSON Pointer).
  Completion means those fixed checks passed. It does **not** prove arbitrary software correctness,
  subjective quality or satisfaction of every natural-language intention. Unsupported verification
  must be surfaced as a question, not replaced with a weaker test.
- Initial context is bounded to 30 files / 48 KB with a 500-entry scan ceiling. Truncation is explicit.
  Individual artifacts are capped at 100 KB, changes at 300 KB per batch and 15 files per contract.
- Paths with symlinks, hard-linked target files, parent traversal, credentials, host instructions,
  and runtime directories are rejected. This is not a security sandbox against hostile local users.
- No shell execution, file deletion, PR/merge/deploy, CI watcher or infrastructure apply is provided
  by the task executor in this pilot. A request requiring them must remain unresolved.
- Brain retrieval, workflow plugins, a visual UI, durable external-job watchers and evaluated
  policy promotion are subsequent integrations. Feedback is persisted and affects the current
  task; there is no claim of automatic cross-task learning yet.
- Existing CLI feature support varies. `doctor` reports installed versions; an unsupported CLI
  invocation stops with its local log path. There is no silent switch to another provider.

Only the small artifact slice is implemented. The broader harness is not production-complete.

## Verification on 2026-10-28

Validated with installed Codex CLI 0.164.0 and Claude Code 2.1.312:

- Each provider alone created a JSON artifact, then applied a natural-language correction while
  retaining the other requested value. The local runtime verified the results.
- A queued task ran through the worker. A task switched from Codex to Claude after a checkpoint.
- A low context-budget probe triggered reconstruction from a checkpoint and completed its task.
- Codex requested a Claude adviser reply; the runtime delivered it, resumed Codex and completed
  the artifact without manual message relay.
- Private isolated fault probes checked wrong-result rejection, repair with fixed checks,
  concurrency, interrupted batches, conflicting external edits, queued feedback and strict JSON types.

These are pilot checks, not evidence of lower cost or better outcomes across real development work.
