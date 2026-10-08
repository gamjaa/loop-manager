---
id: G-215
title: Git 복구 테스트의 공통 준비 재사용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-003] Targeted Worker Unit Tests.md
depends_on:
  - G-214
complexity: low
risk: low
priority: normal
---

## Purpose

Git 복구 테스트의 반복된 경로 준비·ignored cache·실행 patch 책임을 기존 fixture와 작은 파일 내부 helper로 모은다. 외부 실행 계약이 동일한 유지보수 fixture의 Git wrapper는 공유 helper를 재사용한다.

## Acceptance Criteria

- cleanup rebase 사례의 info/exclude·metadata 경로 보정만 재사용하고 merge/apply backend·todo/done/next·ignored 충돌/이미 재생한 경로의 판정은 각 사례에 남는다.
- 네 PR 복구 파일의 동일 `_add_ignored_cache` 준비를 기존 PrRecoveryFixture에서 제공한다. clean snapshot 준비 확인과 tracked overlap·index flag·untracked/복구 상태의 각 assertion을 유지한다.
- workspace 예약의 반복된 저장소/base/safe-worktree patch와 run_project 인자 준비만 묶는다. 중단 주입·Agent mock·branch 충돌·상태/작업물 보존의 사례별 차이는 명시적으로 유지한다.
- `tests/support/maintenance_planning_case.py`의 `git` 이름/인자/예외/UTF-8·stderr 병합·stdout.strip 계약을 `tests/support/git.py`의 동일 helper로 보존한다. 생산 사용자 동작·공개 계약(CLI)·저장 형식과 테스트 격리/필수 범위를 보존한다.

## Implementation

- G-214의 연결 소유권 변경을 먼저 재사용한다. PrRecoveryFixture, WorkspaceFixture와 dummy_worker_sessions를 유지하고 국소 준비 helper만 추출한다. 정책·시나리오 assertion을 공통 실행 helper로 숨기지 않는다.
- 동일한 Git wrapper는 기존 이름의 import/얇은 호환 경계를 사용한다. stderr/인코딩 계약이 다른 `tests/test_blocked_remote_observation.py`의 wrapper를 이 변경에 포함하지 않는다. 새 전역 fixture cache나 범용 patch framework는 추가하지 않는다.

## Validation

- Worker targeted unit: 변경 helper/fixture의 정상·준비 실패·patch 해제와 상대/절대 Git metadata 경로 사례. 유지보수 Git helper의 반환값·실패 예외 계약을 확인한다.
- Manager 전체 verify: `tests/test_cleanup_rebase_artifacts`, 네 PR 복구 파일, `tests/test_workspace_reservations` 및 유지보수 planning/publication의 기존 실제 Git 중단·경쟁·보존 사례를 같은 범위로 수집한다. 실제 복구 검증을 모두 mock으로 바꾸지 않는다.

## Progress

- [x] 저장된 평가 발견 12·19·31·33의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `tests/test_cleanup_rebase_artifacts.py:47`, `tests/test_pr_conflict_scope.py:12`, `tests/test_workspace_reservations.py:70`, `tests/support/maintenance_planning_case.py:30`의 중복 준비 판단을 재사용했다.
- G-214와 같은 workspace 예약 파일을 변경하므로 준비/cleanup 계약을 먼저 확정한다. 추가 기존 READY prerequisite는 없다. complexity low는 기존 helper의 국소 재사용, risk low는 격리된 테스트 준비에 한정돼 diff와 fixture 복원으로 되돌릴 수 있는 영향이다.
