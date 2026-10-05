---
id: G-190
title: Requirement 이동 영수증의 양쪽 경로 검증
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-068] Planner Judgments and Manager-Owned Requirement Retirement.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on: []
complexity: medium
risk: high
priority: critical
---

## Purpose

내용 보존 Requirement 이동·복원을 Git이 rename으로 표시해도 Manager 영수증의 원본·목적지를 모두 검증하고 같은 planning batch를 게시·재개할 수 있게 한다. 실제 변경을 놓치는 관측 경계만 교정하며 영수증·역할별 범위 검사를 유지한다.

## Acceptance Criteria

- Manager가 검증한 archive 이동과 조기 archive 복원의 원본·목적지가 unstaged, staged, commit 후 결과 검증과 재시작에서 모두 관측된다. rename 설정·감지 여부와 공백·비 ASCII 경로가 결과를 바꾸지 않는다.
- 유효한 이동만 영수증으로 Planner 변경 집합에서 분리한다. 원문 변조, 목적지 충돌·symlink, 거짓·누락 영수증, 무관한 Requirement 변경과 Planner의 직접 이동은 계속 거부한다.
- 새 계획과 이동이 함께 있는 batch 및 이동만 있는 batch가 동일한 manifest·내용 hash·원격 확인 계약을 따른다. 원격 경쟁·push 실패·중단에는 기존 원문·session·계획·영수증을 보존하며 중복 이동·commit·모델 재평가를 만들지 않는다.
- 영수증 오류를 사용자 Requirement 수정이나 운영 SQLite 기록 삭제로 해소하지 않는다. 유효하지 않은 판단은 기존 철회·재평가 경계로 처리하고, 독립 작업의 상태와 실제 안전 차단을 보존한다.

## Implementation

- planning 결과 관측과 retirement 영수증 검증·게시 manifest의 책임 경계를 대조한다. 저장된 dirty 목록만 믿지 말고 현재 index·worktree·기준 commit의 필요한 변경을 검증하며 rename의 양쪽 경로를 보존한다.
- planner_publication의 기존 `--no-renames` commit 검증과 NUL 경로 helper를 우선 재사용한다. 전체 Git 설정을 변경하거나 receipt 경로를 검증 없이 실제 변경으로 합성하지 않는다.
- Manager의 내용 보존 이동과 Agent의 허용 범위를 분리한 기존 계약을 유지한다. AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조하고 새 루트 항목이나 별도 정리 명령을 만들지 않는다.

## Validation

- tests/test_requirement_retirement.py와 planning validation/publication unit 경계에 실제 임시 Git 저장소의 내용 동일 rename을 추가한다. archive·복원 방향, 세 index/commit 상태, 오래된 dirty 목록과 재시작, rename 감지 설정 차이, 이동만/혼합 batch 및 실제 변조 거부를 targeted test로 확인한다. Agent 호출은 mock으로 차단한다.
- Manager 전체 verify는 mutable READY 권한, 연속 ID·DAG, Requirement 원문 보존, publication manifest·lease·원격 경쟁 복구와 독립 작업 지속을 회귀 검증한다.

## Progress

- [x] 2026-10-05 기준 a049a76의 planner_validation·workspace_service·retirement·publication 원본과 기존 unit 근거를 대조했다. 과거 내용 보존 이동 b6a0387의 읽기 전용 Git diff는 기본 name-only에서 목적지만, no-renames에서 두 경로를 반환한다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 알려진 관측 계약과 기존 helper를 연결하므로 complexity medium이다. Requirement 복원·이동 게시가 막히거나 범위 검사를 잘못 완화할 영향으로 risk high, 현재 자동 planning 게시를 막는 오류이므로 priority critical이다.
- 현재 R-002 복원은 Manager 소유의 미게시 작업이다. Planner는 원문·index·영수증을 변경하지 않으며, 원격 복원 확인 전 R-002의 후속 구현 계획을 만들지 않는다. 이 계획은 활성 R-068·R-055의 이동·재개 계약만 다룬다.
