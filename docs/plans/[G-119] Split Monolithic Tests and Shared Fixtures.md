---
id: G-119
title: Split Monolithic Tests and Shared Fixtures
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-063
complexity: high
risk: high
priority: critical
---

## Purpose

`tests/test_loop_manager.py`에는 catalog, provider, Agent scope, workspace, publication, recovery와 scheduler 테스트가 19,483줄에 모여 있다. 다른 테스트가 이 파일의 fixture를 import하고 module-wide profile/session patch에 의존한다. 기능별 테스트와 명시적인 fixture 수명으로 분리해 이후 코드 추출의 회귀 근거를 유지한다.

## Acceptance Criteria

- 기존 TestCase와 test method를 책임별 모듈로 옮기고 원래 test ID에서 새 ID로의 대응표를 이 계획에 기록한다. assertion·실패/복구 시나리오·subTest 입력·mock side effect를 보존하며 중복 fixture 제거를 테스트 제거로 처리하지 않는다.
- Git repository, SQLite/catalog, Worker 결과·session fixture는 `tests/support/`의 책임별 모듈에 둔다. test module이 다른 test module에서 fixture나 TestCase를 import하지 않으며 support의 import만으로 patch·파일 생성·DB 갱신을 수행하지 않는다.
- `_profile_for_dummy_worker_config`와 `_ports_with_bound_dummy_sessions` 등 현재 module-wide 보정은 필요한 case의 명시적 context 또는 `setUp`/`addCleanup`으로 제한한다. 다른 case의 profile·session 동작을 조용히 보정하거나 전역 patch 수명을 공유하지 않는다.
- `tests/test_loop_manager.py`에는 루트 진입점·공개 facade의 연결을 직접 검증하는 소수의 case만 남는다. 새 파일도 원래 거대 class를 그대로 옮기는 대신 catalog·scope·publication·recovery 등 독립 책임으로 나눈다.
- 전체 discovery에서 기존 각 시나리오는 정확히 한 번 수집된다. 원래 파일의 TestCase re-export나 fixture 상속으로 중복 수집하지 않으며 두 운영체제의 verify가 새 모듈을 포함한다.

## Implementation

1. G-060의 최신 기준선으로 TestCase·test method·fixture 소비 관계를 정리하고, 실행하지 않는 discovery 또는 AST 조사로 이전 수집 집합과 이동 대응표를 기록한다.
2. 공통 fixture를 `tests/support/git.py`, `catalog.py`, `database.py`, `worker.py` 등 실제 책임별로 추출한다. `test_runtime_state`, `test_execution_handlers`, `test_blocked_checkpoint`, `test_managed_rebase_validation` 등의 공유 factory와 TestCase 인스턴스/메서드를 fixture로 사용하는 경계도 조사한다. 재사용하는 setup만 독립 factory로 옮기고 모든 소비자를 갱신하며 테스트 본문을 support로 옮기지 않는다. support package의 `__init__.py`에 모든 fixture를 다시 모으지 않는다.
3. catalog/planner, Agent/provider/scope, workspace/cleanup, Worker/publication, blocked/rebase recovery, scheduler/logging 테스트를 각각의 기능 모듈에 옮긴다. 기존 `LoopManagerTest`와 `AgentScopeValidatorTest`처럼 내부 책임도 여러 개인 class는 시나리오 경계로 나눈다.
4. `tests/test_maintenance_orchestration.py` 등 소비자의 test-module import를 support import로 갱신한다. G-063 이후 실제 소비 모듈에 patch를 걸고 그 patch가 실행 경로에 적용됨을 결과 assertion으로 확인한다.
5. 각 후속 G-120~G-126이 갱신할 테스트의 새 위치를 대응표로 제공한다. 검증 시간 상한, skip 조건과 검증 강도는 변경하지 않는다.

## Validation

- Worker는 이동한 각 책임의 대표 성공·실패·재개 test method와 새 fixture의 격리·cleanup 경계를 검증하는 targeted unit test만 선택해 실행한다. 단순 이동 case 전부 또는 여러 기능의 전체 suite를 Worker 검증으로 실행하지 않는다.
- profile/session patch가 필요한 case에만 적용되고 다음 case에 남지 않는지, support import가 TestCase 수집이나 실행 자원 생성을 유발하지 않는지 확인한다.
- 실행 없는 이전/이후 수집 집합 대조에서 ID 이동을 정규화한 누락·중복이 없어야 한다. inherited method와 parameterized/subTest 시나리오도 보존한다.
- Manager 전체 verify는 모든 기존 시나리오와 새 파일 discovery를 확인한다. Reviewer는 이동 전후 assertion·mock·fixture diff와 결과로 검증 약화 여부를 읽기 전용으로 판단한다.

## Progress

- [ ] test ID·fixture 소비 대응표 작성
- [ ] 기능별 test module·support 분리 및 module-wide patch 제거
- [ ] 대표 targeted unit test와 실행 없는 수집 집합 대조
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: 사용자 요청의 기능별 분리와 중복 제거를 테스트에도 적용한다. G-063의 import 경로 이동 후 먼저 테스트 경계를 안정화하고 G-120부터 구현을 분리한다.
- 테스트 이름의 이동은 허용하되 기존 회귀 시나리오와 실제 assertion을 보존한다. 줄 수 감소만을 위해 테스트를 합치거나 삭제하지 않는다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.
