# Cosmos Orbit handoff — 2026-10-29

## Start here

Read this document, `README.md`, `CONTRIBUTING.md` and `SECURITY.md` before continuing.
The product is **Cosmos Orbit**, repository **honeyground-org/cosmos-orbit**, default branch `main`.
The working directory may still be named `local-harness`; do not infer that this is a different product.
The last implementation commit before this handoff is `f2d2ed8`.

- Source: https://github.com/honeyground-org/cosmos-orbit
- Interactive preview: https://honeyground-org.github.io/cosmos-orbit/
- Licence: Apache-2.0.
- Primary command: `cosmos-orbit`; `harness` remains a compatibility alias.
- Internal Python package: `harness` (intentionally retained).

At handoff verification, local HEAD and remote main matched, the working tree was clean,
the generated preview was current, and GitHub Pages had built `f2d2ed8` successfully.
The public HTTPS response was previously checked byte-for-byte against `docs/index.html`.
This document is a subsequent documentation commit, not a new runtime release.

## Product purpose and non-negotiable decisions

Reduce repeated instructions about objectives, progress, validation, communication and handoffs.
The target is a running system that preserves intent, conditions, authority and evidence across sessions,
not another collection of workflow guides. Natural-language intake should infer a task contract.
The broader lifecycle is goal → plan → implement → verify → repair → validate objective → report → next work.

Cosmos AIOS, Cosmos Brain and Cosmos Orbit are the chosen product naming family.
Orbit must work independently of Brain. Any future Brain integration is optional and replaceable.
Either Codex or Claude must work alone. User role preferences are mutable; no hard-coded
"Codex plans / Claude implements" requirement. Named advisers are optional.
All public documentation, source comments and product UI are English. Preserve users' original content.
Source and private task/memory data must remain separate.

## Implemented runtime

- Ordinary requests become bounded artifact contracts with fixed acceptance checks.
- SQLite task/event state, compare-and-swap revisions, task/workspace locks, durable inbox,
  before/after hashes and recovery of interrupted file batches.
- Serial queue worker, feedback/replanning, bounded repairs, pause/continue at safe boundaries.
- Named peer consultation: persist question, call the owned adviser, store reply, resume the executor.
- Engine/model switch is queued and applied at a safe boundary. Model identity is reported separately
  from the requested model; unknown identity stays unknown.
- Automatic context handoff: save a durable packet before clearing a native session, then start fresh.
  Claude default is 500k latest-request input including cache tokens. Codex uses the smaller of
  500k and 80% of the reported effective window, based on last-token usage rather than cumulative billing.
  Explicit task thresholds override defaults. Completed work does not spawn another session.
- Handoff intake: launcher **Continue from a handoff**, or `start --handoff-file` / `--handoff-text`.
  Imports preserve text, source, SHA-256 and current request. Limits: 16k handoff / 6k current-request characters.
  The current request is the authority source; historical permission quotes cannot satisfy write provenance.
  Fresh planning examines workspace evidence. Imported completion claims are not automatically trusted.
- Interactive provider/model selection. Browser suggestions include visible local Codex cache entries,
  Claude aliases and managed-session history. CLI suggestions include cache/aliases.
  Suggestions do not prove current account access; custom IDs and CLI default remain available.
- `cosmos-orbit commands`, launcher **Command reference**, browser **Commands** share one reference.

## Engine-room UI and user preferences

The user explicitly rejected generic dashboards, card grids and long prose. Preserve the approved
isometric glass engine-room concept; improve its clarity rather than reverting to ordinary dashboards.
Effects must represent evidence, not invented activity.

- Eight session cores per page, task topic/provider/status/stage labels, hover details, right inspection panel.
- Six orbit nodes map to Intake, Plan, Execute, Apply, Verify and Done. Cap ring = measured context;
  body check count = recorded verification. Do not label either as arbitrary overall progress.
- Cores rotate/glow while executing. Idle/completed cores do not pretend to work.
- Independent engine-to-Orbit links. No implied engine-to-engine communication.
  Transport pulses occur on newly observed, attributable request/response events; ambiguity suppresses pulses.
- Lower planes pulse on real observations: control/feedback (blue), context/checkpoints (violet),
  verification (green). A verification event does not necessarily mean success.
- Snapshot deduplication prevents old events replaying on refresh. Respect reduced motion and disconnected state.
- Edge pagination buttons appear near left/right edges; footer navigation stays available.
- Wheel/trackpad and +/- zoom (55–200%); Reset restores 100% and original rotation.
- Inspection shows context and evidence gauges, model identity, calls/time/attempts, session tabs,
  recorded response/event console, collapsible result/plan/checks, model switching and interventions.

## Public demo and distribution

The README prominently links to the interactive GitHub Pages preview. GitHub README sanitisation
prevents running the JavaScript UI inline; do not claim it is embedded live in the README itself.
The demo uses synthetic fixtures only, no model calls, local state, native model cache or runtime API.
Its CSP blocks connections. Demo action buttons never perform real operations.

`python3 scripts/build_demo.py` generates `docs/index.html` from `harness/observer.html` and the
shared command guide. Regenerate after UI/command changes; `--check` detects drift.
GitHub Pages publishes `main:/docs`, with `.nojekyll`. The earlier `docs/design/engine-room.html`
is a historical prototype, not the current preview or runtime.

Install from source with `./install.sh --launch`, or pipx from the Git repository.
Installer builds a wheel before removing the legacy distribution and reuses the private venv.
It checks unrelated launcher collisions and installs both commands. No provider calls or worker start
as an installation side effect. `--launch` opens the interactive menu.
Existing `HARNESS_HOME`, `HARNESS_INSTALL_DIR`, `HARNESS_BIN_DIR` and private
`~/.local/share/local-harness` state are retained. Do not rename/move live data as branding cleanup.
macOS/Linux Python 3.9+ are supported. No PyPI release or native desktop installer exists yet.

## Key files

| File | Responsibility |
|---|---|
| `harness/cli.py`, `launcher.py` | Explicit commands and interactive setup |
| `harness/intake.py` | External handoff import limits, provenance and request construction |
| `harness/engine.py` | Contract, permission provenance, execution, recovery and feedback |
| `harness/context.py`, `providers.py` | Token measurement, checkpointing and native CLI adapters |
| `harness/store.py`, `workspace.py` | Durable state, locks, safe paths, compare-before-write and checks |
| `harness/observer.py`, `observer.html` | Read-only projection, guarded mutations, visual engine room |
| `harness/commands.py` | Shared terminal/browser command reference |
| `install.sh`, `pyproject.toml`, `bin/cosmos-orbit` | Installation and distribution |
| `scripts/build_demo.py`, `docs/index.html` | Public synthetic preview generator/output |

## Verification already performed

- 23 isolated runtime probes: wrong-result rejection, repairs, fixed checks, concurrency, journal recovery,
  JSON type strictness, queued feedback, adviser round trip and duplicate consultation prevention.
- Four grouped observer probes: pause before application, continuation without another model call,
  read-only stale evidence, HTTP origin/host/token/revision guards and queued controls.
- Additional private probes: handoff import persistence, source-file deletion, historical authority rejection,
  input limits, malformed/hidden model-cache entries, interactive paste import without model calls.
- Signal probes: no initial-history replay, deduplication, request/response direction, ambiguous attribution,
  targeted peers and lower-layer event mapping.
- Browser inspection of the actual UI and public demo, including module details, model dropdown,
  pagination, zoom/reset and command reference.
- Clean installation outside checkout, old-package upgrade, both aliases, installed browser asset,
  immediate launcher, and unauthenticated public-source download/install.
- Secret scans of files/history and English/public-path checks passed before publication.
- Earlier real Codex and Claude artifact runs, natural-language correction, queued worker,
  cross-provider consultation and low-threshold fresh-session handoff succeeded.
  They do not establish general development productivity or a Brain retrieval-quality percentage.

Probe scripts were kept outside the repository under the workspace's no-new-test-files policy.
Do not imply that a complete reusable automated test suite or runtime CI exists in the public repo.
GitHub Pages deployment success is not runtime test coverage.

## Remaining gaps and recommended next work

The broad harness objective is NOT complete. Current execution is bounded UTF-8 text/JSON artifact
work with contains/not_contains/equals/json_equals checks. It cannot run arbitrary shell commands,
software test suites, PR/merge/deploy or infrastructure tasks. Importing a handoff does not remove
those boundaries and does not attach to an existing CLI process/session.

1. Prioritise a real software-development continuation pilot: safely delegate native CLI tools,
   preserve explicit authority, isolate workspaces, track real unit/objective verification and recover.
   Establish acceptance criteria before claiming arbitrary development-session support.
2. Improve product-level task intake from the UI if requested; currently creation/import is CLI-driven.
3. Evaluate source-of-truth model discovery beyond optional cache/aliases; do not hard-code account access.
4. Add install packaging/release automation and platform support as deliberately scoped follow-ups.
   Current source/pipx installation is usable, not a signed standalone GUI app.
5. Measure actual long-session efficiency and goal completion; cross-task learning and adaptive policy
   promotion are not implemented. Brain adapter remains future optional work.
6. Refine visual readability on dense layouts/zoom and preserve state during polling. Avoid fabricated metrics.

No outstanding blocker remains for the last request (README interactive preview); it is shipped.
These next steps are recommendations, not claims of user-approved detailed implementation plans.
Brain's earlier three-arm memory-quality study is a separate unfinished evaluation; do not claim
that Orbit's UI/runtime work proved a 70%→90% memory-quality improvement.
