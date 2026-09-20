---
id: G-011
title: Automatic Requirement Archiving
sources:
  - docs/requirements/[R-005] Automatic Requirement Archiving.md
depends_on:
  - G-014
complexity: high
risk: high
priority: high
---

## Purpose

Requirement의 마지막 관련 구현 PR에 완료된 문서의 archive 이동을 포함할 수 있는 런타임 기반을 만들고, 원래 source 식별자를 유지한 채 archived source를 안전하게 조회할 수 있게 한다. 기존 런타임으로 실행되는 이 도입 PR에서는 실제 Requirement 이동을 하지 않는다.

## Acceptance Criteria

- 현재 ExecPlan을 제외하고 같은 Requirement를 source로 사용하는 모든 ExecPlan이 `MERGED`일 때만 그 Requirement를 archive 대상으로 결정한다.
- 관련 `READY`, `IMPLEMENTING`, `VERIFYING`, `REVIEWING`, `PUBLISHING`, `PR_OPEN` 또는 `BLOCKED` ExecPlan이 하나라도 남아 있으면 Requirement를 이동하지 않는다.
- 하나의 ExecPlan이 여러 Requirement의 마지막 작업이면 조건을 충족한 문서를 모두 같은 구현 branch와 PR에서 `docs/requirements/archive/`로 이동한다.
- 최신 base rebase와 새 ExecPlan 등록 가능성을 반영해 PR 게시 직전에 archive 대상을 다시 계산하고, Worker 결과의 이동 집합과 달라지면 같은 Worker 세션에서 수정한 뒤 verify와 Review를 다시 수행한다.
- archive에 같은 이름의 파일이 있거나 source 관계 또는 상태를 확정할 수 없으면 덮어쓰지 않고 현재 ExecPlan을 구체적인 원인과 함께 `BLOCKED`로 만든다.
- 기존 ExecPlan의 `sources`는 원래 활성 경로를 유지하며, 활성 Requirement가 없을 때 같은 파일명의 archive 문서를 source 조회와 카탈로그 검증에 사용할 수 있다.
- 새 ExecPlan은 계속 활성 PRODUCT 또는 `docs/requirements/` 바로 아래 문서만 source로 사용할 수 있고 Planner는 archive를 입력으로 읽지 않는다.
- BLOCKED 해제용 내용 지문은 활성 Requirement에만 반응하며, archive 이동만으로 새 계획 주기나 프로젝트 완료 알림 주기를 만들지 않는다.
- G-011 Worker는 기존 런타임의 범위 검사로 실행되므로 이 PR에서 `docs/requirements/` 문서를 이동하거나 수정하지 않고 R-003과 R-005를 활성 경로에 유지한다.
- G-011이 Merge된 뒤에도 R-005가 활성 Planner 입력으로 남아야 하며, Planner가 G-011에 의존하는 bootstrap archive 후속 ExecPlan을 만들 수 있어야 한다.
- 후속 ExecPlan은 새 런타임에서 Git, GitHub와 SQLite 이력을 다시 확인하고 일반 eligibility 규칙으로 여전히 대상인 R-003과 R-005를 같은 PR에서 archive한다. 특정 Requirement를 이름만으로 특별 처리하지 않는다.
- 일반 운영에서는 구현 ExecPlan 자신의 Requirement도 마지막 관련 계획이면 구현과 함께 archive되며, PR이 Merge되지 않으면 base branch의 활성 문서는 그대로 남는다.

## Implementation

- ExecPlan source 경로와 SQLite 상태를 결합하는 archive eligibility 계산기를 별도 테스트 가능한 경계로 추가한다.
- Worker prompt에 계산된 archive 후보를 전달하고, Worker 결과 이후와 최종 게시 직전에 실제 이동 집합을 검증한다.
- 활성 source가 사라진 기존 ExecPlan에는 동일 파일명의 archive fallback을 적용하되 Planner의 새 source 검증과 계획 입력 수집은 활성 경로로 제한한다.
- archive 충돌과 불확실한 이력은 기존 BLOCKED 흐름으로 연결하고 원본 파일이나 archive 파일을 자동 덮어쓰지 않는다.
- 현재 저장소 이력에서 이미 완료된 Requirement를 일반 eligibility 규칙으로 식별하되, 실제 이동은 G-011 Merge 후 bootstrap archive 후속 ExecPlan으로 미룬다.

## Validation

- Worker는 새 archive eligibility 및 source fallback 테스트 모듈만 targeted unit test로 실행한다.
- 단일·복수 Requirement, 다른 비종료 계획, 현재 계획이 마지막인 경우와 이미 완료된 과거 계획을 검증한다.
- 최종 rebase 중 관련 계획 추가, archive 충돌, 상태 불확실, PR 미Merge와 archived source 조회를 검증한다.
- G-011 diff에는 Requirement 이동이 없고, R-003과 R-005가 활성 경로에 남으며 bootstrap 후속 ExecPlan에서 archive 후보가 되는 조건을 검증한다.
- 전체 verify에서 Planner 입력 경계, BLOCKED 지문, immutable PR source 링크와 archive-only fingerprint 회귀가 없어야 한다.

## Progress

- [ ] archive eligibility와 상태 조회 경계 구현
- [ ] Worker 이동 및 게시 직전 재검증 구현
- [ ] archived source fallback과 계획 입력 경계 구현
- [ ] 과거 완료 Requirement 후보 계산과 2단계 bootstrap 회귀 테스트 추가

## Decision Log

- Requirement 경로는 안정적 식별자로 유지하고 기존 ExecPlan frontmatter를 다시 쓰지 않는다.
- 게시 직전 base 동기화가 보장된 뒤 archive 조건을 확정해야 하므로 G-014에 의존한다.
- 2026-09-20 사용자 결정: 기존 런타임이 Requirement 변경을 모두 Worker 범위 위반으로 차단하므로, G-011은 기능만 병합하고 실제 R-003/R-005 이동은 새 런타임에서 실행되는 의존 후속 ExecPlan으로 분리한다.
- 2026-09-20 운영 복구: Worker 실행 중 별도 launcher의 `git pull --ff-only`가 사용자 기본 worktree와 `refs/heads/main`을 변경해 G-011이 범위 위반으로 차단됐다. 관리 worktree의 구현 변경은 보존했으며, `ec1be4e`에서 launcher의 Git 갱신을 제거한 뒤 이 기록 변경을 정상 BLOCKED 재평가 신호로 사용한다.
