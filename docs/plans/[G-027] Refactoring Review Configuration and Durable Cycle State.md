---
id: G-027
title: Refactoring Review Configuration and Durable Cycle State
sources:
  - docs/requirements/[R-001] Automation-First Planning and Priority.md
  - docs/requirements/[R-012] Periodic Refactoring Assessment and Execution.md
depends_on:
  - G-016
  - G-024
complexity: medium
risk: high
priority: normal
---

## Purpose

주기적 리팩터링 평가를 프로젝트별로 결정하고 중단 뒤에도 같은 주기를 복구할 수 있도록 공통 설정, Merge 기준선, 활성 유지보수 주기와 bounded Git 변경 문맥의 코드 경계를 추가한다. 이 계획에서는 아직 유지보수 Planner를 scheduler에 연결하지 않아, 후속 계획의 안전한 batch 검증·게시 계약이 Merge되기 전에 자동 평가가 시작되지 않게 한다.

## Acceptance Criteria

- 공통 `[loop].refactoring_review_interval` 설정은 모든 등록 프로젝트에 적용되는 양의 정수이며 생략하면 `5`를 사용한다.
- 0, 음수, 정수가 아닌 설정은 프로젝트 작업이나 상태 변경을 시작하기 전에 비밀값을 노출하지 않는 설정 오류로 거부한다.
- 프로젝트별 유지보수 상태는 마지막 성공 평가의 원격 commit, 그때까지 확인된 MERGED ExecPlan 기준값, 활성 주기 단계와 시작 기준, prerequisite READY 집합, 생성된 리팩터링 계획 집합, 장벽 뒤 재검증 대상 및 필요한 게시 복구 정보를 SQLite에 저장할 수 있다.
- 유지보수 주기 단계는 최소한 idle, assessment, prerequisite wait, refactoring, post-refactor revalidation을 명시적으로 구분하며 ExecPlan 실행 상태를 새 상태로 확장하거나 파일 frontmatter에 실행 상태를 기록하지 않는다.
- 평가 이력이 없는 프로젝트는 최초 평가가 필요한 것으로 판정하고, 이력이 있으면 마지막 성공 평가 뒤 새로 MERGED가 확인된 ExecPlan 수를 audit catalog에서 결정적으로 계산한다.
- 활성 유지보수 주기가 있으면 그 주기에서 Merge된 리팩터링 계획을 새 주기의 도래 조건으로 사용하지 않고 기존 주기를 그대로 반환한다.
- 평가 없음 결과는 현재 원격 commit과 MERGED 기준값으로 성공 기준선을 전진시킬 수 있고, 리팩터링 주기의 기준선은 사후 READY 재검증 성공 전에는 전진시킬 수 없다.
- 저장된 plan ID 집합은 중복·알 수 없는 ID·비문자열 값을 거부하고 결정적인 순서로 직렬화하며, schema migration은 기존 프로젝트·ExecPlan·세션·실패 이력을 보존하는 additive 변경이다.
- 직전 성공 평가 commit부터 현재 `origin/<base-branch>` commit까지의 commit·경로 중심 Git 변경 문맥을 결정적으로 만들되, 최대 commit 수와 출력 크기를 코드 상수로 제한하고 현재 code/test snapshot을 대체하는 요약으로 사용하지 않는다.
- 기준 commit이 현재 원격 이력의 조상이 아니거나 bounded history를 안전하게 만들 수 없으면 임의의 전체 이력이나 로컬 branch로 대체하지 않고 유지보수 평가가 진단 가능한 planning error로 중단할 수 있는 결과를 반환한다.
- 이 계획이 Merge된 뒤에도 일반 Planner 실행 조건, READY 선택 순서와 기존 plan publication 동작은 바뀌지 않으며 유지보수 평가 호출은 G-029가 활성화할 때까지 발생하지 않는다.

## Implementation

- `managed_config.py`의 immutable 설정 모델과 parser에 기본값 5인 `refactoring_review_interval`을 추가하고 예시 설정과 설정 검증 테스트를 갱신한다.
- 유지보수 상태와 plan ID 집합을 전담하는 repository/service 경계를 별도 모듈로 두고, 기존 `projects` 및 `exec_plans` 책임과 섞인 ad-hoc 전역 상태를 추가하지 않는다.
- 기존 MERGED audit row를 기준으로 due 여부와 기준선 전진을 계산하는 순수 함수를 만들고, 활성 주기 생성·단계 전이·완료를 transaction 단위로 제공한다.
- 원격 commit 두 개 사이의 제한된 변경 이력을 인자 배열 Git 호출로 수집하고, 경로·commit 순서를 정규화해 Planner prompt가 사용할 구조화된 값으로 반환한다.
- 후속 G-028과 G-029가 게시 전후 상태를 원자적으로 결합할 수 있도록 주기 record에 시작 base SHA, 기대 단계와 revision 정보를 포함하되 이 계획에서는 scheduler가 해당 API를 호출하지 않는다.

## Validation

- Worker는 managed configuration 테스트와 새 유지보수 상태 repository/service의 test class 또는 test module만 targeted unit test로 실행한다.
- 기본값과 사용자 설정값, 0·음수·비정수 거부 및 설정 오류 전에 프로젝트 상태가 바뀌지 않는 경우를 검증한다.
- 평가 이력 없음, 주기 미도달·도달, 프로젝트별 독립 MERGED 수, 활성 주기 중첩 방지와 평가 없음 기준선 전진을 검증한다.
- prerequisite·refactoring·revalidation plan ID 집합의 결정적 round-trip, 잘못된 ID 거부와 기존 DB의 무손실 additive migration을 검증한다.
- bounded history의 정상 범위, 크기 제한, 변경 없음, 비조상·유실 기준 commit 오류가 로컬 worktree나 branch를 변경하지 않는지 검증한다.
- 전체 verify에서 기존 config, registry, ExecPlan schema migration, planning fingerprint와 READY 선택 테스트가 변경되지 않아야 한다.

## Progress

- [ ] 공통 refactoring review interval 설정과 검증 추가
- [ ] 프로젝트별 유지보수 주기 schema 및 repository 구현
- [ ] MERGED 기준선과 due 계산 구현
- [ ] bounded 원격 Git 변경 문맥 구현
- [ ] 비활성 foundation 및 migration 회귀 테스트 추가

## Decision Log

- 유지보수 평가는 새 ExecPlan 상태가 아니라 프로젝트별 orchestration 상태이므로 별도 additive state record로 관리한다.
- 최초 평가는 등록 시각이나 스케줄러 실행 횟수가 아니라 평가 이력 부재로 표현한다.
- 부분 구현이 READY 선택을 막지 않도록 이 계획은 저장·계산 API만 제공하고 자동 평가 진입은 G-029에 맡긴다.
