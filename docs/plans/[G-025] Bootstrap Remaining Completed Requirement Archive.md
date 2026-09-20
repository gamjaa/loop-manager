---
id: G-025
title: Bootstrap Remaining Completed Requirement Archive
sources:
  - docs/requirements/[R-002] Merge-Independent Worktree Cleanup.md
  - docs/requirements/[R-004] Descriptive Worker Commits and PR Context.md
  - docs/requirements/[R-006] Timestamped Execution Progress Logs.md
  - docs/requirements/[R-008] Progress-Aware Verification and Review Recovery.md
  - docs/requirements/[R-009] Review-Driven Exceptional Worker Level Escalation.md
depends_on:
  - G-011
complexity: low
risk: medium
priority: high
---

## Purpose

G-011보다 먼저 Merge돼 구현과 회귀 테스트가 이미 존재하지만 활성 Planner 입력에 남아 있는 R-002, R-004, R-006, R-008과 R-009를 일반 archive eligibility 규칙으로 정리한다. 기존 ExecPlan의 source 식별자나 Requirement 내용을 바꾸지 않고 완료 문서를 활성 입력에서 제거해 중복 계획을 방지한다.

## Acceptance Criteria

- G-011이 `MERGED`가 되기 전에는 이 계획의 Worker를 시작하지 않는다.
- Worker 시작 시점과 최종 게시 직전에 Git, GitHub와 SQLite 이력을 G-011의 일반 eligibility 규칙으로 다시 계산하고, 현재 ExecPlan 외에 각 Requirement를 source로 사용하는 비종료 ExecPlan이 없는 문서만 archive 대상으로 유지한다.
- 여전히 대상인 `docs/requirements/[R-002] Merge-Independent Worktree Cleanup.md`, `docs/requirements/[R-004] Descriptive Worker Commits and PR Context.md`, `docs/requirements/[R-006] Timestamped Execution Progress Logs.md`, `docs/requirements/[R-008] Progress-Aware Verification and Review Recovery.md`와 `docs/requirements/[R-009] Review-Driven Exceptional Worker Level Escalation.md`를 내용 변경 없이 동일한 파일명으로 `docs/requirements/archive/`에 이동한다.
- eligibility가 달라져 대상이 아닌 Requirement는 이동하지 않으며, 새 관련 계획을 무시하거나 Requirement ID별 예외로 archive하지 않는다.
- archive에 같은 파일명이 이미 있거나 source 관계와 상태를 확정할 수 없으면 기존 파일을 덮어쓰지 않고 구체적인 원인과 함께 이 ExecPlan을 `BLOCKED`로 전환한다.
- archive 이동과 선택된 ExecPlan 진행 기록 외에 코드, 테스트, 다른 Requirement, PRODUCT, ARCHITECTURE와 기존 ExecPlan frontmatter를 변경하지 않는다.
- 기존 ExecPlan과 이 계획의 `sources`는 원래 활성 Requirement 경로를 유지하며, 이동 후 source 조회와 카탈로그 검증은 G-011의 동일 파일명 archive fallback으로 성공한다.
- archive 이동은 이 구현 branch와 PR에 함께 포함되고, PR이 Merge되기 전 base branch의 활성 Requirement는 그대로 유지된다.
- archive-only 입력 변화는 새 작업 주기나 프로젝트 완료 알림 주기를 만들지 않고, 다음 Planner 입력에서는 이동된 Requirement를 제외한다.

## Implementation

- G-011이 Worker에게 제공하는 archive 후보와 게시 직전 재검증 결과를 사용하고, 런타임 코드에 이 계획의 Requirement ID를 위한 특별 처리를 추가하지 않는다.
- 각 eligible 활성 Requirement를 Git 이동으로 archive에 보존하고 원문 bytes가 동일한지 확인한다.
- 최종 rebase 뒤 후보 집합이 바뀌면 G-011의 기존 Worker 재개 흐름에서 실제 이동 집합을 최신 eligibility와 일치시킨다.
- 선택된 ExecPlan의 Progress에는 실제 후보 판정과 이동 결과만 기록하고 기존 plan source 경로는 수정하지 않는다.

## Validation

- 문서 이동 전용 변경이므로 Worker는 targeted unit test를 실행하지 않고 그 사유를 결과에 기록한다.
- Worker는 각 대상의 활성 경로 삭제와 archive 경로 추가가 rename으로 대응하고 파일 내용이 보존됐으며 비대상 Requirement가 변경되지 않았는지 확인한다.
- G-011 런타임의 게시 전 검증으로 현재 plan이 각 이동 Requirement의 마지막 비종료 source인지, archive 충돌이 없는지와 archived source fallback이 유효한지 확인한다.
- 전체 verify에서 Planner 입력 경계, ExecPlan catalog/source 검증, immutable PR source link와 archive-only fingerprint 동작이 유지돼야 한다.

## Progress

- [ ] 실행 시점의 다섯 Requirement archive eligibility 재확인
- [ ] eligible Requirement를 archive로 이동하고 내용 보존 확인
- [ ] 게시 직전 후보 재검증과 archived source fallback 확인

## Decision Log

- G-007, G-008, G-009, G-013과 G-019의 구현은 자동 archive 런타임 G-011보다 먼저 Merge됐으므로 당시 Worker가 완료 Requirement를 이동할 수 없었다.
- G-023은 R-005의 명시된 bootstrap 경계에 따라 R-003과 R-005만 처리하고 다른 활성 Requirement의 완료 여부를 범위에서 제외하므로, 이 계획이 나머지 선행 완료 문서를 별도로 정리한다.
- 완료 여부와 이동 가능성은 실행 시점의 일반 eligibility 계산을 원본으로 삼으며 특정 Requirement에 대한 런타임 예외를 만들지 않는다.
