---
id: G-310
title: 공통 결함 근거의 planning 인계와 검증 후 재개
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
depends_on:
  - G-301
  - G-303
  - G-319
complexity: high
risk: high
priority: high
---

## Purpose

선택 작업 밖의 공통 결함 보고를 원래 작업의 실패·후보·질문과 함께 보존해, 허용된 일반 planning에서 하나의 교정 목표로 검토하고 실제 검증·Merge 뒤 영향 작업만 재평가하도록 연결한다. 기존 명세로 정해진 구현 교정을 사용자 정책 질문으로 바꾸거나 각 Worker에게 범위 밖 수정을 요구하지 않는다. 일반 실패 fingerprint·피드백·질문 의미 재평가·planning 게시를 재구현하지 않는다.

현재 `worker_outcome.py`는 완료 후보와 `needs_user_decision`만 구별하고 Worker handler는 후자를 바로 사용자 판단 차단으로 연결한다. verify·Review handler는 `ValidationFeedbackRepository`에 결과를 남기고 기존 교정/소진 정책을 적용한다. 기록은 작업별이며 여러 보고의 확인된 공통 원인·교정 계획·검증된 반영·영향 작업을 연결해 일반 Planner에 전달하는 경계가 없다. `checkpoint_recovery._assess_user_decision_cause()`의 현재 명세 인용·동일 입력 재사용·원격 재확인·CAS를 재사용한다. G-301의 선택 문서 조회·G-303의 DB 처리와 G-319의 확정된 개정/관련 명세에 따른 원인 재평가를 선행 소비하며, 공통 결함의 원인·교정 계획·실제 Merge/검증 근거 연결만 이 작업에서 보완한다.

## Acceptance Criteria

- R-055: Worker·Manager verify·Reviewer에서 발견한 범위 밖 공통 결함의 보고를 실제 invocation/result·작업 ID·선택 범위·후보/원격 revision·진단 위치·원래 실패/질문에 연결해 보존한다. Worker는 선택 범위 밖 코드·Requirement·다른 ExecPlan을 수정하지 않는다. 보고만으로 공통 원인·사용자 결정·검증 성공을 확정하지 않는다.
- 서로 다른 두 작업의 같은 증상은 실제 코드·검증 근거와 적용 명세를 대조해 원인이 확인된 경우만 같은 교정 목표에 연결한다. fingerprint 일치·같은 timeout·유사 문구만으로 원인을 합치지 않는다. 다른 원인·미확정 원인은 별도로 보존하고, 이미 같은 source·원인·완료 조건을 맡은 유효한 계획이 있으면 재사용한다. 미완료 계획·draft·미Merge PR과도 중복 생성하지 않는다.
- R-082: 보존 근거는 명세 변경·기존 복구/완료 재평가 등 현재 허용된 일반 planning 입력에만 포함한다. 결함 보고·코드·ExecPlan 변경 자체로 새 Planner 호출 조건이나 polling을 만들지 않는다. 현재 source와 교정 소유 범위가 유효한 목표만 허용 snapshot의 개정/새 계획으로 반영하고, 게시·catalog·DB 적용이 확정되기 전에는 교정 계획이 적용됐다고 기록하지 않는다. G-318/G-319의 일반 차단 계획 개정·관련 간접 Requirement 입력을 소비하며, 문서 개정과 공통 교정의 실제 Merge/검증 반영을 구분한다. planning 실패·중단·재처리에서도 근거/연결과 원래 작업을 보존한다.
- 기존 명세가 실행 경계·검증 정책을 이미 정한 경우 구현 교정과 작업 분리는 사용자 정책 판단을 요구하지 않는다. 실제 문서 충돌·남은 제품 선택·변경 권한/보존 의미의 불확실성은 해당 질문을 유지한다. 이미 저장된 사용자 질문·draft·User Decision과 사용자 승인/거절 원문은 작성·삭제·대체하지 않는다.
- 공통 교정의 실제 Merge와 필요한 검증 근거가 확인된 뒤에만 연결된 작업의 현재 원격·명세·후보·원래 원인을 재평가한다. `user_decision_required`는 현재 상위 명세가 구체적 선택을 해소한 근거가 있어야 해제 가능하며, 계획 게시·질문 삭제·교정 Merge만으로 승인되지 않는다. 다른 승인·소유권·종료 불명·원격 삭제 등 독립 차단은 유지한다. 미Merge·검증 불명·원인 미해소는 계속 보류한다.
- 기존 Worker 작업물·branch·worktree·session·실패/검증 이력·checkpoint와 이전 질문/평가 감사를 유지한다. 실제 후보 변화에 필요한 verify·Review를 거쳐 기존 단계에서 재개하며 새 READY 작업으로 초기화하거나 과거 실패를 성공으로 덮지 않는다. 동일 근거의 무진전 재호출·중복 계획/질문/알림을 억제하고 독립 작업·다른 프로젝트는 기존 한도와 순서로 진행한다.
- R-091의 비차단 실패/실행별 retry 묶음은 G-297/G-313의 재시도 대기 뒤 같은 실행 재개를 소비한다. 이 인계가 예산 소진을 사용자 판단/최종 차단으로 승격하거나 다음 외부 실행을 기다리는 추가 장벽을 만들지 않는다. 실제 정책 질문·공통 교정 미반영·필수 검증/종료 불명 등 미충족 조건은 보존한다. 새 runtime의 안전 재시작도 원래 공통 결함/질문·실패·계획 연결을 초기화하지 않는다.

## Implementation

- [x] 확인한 `execution/worker_outcome.py`·`execution_worker_handler.py`의 결과 해석과 `execution_verify_handler.py`·`execution_review_handler.py`의 저장/교정 경계를 대조해 공통 결함 보고를 전달한다. 구조화된 인계가 필요하면 기존 결과 계약에 호환되는 제한된 보고를 추가하고 저장된 완료/사용자 질문의 해석은 유지한다. 보고와 근거 확정·교정 계획 채택을 분리하며, Worker가 정해진 구현 교정의 분리 여부를 사용자에게 선택시키는 경로를 일반 인계로 처리한다. 기존 진짜 사용자 결정과 일반 교정 한도는 우회하지 않는다.
- [x] 확인한 `storage/validation_feedback_state.py`의 불변 `validation_results`·피드백 이벤트와 `storage/execution_checkpoint_state.py`, `execution/validation_feedback.py`의 진단/비밀값 정제를 재사용한다. 작업별 보고·후보·원래 질문·원인 확인 근거·계획 연결·재평가 소비를 중단 후에도 대조할 수 있게 저장한다. 필요한 호환 추가 저장만 기존 storage 경계에 두고 원본 감사 수정·삭제나 새 운영 DB/범용 incident 시스템은 만들지 않는다. 파일/Git/모델 관측 중 DB 쓰기 잠금을 유지하지 않고 적용 직전 대상 identity·generation을 확인한다.
- [x] 확인한 `planning/assessment_service.py`의 일반 Planner prompt 조립·`agents/agent_prompts.py`, `planning/selection_service.py`의 기존 호출 판단에 보존된 미충족 근거를 연결한다. catalog·현재 source·원인/완료 조건을 함께 제공해 기존 계획 재사용과 허용 개정/신규 판단을 수행하게 한다. 코드 조사 없는 증상 묶음은 미확정으로 전달한다. `planning/planner_validation.py`·`planner_publication.py`·`storage/planning_publication_state.py`의 기존 snapshot/게시/CAS를 소비해 실제 반영 결과에 연결을 결합하고, 게시 전 중단·경쟁/PR 출현에는 근거와 미적용 상태를 보존한다.
- [x] 확인한 `recovery/checkpoint_recovery.py:_assess_user_decision_cause()`·`blocked_plan_reconciliation.py`·`plan_recovery.py`의 대상 lease·현재 질문 인용·원격 재확인·원인별 재개를 G-319의 공통 경계로 소비한다. 교정 계획의 실제 Merge·검증 근거를 영향 작업에 연결하고, 필요한 관련 명세·원래 질문·새로 확정된 교정 반영 근거를 전달한다. 문서만의 개정 확정과 실제 코드 교정/검증 반영을 구별하며 별도 사용자 승인이나 성공을 합성하지 않는다. G-319의 간접 Requirement 조회·평가 identity·게시 후 재평가를 다시 구현하지 않고 G-301의 유효한 PRODUCT 부재 처리와 G-303의 짧은 DB 처리 단위를 사용한다.
- [x] 확인한 `tests/test_worker_outcome.py`·`test_execution_failure_policy.py`·`test_task_scheduling.py`·`test_plan_block_revalidation.py`·`test_incremental_planning.py`·`test_planning_publication.py`의 실제 SQLite/파일·모의 Git/Agent fixture를 확장한다. 필요하면 `tests/test_common_defect_recovery.py`를 이 인계의 연결 unit용으로 추가한다. 기존 의미 판단·피드백 재전달·일반 교정/소진·독립 배정 assertion은 재사용하며 같은 조건의 실제 Git/CLI 실행을 복제하지 않는다.
- [x] PRODUCT·ARCHITECTURE에는 실제 구현한 보고 소유권·일반 planning 인계·검증 후 대상별 재개만 통합한다. 정적 역할 규칙을 조정해야 하면 기존 `docs/AGENTS.common.md`·`docs/AGENTS.worker.md`·`docs/AGENTS.reviewer.md`·`docs/AGENTS.planner.md`의 소유 절을 수정하고 작업 prompt에 중복하지 않는다. 이 파일들의 수정은 현재 호출의 권한을 넓히지 않으며 파일명·요약·이유·영향의 PR 보고 계약을 지킨다. Requirement·다른 ExecPlan과 사용자 결정은 수정하지 않는다.

## Validation

- Worker는 위 모듈에서 새 인계·저장/재개와 직접 관련된 최소 unit만 실행한다. 같은 프로젝트의 A/B가 동일한 범위 밖 verify 결함을 보고하고 C는 독립인 fixture에서 실제 결과·checkpoint/질문·후보를 보존한 채 하나의 교정 목표로 연결되는지 확인한다. 동일 fingerprint의 다른 원인·원인 미확정·다른 source/완료 기준은 임의 병합하지 않아야 한다.
- 기존 동등 READY·draft·미Merge PR 각각을 제공해 중복 계획이 없는지 확인한다. 결함/코드/ExecPlan 보고만 있을 때 새 Planner 호출 수는 0이고, 허용된 명세 변경/복구/완료 재평가에서는 보존 근거가 같은 catalog와 함께 한 번 전달돼야 한다. 게시 전 실패·게시 후 DB 미적용·재시작·원격/소유권 경쟁에서도 원본/연결을 보존하고 실제 확정 결과만 채택한다.
- 현재 명세가 이미 정한 구현 교정은 사용자 질문을 새로 만들지 않는다. 실제 정책 선택·상충·권한 불명은 질문을 유지한다. 기존 `needs_user_decision`·draft 답변의 출처 검증과 잘못된 결과/인용 거부를 유지하고, 구조화 보고나 Planner의 문서 편집이 사용자 승인으로 바뀌지 않아야 한다.
- 미Merge 교정·검증 실패/불명·원인 미해소에서는 재개하지 않는다. 실제 Merge·검증 반영 뒤 G-319의 원인 재평가에 해당 근거·관련 명세를 전달하고 원래 질문·실패/수행 이력·session·독립 차단이 남는지 확인한다. 명세 해소만 있고 교정 미반영, 교정 Merge만 있고 선택이 남음, 평가 중 후보/원인/generation 변경, 동일 입력 재처리도 구별한다. G-319의 일반 문서 개정·인용/identity 조합은 재사용하고 이 작업에서는 공통 교정 근거의 연결·소비만 검사한다. verify/Review 미실행 성공과 다른 작업의 성공 재사용은 거부한다.
- A 재평가 중 B/C의 작업별 상태·자리·예약·후보가 유지되고 독립 DB 요청과 배정이 진행되는지 확인한다. 검증/Review 재실패에는 기존 교정·차단 조건을 적용하고 같은 원인의 질문/계획/알림이 반복되지 않아야 한다. 실제 운영 SQLite의 개별 사건 조사나 사용자 차단 해제는 이 작업의 테스트·완료 근거로 요구하지 않는다.

## Progress

- [x] 첫 보존 단위: 선택적 `COMMON_DEFECT_REPORT`의 제한된 관측 계약과 `validation_results`에 연결된 불변 저장을 추가했다. verify·Review의 기존 결과 저장은 보고를 같은 transaction에 보존하며 잘못된 보고는 원문·형식 오류를 유지한다. 보고 유사성에 따른 병합이나 기존 판정 변경은 하지 않는다. PRODUCT·ARCHITECTURE에는 이 저장 경계만 반영했다.
- [x] `tests.test_common_defect_recovery`의 5개 unit으로 형식/경로/크기·비밀값 정제·실제 SQLite 재시작·작업별 분리·불변성·동시 rollback을 확인했다. 최초 실행의 2개 실패는 기존 정제 규칙에 없는 `token=`을 사용한 fixture 오류였으며 기존 대상인 `API_KEY=`로 교정한 같은 명령의 재실행은 5개 통과했다. 기존 피드백 재발/해소, native 관측의 legacy 재수입 방지, JSON 비밀값 저장 회귀도 각각 최소 unit 1개씩 통과했다. 전체 verify는 실행하지 않았다.
- [x] 두 번째 보존 단위: Worker 반환 보고의 실제 invocation·session·선택 문서·후보 형상을 원래 결과에 연결하고, verify·Review의 결과 감사에 관측 문맥을 추가했다. 알려지지 않은 과거 identity는 보완하지 않으며 후보 관측 실패도 원래 보고와 함께 보존한다. 기존 일반 planning의 prompt 조립에서만 프로젝트별 보고 snapshot을 현재 catalog와 전달하고 새 호출 조건·보고 소비·계획 적용 표시는 만들지 않았다. 공통·Planner 역할 지침과 두 설계 문서를 이 전달 범위에 맞췄다.
- [x] 두 번째 단위의 targeted 검증: `tests.test_common_defect_recovery` 10개, Worker 보고/checkpoint 연결·후보 관측 불가 보존 2개, 기존 timeout verify·Review 보고/해소·Review 반환별 감사·범위 거부 감사 4개와 legacy 피드백 보존·재수입 방지 2개가 최종 통과했다. Worker 연결 assertion의 fixture 경로 참조 오류와 Review 후보 HEAD 기대값 오류를 각각 교정하고 동일 명령을 재실행했다. 최종 검토에서 Worker 보고가 legacy 검증 실패 이관을 가리지 않도록 native 검증 관측의 단계를 한정하고 과거 실패·Worker 감사의 동시 보존 assertion을 유지했다. 전체 verify는 실행하지 않았다.
- [x] 세 번째 보존 단위: 실제 prompt에 전달한 보고 ID를 planning attempt에 결합하고 `COMMON_DEFECT_LINKS`의 명세/코드 원문·현재 catalog/검증 결과·ID와 경로 재배정을 검증한다. 원인·source·완료 조건의 명시적 목표와 교정 소유 계획은 일반 planning의 기존 원격/catalog/DB 확인 transaction에서만 불변 감사로 채택한다. 제안만 있는 상태·게시 중단/rollback·catalog 경쟁은 미적용으로 보존하며 같은 목표의 유효한 READY·draft·미Merge PR 소유 계획을 재사용한다. 계획 연결로 실제 교정 Merge·검증·질문 해소를 선언하지 않는다.
- [x] 세 번째 단위는 `tests.test_common_defect_planning` 9개와 기존 보고/전달 10개, 일반 batch 확정·문서 변경 없는 확정 2개 최소 unit을 실행해 통과했다. 실제 SQLite의 제안/채택 분리·원본 보존·불변성·중단 재처리·목표 소유권과 모의 Git의 고정 revision 인용을 확인했다. 전체 verify는 실행하지 않았다.
- [x] 최종 구현 단위: `common_defect_pending` 결과와 `condition_wait`를 추가해 명시적인 구현 교정 대기를 사용자 질문·조치 알림과 구분했다. 실제 Manager PR Merge 관측의 후보/mergeCommit·같은 G/실행 주기의 전체 run 또는 증명된 reuse·Review token을 보존하고 원격 포함을 확인한다. 현재 실패/질문에 연결된 근거만 G-319 reader·인용·cache·generation/CAS로 평가해 해소된 작업을 기존 Worker부터 재검증한다. 코드의 공백·들여쓰기·실제 삭제/빈 파일도 대조하며 기존 질문에는 현재 상위 명세의 구체적 선택 인용을 계속 요구한다. 다른 승인·소유권·원격 삭제·종료 불명과 독립 작업은 유지한다.
- [x] 최종 targeted 결과: 공통 보고/전달·계획 연결·검증 후 재평가 모듈 32개, Worker 결과 계약 9개와 관련 Worker 대기·차단 재평가·PR 관측·DB 잠금/cleanup·복구 정책의 최소 unit 10개가 통과했다. 이 호출의 Worker 대기 테스트는 fixture가 차단 저장을 mock한 최초 실패를 기존 실제 저장 경계로 교정한 뒤 동일 명령 재실행에서 통과했다. 외부 GitHub/Agent/verify는 mock이며 실제 SQLite와 파일 저장·rollback·cache·독립 작업 보존을 검사했다. Requirement rename은 기존 resolver를 재사용해 선언을 유지하며 archive-only·모호함을 거부하는 unit으로 보완했다. 전체 verify·전체 Review·실제 운영 차단 해제는 실행하거나 성공으로 기록하지 않았다.
- [ ] Manager의 전체 verify·Reviewer 검토·dependency/최종 publication 확인과 사용자 PR Merge가 남는다. 이 결과는 구현 완료 후보이며 자동 작업 완료 선언이 아니다.
- [x] revision `9f877ad110999e0f198f7977d23d31d6b8eb5d89`의 R-055 추가 조건, Worker 결과 계약·verify/Review 실패 저장·일반 planning 호출 판단·현재 질문 의미 재평가를 정적으로 대조했다. 공통 원인과 교정 계획/검증 반영을 연결하는 인계가 없음을 확인했으며 구현·테스트·운영 상태 변경은 하지 않았다.
- [x] 보고·확인 근거의 durable 보존과 기존 일반 planning의 교정 목표 연결
- [x] 실제 검증/반영 뒤 대상별 원인 재평가·최소 회귀와 설계 정합화

## Decision Log

- R-082의 일반 `PLAN_BLOCKED` 개정과 확정 후 간접 관련 명세 재평가는 G-318/G-319로 분리한다. 이 계획의 여러 작업 보고·확인된 공통 원인·교정 목표·실제 Merge/검증 인계는 보존하고 G-319를 선행 소비해 공통 평가 구현을 중복하지 않는다. snapshot token `a886942395e0838d5daf76f3afb84c1a56d471fd039e4ed5a0dc6aee9896de3b`와 원문 hash를 확인했으며 기존 ID·경로·G-301/G-303·metadata·수행 기록을 유지한다. 정적 delta 검토이며 구현·테스트·차단 해제 성공이 아니다.

- 이번 R-055/R-091 delta의 교정/소진 정책 보존은 실제 최종 차단·필수 검증·사용자 질문을 뜻하며, 비차단 실행 예산 소진의 다음 외부 실행 전용 보류를 유지하라는 뜻이 아니다. G-297/G-313의 대기 결과를 기존 인계가 보존하도록 조건을 통합한다. 인계 기능은 독립 구현 가능하므로 새 dependency는 추가하지 않고 기존 G-301/G-303과 현재 목표·기록을 보존한다. 구현·테스트는 실행하지 않았다.

- R-055의 공통 교정 경로만 새로 연결한다. G-309의 runner 기반 교정은 이 메커니즘의 선행 조건이 아니며, 테스트는 특정 오류 이름에 고정하지 않는다. G-189는 이 구현 Merge 뒤 실제 차단/재개·알림 소비자를 감사한다. G-297의 프로젝트 배정 중단과 G-298의 작업 귀속을 중복 구현하지 않는다.
- G-301은 현재 의미 판단 reader의 유효 프로젝트 호환성을, G-303은 공통 DB 접근/잠금 재시도를 담당한다. 이 결과를 소비해 선택 문서 부재·일시 DB 대기를 새 사용자 판단이나 성공으로 오인하지 않는다. 일반 planning의 새 호출 조건·새 자동 승인·실행 권한은 도입하지 않는다.
- complexity high: 여러 작업의 원인 동일성·불변 감사·게시 전후 연결과 검증 반영 뒤 원인별 재평가를 중단/경쟁에서도 보존해야 한다. risk high: 잘못된 병합·질문 해제·후보 성공 승계는 필수 검증과 사용자 판단을 우회할 수 있다. 원본/인용·계획 소유권·실제 Merge/검증 근거·현재 후보/CAS를 확인하고 불확실성은 대상별로 보존해 수동 복구 가능성을 유지한다.
- priority high는 반복 실패와 불필요한 사용자 판단 대기를 해소하면서 독립 작업을 유지하기 위한 것이다. 최초 level 5, Model·Effort는 Manager 설정을 따른다. 이번 delta는 최종 제품 충족 확인이나 실제 교정/차단 해제 선언이 아니다.
