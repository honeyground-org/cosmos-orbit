# Cosmos Orbit

**Keep your AI work in orbit.** Part of the Cosmos family: Cosmos AIOS,
[Cosmos Brain](https://github.com/honeyground-org/local-brain) and Cosmos Orbit.

[Source](https://github.com/honeyground-org/cosmos-orbit) · [Apache-2.0 licence](LICENSE) ·
[Interactive demo](https://honeyground-org.github.io/cosmos-orbit/) ·
[Cosmos Brain (local-brain)](https://github.com/honeyground-org/local-brain)

### [Launch the interactive engine room →](https://honeyground-org.github.io/cosmos-orbit/)

[![The Cosmos Orbit engine room: AI session cores, context and verification layers, live signals. Click to open the interactive demo.](docs/assets/engine-room.jpg)](https://honeyground-org.github.io/cosmos-orbit/)

**▶ https://honeyground-org.github.io/cosmos-orbit/**

**Rotate it. Zoom in. Inspect an engine. Watch signals travel through Orbit.**

Explore 12 synthetic sessions, animated context and verification layers, attached sessions with
their live context and checkpoints, and session details directly in your browser. No installation,
account or AI calls required. This is an interactive preview of the real UI, not a recording.
GitHub README pages cannot run JavaScript, so the preview opens on GitHub Pages. It starts in
English; the language menu offers six languages and remembers your choice.


A local companion for **Codex CLI or Claude Code**. A CLI session forgets its goal when the
context is compacted, grades its own work, and ends when its process ends. Orbit is the layer
that holds what the session cannot hold for itself: the original goal and every correction,
evidence per completion item, one independent review of the completion claim, and a replayable
ledger from which handoffs and measurements are generated. Two entrances, one ledger: attach Orbit
to the session you already use, or run a request unattended in a private clone. Either engine works
alone. Brain is optional. Orbit never commits, pushes or deploys task changes.

> **Pairs with [Cosmos Brain (local-brain)](https://github.com/honeyground-org/local-brain)**, an
> open-source memory system for coding agents that lives on your machine. Orbit keeps one task on
> course; Brain carries your decisions, lessons and project context into every session. Related
> memories attach as you type, the guidance you once wrote appears right before a risky push or
> migration, and eleven MCP tools answer when the agent asks. Local index, zero dependencies,
> Apache-2.0, works with Claude Code, Codex and any MCP client.
>
> ```sh
> git clone https://github.com/honeyground-org/local-brain ~/tools/local-brain
> cd ~/tools/local-brain && ./install.sh --with-hook --with-guard
> ```

## Why Orbit does so little

Claude Code and Codex do their best work with the fewest constraints. Orbit does not start your
session, does not wrap your terminal and does not sit between you and the model. Every extra rule, planning
call, per-turn manager or wrapper costs tokens and attention, and a harness that over-manages the
model produces worse results than the CLI on its own. That gap will widen, not narrow, because the
native CLIs keep improving.

Orbit is designed from that premise. It does not plan for the model, does not manage it turn by
turn, and calls no model while nothing is wrong. It takes over only the management points a session
cannot hold for itself:

1. the original request and every later correction, fixed and re-presented after compaction and
   at the next session;
2. evidence per completion item, recorded from commands actually run;
3. one independent, read-only review of the completion claim, in a fresh context;
4. a durable ledger that survives the process, from which handoffs and measurements are generated;
5. a lean context: a long session re-reads everything on every turn, so at a measured point the work
   continues in a fresh session rebuilt from the ledger instead of dragging the whole history along.

Everything else is left to the native CLI, and Orbit judges itself by one thing: the result.

## Attach Orbit to the session you already use

Requires macOS or Linux, Python 3.9+, Git, and an installed, authenticated provider CLI.
Install once:

```sh
git clone https://github.com/honeyground-org/cosmos-orbit.git
cd cosmos-orbit
./install.sh
cosmos-orbit hooks install        # SessionStart · UserPromptSubmit · Stop · PreCompact (+ SessionEnd for Claude Code)
```

The hooks are inert until something is switched on. Then use your normal command: `claude`,
`claude --dangerously-skip-permissions` or `codex`. Nothing else changes.

### Switches: three scopes, each on or off in one command

| Scope | On | Off | Lasts |
|---|---|---|---|
| Every project, every `claude` and `codex` command | `cosmos-orbit on --global` | `cosmos-orbit off --global` | until changed; stored in the Orbit store |
| One project (its Git root; run inside it, or give `PATH`) | `cosmos-orbit on` | `cosmos-orbit off` | until changed; stored per project |
| One terminal session | `export ORBIT=on` (or `ORBIT=on claude`) | `export ORBIT=off` or `unset ORBIT` | that shell only |

Precedence, highest first: terminal session → project → global → off. So `on --global` plus
`off` in one project means "everywhere except here", `off --global` plus `on` in a few projects
means "only there", and `ORBIT=off` silences one terminal whatever the stored switches say.
`cosmos-orbit settings` prints the global default, every project switch, the environment, and
which one applies in the current folder. Outside a Git repository, or wherever the result is off,
the hooks return nothing and cost nothing. `cosmos-orbit hooks uninstall` removes the hooks; a
backup of each host file is kept beside it.

One Codex detail: its workspace-write sandbox blocks writes outside the checkout, so the
executor's `cosmos-orbit done/evidence/claim` calls need the Orbit store added to
`sandbox_workspace_write.writable_roots` (in `~/.codex/config.toml`, or
`codex -c 'sandbox_workspace_write.writable_roots=["~/.local/share/local-harness"]'`). The hooks
themselves run outside that sandbox and need nothing. Codex asks once to trust new hooks.


Several sessions can be open in one project at once. Each session works on its own task: a new
terminal or a `/clear` only takes over an existing task when a handoff checkpoint invited it (one
taker) or when the sessions that held it have ended (Claude Code reports SessionEnd; Codex does not,
so a Codex task stays with its session until a handoff). Only a session that is new when Orbit first
sees it can take over; a session already open without a task — or one that was mid-conversation when the
switch was turned on — starts its own. A conversation resumed under a new id keeps its own task. Prompts
from one session never land in another session's task. Host notifications are ignored.

What happens in an attached session:

1. The first substantial prompt becomes the goal. Every later prompt is recorded; a prompt that
   answers an open question is applied as a correction. The goal is never silently rewritten.
   Corrections typed with `cosmos-orbit feedback` are marked as command-line corrections: the
   executor's own shell can run that command, so the reviewer weighs them as claims. Your prompts
   are your words.
2. At session start, after compaction, and whenever it changed, the hook hands the executor a
   short **brief**: goal, corrections, the completion checklist with evidence counts, the last
   verdict, the stop reason, the latest checkpoint and the exact commands to record evidence and
   claim. A brief that is already in the session's context is not repeated.
3. The executor, your own session, proposes the checklist and records evidence as it works:

   ```sh
   cosmos-orbit done TASK_ID 'add(2,3) returns 5' 'subtraction is no longer used'
   cosmos-orbit evidence TASK_ID D1 --note 'verify.py passes' --command '["python3","verify.py"]'
   cosmos-orbit claim TASK_ID --summary 'add fixed' [--remaining '…'] [--wait]
   ```

4. `claim` runs the fixed checks immediately. A failure is returned to the executor, and the Stop
   hook keeps the turn open until it is fixed; nobody is asked to intervene. A pass queues one
   read-only review in a fresh context (`--wait` runs it now). Unmet items come back in the next
   brief; `verified` closes the task.
5. A turn that ends with completion wording but no claim is stopped once and asked to claim or to
   list what remains. A claim without evidence is returned unmet.
6. `cosmos-orbit handoff TASK_ID --write docs/HANDOFF.md` renders the ledger as Markdown. No
   handoff is written by hand. `cosmos-orbit lessons` lists corrections repeated across tasks, the
   candidates for a guard rule.

`cosmos-orbit attach --workspace . --check '["python3","verify.py"]' --done '…' 'Your request'`
creates the same ledger explicitly, with fixed checks the model cannot change. `cosmos-orbit brief`
prints what the hooks inject. `cosmos-orbit detach` closes a task without verification.

## When the request changes

Real work keeps growing: a case is added, a feature widens, a rule changes. Every such change has to
revise what "done" means, or the review checks yesterday's task. Orbit makes that a rule instead of
a habit:

1. Each prompt is classified without a model. A go-ahead (`continue`, `진행해`) or an information
   question is only recorded; a request phrased as a question ("can you also add CSV export?") and a
   short refusal ("no", "안돼") count as changes. When unsure, Orbit counts it: a missed change costs
   more than one `done --keep`. Host notifications that arrive through the same hook (background tasks, monitors,
   slash-command echoes) are ignored: only a person's words set or change a goal. Anything else is a
   **requirement change**: it joins the corrections that bind the independent review, and the
   session gets one short notice, not the whole brief again.
2. Until the result and verification goals are revised, a completion claim is refused and the Stop
   hook asks the executor once to revise them. The executor understands the change, so it writes
   the revision:

   ```sh
   cosmos-orbit done TASK_ID --add 'CSV export writes one row per item' \
     --check '["python3","-m","pytest","tests/test_csv.py"]'   # new item + new fixed check
   cosmos-orbit done TASK_ID 'D1 …' 'D2 …'                      # rewrite the executor's own items
   cosmos-orbit done TASK_ID --keep                              # the current items already cover it
   ```

3. Fixed checks can only be added, never replaced or removed. Definitions the user fixed can be
   extended with `--add` but not rewritten by the executor. Up to twelve items.
4. Every revision is kept with its number, time, origin, reason and what it added; the brief shows
   `DONE (rev N)`, and the dashboard lists the revisions.
5. A change that arrives while the review runs is queued, not lost; a verified verdict then reopens
   the work as `requirements_changed` instead of closing it.

Unattended work follows the same rule: a correction marks the definitions stale, the executor's next
response proposes the revised set, and Orbit adopts it as a new revision. A completion claim made
while the definitions are still stale goes back to the executor instead of to the review.

## Context and mid-task handoff

A session re-reads its whole context on every turn, cached or not, and long contexts also dilute
the model's attention. Orbit measures each session's context from the host's own transcript (Claude
Code) or rollout (Codex) and hands the work over to a fresh session at a measured point:

1. The hooks read the current context size on every event. No model is called.
2. When a session reaches the **handoff point** (default 200k tokens) and has grown at least 60k
   since it started, the brief says so, and at the end of that turn the Stop hook asks the executor
   once for a short checkpoint: the next concrete step and what the ledger cannot know.

   ```sh
   cosmos-orbit checkpoint TASK_ID --next 'Write tests/test_mod.py for main()' \
     --notes 'mod.py extracted; app.py still imports nothing' --file mod.py
   ```

3. The next Stop tells you once: type `/clear` (Claude Code) or `/new` (Codex). The fresh session
   starts with the brief, the checkpoint and a note to open only the files it needs and not to
   repeat finished work. Nothing to copy, nothing written by hand; the ledger records the rotation.
4. If native compaction comes first, the PreCompact hook writes a checkpoint from the ledger when the
   executor wrote none, and the full brief returns after compaction.
5. Unattended work does the same without you: past the point, the next executor turn starts a
   fresh native session from a ledger packet (goal, corrections, checklist, last verdict without
   tool traces, remaining work, changed files). No extra model call writes the handoff.

Why 200k: replaying 99 real Claude Code sessions from the author's last 14 days (median start 78k,
median peak 506k) with a handoff at 200k saved 31–51% of cost-equivalent tokens (input ×1, cache
write ×1.25, cache read ×0.1) whether a fresh session had to re-read 15k or 80k tokens of files; 42
Codex sessions saved 9–23%. Lower points thrashed when re-reading was heavy (150k: −286% at 80k).
Measure your own sessions before changing it:

```sh
python3 scripts/measure_context_growth.py --days 14
```

The handoff point follows the same three scopes as the on/off switch:

| Scope | Set | Off | Show |
|---|---|---|---|
| Every project | `cosmos-orbit context 300k --global` | `cosmos-orbit context off --global` | `cosmos-orbit settings` |
| One project | `cosmos-orbit context 150k` | `cosmos-orbit context off` | `cosmos-orbit context` |
| One terminal session | `export ORBIT_HANDOFF_AT=250k` | `export ORBIT_HANDOFF_AT=off` | `cosmos-orbit context` |

`cosmos-orbit context on` restores the default. Unattended tasks take the point at creation;
`--context-budget TOKENS` overrides it for one task. Measurement continues when handoff is off.

## Unattended in a private clone

```sh
cosmos-orbit doctor
cosmos-orbit start --provider codex --workspace /path/to/repo \
  --check '["python3", "verify.py"]' 'Your complete request'
```

`--workspace` must be a clean, committed Git repository root. Orbit makes a private clone without
an origin remote. The source checkout, uncommitted and ignored files are not copied or changed;
submodules are currently unsupported. Review and transfer the resulting patch yourself.

Use `--provider claude` for Claude alone. Executor calls keep the engine's native settings,
project guidance, skills and memory behaviour. Native permissions remain in force. The shell
runs inside the native CLI sandbox; Claude requires its sandbox and cannot retry unsandboxed.
`--public-web` explicitly permits public research; it does not authorise account writes or orders.
Custom MCP servers and hooks remain user-controlled and are outside the shell sandbox boundary.
Project settings that are uncommitted or ignored are absent from the clone; native memory keyed
to a checkout path can differ from a direct terminal session.

`--check` is optional and repeatable. Each value is a JSON argument array owned by the user;
the model cannot replace it. These trusted commands run locally with the user's permissions,
without an implicit shell. They must be safe to repeat after an interrupted check.
Without fixed commands, Orbit still records the workspace fingerprint and independently reviews
the completion claim; it does not pretend that a test suite was configured.

## The loop

1. Keep the **original request and every correction**. The mandatory G0 criterion checks the whole
   request even when an executor's proposed checklist misses something.
2. Freeze up to twelve explicit `--done` items, or obtain them from the executor (its first response
   unattended, `cosmos-orbit done` attached). There is no separate planner call. A later
   `feedback --done` explicitly replaces the checklist.
3. Resume the owned native session while work remains (unattended), or hand the brief back to the
   user's session (attached). A failed fixed check returns its real failure output directly to the
   executor, without paying for a reviewer first.
4. When the executor claims completion and current fixed checks pass, use a fresh context to
   inspect the actual workspace and run independent, read-only counterexamples. Missing work
   returns to the executor. Scope drift or an essential question is shown to the user.

The reviewer must produce evidence for every completion ID. Empty evidence cannot pass. A
successful native tool observation is required for `verified`; a claim that tests were run is
insufficient. Evidence the executor recorded about itself is handed to the reviewer as claims to
check, never as proof. Read-only review cannot change the fixed commands or implementation, and
any workspace change during review invalidates the evidence. Review is proportional to the stated
task, rather than an invitation to keep expanding it. These checks reduce unsupported completion
claims; they are not mathematical proof of arbitrary software correctness.

```sh
cosmos-orbit start --provider claude --workspace /path/to/repo \
  --done 'The requested behaviour works for the stated inputs' \
  --max-turns 8 --queue 'Your request'
cosmos-orbit worker --background
cosmos-orbit feedback TASK_ID 'Keep the original purpose; also handle this case.'
cosmos-orbit verify TASK_ID
```

`verify` rechecks a saved result without rerunning implementation. Failed checks or unmet
requirements remain visible for feedback. Execution stops as `verified`, `needs_input`,
`stalled`, `interrupted` or `paused`; active phases are `received`, `working`, `checking`,
and `verifying`. An attached task stays `working` while your session works and carries its
stop reason (`checks_failed`, `unmet`, `detached`) in the brief. The executor-turn limit for
unattended work defaults to eight and is explicit, not a clock deadline. Repeated unchanged files,
remaining work and observed tool evidence can report `stalled`. A supported negative research
result can be a valid conclusion.

## Recovery and continuity

Orbit saves a durable native-call receipt **before launch**: its owned session, private output
paths and process identity. A small supervisor retains the task/workspace locks and saves the
exit receipt even if the foreground app dies. Native tools do not need to preserve those locks.

When the worker or app starts again:

- A still-live owned process is allowed to finish. Recovery never starts a competing executor.
- A terminal response already saved by the CLI is recovered before requesting any new work.
- An incomplete response resumes the owned session with the original goal, corrections and
  retained files. The executor is told to inspect them and continue only missing work.
- Interrupted completion review reruns current fixed checks and recovers or repeats the independent
  review. It does not rerun implementation merely because the reviewer was interrupted.
- Explicit provider usage limits and temporary connection failures record a retry time. The worker
  waits and resumes when it is due, with at most five consecutive automatic failure retries.
  An explicit reset time in supported CLI messages is honoured. Silence is never a retry signal.
- An attached task needs no process at all between turns: the ledger is the state, and the next
  session start re-presents it.

Opening the interactive `cosmos-orbit` app starts the local worker for previously authorised work.
`worker --background` keeps it running after the terminal closes. A foreground task deferred by a
provider limit also starts a background worker so the retry does not require another instruction.
No operating-system login service is installed. Reopening the app after a computer restart starts
recovery. Native sessions, filesystem and provider availability still determine what can be recovered;
arbitrarily detached jobs and external side effects are not an exactly-once execution guarantee.

```sh
cosmos-orbit pause TASK_ID
cosmos-orbit resume TASK_ID
cosmos-orbit handoff TASK_ID [--write PATH]
cosmos-orbit switch TASK_ID --provider claude
```

A user pause or Ctrl-C is preserved. Automatic recovery never clears it. Engine/model switching
applies at a safe boundary and retains the original request. Context measurements, the handoff
point, checkpoints and session rotations stay visible; a manual handoff retains the goal,
checklist, corrections and evidence. Native CLI compaction continues normally. Automatic handoff
rebuilds a fresh session from the ledger; it never calls a model to write the handoff.

## Observe the same work

```sh
cosmos-orbit dashboard
cosmos-orbit watch --once
cosmos-orbit status TASK_ID
cosmos-orbit events TASK_ID
cosmos-orbit result TASK_ID
cosmos-orbit lessons
```

The terminal and engine-room view use the same task store, completion checklist, executor
evidence, call history, fixed-check exit codes and stop/retry reason, for both entrances. Effects
represent observed execution and transport; unknown context and remote progress remain unknown.
The six-language view respects reduced motion. Use the same `--home PATH` (or `HARNESS_HOME`) for
hooks, worker and observers. Observation does not start model work.

The dashboard binds to `127.0.0.1` and validates its host, origin and per-launch token. It is a local
single-user interface, not a public server. Do not expose it through a proxy.

## Memory with Cosmos Brain, and imported work

Orbit's ledger answers "what happened in this task"; [Cosmos Brain](https://github.com/honeyground-org/local-brain)
answers "what is still true across all of them". Together they split the work cleanly: Brain hands
the executor the decisions and lessons that matter when a task starts, and only results Orbit has
verified flow back into Brain as memory. An unverified completion claim never becomes a memory.
Orbit works without Brain; Brain works without Orbit.

`--memory off` disables Orbit's memory bridge; it does not disable native CLI memory/settings.
`native`, `brain` and `both` provide bounded, attributed context. `--remember` with Brain queues
historical checkpoints, with idempotency and conflict handling. Memory delivery never changes a
failed task into a completed task, and an acknowledgement is not proof of completed embedding.

```sh
cosmos-orbit config --brain-command '["/path/to/brain-mcp"]'
cosmos-orbit start --provider codex --workspace /path/to/repo \
  --memory both --remember --project-id my-project 'Your request'
cosmos-orbit memory TASK_ID
cosmos-orbit start --workspace /path/to/repo --provider codex \
  --handoff-file /path/to/HANDOFF.md 'Continue the remaining work.'
```

Imported handoffs are historical context, not fresh authority. The original text, source and hash
are retained. Current files and the current request take precedence. This starts a managed task;
it does not attach to an unrelated CLI session or scan other conversations.

## Existing records and installation

Old task/event/message history stays readable. An old native-development task is upgraded only
when execution is requested; old conclusions are not relabelled as new verified evidence. Historical
artifact proposals and pending file batches are retained for inspection rather than replayed with
broader native permissions. `recover --keep-current-files` can abandon a historical pending proposal
without overwriting files. The planner, per-turn manager and artifact execution loops are removed.

`cosmos-orbit` is the main command and `harness` remains an alias. Existing `HARNESS_HOME`,
`HARNESS_INSTALL_DIR`, `HARNESS_BIN_DIR` and private storage paths remain supported. Run `install.sh`
again to update, or use `pipx install git+https://github.com/honeyground-org/cosmos-orbit.git`.
The installer starts no service by itself; `--launch` opens the app. Uninstalling the application
retains private task data. No host auth files, global CLI settings or shell profiles are rewritten
except by the explicit `hooks install` / `hooks uninstall` commands, which edit only the `hooks`
entries that carry the `cosmos-orbit hook` marker and keep a backup.

Private SQLite state, native transcripts, call receipts, patches and worker logs must not be published.
Interrupted calls may lack usage; unknown duration and usage are not fabricated. The repository
has no runtime Python dependencies. Model-free checks live in `tests/` (`python3 tests/verify_attach.py`).
See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

The public interactive demo is generated from the installed UI using synthetic fixtures only:

```sh
python3 scripts/build_demo.py
python3 scripts/build_demo.py --check
```

It makes no provider calls or runtime requests and is not evidence of real task completion or
recovery. Process-fault probes and live-provider validation are separate. Full power-loss/network
recovery and general long-task productivity require further real-world evidence.
