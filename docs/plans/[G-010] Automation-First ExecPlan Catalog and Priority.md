---
id: G-010
title: Automation-First ExecPlan Catalog and Priority
sources:
  - docs/requirements/[R-001] Automation-First Planning and Priority.md
depends_on:
  - G-005
complexity: high
risk: high
---

## Purpose

계획 입력이 바뀔 때 현재 식별 가능한 미충족 작업 전체를 한 번에 계획하고, 비종료 ExecPlan의 영속 카탈로그에서 의존성과 자동화 중심 priority를 적용해 다음 Worker를 결정적으로 선택한다.

## Acceptance Criteria

- 공통 `docs/PLANS.md`와 ExecPlan 파서는 `critical`, `high`, `normal`, `low` 중 하나인 `priority`를 지원하고 새 계획에는 이를 필수로 요구한다. 기능 도입 전에 작성된 계획처럼 `priority`가 없는 기존 ExecPlan은 `normal`로 해석하며 잘못된 값은 거부한다.
- SQLite가 프로젝트별 ExecPlan의 ID, 경로, sources, depends_on, priority와 실행 상태를 영속화하고, 상태의 유일한 원본 역할을 한다. 기존 단일 활성 작업 DB는 데이터 손실 없이 이관되며 이미 완료된 기존 계획을 READY로 잘못 재등록하거나 다시 실행하지 않는다.
- 실행 중인 IMPLEMENTING, VERIFYING, REVIEWING, PR_OPEN 또는 BLOCKED 계획은 새 입력이나 더 높은 priority로 선점하지 않는다.
- 실행 중인 계획이 없고 계획 입력이 변경됐으면 기존 READY 계획이 있어도 다음 Worker를 선택하기 전에 Planner를 실행한다. 입력이 변경되지 않았으면 Planner를 다시 호출하지 않고 기존 READY 카탈로그에서 선택한다.
- Planner에게 전달하는 compact catalog에는 비종료 ExecPlan의 ID, 제목, SQLite 상태, sources, depends_on과 priority만 포함하고 AI 요약을 만들지 않는다. MERGED 계획은 prompt와 기본 읽기 대상에서 제외하되 프로그램은 ID 고유성, 번호 할당과 의존성 판단을 위해 계속 관리한다.
- Planner prompt는 현재 코드·테스트와 활성 제품 입력에서 식별 가능한 미충족 작업 전체를 독립적으로 검증·Merge 가능한 ExecPlan들로 작성하도록 요구하고, compact catalog가 충족하는 작업이나 이미 구현된 작업을 다시 계획하지 않도록 한다.
- 한 Planner 결과에서 새 `docs/plans/*.md` 여러 개를 허용하되 그 밖의 경로 변경, 기존 계획 덮어쓰기, 중복 ID와 잘못된 메타데이터·source·의존성·순환 그래프는 게시 전에 거부한다. 추가 작업이 없으면 기존 계획 파일을 변경하지 않는다.
- 유효한 새 ExecPlan 전체는 하나의 계획 commit으로 최신 `origin/<base-branch>`에 fast-forward push되고, 원격에서 같은 commit을 확인한 뒤에만 하나의 transaction으로 READY 등록된다. base 변경, push 실패 또는 중단 시 일부 계획만 READY가 되지 않으며 기존 계획 게시 복구 경계를 유지한다.
- READY 후보에는 모든 depends_on이 MERGED인 계획만 포함하고 `critical`, `high`, `normal`, `low` 순서와 숫자 ID 오름차순으로 하나를 선택한다. 충족되지 않은 의존성을 우회하지 않고 같은 프로젝트에서 둘 이상의 계획을 동시에 시작하지 않는다.
- 선택된 각 ExecPlan은 기존 결정적 branch·별도 worktree·Worker→VERIFYING→REVIEWING→PR 흐름을 그대로 사용하고, PR Merge 확인 시 해당 계획만 MERGED로 전이한 뒤 다음 실행 가능한 READY 계획을 선택할 수 있다.
- priority 호환성, 기존 DB 이관, compact catalog 경계, 여러 계획의 원자적 게시·등록, 변경된 입력의 READY 선행 재계획, dependency gate와 결정적 선택, 비선점 및 중단 복구를 자동 테스트로 검증한다.

## Implementation

- `docs/PLANS.md`, frontmatter 파서와 immutable 게시 메타데이터 파서를 함께 확장해 새 계획의 priority를 검증하고 기존 계획에는 코드에서 `normal` 기본값을 적용한다.
- 프로젝트 단위 planning 상태와 계획별 실행 상태를 분리하는 ExecPlan 저장소를 추가한다. 기존 `projects` 행의 활성 plan, phase, 세션, worktree, branch와 PR 정보를 호환 가능한 migration으로 계획별 레코드에 연결하고, 기존 계획의 완료 여부는 저장 상태와 Git/GitHub 이력으로 보존적으로 대조해 재실행을 방지한다.
- 전체 파일 카탈로그와 SQLite 상태를 조합해 비종료 compact catalog를 결정적으로 직렬화하는 경계를 만들고 Planner prompt에 전달한다. MERGED 본문은 Planner 입력으로 읽지 않지만 전체 파일 카탈로그 검증에는 유지한다.
- Planner 결과 검증을 단일 파일 가정에서 새 ExecPlan 집합으로 확장하고, 시작 commit 대비 새 파일만 허용한 뒤 전체 카탈로그와 의존성 그래프를 한 번 검증한다. 기존 최대 숫자 ID 다음의 연속적이고 고유한 ID를 사용하도록 검증한다.
- 계획 commit 게시와 READY 등록을 분리해 원격 확인 이후 한 transaction에서 모든 새 계획을 등록하고, push 후 DB 기록 전 중단된 경우 원격 commit의 계획 집합을 다시 파싱해 중복 없이 복구한다.
- 실행 중인 계획 유무, 계획 입력 지문과 READY 존재를 명시적으로 판정하는 orchestration 경계를 만든다. 입력 변경 재계획이 끝난 뒤 dependency gate와 priority/ID 정렬을 적용해 하나의 READY 계획만 활성화한다.
- PR 상태 동기화가 현재 계획의 상태만 갱신하도록 기존 단일 작업 흐름을 계획별 저장소에 연결하고, MERGED 전환과 worktree 정리 뒤 남은 READY 계획 또는 새 계획 주기로 자연스럽게 이어지게 한다.

## Validation

- 새 priority의 정상·오류 값과 priority가 없는 기존 G-001~G-010을 `normal`로 읽는 parser targeted unit test
- 기존 단일 활성 작업과 idle DB를 계획별 저장소로 이관해 현재 작업·세션·PR을 보존하고 완료된 계획을 재실행하지 않는 migration targeted unit test
- MERGED 계획을 제외하고 비종료 계획의 허용된 필드만 결정적 순서로 제공하는 compact catalog 및 Planner prompt targeted unit test
- 한 Planner 실행이 여러 새 계획을 만들거나 변경 없음으로 끝나는 경우와 범위 위반·기존 파일 수정·ID 간격·전체 dependency graph 오류를 검증하는 targeted unit test
- 계획 commit 원격 확인 전에는 READY를 만들지 않고, 여러 계획을 원자적으로 등록하며 push 후 중단 복구에서 중복 등록하지 않는 상태 전이 targeted unit test
- 변경된 입력이 기존 READY보다 먼저 Planner로 가고, 변경 없는 입력은 Planner를 건너뛰며, 활성 작업은 선점하지 않는 orchestration targeted unit test
- dependency 미충족 계획을 제외한 뒤 priority와 숫자 ID로 하나만 선택하고 PR Merge 후 다음 후보를 선택하는 targeted unit test
- 전체 verify에서 기존 planning validation 재시도, 계획 입력 지문, Worker·verify·Reviewer, immutable PR 문맥과 Merge 후 cleanup 흐름이 회귀하지 않는지 확인

## Progress

- [ ] priority 형식과 기존 ExecPlan 호환 파싱 추가
- [ ] 계획별 SQLite 카탈로그와 호환 migration 구현
- [ ] compact catalog 기반 일괄 Planner 결과 검증·게시·복구 구현
- [ ] 입력 변경 gate와 dependency·priority READY 선택 연결
- [ ] migration, Planner batch, 선택 순서와 기존 실행 흐름 회귀 테스트 추가

## Decision Log

- R-005의 자동 Requirement archive는 관련 계획 전체의 MERGED 상태를 신뢰할 수 있어야 하므로, 그 선행 기반인 R-001의 영속 카탈로그와 선택 규칙을 먼저 구현한다.
- 이 변경 전에 게시된 ExecPlan에는 `normal`을 적용해 기존 파일을 일괄 수정하지 않고도 새 공통 형식으로 이행한다.
- 여러 READY 계획과 계획별 상태를 프로젝트의 단일 phase 행에 계속 누적하지 않고 별도 저장 경계로 분리해 후속 archive 판단과 복구가 같은 원본을 재사용하게 한다.
