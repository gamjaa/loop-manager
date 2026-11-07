---
id: G-357
title: 본문 답변과 User Decision의 질문별 근거 결합
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

검증된 사용자 본문 변경이 기존 질문을 해소해도 형식이 불완전한 User Decision이 함께 있다는 이유로 planning 게시 전체를 거부하는 경로를 교정한다. 사용자의 구체적인 Answer와 같은 원격 변경의 Requirement 정정을 현재 질문·입력에 결합하고, 질문별 의미 판단과 Manager의 출처·현재성 검증 뒤에만 기존 정리·게시·DB 전환을 수행한다. 사용자에게 Decision/Revision 재작성이나 draft 직접 제거를 요구하지 않는다.

현재 G-344에는 5회 유지라는 verified Answer와 같은 commit의 verified R-091 정정이 있다. `storage/execplan_draft_state.py:observe()`의 `approved`는 `Decision == approve`와 전체 basis 동일성을 요구하고, `catalog/draft_decisions.py:prepare()`는 비어 있지 않은 `decision`의 `decision_ready`가 거짓이면 질문별 source/quote 검증보다 먼저 거부한다. 따라서 R-091을 인용 source로 바꾸는 것만으로 같은 오류를 해결할 수 없다. 이 관측은 현재 코드와 게시 준비 오류의 정적 근거이며 새 테스트 실행이나 운영 DB 조사를 뜻하지 않는다.

## Acceptance Criteria

- R-061: verified 사용자 Requirement/ExecPlan 본문이 기존 질문을 구체적으로 해소하고 현재 원문·입력 hash·계획 identity·소유권과 연결되면 질문별 결과를 수용한다. 같은 문서에 Answer만 있는 User Decision이 있어도 `Decision: approve` 부재만으로 이 검증을 일괄 거부하지 않는다. 형식 필드를 Agent나 Manager가 보충해 명시 승인을 만들어내지 않는다.
- 현재 사례와 같은 원격 변경의 Requirement 정정과 Answer를 함께 검증한다. 의미상 같은 선택을 확인한 뒤에만 본문 통합·임시 입력 정리·게시·대기 해제를 진행하며, 관련 입력이 바뀌었다는 사실을 새 의미 판단의 근거로 사용하되 이전 승인이나 과거 `decision_ready`를 최신 입력에 소급하지 않는다.
- 명시적 reject/defer, stale Revision, 다른 질문·계획·source, 상충/불충분 답변·출처 불명·Agent/Manager가 작성한 답변은 기존 보존·거부 경계를 유지한다. 독립적인 현재 본문 근거와 과거 명시 결정의 귀속을 구분하고, unready 플래그를 무조건 무시하거나 비어 있지 않은 Answer만으로 대기를 해제하지 않는다. 현재 근거로 연결하지 못한 User Decision은 소비·삭제하지 않는다.
- R-082/R-061: 불확실한 대상은 원래 질문·User Decision·판단 revision·draft·dependency·실행/실패 기록을 유지하고 독립 planning 결과는 게시할 수 있다. 같은 입력의 무답변/uncertain 처리는 질문 해소·정리 성공으로 기록하지 않으며 새로운 Planner 호출 사유나 반복 사용자 질문을 만들지 않는다.
- 정리 준비·파일 교체·원격 게시·catalog/DB 적용의 기존 경계를 재사용한다. 질문 일부만 해소하면 해당 근거만 소비하고 나머지를 보존하며, 중단·재시작·원격/source hash·소유권 경쟁에서는 낡은 적용을 거부한다. 코드 교정의 Merge를 G-344의 판단 확정·검증 성공으로 대신하지 않는다.

## Implementation

- [x] 확인한 `storage/execplan_draft_state.py:observe()`·`semantic_review_identity()`·`prompt_evidence()`와 `catalog/execplan_drafts.py:metadata()`/`fields()`의 질문·scope·입력·실제 사용자 필드 보존을 재사용한다. 명시 승인 readiness와 verified 본문/Answer의 의미 평가 가능성을 구분해 현재 질문에 필요한 근거를 제공한다. 새 승인 상태 원본·비호환 schema·운영 데이터 직접 수정·기존 필드 자동 보충을 추가하지 않는다.
- [x] 확인한 `catalog/draft_decisions.py:user_changes()`·`prepare()`·`_clean_answers()`에서 인용 출처·before/after의 실제 추가 원문·현재 source hash·질문/판단 revision을 검증하는 경계를 재사용한다. 전역 `decision && !decision_ready` 거부와 User Decision 인용의 추가 거부를 현재 근거별로 정합화한다. 같은 원격 변경의 verified Requirement 정정·Answer가 현재 질문을 해소하는 경우와 거절·stale 명시 결정·불명확 근거를 구분하고, 모든 질문 해소 시 입력을 제거하는 기존 정리가 확인하지 못한 User Decision까지 소비하지 않게 한다. 원문 인용의 단순 존재를 의미상 충분성으로 대체하지 않는다.
- [x] 확인한 `prepare_resolution()`·`resolve()`의 준비 영수증·scope/입력 CAS·게시 확인 뒤 원자 전이와 기존 `resolutions` 감사를 유지한다. 이전 판단 지문·답변 출처·새 질문별 판단과 현재 owner를 연결하며 준비된 결과 재사용·부분 해소·재관측에서 같은 답변을 잃거나 중복 적용하지 않게 한다.
- [x] 확인한 `tests/test_execplan_drafts.py:ExecPlanDraftCatalogTest`·`DraftAnswerReobservationTest`·`ExecPlanDraftPublicationTest`의 실제 SQLite·임시 파일·관측 double을 재사용한다. 필요한 최소 재현을 추가하고 현재 거부/보존 assertion을 다른 성공 기대값으로 바꾸지 않는다. 정책 조합을 위해 실제 Git/CLI lifecycle을 추가하지 않으며 `docs/ARCHITECTURE.md`의 draft 사용자 결정과 게시 절에는 구현된 근거 결합·보존 경계만 반영한다.

## Validation

- Worker targeted unit은 `ExecPlanDraftCatalogTest`의 변경 경계와 `DraftAnswerReobservationTest`의 해당 재관측 case로 한정한다. 동일 질문에 verified Requirement 정정과 Answer만 있는 User Decision을 같은 관측으로 준비하고, Decision/Revision 부재·draft 유지 상태에서도 실제 의미 결과와 현재 source/원문 검증 뒤 정리 준비에 도달하는지 확인한다. 명시 승인 필드가 자동 생성되지 않고 실제 게시 확인 전에는 pending이 유지돼야 한다.
- 같은 fixture에서 Answer만 있고 의미 판단이 없는 경우, 모호한 Answer, unverified/혼합 작성 이력, 원문에 없거나 before에도 있는 인용, 현재 source hash 불일치, explicit stale Revision·reject/defer, 다른 질문/owner와 실제 상충 답변을 거부·보존한다. verified 본문 근거를 사용하는 경우도 함께 있는 User Decision의 현재성 확인을 생략하면 실패해야 한다.
- 기존 `test_requirement_answer_requires_current_source_and_preserves_user_document`·`test_user_body_resolution_cleans_forms_and_retains_audit`·`test_answer_presence_without_semantic_resolution_remains_held`·`test_no_answers_with_unchanged_question_preserves_draft_without_blocking_batch`를 유지한다. `test_missing_answer_marker_only_stale_rejected_and_deferred_decisions_wait`와 관련 입력 전진의 stale 거부는 필요한 회귀이며 삭제·skip·assertion 완화하지 않는다.
- 기존 부분 해소·prepared cleanup 중단/재진입·DB rollback·원격 게시 후 적용·재관측 fixture로 새 근거 경로의 누락만 연결한다. 같은 질문과 현재 입력에서는 한 번 적용되고 나머지 질문/답변·실행/실패 감사·독립 작업은 보존돼야 한다. Requirement 원문은 수정되지 않고 실제 dependency Merge 전 Worker 선택은 계속 거부한다. 새 근거 경로의 성공을 현재 G-344 후보나 전체 verify 성공으로 기록하지 않는다.

## Progress

- [x] revision `d59136fc6d559ba21c69e2232a21bb4a4be9fd90`의 R-061·R-082, 관측/정리/게시 경계와 관련 회귀를 정적으로 대조했다. 이전 게시 준비 오류가 발생하는 전역 readiness 거부와 Answer-only 입력의 형식 조건을 확인했다. 구현·테스트·verify·운영 DB 조회는 수행하지 않았다.
- [x] 중간 보존점에서 확인되지 않은 User Decision의 전체 정리를 막은 뒤 질문별 복수 근거 결합과 부분 답변 처리를 완성했다.
- [x] 현재 질문·verified 본문/Answer 근거 결합과 기존 명시 결정 보호의 정합화
- [x] targeted unit·부분 해소/중단·게시/재관측 회귀와 설계 반영. 전체 verify와 Reviewer 검토는 Manager가 수행한다.
- [x] rebase 후 942080c의 base 대비 전체 diff와 R-061·R-082를 대조하고, 본문 근거의 부분 해소에서도 함께 남는 User Decision의 현재 출처 검사를 추가했다. 같은 관측의 상충·불확실 답변과 의미 판단 부재는 정리 없이 보존한다. 현재 후보의 `ExecPlanDraftCatalogTest` 37건, `DraftAnswerReobservationTest` 10건 및 추가 불확실 판단 case를 통과했다.
- [x] 과거 verify에서 보고된 checkpoint 복구 테스트는 현재 `BlockedCheckpointRefreshUnitTests` 29건, 로컬 복구 알림은 해당 1건을 targeted runner로 다시 확인했다. 이전 전체 verify 실패 기록은 그대로 보존하며 현재 후보의 전체 verify와 Review는 Manager에게 남긴다.
- [x] Review의 여러 줄 답변 정리 누락을 교정했다. 질문별 연속 원문 범위를 합쳐 전체 정리와 부분 정리 후 재관측에 적용하고, 위치가 모호하거나 줄 일부만 인용된 입력은 보존한다. `DraftAnswerReobservationTest` 14건을 통과했으며 전체 verify와 후속 Review는 Manager가 수행한다.
- [x] Review의 연속 부분 정리 재생 누락을 교정했다. 각 게시의 인용을 당시 남은 답변에 순서대로 적용하며, 중복 줄이 나중에 유일해지는 세 질문의 두 차례 게시·재관측과 마지막 정리를 확인했다. `DraftAnswerReobservationTest` 15건과 기존 부분 정리·중단 재진입 각 1건이 통과했다. 전체 verify와 후속 Review는 Manager가 수행한다.

## Decision Log

- R-061은 사용자 본문 변경과 기존 User Decision을 모두 판단 입력으로 허용한다. 이번 교정은 그 계약의 구현 부족이며 5회/3회 정책을 새로 선택하는 작업이 아니다. 현재 호출은 G-344의 원문·대기를 uncertain으로 유지해 별도 교정 계획의 게시를 막지 않고, 실제 반영 뒤 Manager가 원래 입력으로 재평가한다.
- G-189는 여러 선행 뒤 후속 선택·게시·알림을 감사하는 작업이고 G-278/G-338은 planning 보존/복구 진입을 소유한다. 현재 catalog에 이 질문별 근거 결합의 직접 교정·완료 조건을 맡은 비종료 계획은 없다. 이 작업은 독립적으로 구현·검증·Merge할 수 있으며 G-344를 선행으로 요구하지 않는다. G-189가 이 교정을 소비하도록 연결하고 순환을 만들지 않는다.
- complexity medium: 기존 관측·인용·정리 영수증·게시/CAS 경계 안에서 명시 결정과 본문 근거의 자격을 연결한다. risk high: 잘못된 결합은 사용자 판단을 위조해 의도하지 않은 Worker 실행을 허용할 수 있다. 원문·감사를 보존하고 미확정 입력의 대기를 유지하며 출처/질문/현재성·거절·경쟁 거부와 게시 전후 pending을 필수 검증으로 둔다. priority high는 기존 draft 처리 오류가 정상 planning batch를 막는 원인을 교정하기 위한 것이다. 모델·effort는 Manager 설정을 따른다.
