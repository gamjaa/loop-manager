---
id: G-074
title: Dirty-Preserving Source Fast-Forward
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-053
complexity: high
risk: high
priority: high
---

## Purpose

현재 `synchronize_base_worktree`는 ignored 파일을 제외한 로컬 변경이 하나라도 있으면 fast-forward를 생략한다. R-023은 원격 변경 경로와 겹치지 않는 staged·unstaged·untracked·ignored 변경을 그대로 보존하며 저장된 base branch를 전진시키도록 요구한다. 이 판정은 bootstrap freshness gate와 일반 프로젝트 source 동기화에서 공유한다.

## Acceptance Criteria

- 저장된 `<base-branch>` checkout, local HEAD의 behind-only 관계와 Git operation 부재를 확인한다. 원격 fast-forward가 바꿀 tracked 경로와 모든 로컬 staged·unstaged·non-ignored untracked·ignored 변경 경로를 양쪽 이름과 부모·자식 관계까지 비교한다. 겹침이 없으면 변경 내용과 index 상태를 보존하며 `git merge --ff-only` 상당으로 원격 SHA에 전진한다.
- 원격 SHA와 local HEAD가 같으면 로컬 변경이 있어도 no-op이다. behind-only 상태라도 겹침, 다른 branch·detached, ahead·diverged, Git operation, 비정상 index, eligibility 뒤 동시 변경 또는 Git 거부가 있으면 정확한 이유로 skip·fail하고 source 파일·index·HEAD를 보존한다.
- tracked rename·삭제·디렉터리 전환, 운영체제별 대소문자 충돌과 ignored overlap에서 변경 경로를 누락하지 않는다. reset·clean·stash·autostash·source rebase·강제 checkout은 수행하지 않는다.
- eligibility 및 실제 Git update의 결과가 기록되며 G-055 bootstrap은 skip·fail을 실행 차단으로, 최신 runtime에서 수행한 일반 project sync는 기존 origin-only 관리 작업과 분리된 non-gating 결과로 사용할 수 있다.
- 기본 worktree 동기화의 장기 규칙을 구현 PR에서 `docs/PRODUCT.md`에 반영한다. Architecture Change Policy의 worktree mutation 경계 변경에 필요한 사용자 승인 상태를 Worker 시작 전에 확인한다.

## Implementation

- `loop_manager.py`의 `_base_worktree_snapshot`·`synchronize_base_worktree`에 로컬 변경 경로와 원격 변경 경로의 보수적인 충돌 판정을 둔다. `git diff`의 rename 표시에 의존해 이전 경로를 놓치지 말고 Git의 실제 index·untracked·ignored 상태를 사용한다.
- 판정 직후 HEAD·branch·index·파일 signature·operation을 다시 비교하고 대상 remote SHA도 고정한다. race 또는 merge 실패 뒤 강제 복구 없이 구체적인 `BaseWorktreeSyncResult`를 반환한다.
- 일반 project source sync와 G-055 bootstrap이 같은 검증 함수를 사용하되 결과에 따른 실행 허용 판단은 각 호출자가 소유한다.

## Validation

- Worker는 `tests/test_origin_isolation.py`의 실제 Git safe fast-forward 및 `tests/test_loop_manager.py`의 직접 관련 source-sync targeted unit test만 실행한다.
- equal SHA와 dirty, behind-only와 staged·unstaged·untracked·ignored non-overlap, 같은 경로·부모/자식·rename·삭제·case 충돌, ahead·diverged·다른 branch·operation·index flag·race·Git 거부를 검증한다. 성공과 실패 전후 로컬 delta와 index 보존을 실제 checkout에서 비교한다.
- 전체 verify에서 관리 worktree의 origin-only 입력, ignored artifact 처리와 기본 worktree 비파괴 정책이 유지돼야 한다.

## Progress

- [ ] 로컬·원격 경로 충돌 판정과 race gate 구현
- [ ] dirty 보존 fast-forward 및 실패 진단 연결
- [ ] 실제 Git checkout targeted test 추가

## Decision Log

- 변경 경로의 출처를 증명할 수 없으면 자동 갱신을 생략한다. source checkout의 로컬 변경은 관리 worktree의 계획 입력이 아니다.
