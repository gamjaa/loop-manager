---
id: G-032
title: Archived ExecPlan Catalog and Recovery Compatibility
sources:
  - docs/requirements/[R-014] Automatic Completed ExecPlan Archiving.md
depends_on:
  - G-029
  - G-049
complexity: high
risk: high
priority: high
---

## Purpose

완료된 ExecPlan 파일이 장차 `docs/plans/archive/`로 이동해도 ID, 최초 path, content hash, dependency, PR 이력과 다음 번호 할당을 잃지 않는 catalog 및 복구 경계를 먼저 만든다. 활성 실행과 AI 입력은 계속 `docs/plans/` 바로 아래의 비종료 계획으로 제한하고, archive 위치만으로 완료를 추정하지 않는다. 이 계획은 실제 파일 이동을 활성화하지 않는 안전한 기반 단계다.

## Acceptance Criteria

- 코드가 `docs/plans/` 바로 아래의 active catalog와 `docs/plans/archive/`의 historical catalog를 명시적으로 분리해 읽으며 중첩 디렉터리나 symlink를 계획 파일로 받아들이지 않는다.
- Planner compact catalog, Planner prompt, Worker 선택과 active ExecPlan metadata 검증은 archive 본문을 기본 AI 입력으로 읽거나 실행 후보로 사용하지 않는다.
- 전체 catalog 검증은 active와 archived 파일을 함께 사용해 프로젝트 전체 ID와 파일명 고유성, 다음 ID, dependency 존재 및 cycle을 확인한다.
- 새 ExecPlan의 ID는 active와 archived 전체의 과거 최대 숫자 다음 값이어야 하며 archived ID를 재사용하거나 active-only 최대값으로 되돌아가지 않는다.
- active 계획이 archived 계획에 의존할 수 있지만 SQLite와 GitHub/Git 이력으로 해당 계획의 Merge가 확인된 경우에만 dependency가 충족된다. archive 파일 존재만으로 MERGED를 추정하지 않는다.
- SQLite의 `exec_plans.path`, plan SHA와 content hash는 최초 `docs/plans/[G-000] Title.md` 경로와 게시 내용을 안정적으로 유지하며 archive 위치를 새 identity로 덮어쓰지 않는다.
- archived 파일의 byte hash가 저장된 원본 content hash와 다르면 완료 이력으로 사용하지 않고 구체적인 진단을 기록한다.
- 기존 설치 migration과 명시적인 새 환경 등록·상태 복구는 active 및 archived catalog를 함께 대조한다. archived 계획은 관련 PR Merge 또는 동등한 Git 이력으로 완료가 증명될 때만 MERGED audit row로 복구한다.
- 완료를 증명할 수 없는 archived 계획은 READY나 Worker 대상으로 등록하지 않고, ID/dependency 검증에 사용할 historical entry와 진단 상태로 보존한다. 다른 안전한 active 계획을 archive 위치만을 근거로 완료 처리하지 않는다.
- 기존 immutable PR body 링크는 plan SHA와 최초 active path를 계속 사용하고 archive 이동 여부에 따라 URL을 다시 쓰지 않는다.
- G-028/G-029의 maintenance catalog snapshot, revision publication과 dependency 검증도 archived historical IDs를 보존하되 archive 파일을 수정 가능한 READY 계획으로 노출하지 않는다.
- schema/API 도입만으로 active 계획을 이동하거나 archive commit, branch, PR, Planner, Worker 또는 Reviewer를 시작하지 않는다.

## Implementation

- `loop_manager.py`의 `_parse_execplan`, `load_execplan_catalog`와 dependency graph 검증을 새 `execplan_catalog.py`의 순수 parser/snapshot API로 옮긴다. active top-level entry와 archived historical entry를 서로 다른 typed collection으로 반환하고, 기존 함수는 호출자 이관 동안 얇은 compatibility wrapper로만 유지한다.
- historical merge proof, 관측 경로·final hash와 복구 진단은 새 `execplan_catalog_state.py` repository가 소유한다. 실행 phase와 project compatibility projection은 계속 `runtime_state.py`가 소유하며 두 저장 책임을 한 repository나 raw SQL 호출에 섞지 않는다.
- `planning_runtime.py`의 validation/publication port와 project registration·legacy migration adapter가 full snapshot을 사용하게 하고, 수정 가능한 READY snapshot과 Agent compact catalog는 active collection만 받게 한다. bootstrap wiring만 `loop_manager.py`에 남긴다.
- GitHub PR 및 immutable plan commit 조회는 catalog recovery service에 주입하는 merge-proof port로 격리한다. parser와 repository는 Git, GitHub, Agent 또는 notification을 직접 호출하지 않고, 증명 실패를 실행 상태와 분리된 typed diagnostic으로 반환한다.
- dependency selection은 확인된 SQLite MERGED 상태와 full snapshot의 identity graph를 결합하되 위치를 상태로 해석하지 않는다. 다음 ID, immutable PR link와 maintenance snapshot 호출자도 동일 snapshot API를 사용해 active/archive 규칙을 중복 구현하지 않는다.

## Validation

- Worker는 새 `tests/test_execplan_catalog.py`와 `tests/test_execplan_catalog_state.py`의 직접 관련 test class, 그리고 이관한 planning/registration adapter의 targeted case만 실행한다.
- 순수 snapshot 테스트에서 active+archive ID 중복, regular-file/symlink 경계, 다음 번호 연속성, archived dependency 존재·cycle 및 active-only compact projection을 검증한다.
- repository/merge-proof fake로 archived MERGED dependency 성공, 파일만 존재하는 미확인 dependency 대기, final hash 변조와 Merge commit·Squash·Rebase 이력 복구를 검증한다.
- `tests/test_planning_runtime.py`의 targeted case로 maintenance barrier/revalidation과 일반 Planner가 historical identity를 보존하면서 archive를 수정 가능 READY나 Agent 입력으로 노출하지 않는지 검증한다.
- `tests/test_execution_runtime.py` 또는 catalog selection adapter의 targeted case로 dependency gate가 file location이 아닌 확인된 상태를 사용하고 immutable PR link가 stable active path를 유지하는지 검증한다.
- 이 계획 Merge만으로 archive dispatcher, 파일 이동, commit·push, Agent 호출 또는 완료 알림이 실행되지 않는지 port spy로 검증한다.
- 전체 verify에서 기존 active catalog parsing, Planner batch publication, legacy migration, READY priority/dependency 선택과 Requirement archive 회귀가 없어야 한다.

## Progress

- [ ] `execplan_catalog.py` active/historical snapshot과 compatibility wrapper 분리
- [ ] `execplan_catalog_state.py` historical audit·merge-proof 진단 저장 경계 구현
- [ ] planning·registration·migration·dependency adapter를 snapshot API로 이관
- [ ] immutable link·stable path와 active-only compact projection 통합
- [ ] catalog/state/coordinator adapter targeted 회귀 테스트 추가

## Decision Log

- 실제 이동 전에 모든 실행·복구 경로가 archived 계획을 안전하게 이해해야 한다. 이 계획은 호환성만 Merge하고 archive 쓰기는 G-033이 원자적 정리 단계와 함께 활성화한다.
- archive 파일은 감사 자료이지 상태 원본이 아니다. 완료 증명이 없으면 새 실행 상태를 발명하지 않고 실행 불가 historical entry와 진단으로 격리한다.
- R-014의 “새 환경 복구”는 기존 제품의 명시적 DB 초기화·프로젝트 재등록 경계 안에서 이력을 재조정하는 것으로 구현하며 손상된 SQLite의 무인 자동 재생성 범위를 추가하지 않는다.
- Post-refactor revalidation: G-048/G-049 이후 catalog parsing·복구는 planning/execution coordinator 내부에 다시 구현하지 않고 독립 snapshot과 repository port로 제공한다. `loop_manager.py`는 외부 효과 wiring과 호환 진입점만 유지한다.
