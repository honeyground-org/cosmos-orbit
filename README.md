# Local Harness — executable pilot

A local task runtime for ordinary-language requests. Use **Codex alone or Claude alone**;
neither provider has a fixed role and the other subscription is not required. Existing CLI
authentication is reused. The runtime does not install hooks or modify host configuration.

This first vertical slice performs bounded UTF-8 text/JSON artifact work. It is a running
program with SQLite state, a serial queue worker, provider adapters, file reconciliation and
a deterministic feedback loop. It is not a skill bundle or a workflow document interpreter.

## Run

Requirements: macOS or Linux, Python 3.9+, and one installed/authenticated CLI.

Run `bin/harness` (or `harness` when on PATH) without a subcommand for the interactive launcher.
Choose a new task, browser dashboard, terminal monitor, worker, or an existing task's engine/model
selection. Task setup asks for an installed CLI, CLI-default or explicit model ID, workspace,
ordinary-language request, and immediate execution or queueing. The launcher prints the state-store
location. `--home PATH` works with the launcher too. Noninteractive callers use explicit subcommands.
The model selector currently accepts an ID; it does not yet discover account-specific model lists.

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

## Observe and intervene

```sh
bin/harness watch --lang ko          # live terminal view; en is also available
bin/harness watch --once             # plain snapshot, suitable for redirected output
bin/harness watch --task TASK_ID
bin/harness dashboard --lang ko      # loopback browser UI, opens automatically
bin/harness dashboard --no-open      # print URL without opening a browser
bin/harness pause TASK_ID            # request pause at a safe boundary
bin/harness resume TASK_ID           # clear pause and execute in this terminal
```

Both views show **only tasks and sessions registered in the selected harness state store**.
The mission view presents intake, planning, execution, application, verification and completion,
with recorded stages highlighted. Cards show the engine, available model evidence, check counts,
completed AI calls and cumulative response time. Click a card for the plan, evidence, sessions,
conversation, timeline and interventions. These are observed workflow stages, not percentage estimates.
They do not scan unrelated terminals or attach to external sessions. The browser shows goals,
session bindings, verification evidence, consultation messages and recent state changes.
An active recorded state without its executor lock is marked as executor absent. Completed
artifacts are rechecked without modifying them; changed evidence is displayed as stale.

Browser feedback is queued durably. Pause waits for the current model call or file application
to reach a safe boundary; it does not kill the provider midway. Continue clears the pause request.
A running `harness worker` processes queued work; otherwise the UI explicitly reports that the
request is waiting for a worker. Opening a monitor never starts model work. Closing it leaves
independent tasks running. Use the same `--home PATH` for the worker and both views.

The browser listens only on `127.0.0.1`, with host validation, a per-launch request token and
same-origin mutation checks. It is a local single-user observer, not a remotely hosted service
or a multi-user authentication boundary. Do not expose its port through a public proxy.

## Switch engine or model

```sh
bin/harness switch TASK_ID --provider claude --model MODEL_ID
bin/harness switch TASK_ID --provider codex  # reset to this CLI's default selection
```

The launcher and mission detail panel offer the same controls. Selection requests are durable;
the latest unconsumed request replaces the preceding one. The worker or `resume` applies a selection
at a safe boundary, after any pending file batch is reconciled. Paused work remains paused.
The fixed goal, acceptance checks, feedback, attempt budget and history are retained. A fresh
target-provider session receives the durable task context. Completed tasks are not reopened simply
to change a selection, and conflicted pending writes still require explicit recovery.

Requested models and models reported by the CLI are separate fields. Historical calls and CLI
outputs without model identity are marked unreported, never inferred from a default. The next
CLI invocation checks model availability; unsupported selections interrupt the task without
silently falling back. This control currently applies to the task executor, not its named advisers.

## Distribution direction

Local Harness is being developed as a standalone service intended for future open-source
publication. Provider adapters, durable state, execution and observation are separate modules;
no personal paths, organisation services, Brain installation or fixed AI role split are required.
Local Brain integration will be optional. Currently supported providers are Codex and Claude;
additional adapters and automatic service installation are not implemented yet.

### Memory independence

The default is standalone operation, with **no Local Brain dependency or calls**. Harness-owned
memory is the durable task contract, progress, feedback, verification evidence and checkpoints
in its own SQLite store. Provider sessions supply conversational continuity. Losing or omitting
an external memory service must not erase task ownership, permissions or completion evidence.

Optional memory integrations may enrich context; they must not become the source of task state
or grant execution authority. Local Brain is one possible integration, not the required backend.
No external-memory adapter is implemented in this pilot yet. Host CLI personal-memory discovery
is also not claimed: calls run in isolated directories with tools and host configuration disabled
where supported. The verified standalone path uses harness state and resumed provider sessions.

Source code and private runtime data must remain separate. Do not commit state databases,
provider logs, task exports or credentials. Example commands use disposable workspaces.
This checkout is an unpublished pilot: a public repository, release process and open-source
licence have not yet been established. Public distribution requires an explicit licence choice;
the intention to publish is not itself a licence grant.

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
- Brain retrieval, workflow plugins, durable external-job watchers and evaluated
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
- Observer probes checked managed-only projection, model-free stale-evidence detection, HTTP
  host/origin/token validation, revision conflicts, queued feedback and pause/continue controls.
  A delayed executor probe paused before applying its pending proposal and continued without
  another model call. Browser rendering was inspected against real pilot task/session records.
- Interactive terminal setup was exercised through engine/model selection and durable queueing.
  An isolated controlled-provider probe switched engines during execution, reconciled the pending
  artifact, retained the fixed contract and repaired it with the selected provider/model.

These are pilot checks, not evidence of lower cost or better outcomes across real development work.
