---
id: G-096
title: Structured Worker Decision Blocking
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: high
risk: high
priority: critical
---

## Purpose

Worker가 사용자 결정이나 Architecture 승인이 필요해 구현을 중단했는데도, 일반적인 commit 제목·Summary·targeted test 보고 형식을 갖추면 Manager가 이를 검증할 완료 후보로 처리한다. G-088 조사에서는 승인 대기 기록과 테스트만 변경한 결과가 전체 verify와 Review로 넘어갔고, 같은 `loop_manager.py::run_agent::missing-write-isolation` 문제가 반복됐다.

PRODUCT의 현재 최종 사용자 조치 차단 규칙을 Worker 결과 처리에 연결한다. 사용자 결정이 필요한 결과를 완료 후보와 명시적으로 구분하고, 작업물·session·실패 이력을 보존한 채 검증 전에 `PLAN_BLOCKED`로 전이한다. 같은 입력에서 반복 실행해도 Worker·전체 verify·Review가 다시 시작되지 않아야 한다.

## Acceptance Criteria

- Codex·Claude 공통 Worker 결과에 `ready_for_validation`과 `needs_user_decision`을 구분하는 구조화된 계약을 제공한다. 사용자 결정 대기에는 결정이 필요한 변경, 근거 문서의 경로·절과 사용자가 답해야 할 질문을 포함한다. 본문의 `승인`, `중단`, `BLOCKED` 같은 단어나 과거 Decision Log 기록만으로 실행 상태를 추정하지 않는다.
- 정상 CLI 결과와 Worker workspace 범위·소유권 검증을 통과한 `needs_user_decision`은 일반 완료 결과의 `COMMIT_TITLE`, `PR_SUMMARY`, `TARGETED_TESTS` 검증보다 먼저 처리한다. 구현을 완료하지 못한 결정 대기에 성공한 targeted test나 commit 제목을 만들도록 요구하지 않는다. 코드·테스트가 이미 변경됐거나 ExecPlan 기록만 변경됐거나 파일 변경이 없는 경우를 모두 처리한다.
- 결정 대기는 기존 `RecoveryReason.USER_DECISION`과 `BlockReasonCode.USER_DECISION_REQUIRED`를 사용해 해당 ExecPlan만 최종 차단한다. 차단 원인·질문·근거 위치, 원래 Worker 결과, session·binding, branch·worktree 소유권, 실행 전·결과 scope 증거와 기존 verify·Review 실패 이력을 일관되게 보존한다. Worker가 SQLite를 직접 수정하지 않는다.
- 일반 Worker, verify·Review 교정 Worker, rebase 교정 Worker 및 중단 뒤 저장 결과를 이어 처리하는 경로에 같은 판정을 적용한다. 결정 대기 결과는 완료 후보 commit, rebase stage·continue·skip, 전체 verify, Reviewer 호출과 PR 생성 단계로 넘어가지 않는다. 최종 차단 이후의 기존 안전한 remote checkpoint dispatcher는 그대로 사용하며, 미해결 rebase 등 게시가 불가능한 경우에는 local-only 사유와 작업물을 보존한다.
- 같은 ExecPlan·관련 입력으로 외부 실행을 최소 3회 반복하고 프로세스를 재시작해도 차단과 작업물·session·실패 이력이 유지된다. Worker가 이번 turn에 쓴 Decision Log나 checkpoint commit, 단순 원격 base 전진만으로 자기 차단을 해제하지 않는다. 기존 최종 차단 transaction과 checkpoint 결과에 따라 같은 사건의 BLOCKED 알림을 중복 생성하지 않는다.
- 일반 사용자 결정 대기의 해제는 현재 PRODUCT의 계획·sources 입력 변경 및 안전한 workspace 복구 정책을 따른다. 차단 해제 기준은 Worker 시작 때 읽은 원격 계획·sources에 결합하고, 이번 Worker의 승인 대기 기록을 새 사용자 입력으로 취급하지 않는다. 유효한 관련 입력 변경 뒤 같은 작업물을 복구해 Worker가 결정을 다시 확인하게 하며, 입력 변경 자체를 승인으로 해석하지 않는다. 기존 정상적인 차단 해제의 재시도·Worker level 초기화 규칙을 유지한다.
- 새 Worker 호출에서는 결과 구분 계약을 명시한다. 기존에 저장된 정상 완료 결과는 기존 publication 계약으로 읽을 수 있게 하고, 과거 자유 형식 승인 대기 문구를 자동 승인이나 구조화된 차단 근거로 변환하지 않는다. 기존 session을 다시 호출할 때는 새 결과 계약을 전달해 현재의 결정 대기 여부를 명시적으로 보고받는다.
- 잘못된 JSON, 중복·충돌하는 결과 선언, 알려지지 않은 kind, 빈 질문, 근거 문서 밖의 위치는 기존 bounded Worker protocol correction으로 처리한다. CLI timeout·서비스 오류·사용 한도, workspace 위반, guidance 제안과 Reviewer `SPEC_BLOCKED`의 기존 disposition·scope·재시도 규칙을 유지한다. 유효한 사용자 결정 대기를 일반 protocol 오류나 Review 실패로 누적하지 않는다.
- `ready_for_validation`은 기존 targeted test 계약, 최신 base 반영, 전체 verify, 읽기 전용 Review와 publication 순서를 따른다. 전체 verify 성공으로 Review 실패 이력을 지우거나 모델 승격으로 동일 Review 지문의 한도를 초기화하지 않는다. 변경 없는 branch를 차단하는 기존 gate도 유지한다.

## Implementation

1. `worker_outcome.py`에 provider와 SQLite에 의존하지 않는 결과 모델·parser를 둔다. `loop_manager.py`의 `WorkerPublication` 및 `parse_worker_result`와 책임을 분리해, 완료 후보의 publication metadata 검증과 사용자 결정 대기의 검증이 서로 성공 조건을 요구하지 않도록 한다.
2. 아래처럼 한 줄 JSON 선언을 사용한다. `needs_user_decision`의 `locations`는 선택된 ExecPlan, 해당 sources, PRODUCT 및 ARCHITECTURE의 실제 경로·절로 제한한다. 경로 traversal·절 누락·과도한 payload와 비밀값 노출을 거부하거나 기존 redaction 경계에서 처리한다. 근거 위치는 사용자 수정 대상을 안내하는 정보이며 승인 증거가 아니다.

   ```text
   WORKER_OUTCOME: {"kind":"ready_for_validation"}
   COMMIT_TITLE: 실제 변경 제목
   PR_SUMMARY: 실제 변경 요약
   TARGETED_TESTS:
   최소 targeted unit test 결과 또는 구체적인 미실행 사유
   ```

   ```text
   WORKER_OUTCOME: {"kind":"needs_user_decision","problem":"기존 Architecture에서 승인되지 않은 실행 경계 변경이 필요함","proposal":"현재 작업에 필요한 변경과 영향","locations":[{"path":"docs/ARCHITECTURE.md","section":"Architecture Change Policy"}],"question":"이 변경을 허용하는 설계 결정을 근거 문서에 반영해야 함"}
   ```

3. `worker_prompt`의 최초·재개·session replacement 결과 지시에 이 계약을 연결한다. `G-066`의 prompt 축약 작업과 공유할 계약은 새 모듈의 한 formatter에서 제공해 서로 다른 결과 형식이 생기지 않게 한다. 기존 provider 설정이나 세션 교체 정책을 변경하지 않는다.
4. `execution_handlers.py`의 일반 Worker 결과와 저장된 rebase Worker 결과 처리에 공통 outcome 판정을 넣는다. 기존 workspace·metadata·guidance 안전 검사 뒤, publication parser와 `_finish_worker` 전에 사용자 결정 대기를 최종 차단 경로로 전달한다. 저장 결과 재개 시에도 먼저 판정해 완료 단계가 재생되지 않게 한다.
5. `recovery_runtime.py`, `recovery_state.py`, `runtime_state.py`와 기존 block facade를 이용해 사용자 조치 판정, 증거 보존 및 해당 계획의 실행 투영 정리를 한 transaction으로 처리한다. Agent scope의 clear 순서 때문에 실행 역할이나 snapshot이 사라지지 않게 한다. 새 책임을 `loop_manager.py`의 결과 parser에 누적하지 않는다.
6. `block_active_execplan`과 `resume_blocked_execplan_if_inputs_changed`의 일반 사용자 결정 처리에서 Worker 시작 원격 입력과 차단 해제 기준의 관계를 확인한다. 현재 turn의 ExecPlan 변경과 원격 입력이 다른 상태에서도 동일 입력 유지가 정확히 판정되게 한다. 필요한 저장은 호환 가능한 추가만 사용하고, 차단 해제는 기존 안전 검사와 입력 변경 규칙을 유지한다.
7. `notifications.py` 및 remote checkpoint는 기존 최종 차단 경로를 사용한다. 질문·제안·근거 위치가 전달되고 같은 사건이 중복되지 않는지 확인하며, 다른 계획·planning 상태의 투영과 기존 알림 감사 이력을 보존한다. durable 사용자 대상 정보가 필요하면 기존 recovery evidence에 구조화해 연결한다.

## Validation

- Worker는 새 `tests/test_worker_outcome.py`와 직접 변경한 `tests/test_execution_handlers.py`, `tests/test_recovery_state.py`, `tests/test_scoped_blocking.py`, `tests/test_runtime_state.py`, `tests/test_notifications.py`, `tests/test_loop_manager.py`의 관련 개별 case 또는 test class만 targeted unit test로 실행한다. 기존 광범위 모듈 전체를 실행하기보다 결과 parser·차단·재개에 대응하는 최소 범위를 선택한다.
- parser에서 정상 완료·사용자 결정 대기, 기존 완료 결과 호환, 중복 선언·잘못된 kind·JSON·근거 위치, 자유 형식의 과거 승인 문구와 현재 구조화된 결과의 구분을 검증한다.
- G-088과 같은 `Review FAIL → Worker가 승인 대기 보고` 시나리오에서 코드·테스트 변경 유무와 관계없이 즉시 `user_decision_required`가 저장되고 publication parser, 완료 후보 commit, 전체 verify와 Reviewer 호출이 발생하지 않는지 검증한다. verify·Review 실패 이력과 effective Worker level이 차단 기록 중 임의로 초기화되지 않아야 한다.
- 일반·rebase·저장 결과 재개에서 같은 판정을 검증한다. 미해결 rebase의 index·작업 파일·checkpoint와 Worker session을 보존하고 stage·continue·skip이 실행되지 않아야 한다. checkpoint 게시 불가 사유는 현재 정책대로 기록한다.
- Worker가 Decision Log를 변경한 상태에서 차단한 뒤 같은 원격 입력으로 3회 이상 반복하고 DB connection을 닫아 다시 열어도 Worker·전체 verify·Review 무호출, 상태·작업물·session 보존과 동일 사건 알림 중복 방지가 유지돼야 한다. 최종 차단 저장 뒤·checkpoint 뒤·알림 기록 경계의 중단도 포함한다.
- 사용자에 의한 유효한 계획·sources 입력 변경 뒤에는 소유권 검사와 기존 복구를 거쳐 Worker를 재개한다. 입력은 바뀌었지만 결정이 여전히 없으면 새 완료 후보나 PR 대신 다시 명시적인 사용자 결정 대기로 처리하는지 확인한다. 다른 차단 계획과 보존된 planning state는 변경하지 않아야 한다.
- 기존 guidance 승인, Reviewer `SPEC_BLOCKED`, CLI 일시 오류·사용 한도, Worker 결과 오류, workspace 위반 및 정상 완료 후보의 전체 verify·Review 경로를 targeted 회귀 case로 보존한다. 동일 Review 지문은 최초 실패와 3회 재발에서 차단되고 모델 단계 상승이나 verify 성공으로 한도가 사라지지 않아야 한다.
- Worker는 새 모듈과 테스트에 맞춰 현재 환경의 전체 검증 스크립트를 유지한다. 전체 verify는 Loop Manager가 실행하며 기존 provider invocation·scope·rebase·checkpoint·알림 및 최종 revision 검증 범위가 약화되지 않아야 한다.

## Progress

- [ ] Worker outcome 계약과 순수 parser·formatter 작성
- [ ] 일반·rebase·저장 결과 처리의 검증 전 사용자 결정 차단 연결
- [ ] 원격 입력 기준·scope·session·실패 이력 및 알림 보존 확인
- [ ] 반복 실행·중단·정상 재개와 기존 disposition 회귀 검증

## Decision Log

- 2026-09-27: 사용자의 G-088 반복 원인 조사 및 재발 방지 계획 초안 요청을 근거로 작성했다. 현재 PRODUCT의 최종 사용자 조치 차단과 검증 책임을 구현하는 결함 수정이며, 새 Current Goal이나 Requirement를 추가하지 않는다.
- 조사 근거: 2026-09-26~27의 SQLite `validation_results`에는 같은 `missing-write-isolation` Review 실패가 7회 남아 있다. 첫 주기의 네 번째 실패는 실제로 `review_retry_exhausted`로 차단됐고, 관련 입력 변경·복구 후 두 번째 주기가 시작됐다. 재시도 상한 부재를 원인으로 취급하지 않는다.
- 직접 누락: `execution_handlers.py`는 guidance 제안만 별도 승인 대기로 처리하고, 다른 사용자 결정 대기 결과도 일반 publication parser를 통과하면 `VERIFYING`으로 넘긴다. 이 경계에 기존 `user_decision_required` 원인을 연결한다.
- `depends_on`은 비워 둔다. 이 기능은 현재 Merge된 Worker·recovery·block 경계에서 구현할 수 있고, G-088의 완료를 기다려야 할 근거가 없다. G-088의 실제 쓰기 격리 구현과 G-093~G-095의 planning 보존은 각각의 기존 계획 범위를 따른다.
- G-066과 결과 prompt·session 경계가 겹친다. 구현 시 최신 Merge 구조에 adapter만 연결하고 계약 정의는 새 모듈로 모은다. 기존 G-066 문서를 수정하거나 snapshot 없이 의존성을 추가하지 않는다.
- 사용자 결정 대기에는 현재 입력 변경 기반 해제 정책을 사용한다. 모든 승인 종류에 새로운 `User Decision`·patch hash 승인 저장소를 도입하거나 단순 문서 변경의 재개를 금지하는 정책은 이 계획에서 확정하지 않는다.
- 동일 코드·동일 Review issue의 더 이른 무진행 차단, Worker turn 사이의 전체 verify 결과 재사용, 문서만 바뀐 경우의 verify 생략과 재시도 횟수 변경은 현재 제품의 검증·재시도 의미를 바꿀 수 있다. 필요하면 별도 사용자 결정과 Requirement에서 시작해야 하며 이 계획의 구현 항목으로 승격하지 않는다.
- G-088이 요구하는 실제 provider 격리의 성공·거부 검증은 해당 계획의 Acceptance Criteria와 Validation을 따른다. 모든 호출을 거부하는 구현이나 mocked unit test 성공을 이 계획의 결과 처리 수정으로 제품 완료 처리하지 않는다.
- 비호환 SQLite 상태 변경, Git·인증·비밀값 경계 변경 또는 새로운 infrastructure가 필요하다고 확인되면 Architecture Change Policy에 따라 그 구체적인 변경을 사용자 결정 대상으로 남기고 구현 전에 차단한다.
