# Development pilot and memory integration continuation — 2026-10-29

Read `2026-10-29-cosmos-orbit.md` first for the existing product decisions.
This note describes subsequent local working-tree changes, not a published release.

## Implemented development pilot

`start --development --check ARGV_JSON` creates a private clone of a clean Git repository,
without an origin remote, and runs the chosen native CLI there. Codex uses workspace-write
with network disabled. Claude uses restricted file tools and safe mode. User-declared command
checks run separately as trusted local programs; Orbit does not sandbox those commands.

The command list is fixed. Failed checks trigger bounded repairs. Passing checks yield
`needs_review`, preserving the distinction between command evidence and objective completion.
Status and the inspector expose the clone, base commit, changed paths, tracked-file patch,
untracked files and logs. Observation hashes files instead of rerunning commands.

Native edits remain in the clone. No source application, commit, push or deployment is automatic.
An interrupted native turn is not replayed automatically: inspect the retained clone and provide
feedback to authorise another turn. Direct children inherit task/workspace lock descriptors.
Plan-only records a durable hold until explicit resume or feedback, without calling a provider.
Queue, pause, feedback, selection switching and context handoffs use the existing runtime state.

Key changes: `harness/development.py`, `cli.py`, `engine.py`, `providers.py`, `store.py`,
`context.py`, observer projection/template and shared command reference. The demo was regenerated.

Live Codex and Claude runs each fixed a small Python bug and passed explicit behaviour checks,
while leaving the source checkout unchanged. Private controlled probes covered repairs, fixed
commands, budgets, pause/continue, stale evidence, uncertain-turn recovery, plan holds, command
timeouts and inherited leases. Python/JavaScript syntax and demo drift checks passed. No reusable
test suite was added. This does not establish general repository or long-session productivity.

## Confirmed user requirements: Brain and host memories

- Cosmos Brain and Cosmos Orbit must each remain standalone products.
- Together they must improve real work: Brain owns remembering, storing, finding, recalling and
  analysis evidence; Orbit invokes those capabilities at useful task boundaries and returns
  verified outcomes and user corrections.
- Codex and Claude must each work independently, with compatible native memory behaviour.
  Do not hard-code one engine as planner and the other as implementer.
- Preserve the original project's identity when execution uses a private clone. Do not accidentally
  lose its memories or merge unrelated projects because their execution paths differ.
- Respect host memory settings and preserve original content, language, provenance and freshness.
  Memory text does not grant execution authority or establish current completion.
- Use a replaceable, versioned adapter, bounded context, deduplication, invalidation, durable write
  retries and honest observation. Brain downtime must not erase Orbit's task state.
- Distinguish returned, injected, used, independently verified and user-rated memories. Do not
  manufacture usefulness feedback from exposure alone.
- Compare native-only, Brain-only integration, Orbit-only and combined paths under equivalent
  tasks, corpus versions and provider settings. Measure quality, task completion, rework, latency,
  tokens and resources. Integration success is not proof of a performance gain.

## Optional memory adapter implemented locally

`--memory off|native|brain|both` defaults to standalone/off. Configure the MCP launcher with
`config --brain-command ARGV_JSON`; `--remember` opts into durable historical verification
checkpoints. Native Markdown is read-only, bounded and selected for the active provider and
original project. Known host opt-outs are respected. This is a partial read bridge, not full
native automatic memory generation or write compatibility.

The stdio MCP adapter negotiates capabilities, uses structured/scoped recall when advertised,
and retains legacy text recall. Context carries source/version metadata, bounded excerpts and
explicit truncation. Matching native/Brain source versions can be deduplicated. Development
executors can request bounded recall, neighbors, timeline or analysis reads; they cannot request
memory mutations through this channel. Returned context is delivery evidence, not usefulness.

The durable outbox uses immutable names, project identity and idempotency keys. Writes require
advertised `orbit-1` JSON/idempotency/version/scope support. Unsupported servers leave records
pending locally. Conflicts never overwrite existing memory. Storage, indexing and embedding
acknowledgements remain distinct. Optional adapter failures do not erase task state.

Controlled stdio probes covered native deduplication/opt-outs, missing-server fallback, bounded
reads, unsafe-write rejection, durable retries and acknowledgement loss. Real Codex and Claude
each completed a development task with native context and a controlled MCP server; checkpoint
writes in those probes used the fixture, not live Brain. Actual Brain MCP legacy recall was
also exercised successfully. No comparative retrieval or productivity gain has been established.

## Remaining integration and product work

Brain O1 capabilities has an open implementation PR; O2 structured recall, O3 safe remember and
O4 scoped retrieval remain pending in the shared contract at this checkpoint. Verify their final
schemas and run isolated end-to-end checks against the actual backend before claiming safe live
writes. Full host-native write compatibility, comparative quality evaluation, and useful-memory
feedback based on independent evidence remain open. Session inbox routing is a separate proposed
O5 capability, not an implemented feature.

The user assigned visual refinement to Claude first, followed by Codex review. Target: brighter
dark surfaces, restrained luminous supercomputer/quantum-computer character, Apple-like glass,
and shining dark space without decorative stars. Preserve real task status, memory disclosure,
readability, accessibility and reduced motion. Avoid concurrent edits to the observer template
and generated demo while Claude owns this pass.

A separate Claude session is currently working on Brain. The user requested that the full
requirements be documented and delivered to that existing session. A shared workspace design
brief and wiki mirror were created, and the targeted session's input queue received the brief.
The targeted Claude session acknowledged the brief and began comparing the proposed contract
with its current server implementation. Interface review is not implementation completion. Brain work continues independently; this does not authorise
conflicting edits to Orbit or claim the integration has shipped.

## Live workflow correction (29 October, in progress)

The deployed synthetic demo is not evidence of live orchestration. User acceptance requires actual
session/layer traffic and a visible plan-to-result lifecycle in both CLI and browser. Arbitrary
circulating light has been removed. `telemetry.py` now projects real native tool lifecycle events
and public messages into the durable event store while providers are running. Check execution also
emits start/finish events. Recorded events drive per-session layer pulses; idle sessions stay still.
A real research run yielded 36 observed tool-signal frames with no browser exception after fixing
a negative animation-time index. This is observation coverage, not a performance benchmark.

`planning.py` records an execution plan before new development work. Default execution continues;
`--plan-first` or the browser review checkbox opts into a review hold. `plan TASK_ID --auto`
plans and executes in the terminal. New task UI requires a clean Git repository and fixed command
checks; it does not yet support repository-free research intake. Actual form submission exposed
an absent options dictionary, now fixed; isolated UI creation and detail opening were verified.

`--public-web` enables native public research explicitly. `results.py` publishes a bounded plain-text
snapshot of a selected workspace report to CLI `result` and the task inspector. Active task cards
show actual recent provider activity rather than the previous report summary. Results do not
require opening an editor. The original research goal is preserved separately from later feedback.

Still incomplete: per-plan-step completion evidence/scheduling, a general research workflow,
peer telemetry, burst event cursor catch-up, and combined Brain remember/ACK/retry/scope validation.
Do not label the harness complete or financial hypotheses validated merely because fixed checks pass.
Private operational task IDs, user data and current research outcomes live in the local collaboration
tracker, not in the public demo. No commit, push or deployment was performed for these changes.

A subsequent correction adds bounded continuation: native responses now report `next_action` and
`remaining`, persisted as `objective_progress`. After fixed checks pass, a feasible next action
schedules another native turn within the existing attempt budget. Budget exhaustion preserves the
pending action and is not completion. A private executable probe verified two-turn continuation,
passing partial checks, bounded stopping and source isolation. This does not yet implement independent
per-plan-step completion judgments or automatic recovery from interrupted native calls. The view
also distinguishes results awaiting review from tasks requiring input, and shows current-call elapsed
and remaining timeout while execution is active.

## 하네스 뷰 재구성

사용자 기준은 터미널의 동일 세션을 따라보며 하네스 내부 처리를 이해하는 화면이다.
`operations.py`가 단계·상태·이유·다음 행동을 투영하고 뷰, watch, status가 공유한다.
상세는 현재 처리와 공개 응답을 먼저 보여주며 긴 보고서와 기술 정보는 접는다.
레이어별 마지막 기록은 전체 이벤트에서 조회하고, 인증된 `/events`에서 이전 흐름을
100개씩 불러온다. 도구 호출/응답 쌍의 소요 시간과 표시/전체 기록 수를 보여준다.
실행 시도·컨텍스트·기억·검사 상태는 상단 스트립으로 확인한다. 표시를 위해 가짜
단계 완료나 데이터 이동을 생성하지 않는다. 이 변경으로 실행 스케줄러 자체의
남은 요구가 완료되는 것은 아니다.

## 유휴 엔진·세션 점검과 맥동

`health.py`는 엔진의 로컬 버전 응답, 작업 공간, 해당 세션 파일 메타데이터를
백그라운드에서 확인한다. 30초 캐시·45초 유효기간이며 모델이나 원격 API를
호출하지 않는다. ready는 로컬 대기 준비 근거이고 로그인/원격 연결/재개 성공을
보장하지 않는다. 뷰는 연결됨·유효한 ready·해당 세션 유휴 조건에서만 옅은 초록
맥동을 표시한다. 연결 끊김·점검 만료·동작 줄이기에서는 움직임을 멈춘다.
실제 화면에서 각 조건을 스크린샷으로 대조했고 오류가 없었다. 상태와 무관하던
lumen-drift 배경 회전은 제거하고 정적 글래스 표현을 유지했다.

## 2026-10-29 관리자 루프 점검

목표 대비 별도 관리자 평가와 후속 실행을 추가했다. 검사 통과만으로 목표 완료로 판단하지 않는다. 구현·실측·미해결 범위는 [관리자 기능 감사](../audits/2026-10-29-runtime-management.md)를 읽는다.
