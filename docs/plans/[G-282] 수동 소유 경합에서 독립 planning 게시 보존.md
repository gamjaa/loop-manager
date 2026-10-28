---
id: G-282
title: 수동 소유 경합에서 독립 planning 게시 보존
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-077] G 계획 수동 구현과 R·코드 일괄 제출.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-337
complexity: high
risk: high
priority: high
---

## Purpose

일반 planning의 한 batch에서 특정 계획의 수동 소유권이 snapshot 뒤 바뀌면 그 개정과 영향받는 결과를 보류하고, 관계가 확인된 독립 결과는 보존해 게시를 계속한다. 현재 수동 group·membership 지문과 게시 직전 거부는 구현돼 있으나, `record_general_intent()`의 거부를 게시 서비스가 batch 전체의 `planning_error`로 저장해 독립 결과도 게시하지 못한다. 기존 수동 소유 보호를 완화하거나 다시 구현하지 않고 이 거부 이후의 처리만 보완한다.

## Acceptance Criteria

- R-077/R-082: snapshot 후 수동 start·submit 준비, 수락된 묶음 소유 또는 membership 변경이 생기면 해당 계획의 개정·rename·삭제를 게시하지 않는다. 실행 row가 그대로인 예약도 기존 소유 지문으로 확인하며 수동 원문·branch·worktree·요청·session·실행/실패 이력을 보존한다.
- PRODUCT/R-055: 같은 batch의 독립 새 계획·개정과 허용된 FUTURE_IDEAS 변경은 원문·판단·session을 보존하고, 최신 권한·원격 입력·전체 catalog를 재검증한 결과로 게시할 수 있다. 수동 경합만으로 이 결과를 영구 planning 오류에 묶거나 동일 입력의 모델 호출·실패 집계를 반복하지 않는다. 분리할 유효 결과가 없으면 이유와 후보를 보존하며 빈 commit·push를 만들지 않는다.
- R-082: 독립성은 source·dependency와 개정 전후 목표·공유 완료 조건으로 확인한다. 보류된 개정의 새 목표나 변경할 dependency에 의존하는 결과는 함께 보류한다. 기존 선언을 유지한 catalog에서 목표와 graph가 유효함을 확인하지 못하면 같은 Planner session의 의미 재평가로 연결하고 dependency 삭제나 원래 목표 축소로 게시를 강행하지 않는다.
- 거부된 변경·원래 batch와 재검증한 게시 후보는 구분해 보존한다. 기존 prepared 또는 원격에 게시된 intent를 부분 manifest로 덮어쓰지 않는다. 게시 후 DB 미적용은 같은 intent의 실제 결과부터 복구하고, 중단 뒤 새 lease·후보를 끼워 넣거나 보호 대상 변경을 다시 포함하지 않는다.
- 게시 가능한 결과의 원문·hash·실제 PR 부재는 공통 DB 접근 밖에서 준비하고, 현재 snapshot·branch/generation·수동 소유 지문과 준비한 intent identity는 기존 예약·DB 적용에서 다시 확인한다. 독립 후보를 다시 구성하면 그 후보의 준비 값도 다시 결합하며 원래 전체 batch의 값을 섞지 않는다. DB 재시도는 같은 준비 값의 CAS·원자 적용만 수행하고 PR/Git 관측·commit/push·worktree 제거·Planner를 반복하지 않는다. fast-forward 게시와 같은 batch의 원자 catalog 적용, 새 ID 예약·취소 감사·draft/User Decision 보호를 유지한다. 실제 원격·공유 자원 불확실성과 독립 오류는 이 경합 처리로 해제하지 않는다.
- 독립 batch의 DB 적용 뒤 정리·최종 해제는 G-337의 `publication_remote_oid`·잔여 정리 보존과 release 경계를 사용한다. writer 종료·게시/DB 확정과 독립 보존이 확인된 제거 실패는 정리 대기로 분리한 뒤 최종 CAS로 해제할 수 있다. 원래 보류 결과·session·미확인 목표와 적용된 독립 결과는 유지하고 부분 게시를 전체 planning 성공으로 승격하지 않는다. 실제 소유/원격/종료 미확정에는 해제하지 않으며 중단 뒤 이미 끝난 게시·등록·제거를 반복하지 않는다.
- 독립 부분 게시만으로 전체 planning 성공·미확인 목표 해소·Requirement archive·프로젝트 최종 전문 확인을 기록하지 않는다. 최신 입력에서 남은 목표가 수동 소유로 유효하게 보호되는지, 추가 판단이나 후속 작업이 필요한지 확인한 결과만 기존 성공 기준에 반영한다. 보류·게시·재개 사이 중단에서 같은 결과를 중복 등록하거나 작업을 초기화하지 않는다.

## Implementation

- [ ] 확인한 `loop_manager_internal/planning/planner_validation.py:_assert_mutable_ready_snapshot_current()`와 `loop_manager_internal/storage/work_claims.py:assert_manual_scopes_available()`의 거부를 계획 ID·원인·현재 소유 근거로 식별한다. 이미 구현된 `manual_work_state.py:planning_ownership_state()`와 `planning_catalog.py`의 snapshot/manifest 직렬화를 재사용하며, 실제 판단을 성공값이나 예외 무시로 대체하지 않는다.
- [ ] 확인한 `planning/publication_service.py:_publish_planner_result()`의 intent 예약 실패 처리를 현재 batch의 재검증·교정에 연결한다. 거부된 변경과 관계상 보류할 결과를 보존하고 독립 결과의 후보·publication_paths·hash·snapshot을 함께 검증한다. 코드·문서를 문자열로 잘라 목표를 결정하지 않으며 의미가 불확실하면 기존 Planner 재평가 경계를 사용한다. 최신 구현이 검증 후의 `git.changed_files(worktree)`로 staging/commit 필요성을 다시 확인하는 경계는 그대로 소비한다. Planner 종료 시점의 `run.dirty_files`만 사용하거나 이미 commit된 후보에 빈 commit을 만드는 경로를 복원하지 않는다. 이 교정 자체가 수동 경합 뒤 독립 후보 분리·재검증을 구현한 것으로 보지 않는다.
- [ ] 확인한 `planning/planner_publication.py:record_general_planning_publication_intent()`·`_prepare_execplan_rows()`·`_apply_general_planning_batch()`, `planner_validation.py:_observe_mutable_ready_prs()`와 `storage/planning_publication_state.py`·`planning_input_state.py`의 준비 값·intent·원자 적용·성공 기준을 재사용한다. 독립 후보의 manifest·계획 집합·게시 commit과 준비 값이 일치해야 하며 준비 중 수동 소유가 바뀌면 기존 CAS로 해당 적용을 거부한다. DB 요청 안에서 원문/PR/Git을 다시 읽는 옛 경계를 복원하지 않는다. 확인한 `planning/agent_ports.py`·`state_ports.py`의 저장 결과·session과 교정 원문을 유지하고, 게시 전 후보 변경과 이미 게시된 결과의 복구를 분리한다. `publication_service.py:_complete_publication_release()`와 state port의 적용·외부 정리·최종 해제를 소비해 보류한 전체 결과가 독립 batch 해제 과정에서 유실되지 않게 연결한다. 전체 catalog 재검증은 `catalog/catalog_service.py`·`project_catalog_refresh.py`·`storage/execplan_catalog_state.py`가 보존하는 같은 revision의 Requirement 원문과 기존 source resolver를 유지한다. 기존 source 문자열의 rename 조회를 신규 source 선택이나 독립성 증명으로 대체하지 않는다. 새 실행 엔진·비호환 schema나 별도 전체 계획 성공 상태를 만들지 않는다.
- [ ] 확인한 `application/manual_concurrency.py:preparation_gate()`와 기존 자원 lock의 양방향 예약을 유지한다. Planner가 먼저 예약한 경우 수동 준비를 계속 거부하고 수동 경합을 처리하려고 예약을 탈취·해제하지 않는다. 유지보수의 기존 활성 cycle gate와 미착수·무소유 snapshot 제한은 확대하지 않는다.
- [ ] 아래 기존 fixture에 최소 회귀를 추가하고 PRODUCT·ARCHITECTURE의 해당 게시 실패·수동 경합 설명만 실제 구현에 맞게 통합한다. G-243의 보호 helper와 직접 구현된 수동 submit 후보의 scope 기준을 재구현하지 않는다.

## Validation

- Worker targeted unit은 `python3 -m unittest tests.test_incremental_planning.ManualOwnershipPlanningTest tests.test_planning_publication.PlanningPublicationTest`에서 추가·변경한 경합 case를 중심으로 실행한다. 확인한 `tests/support/planning_case.py`의 publication port와 실제 SQLite를 재사용하며 Git·PR·Agent 관측만 mock한다.
- 개정 대상 하나와 독립 새 계획/개정을 같은 batch에 두고, snapshot 후 실행 row를 바꾸지 않는 start intent·submit membership 변경을 관측한다. 보호 대상의 원격/DB 선언과 수동 원문은 그대로이고 독립 결과만 검증한 manifest로 게시·등록돼야 한다. 보호 대상에 의존하거나 개정된 목표를 공유하는 결과는 보류해야 한다.
- 기존 `tests/test_incremental_planning.py`의 관련 소유 근거 손상·orphan membership·묶음 전체 보호·manifest round-trip을 유지한다. 분리 불가능한 source/목표, 실제 PR 출현·조회 실패·원격 전진과 별도 오류를 수동 경합의 독립성으로 오인하지 않는다. 독립 후보 재검증에서도 rename된 Requirement의 같은 원문과 기존 source 문자열을 유지하며 누락·모호함은 보류한다. `tests/test_execplan_catalog.py:ScopedCatalogParsingTest`와 `tests/test_project_catalog_refresh.py:CatalogArchiveConsumerTests`의 직접 구현된 원문 보존·원격/SQLite 경쟁 회귀를 재사용하고 archive 동작을 중복 구현하지 않는다. 불필요한 재호출 없음과 전체 성공 기준 미전진을 실제 저장값·호출 수로 확인한다.
- 후보 보류 뒤 재검증, intent 저장 전/후, push 후 DB 미적용과 DB transaction 실패·재시작에서 원래 결과·session·소유권·실패 감사를 보존한다. 이미 게시된 intent는 원문 그대로 복구하고 중복 push·ID 등록·draft 해제·전체 planning 성공 합성이 없어야 한다. `tests/test_planning_publication.py:PlanningPublicationTest.test_publication_uses_current_changes_after_manager_validation`의 Manager 검증 후 변경/이미 commit된 후보 사례를 재사용해 검증된 publication_paths와 실제 plan commit을 intent에 결합한다. `tests/test_incremental_planning.py`의 준비 중 intent/owner 변경·DB retry 회귀와 `tests/test_planning_publication.py:test_applied_publication_recovers_after_cleanup_before_db_release`의 release fixture에 혼합 batch의 새 연결만 추가한다. PR 관측·정리 대기 중 독립 DB 요청이 진행하고 DB retry는 같은 준비 값만 사용하며, 제거 후 해제 실패에서도 게시된 독립 결과·보류 원문/목표·session이 보존돼야 한다. G-337의 안전한 제거 실패 분리→최종 해제→후속 독립 선택에서도 같은 보존을 확인하고, 재시작한 잔여 정리가 부분 batch의 전체 성공을 합성하거나 새 planning 경로를 제거하지 않아야 한다. 같은 경우를 정책 조합마다 재작성하지 않는다.
- `tests/test_manual_concurrency.py:ManualConcurrencyTests`의 Planner/수동 양방향 선점과 게시 후 재개 회귀를 재사용한다. 실제 fast-forward ref 효과가 필요한 혼합 batch 사례만 최소 Git 통합 회귀로 추가하고 조건 조합은 mock으로 검사한다. 이 통합 회귀는 Manager 검증에 남기며 Worker가 전체 통합 흐름을 실행하지 않는다. 부분/전체 verify는 기존 수동 제출·catalog 원자 적용·입력 기준·작업 재개 회귀를 유지한다.

## Progress

- [ ] 수동 경합 대상과 독립 결과의 보존·재검증·게시 연결
- [ ] 경합·dependency·중단 복구의 targeted unit 검증과 관련 문서 정합성
- [ ] 독립 후보의 준비 값·DB-only retry·정리/최종 해제와 보류 결과 보존 연결
- [ ] G-337의 잔여 정리 분리·재시도에 부분 batch와 보류 원문/목표를 연결

## Decision Log

- 최신 R-082의 잔여 정리 분리는 G-337을 선행 소비한다. 혼합 batch에서 독립 결과만 확정·해제되고 보류 원문/목표·수동 소유가 남는 경우를 기존 release fixture에 연결한다. 별도 publication 정리 엔진을 만들지 않으며 started ID·경로·기존 기록과 사용자 판단/차단을 보존한다.

- `993b7dfd`의 일반/유지보수 게시 분리는 원격 base의 직접 구현이다. 수동 경합 뒤 독립 후보의 의미 판단·분리는 여전히 이 작업이 담당하되, PR/원문 관측을 transaction 안으로 되돌리지 않도록 R-091을 source로 연결한다. 적용 후 정리·해제와 부분 게시의 전체 성공 미합성을 함께 검증하고 기존 질문·차단·실행 기록을 개정만으로 해소하지 않는다. started ID·경로·complexity/risk·priority·dependency는 보존하며 G-331의 완료 범위를 재구현하거나 새 선행 작업을 만들지 않는다.

- 최신 publication service의 검증 후 실제 변경 재조회와 publication·assessment·code-map fixture 정합화는 직접 구현으로 소비한다. 이 작업은 수동 소유 경합 뒤 독립 후보·manifest·원자 적용에 남은 범위만 보완하며 dirty snapshot 교정·빈 commit 방지를 중복 구현하지 않는다. 이번 정적 재평가로 전체 planning 성공·검증·Merge 근거를 만들지 않는다.

- 2026-10-14 최신 입력 재평가: 이전 기준 `eafe3e95b8969fe54db5021ecdee90fe58ac1843`에서 `b72b0f7ba7be68d3977151c2a4ecbbba20af89aa`로 바뀐 다섯 코드·테스트 파일을 확인했다. `PinnedCatalog.requirement_documents`와 archive 결과의 같은 원문 전달, rename source 보존 회귀는 직접 구현돼 있어 새 작업으로 계획하지 않는다. 명세·검증 선언·계획 규칙과 수동 경합의 게시 서비스는 이 추가 delta에서 바뀌지 않았으므로 기존 미충족 목표를 유지하고 해당 catalog 계약을 후보 재검증에 연결했다. 이번 입력 지문 `20f2ef6f407a0e1ce2782bb56b1607718c8f342ddf2abed41ccac7e3a644d10b`의 변경 검토이며 구현·테스트·verify 또는 최종 전문 확인 성공을 기록하지 않는다.
- 이번 delta의 수동 group 보호·지문 직렬화·예약 재검사는 실제 코드와 `tests/test_incremental_planning.py`·`tests/test_manual_concurrency.py`에 있다. 하지만 게시 서비스는 intent 예약의 모든 RuntimeError를 프로젝트 planning 오류로 반환하며 혼합 batch의 독립 결과 처리 경로가 없다. 단일 개정 거부와 무관한 snapshot 항목의 무검사만으로 이 조건을 충족했다고 판단하지 않는다. 정적 검토이며 테스트·verify 성공을 만들지 않았다.
- G-242는 DOCUMENT_RESULT 형식과 실제 PR 출현/legacy 후보 경합, G-244는 사용자 draft 답변의 출처·정리, G-278은 실패 planning에서 실행 자리를 비운 뒤 이미 게시된 독립 작업의 선택을 담당한다. 이 작업은 일반 batch의 수동 경합 뒤 미게시 독립 결과를 보존·게시하는 범위로 한정한다. 현재 G-206의 구현 후보·기록과 다른 계획을 변경하지 않는다.
- complexity high: 목표·dependency의 독립성, 원래 결과 보존과 재검증한 후보의 identity를 Git 게시/SQLite 적용 사이 중단에도 연결해야 한다. risk high: 잘못 분리한 후보는 보호된 선언을 게시하거나 미확인 목표의 전체 성공을 기록해 수동 원문·상태 복구가 필요하다. 현재 권한 재검사·전체 graph·정확한 manifest·원자 적용과 중단 회귀로 이를 방지한다. priority high는 수동 경합 하나가 독립 자동 계획 게시까지 막는 사용자 복구 경로를 줄이는 기준이다.
- 기존 fast-forward 게시·PR 경계·수동 gate·Planner/공유 게시의 직렬화와 사용자 결정 정책 안의 누락 처리다. R-077이 참조하는 R-091에 따라 프로젝트의 독립 구현 작업까지 단일 실행으로 제한하지 않는다. 사용자 선택이나 새로운 승인 경계를 작성하지 않으며 변경 검토를 최종 제품 전문 확인으로 취급하지 않는다.
