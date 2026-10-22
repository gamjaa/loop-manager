---
id: G-285
title: verify 입력 목록 생성 요청의 최초 등록 우선순위
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

Manager의 실제 verify 입력 목록 누락 요청에 따라 새로 만드는 ExecPlan에는 `priority: critical`을 전달하고 최초 등록에서 다른 값을 거부한다. 정책 적용 전에 등록된 계획·draft·PR과 요청은 보존한다. 이번 작업은 R-072의 요청·등록 정책 구현이며 특정 프로젝트의 목록 생성 요청이 아니다.

## Acceptance Criteria

- 일반 planning과 일반 계획을 포함한 유지보수 planning의 실제 누락 요청 prompt에 새 생성 계획의 `priority: critical` 조건이 명시된다. 최신 원격 목록 확인·관련 미완료 계획/draft/PR 중복 방지와 유지보수 전용 호출의 요청 제외를 유지한다.
- 새 목록 생성 계획의 Planner 결과 검증·게시 준비·최초 catalog 등록은 같은 조건을 적용한다. `critical`은 기존 요청 identity·정확한 Purpose·source 범위 검증 뒤 등록하고, `high`·`normal`·`low` 및 생략은 잘못된 신규 선언으로 거부한다. 등록 실패에서 해당 새 행·draft 실행 대기·성공 기준을 만들지 않는다.
- 이전에 등록된 미완료 계획·draft·미Merge PR의 기존 priority·상태·session·소유 자원·실패와 게시 감사는 보존한다. 과거 요청의 선언·규칙 버전·Purpose를 변경하거나 priority 조건만으로 요청을 무효화·재발급·중복 생성하지 않는다. archive 문서의 완료 수락·기존 등록의 멱등 재개를 신규 목록 생성과 혼동하지 않는다.
- 신규 여부는 현재 등록·게시 근거와 검증된 snapshot으로 판정한다. 파일 경로·ID 숫자·요청이 오래됐다는 추측만으로 신규 조건을 면제하거나 기존 작업에 소급 적용하지 않는다. 게시/등록 사이 중단과 snapshot 경쟁에서는 기존 intent·작업물을 보존하고 현재 근거로 동일 결과를 재검증한다.
- 범용 ExecPlan parser에 모든 `manager:verify-inputs` 계획의 priority를 강제해 과거 선언을 읽지 못하게 만들지 않는다. 일반 PRODUCT/Requirement 작업의 priority·source·dependency와 등록 오류 격리 정책은 유지한다.
- 새 계획은 기존 유효 우선순위·숫자 ID 선택을 사용한다. 진행 작업 선점·dependency 또는 유지보수 장벽 우회는 없으며 목록이 없거나 아직 유효한 성공 증명이 없으면 전체 verify를 계속 실행한다. 입력 증명 adapter·Review 재사용은 기존 담당 작업에서 구현한다.

## Implementation

- [x] 확인한 `loop_manager_internal/planning/verify_input_request.py:instruction()`의 실제 누락 요청 문구에 최초 생성 priority 조건을 추가한다. `catalog/manager_sources.py`의 `VERSION`·`PURPOSE`·`RULE`·`MANAGER_REQUEST` 선언과 기존 중복 확인을 재사용하며 등록 요청 원문을 새 형식으로 교체하지 않는다.
- [x] 확인한 `storage/verify_input_requests.py:validate()`의 요청 소유·범위 검증과 `planning/planner_validation.py:validate_planner_result()`의 신규/기존 snapshot 구분을 재사용한다. 신규 계획의 최종 분류와 ID 정규화 뒤에도 priority를 확인하고 재파싱·별도 priority 저장소 없이 검증된 metadata를 소비한다. `planning/validation_ports.py:validate_manager_source()`의 기존 source 소비자는 과거 요청 검증을 계속 수행할 수 있어야 한다.
- [x] 확인한 `storage/execplan_catalog_state.py:new_declaration_issues()`·`CatalogReconciliationService`의 신규 선언 판정, `CatalogStateRepository`의 snapshot/transaction과 `register_published_plan_row()`·`insert_published_plan_row()`의 실제 등록 경계를 대조해 게시 직전 및 최초 등록 누락을 막는다. 기존 row 재검사·멱등 등록과 archive fallback에는 신규 조건을 소급하지 않는다. 원격 게시가 확인된 기존 durable intent의 복구만 legacy priority를 보존해 반복 실패를 막는다. 기존 source 진단·독립 선언 보류를 재사용하고 비호환 schema나 새 복구 계층을 추가하지 않는다.
- [x] 확인한 `tests/test_verify_input_requests.py`의 모의 원격·실제 SQLite fixture와 `tests/test_planning_publication.py`의 게시/DB 확정 경계를 확장한다. 실제 구현 범위의 요청 우선순위·이전 등록 보존을 PRODUCT·ARCHITECTURE의 관련 절에 반영하되 Requirement·다른 ExecPlan을 Worker에게 편집시키지 않는다.

## Validation

- Worker는 변경에 직접 관련된 targeted unittest만 실행한다. prompt의 조건뿐 아니라 실제 검증·SQLite 행·실패 결과를 확인한다. Git 관측은 기존 mock으로 제공한다.
- 같은 등록 요청에서 신규 critical의 성공, 나머지 priority/생략의 실패, 일반 source 계획의 정상 등록, 기존 normal READY·draft·PR 및 archive 선언의 읽기/재동기화 보존을 구분한다. 새 priority 조건이 기존 요청 deduplication과 규칙·Purpose·미등록 요청 거부를 약화하지 않아야 한다.
- 게시 후 DB 적용 중단·같은 등록 재개·등록 직전 row/snapshot 변경에서 중복 행·새 요청·소급 priority 변경·거짓 성공 기준이 없음을 확인한다. 거부된 신규 계획 밖의 유효한 독립 catalog와 현재 작업의 상태는 보존한다.
- Manager의 전체 verify·Review는 기존 priority 전파·선택, dependency/유지보수 장벽, 현재 작업 비선점과 목록 부재의 전체 실행 회귀를 유지한다. Worker는 직접 관련 targeted unit test만 실행한다.

## Progress

- [x] 요청 문구, 일반·유지보수 Planner 검증, revision-pinned catalog 검사와 최초 row 삽입에 `critical` 조건을 연결했다. 신규 계획의 거부·row/draft 미생성, 기존 priority·PR 소유 상태와 archive fallback 보존을 포함한 요청·catalog·prompt·publication·maintenance unit test를 추가했다. PRODUCT와 ARCHITECTURE에 제품 규칙과 등록 경계를 반영했다.
- [x] 2026-10-22 관리 rebase 뒤 base `origin/main` (`5e7d4465`) 대비 현재 후보 `c6b973bf`의 전체 11개 파일 diff를 확인했다. `scripts/verify.json` unittest adapter version 1·launcher Python, `docs/VERIFY.md`의 Python 3.11–3.14 계약과 R-072 source 경로를 재확인했다. Python 3.12.3 targeted runner로 이 계획이 지정한 다섯 단위 ID를 실행해 25개 모두 통과했다. 이전 후보 결과는 재사용하지 않았다.
- [x] 전달된 미해결 verify feedback `verify:69c1bd7d869db40da5e13b85cf557e8211e720e44dcd4310e81e4cff38bdeb64`의 artifact `a3818e5813c4ae5e26381aae315ac10a`는 revision `b6e883e`에서 4,436개 성공·2개 실패를 기록했다. partial-runner는 30.060초에 exit -9·빈 결과였고, coordinated Python language subtest는 테스트를 시작하기 전 `resource_wait`에 29.455초 머물러 30초 timeout을 넘었다. nested runner는 `CPU 부하/미반영 예약 대기`를 보고했지만 어느 자원/예약 조건이 계속 막았는지와 partial-runner의 내부 대기 지점은 확인되지 않았다. 실패한 테스트와 공통 verify 실행기는 G-285 diff 밖이다. 전체 verify·integration 재실행은 Worker 범위에서 금지되어 있으므로 관련 코드를 임의로 바꾸지 않았고 Manager의 재판정이 남아 있다.
- [x] 2026-10-22 관리 rebase 결과의 base 대비 전체 diff와 현재 후보 수정사항, adapter·R-072 source를 다시 확인했다. ID가 기존 등록 계획과 충돌하는 새 `manager:verify-inputs` 파일은 정규화 후 최종 신규 계획으로 priority를 재확인한다. 원격 게시가 확인된 durable intent 복구는 legacy priority를 보존하고 신규 publication의 critical 검사는 유지한다. Python targeted runner로 `test_new_manager_plan_id_collision_rechecks_priority_after_normalization`, `test_recovery_applies_legacy_manager_plan_from_published_intent`, `test_new_manager_source_publication_requires_critical_before_db_confirmation`을 실행해 통과했다. 첫 회 실행은 recovery test mock 경로 오류로 실패했으며 수정 후 동일 명령은 통과했다.
- [x] 2026-10-22 추가 verify 실패에서 일반 publication effect callback에 기본값 `allow_legacy_priority=False`까지 전달해 기존 positional callback이 깨지는 것을 확인했다. legacy 복구 경로에서만 keyword를 전달하도록 수정했다. `test_remote_confirmed_move_marks_receipt_and_preserves_old_audit`는 수정 전 해당 TypeError로 실패했고 수정 후 통과했으며, `test_recovery_applies_legacy_manager_plan_from_published_intent`도 현재 후보에서 통과했다.
- [x] 2026-10-22 maintenance prepared intent 복구도 새 priority를 소급하지 않도록 복구 재검증과 row 등록에 legacy 예외를 연결했다. request identity/scope 검증은 유지한다. 과거 `normal` manager 계획을 포함한 게시 intent가 restart 뒤 SQLite row로 등록되고 maintenance cycle이 성공 기준선을 갱신하는 targeted 회귀를 추가했다. 수정 뒤 targeted recovery test 통과.
- [x] 2026-10-22 현재 후보 HEAD `ee08fcc8`와 base `d0ed33bb`의 15개 파일 diff를 확인했다. 선택 adapter unittest v1·launcher Python과 Python 3.11–3.14 계약, R-072 source를 재확인했다. Python targeted runner로 `test_legacy_manager_plan_recovers_from_published_maintenance_intent`와 신규 maintenance 계획의 critical priority 검사를 실행해 2개 모두 통과했다.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 실제 목록 생성 요청이 이번 호출에 제공되지 않았으므로 `manager:verify-inputs` source의 생성 계획은 만들지 않는다. G-241의 일반 입력 증명·현재 제외 근거와 G-183의 최종 Review 재사용은 이 scheduling 정책을 포함하지 않아 그대로 유지한다. 그 복잡한 관측 구현을 이 국소 등록 조건의 선행으로 요구하지 않는다.
- complexity medium: 요청 문구 자체는 작지만 Planner 신규 결과, 원격 catalog의 최초 등록, 게시 중단 재개와 과거 등록 호환에 같은 조건을 연결해야 한다. risk medium: 소급 검증은 기존 계획·draft·PR을 보류하거나 요청을 중복 생성할 수 있다. 현재 등록/snapshot 판정·기존 transaction·legacy fixture로 이를 보호하며 파일 교정으로 복구할 수 있다.
- priority normal: 현재 제품의 명시된 요청/등록 정책을 완성하는 작업이다. 여기서 구현할 `critical`은 실제 누락 요청으로 생성하는 목록 작업의 값이며 이 제품 정책 작업의 source나 우선순위로 혼동하지 않는다. 최초 Worker level은 3이고 Model·Effort는 Manager 설정을 따른다.
- 새 사용자 동작·데이터 삭제·주요 infrastructure·Git 쓰기 권한 변경을 도입하지 않는다. G-206의 보존 후보·실행 기록은 그대로 두며 후보 확인만으로 검증·Review 성공을 만들지 않는다.
