# Cosmos Orbit 관리자 기능 점검 및 보완

> Status: **구현·실증 진행** — 2026-10-29
> Owner: Sean / Codex

## 판단 기준

Orbit는 터미널 실행을 표시하는 장식이 아니라, 사용자 목적을 보존하고 결론에 도달할 때까지 계획·실행·근거 확인·방법 변경을 관리하는 시스템이다. 컨텍스트와 토큰을 관리하고 Brain은 선택적으로 기억·리콜을 제공한다. Codex와 Claude는 각각 단독 사용이 가능해야 한다.

## 발견한 결함과 이번 수정

1. 실행 도중 feedback 수신 시 계획을 superseded로 만들면서 같은 루프에서 다음 실행을 바로 호출했다. 격리 재현에서 `executor → executor`, `plan_requested=true`를 확인했다. 루프 경계에서도 계획 요청을 확인해 `executor → planner → 다음 executor`로 수정했다.
2. 고정 검사 통과와 실행자의 빈 next_action만으로 needs_review가 됐다. 전체 목적/계획 acceptance를 별도로 평가하는 단계가 없었다.
3. 인계 패킷에 mission_goal, 실행 계획, 남은 일과 관리자 평가가 빠져 있었다. 현재 요청 본문에는 일부 정보가 있었지만 패킷 자체의 보존성이 부족했다. 이 필드를 추가했다.
4. 기존 needs_review의 “결과 준비” 문구는 평가 완료를 암시했다. “검토 대기”로 바꾸고 관리자 평가 유무를 명시했다.

## 관리자 루프

개발 작업의 실행 → 원래 고정 검사 → 별도 관리자 평가 → 후속 실행 또는 결론/필수 입력 대기.

- 실행자와 같은 선택 엔진을 사용하되, 매번 **별도 새 컨텍스트와 도구 없는 호출**로 평가한다. 다른 공급자를 암묵적으로 호출하지 않는다.
- 원래 목적, 누적 사용자 수정, 계획 acceptance 각각에 R ID를 부여한다. 후속 수정이 앞선 요구와 충돌하면 최신 사용자 지시를 기준으로 판정 근거를 설명한다.
- 실행자 요약만으로 평가하지 않는다. 현재 변경 파일 발췌, 보고서, 실제 고정 검사 출력, 이전 관리자 평가를 제공한다.
- `continue`: 부족한 근거, 다음 행동, 방법을 저장해 같은 루프에서 실행한다.
- `concluded`: 전 요구사항 met, 항목별 근거, 빈 gaps/next_action, 현재 파일과 일치하는 고정 검사 통과를 런타임이 확인한 뒤 `needs_review`로 전달한다. 사용자 검토/머지와는 별개다.
- `needs_input`: 필수 질문을 저장한다. 실행할 다음 행동이 남아 있는 입력 대기 판정은 거부한다.
- 같은 파일 상태에 같은 next_action이 반복되면 blocked. 실행 시도 한도에 도달해도 남은 일을 보존하고 blocked. 완료로 바꾸지 않는다.
- 관리자 응답의 항목 누락/잘못된 완료 판정은 interrupted로 명시하며 완료 처리하지 않는다.
- 실행자가 blocker를 보고해도 바로 끝내지 않고 고정 검사와 관리자 평가에서 대안을 검토한다.
- 수익이 없다는 근거 있는 연구 결론은 유효하다. 긍정적인 수익을 얻을 때까지 탐색하라는 시스템이 아니다.

`evaluation_started`, `evaluation_finished`, `continuation_scheduled` 이벤트를 영속 저장한다. CLI status/watch와 브라우저는 같은 상태를 사용한다. 뷰는 실행자/관리자 세션, 평가 단계, 항목별 근거, 다음 작업과 필수 질문을 보여준다. 기존 결과는 `cosmos-orbit assess TASK_ID` 또는 뷰의 “관리자 평가 후 이어가기”로 평가하며 고정 검사를 다시 실행한다. 기존 기록을 평가된 기록으로 일괄 덮어쓰지 않는다.

## 검증 근거

저장소의 테스트 미작성 방침에 따라 재현 프로그램은 `/tmp`에서 실행했다.

| 검사 | 결과 |
|---|---|
| 실행 중 feedback 재계획 우회 재현 및 수정 | 수정 전 실패, 수정 후 executor→planner→executor 통과 |
| 관리자가 실행자의 완료 주장을 거부하고 다음 실행 지시 | 격리 응답 주입: executor→manager→executor→manager 통과 |
| 필수 입력 대기, 근거 없는 완료 거부, 정체 감지, 시도 예산 | 격리 응답 주입 4경로 통과 |
| 임계값 인계에 전체 목표/계획/남은 작업 전달, 구 세션 ID 거부 | 격리 강제 임계값 통과 |
| 실제 Codex 수정→고정 검사→별도 관리자 평가 | `orbit-live-twuc6034`, concluded/needs_review. calc.py 내용과 2개 합산 검사를 실제 대조 |
| 실제 Claude 수정→고정 검사→별도 관리자 평가 | `orbit-live-jl85wsve`, concluded/needs_review. 실제 파일 내용과 인터페이스·고정 검사 근거 대조 |
| 실제 Codex의 기존 결과 assess | `orbit-live-0fziiejg`, 관리자 항목별 평가 저장 확인 |
| 기존 하이닉스 결과 assess | 최초 평가 continue(10개 기준: met 4/unmet 2/unknown 4). 실행 시도 2 자동 시작 후 실제 보완 실행·고정 검사 종료, 두 번째 관리자 평가 진행 |
| 실제 Brain MCP 격리 결합 | 저장/new, noop, CAS conflict/update, 사람 메모리 보호, scope 리콜 격리, Orbit outbox ACK 유실 재시도 통과 |
| 기억·telemetry 회귀 | 선택적 기억 off/legacy/failure fallback, 요청 기반 recall, write-tool 거부, 실제 스트림 start/finish 투영 통과 |
| 브라우저 실제 상태 | 평가 중 관리자 running/실행자 idle, 후속 실행 중 반대 상태, JS 오류 0. Claude 독립 대조도 저장소 3개 작업 상태 일치·평가 10개 항목 표시 확인 |

실제 Brain 검증 경로는 `~/.claude/brain/experiments/2026-10-29-orbit-sandbox`로 한정했다. ACK의 `stored=true`, `indexed_words=true`, `embedded=false`를 확인했다. 임베딩/벡터 검색 성능을 검증한 결과는 아니다.

## 아직 충족하지 못한 관리자 기능

이 변경을 전체 하네스 완성으로 판정하지 않는다.

- **목표 전체 누적 토큰/비용 예산**: 아직 강제하지 않는다. 현재 context_budget은 세션 인계 임계값이며 지출 한도가 아니다. max_attempts는 실행 시도 한도이며 관리자 평가 호출과 전체 토큰 예산을 대체하지 않는다.
- **컨텍스트**: 이전 성공 호출의 측정값으로 다음 호출 전에 인계한다. 현재 프롬프트의 사전 토큰 추정/출력 예약이 없고 전체 feedback가 계속 들어간다. 관리자 파일 증거는 최대 20개, 파일당 8000자, 합계 48000자로 제한하지만 목표/feedback의 전체 길이를 토큰 예산으로 통제하지 않는다.
- **중단 사용량**: timeout/프로세스 실패 호출은 완료 metadata를 반환하지 못한다. 현재 누적 사용량 표시가 완전한 청구 토큰 원장인 것은 아니다.
- **평가의 신뢰도**: 구조/항목 대응/고정 검사/파일 fingerprint는 런타임이 확인하지만, 서술 근거가 목표를 충분히 입증하는지는 모델의 판단이다. 독립 평가 호출도 같은 모델의 오류 가능성을 없애지 않는다. 발췌 밖의 자료를 확인했다고 주장하면 안 된다.
- **복구**: native 실행 timeout은 보수적으로 interrupted를 남기고 자동 재실행하지 않는다. 실행 중 외부 효과 여부를 구분해 복구하는 감독자는 아직 없다.
- **범위**: 이번 관리자 루프는 development 경로에 적용했다. 기존 제한된 artifact 경로까지 동일하게 확장한 것은 아니다.
- **언어**: 요청 언어를 따르도록 지시하지만 출력 언어의 강제 검증은 없다. 기존 Claude 실사에서 일본어 계획이 나온 이력을 해결 완료로 표시하지 않는다.
- **하이닉스**: 공개 자료 제약 및 권리조정/가격 세션/거래원 역사 자료 문제는 남아 있다. 수익 우위를 확보했다고 판정하지 않는다.

다음 우선순위는 누적 사용량의 관측 완전성 표시와 목표 예산 → 큰 컨텍스트의 요약/근거 재조회 → 중단 복구 정책 → 실제 장기 작업에서 관리자 오판/불필요 반복 측정이다.

## 관련

- 구현: `harness/manager.py`, `harness/development.py`, `harness/context.py`
- 공동 작업 상태: `/Users/sean/development/projects/docs/domains/local-brain/tracker.md`
- 위키: `/Users/sean/sean_wikis/01 Projects/local-harness/Runtime management audit 2026-10-29.md`

## 후속 사용자 결정: 시간 제한 폐지와 무출력 관측

정상 동작을 시간 경과만으로 종료하지 않는다. 실행·계획·관리자 평가·고정 검사에서 기존 시간 한도를 제거했다. 기존 --timeout 값은 호환 입력으로만 남고 종료 조건으로 쓰지 않는다. 이전 1200초 제한으로 중단된 하이닉스는 과거 중단 기록이며 자동 재실행하지 않았다.

실행 소유자는 5초마다 실제 프로세스 poll, OS 상태, 프로세스 그룹 CPU, stdout/stderr 크기 변화, TCP 상태를 관측한다. task.process_health와 runtime.process_health를 터미널/status/뷰에서 공유한다. 무출력은 alive_silent이며 장애 판정이 아니다. OS 정지는 process_stopped, 실제 종료는 종료 코드로 기록한다. 소유자가 없거나 15초 이상 관측이 낡거나 새 호출에 이전 표본만 남으면 not_current로 표시한다. 원격 모델의 응답/추론 진행은 관측할 근거가 없으면 unverified다. TCP ESTABLISHED도 모델 정상의 증거로 확대하지 않는다.

진단 표본은 작업 진척 이벤트·빛의 데이터 이동으로 취급하지 않는다. 작업 흐름과 푸터에서 제외하고 CLI events 원장에는 남긴다. 실패 원문은 failure.detail에 보존하고 사용자 상태 문장은 짧게 분리했다. 짧은 ps/lsof 진단 제한은 진단을 보호할 뿐 작업을 종료하지 않는다. 기존 MCP 전송 오류 처리와 실행 시도 한도는 별도이며, 단계의 경과시간 한도와 다르다.

검증: 실제 무출력 subprocess가 이전 1초 한도를 넘어 6초 후 정상 반환; 5초 생존 표본 확인; exit 9 오류 반환; SIGSTOP/SIGCONT 관측 구별; 이전 호출·실행자 없는 표본의 freshness 무효화; 고정 검사 1.4초 실행이 기존 1초 설정에도 통과. 실제 격리 관측 서버(8789) 브라우저에서 PID·무출력·OS·CPU·원격 미확인 표시, 카운트다운 없음, JS 오류 0 확인. 현재 실사용 워커/8788 서버에 적용했다.


## 2026-10-30 completion-management repair

A four-arm live development comparison exposed unnecessary executor loops and invalid manager
verdicts after passing fixed checks. This change separates planner suggestions from binding user
goals/corrections, supplies recorded Git changed paths plus bounded native tool evidence, and gives
planning/execution/evaluation the actual native tool capabilities. Claude remains file-tool-only;
this change does not silently broaden permissions or claim full interactive CLI compatibility.

A rejected manager assessment is recorded and gets one fresh assessment against the same evidence
and explicit validation error. Implementation is not replayed for a response-contract error.
Completion still requires all user criteria met with evidence, empty gaps/action/question, passing
fixed checks and the same verified tree. A second invalid response remains interrupted.

Private controlled probes cover contradictory verdicts, missing criteria, failed/stale checks,
repair without executor replay, tool evidence excluding reasoning, and capability reporting.
Fresh live verification repeats the original two fixtures with both providers, including planning.
Results are recorded after those runs finish; prior benchmark failures remain preserved.

The first repair validation exposed an implementation error: `Store.save` replaces `task.data`,
so a stale local reference could record an evaluation event without persisting the evaluation
in the task body. That run was stopped on this concrete fault, not on elapsed time, and retained
as failed validation. The implementation now refreshes the reference after saving call metadata.
The private probe reloads the task from SQLite and checks both evaluation and objective progress;
it also verifies that a second invalid manager response interrupts after exactly two calls.

Final live verification: all four runs reached persisted concluded/needs_review with one implementation and three CLI calls each. CLI projections confirmed current evidence. Extra boundary checks still failed two Decimal cases in Claude CSV. A separate recovery fixture included those failures in fixed checks: failed checks -> continue -> one native repair -> passing checks -> concluded (143.157 seconds). This validates evidence-driven repair, not autonomous discovery of all missing cases. The idle local worker was refreshed; interrupted research was preserved.
