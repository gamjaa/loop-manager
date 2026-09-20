---
id: G-032
title: Archived ExecPlan Catalog and Recovery Compatibility
sources:
  - docs/requirements/[R-014] Automatic Completed ExecPlan Archiving.md
depends_on:
  - G-029
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

- 현재 `load_execplan_catalog`의 호출 목적을 active execution catalog와 full historical validation catalog API로 분리한다. 두 위치의 regular-file 검사, 중복 ID/path 검증과 stable active-path 계산을 공통 parser 위에 둔다.
- Planner batch validation과 maintenance revision publication은 새 파일의 연속 ID를 full catalog 최대값으로 검사하고, 수정 가능 대상은 active top-level READY snapshot으로 제한한다.
- `migrate_legacy_execplans`와 project registration reconciliation에 archived entry를 위한 merge-proof adapter를 추가한다. GitHub PR 및 immutable plan commit 이력을 재사용하고 증명 실패를 실행 상태와 분리된 진단으로 기록한다.
- ExecPlan row에는 필요할 경우 archive 관측 경로·검증 hash와 audit 상태를 additive column 또는 별도 table로 저장하되 `path`와 MERGED source of truth를 바꾸지 않는다.
- dependency gate는 DB의 확인된 MERGED 상태를 사용하고 file location을 상태 판정에서 배제한다. compact catalog serializer와 Agent prompts에는 archive metadata를 추가하지 않는다.

## Validation

- Worker는 새 ExecPlan archive-catalog test module 또는 parser/migration의 직접 관련 test class만 targeted unit test로 실행한다.
- active+archive ID 중복, 다음 번호 연속성, archived dependency 존재와 cycle 검증 및 active-only AI 입력을 검증한다.
- archived MERGED dependency 성공, archive 파일만 존재하는 미확인 dependency 대기와 hash 변조 진단을 검증한다.
- Merge commit, Squash merge와 Rebase merge 이력을 이용한 기존 설치 및 새 등록 복구, Merge 증명 부재와 불일치 PR 이력을 검증한다.
- stable active path, immutable PR 링크, archived 위치 관측과 compact catalog의 비노출을 검증한다.
- maintenance barrier/revalidation이 archive를 수정하지 않고 historical dependency를 보존하는지 검증한다.
- 이 계획 Merge만으로 파일 이동, archive commit·push, Agent 호출 또는 완료 알림이 발생하지 않는지 검증한다.
- 전체 verify에서 기존 active catalog parsing, Planner batch publication, legacy ExecPlan migration, READY priority/dependency 선택과 Requirement archive가 유지돼야 한다.

## Progress

- [ ] active와 historical ExecPlan catalog API 분리
- [ ] 전체 ID·dependency·번호 검증과 stable path 계약 구현
- [ ] archived Merge 증명 및 migration/recovery 진단 구현
- [ ] maintenance·Planner·PR 링크 호출자 통합
- [ ] archived catalog 호환성 테스트 추가

## Decision Log

- 실제 이동 전에 모든 실행·복구 경로가 archived 계획을 안전하게 이해해야 한다. 이 계획은 호환성만 Merge하고 archive 쓰기는 G-033이 원자적 정리 단계와 함께 활성화한다.
- archive 파일은 감사 자료이지 상태 원본이 아니다. 완료 증명이 없으면 새 실행 상태를 발명하지 않고 실행 불가 historical entry와 진단으로 격리한다.
- R-014의 “새 환경 복구”는 기존 제품의 명시적 DB 초기화·프로젝트 재등록 경계 안에서 이력을 재조정하는 것으로 구현하며 손상된 SQLite의 무인 자동 재생성 범위를 추가하지 않는다.
