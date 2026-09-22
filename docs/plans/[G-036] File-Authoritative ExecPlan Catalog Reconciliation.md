---
id: G-036
title: File-Authoritative ExecPlan Catalog Reconciliation
sources:
  - docs/requirements/[R-017] File-Authoritative ExecPlan Source Reconciliation.md
depends_on:
  - G-031
  - G-032
  - G-049
complexity: high
risk: critical
priority: critical
---

## Purpose

최신으로 고정한 `origin/<base-branch>`의 전체 ExecPlan 파일을 계획 선언 정보의 원본으로 삼고, SQLite의 현재 선언 cache를 같은 ID의 유효한 파일에 원자적으로 맞춘다. 최초 게시 경로·commit·hash는 불변 감사 정보로 분리하고, 유효한 선언 변경은 오류로 만들지 않으며 잘못된 catalog만 실제 원인 파일을 가리키는 프로젝트 범위 진단으로 격리한다.

## Acceptance Criteria

- reconciliation은 하나의 고정된 원격 base revision에서 G-032의 active 및 archived catalog를 함께 읽고, regular file 여부, UTF-8/frontmatter, ID와 경로·제목 대응, source 경로 형식과 존재, 전체 ID·경로 고유성, dependency 존재 및 cycle을 SQLite 변경 전에 모두 검증한다.
- 같은 ID의 유효한 파일은 현재 경로·제목·sources·depends_on·complexity·risk·priority·본문 hash와 현재 plan revision SHA가 달라도 기존 실행 row와 대응하며, 이 차이만으로 `PLAN_BLOCKED`, `PROJECT_BLOCKED` 또는 planning error를 만들지 않는다.
- SQLite schema와 repository API는 최초 게시 commit SHA·active 경로·content hash를 불변 감사 및 PR 링크 정보로 보존하고, 최신 선언 경로·metadata·content hash·revision SHA를 별도의 현재 cache로 제공한다. additive migration은 기존 설치의 값을 손실 없이 두 역할로 초기화한다.
- 최신 선언 cache 갱신은 READY, 실행 중, PR_OPEN, 차단 또는 MERGED 상태와 재시도·실패 이력, Agent session, branch, worktree, workspace 소유권 및 PR 연결을 변경하지 않는다.
- 전체 catalog가 유효하고 원격 revision 및 대상 row의 상태·소유권 snapshot이 그대로일 때만 모든 달라진 row를 하나의 transaction으로 갱신한다. 원격 base 또는 SQLite snapshot이 바뀌면 일부 row를 적용하지 않고 최신 상태에서 다시 계산한다.
- source 추가·제거는 Requirement 관계와 입력 fingerprint의 다음 계산에 최신 값을 제공하고, dependency 변경은 검증된 전체 graph로 다음 READY 선택에 반영되며, priority 변경은 현재 작업을 선점하지 않고 다음 선택부터 적용된다.
- complexity 또는 risk 변경은 최초 Worker level cache를 다시 계산한다. 아직 시작하지 않은 READY 계획은 새 최초 level을 사용하고, 실행 이력이 있는 계획의 effective level과 Review 승격 이력은 낮추거나 지우지 않는다.
- current path와 title 변경은 compact catalog, 로그·알림 metadata와 이후 파일 조회에 반영되지만, 기존 PR의 ExecPlan 링크는 최초 게시 commit과 최초 active path를 계속 사용한다.
- Requirement archive source 관계와 후보 계산은 동기화된 현재 catalog를 사용한다. 최신 원격 `sources`와 과거 SQLite cache의 차이는 archive 오류가 아니며 같은 실행에서 새 관계로 후보를 다시 계산한다.
- G-031의 원격 `sources`가 최초 SQLite 값과 달라도 G-031 row를 갱신한 뒤 독립적인 G-035의 archive eligibility와 선택을 계속하고 G-035를 계획 내부 차단으로 바꾸지 않는다.
- 원격 ExecPlan 누락, invalid frontmatter, symlink·비regular entry, 존재하지 않는 source, ID 불명·중복 또는 dependency 오류에서는 선언 cache를 부분 갱신하지 않는다. G-031의 구조화된 project/catalog 원인에 원격 revision, 실제 문제 ExecPlan 경로와 누락 source를 기록하고 무관한 ExecPlan row를 차단하지 않는다.
- catalog 오류 fingerprint는 원인 ExecPlan과 필요한 source blob을 포함한다. 해당 원격 파일을 수정해 push하면 다음 실행에서 자동 재검증할 수 있고, dirty 기본 worktree와 push되지 않은 로컬 파일은 판단에 포함하지 않는다.
- PR 상태 동기화 뒤와 compact catalog 생성, Planner·Worker 선택, Requirement archive eligibility, 차단 재평가가 선언 정보를 읽기 전에 같은 reconciliation service를 사용한다. 반복 호출과 push 후 SQLite 갱신 전 중단은 부작용 없이 같은 최신 cache로 수렴한다.
- 장기 source-of-truth, 최초 게시 감사 정보와 현재 선언 cache의 구분 및 catalog 오류 범위를 `docs/PRODUCT.md`에 반영하되 Requirement와 ARCHITECTURE는 수정하지 않는다.

## Implementation

- G-032의 `execplan_catalog.py` snapshot 위에 revision-pinned `ExecPlanDefinition`과 전체 catalog validation을 추가하고, 선언 cache 및 immutable publication audit은 `execplan_catalog_state.py` repository가 소유한다. 실행 phase projection을 맡는 `runtime_state.py`에 catalog 파싱이나 Git 조회를 넣지 않는다.
- reconciliation service는 source 존재·dependency graph 검증이 끝난 snapshot과 expected row version/ownership만 입력받아 compare-and-swap transaction을 수행하고 typed `reconciled`, `stale`, `invalid-catalog` 결과를 반환한다. Git fetch와 revision 고정은 주입 port가 담당한다.
- project runner는 PR 동기화 뒤 planning/READY 선택 전에 한 번 reconciliation을 수행한다. `planning_runtime.py`에는 current catalog snapshot port를, G-049 `ExecutionContext`에는 reconciled plan metadata와 definition revision token을 전달해 각 phase가 raw row나 원격 파일을 다시 해석하지 않게 한다.
- `execution_handlers.py`의 prepare/archive preflight는 injected catalog port 결과만 소비하고, `_execplan_metadata_identity_error`와 stored-vs-file equality 경로는 ID 대응·snapshot validity로 대체한다. bootstrap wiring과 legacy compatibility wrapper만 `loop_manager.py`에 남긴다.
- catalog/project 오류 record는 `scoped_blocking.py`의 project-scope 저장 경계와 결합하되 declaration delta와 invalid snapshot을 별도 result type으로 유지한다. initial Worker level cache 갱신은 catalog transaction에서 계산하고 effective execution level은 runtime-state repository 규칙으로 보존한다.

## Validation

- Worker는 `tests/test_execplan_catalog.py`, `tests/test_execplan_catalog_state.py`와 새 reconciliation service의 직접 관련 targeted case만 실행한다.
- title, active/archived path, sources, depends_on, complexity, risk, priority와 본문 변경을 각각 snapshot delta로 만들고 상태·session·workspace·PR 이력을 보존한 원자 갱신을 검증한다.
- source 관계와 archive 후보, dependency/priority 다음 선택, initial/effective Worker level 비감소를 repository 및 selection port fake로 검증한다.
- `tests/test_execution_handlers.py`의 targeted case로 prepare가 reconciled metadata/token을 사용하고 최초 게시 SHA·path·hash와 immutable PR link는 최신 cache와 분리되는지 검증한다.
- base race, stale row snapshot, transaction 중단과 반복 호출에서 부분 갱신 없이 `stale`로 재시도하고 같은 revision에 멱등하게 수렴하는지 검증한다.
- 누락·invalid·symlink 계획, 중복·변경 ID, 없는 source/dependency와 cycle이 실제 파일을 가리키는 project/catalog 오류가 되며 원인 수정 뒤 자동 재검증되는지 검증한다.
- G-031/G-035 source 불일치 회귀와 planning/execution 양쪽 snapshot 소비자가 독립 계획을 차단하지 않는지 검증한다.
- 전체 verify에서 Planner/maintenance publication, active/archived recovery, scoped blocking, compact catalog와 Requirement archive 회귀가 없어야 한다.

## Progress

- [ ] revision-pinned definition snapshot과 전체 catalog validation 구현
- [ ] immutable audit/current cache repository 및 additive migration 구현
- [ ] 원자 reconciliation service와 stale snapshot 재시도 구현
- [ ] project runner·planning port·execution context 소비 경계 통합
- [ ] catalog 오류 fingerprint와 G-031/G-035 targeted 회귀 테스트 추가

## Decision Log

- G-028/G-029가 plan revision publication을, G-031이 plan/project 차단 범위를, G-032가 active/archive 전체 catalog를 제공한 뒤 하나의 reconciliation 경계로 통합한다.
- ID만 실행 이력의 안정적인 identity로 사용한다. 다른 선언 필드는 최신 신뢰 원격 파일을 따르되, 최초 게시 정보는 기존 PR의 불변 링크와 감사 목적으로 별도 보존한다.
- 실행 중 변경에 따른 단계별 무효화와 worktree 재개는 G-037로 분리해 이 계획의 catalog transaction과 오류 격리를 독립적으로 검증한다.
- Post-refactor revalidation: G-049 handler가 원격 파일·SQLite cache·실행 상태를 동시에 조정하지 않게 한다. catalog reconciliation은 execution 이전의 독립 snapshot/repository 경계이고 phase coordinator에는 검증된 metadata와 revision token만 전달한다.
