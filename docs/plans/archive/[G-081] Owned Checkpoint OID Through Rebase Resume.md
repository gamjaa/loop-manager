---
id: G-081
title: Owned Checkpoint OID Through Rebase Resume
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: high
risk: critical
priority: critical
---

## Purpose

`PLAN_BLOCKED` 중 Loop Manager가 게시한 checkpoint가 있는 계획을 해제하고 최신 base에 rebase하면 로컬 HEAD는 새 OID가 되지만 원격 branch는 마지막 게시 checkpoint OID에 남는다. 현재 해제 경로는 `publication_remote_oid`를 지우고, 실행 checkpoint와 게시 경계는 저장된 checkpoint 소유권을 사용하지 않아 이 정상 상태를 `external_remote_oid`와 `PROJECT_BLOCKED`로 오인한다. 게시 증거를 실행·게시·복구 경계까지 연결한다.

## Acceptance Criteria

- PR이 없는 동일 ExecPlan의 게시 checkpoint가 있고, 저장된 plan ID·branch·관리 worktree·게시 commit identity와 tree 및 마지막 채택 원격 OID가 현재 원격 ref와 정확히 일치하면, `PLAN_BLOCKED` 해제와 Loop Manager 소유 rebase 뒤에도 그 원격 OID를 소유한 게시 기준으로 판정한다. 로컬 HEAD와 원격 OID의 차이만으로 프로젝트를 차단하지 않는다. 재활성화 전 HEAD, rebase 전후 HEAD와 실행 checkpoint 기록을 교차 검증하며, 관측 OID만으로 소유권을 부여하지 않는다.
- 차단 해제 시 실패 지문, 검증·Review 증거와 publication token을 초기화하는 기존 규칙을 지키면서 게시 checkpoint의 소유권 증거를 보존하거나 검증해 재구성한다. 재시작과 상태 저장 중단 뒤에도 실행 checkpoint와 게시 경계가 같은 기대 원격 OID를 사용한다. 새 Worker 결과, 다른 계획 활성화 또는 다른 branch의 과거 checkpoint를 현재 계획의 소유권으로 채택하지 않는다.
- 안전하게 재개한 계획은 최신 로컬 후보에 필요한 targeted test, 전체 verify와 Review가 성공하기 전 push 또는 PR 생성을 하지 않는다. 기존 PR이 없는 소유 branch의 rebase 후 push가 필요하면 마지막으로 검증한 원격 OID를 명시한 `--force-with-lease`만 사용하고, push 직전 원격 ref를 다시 확인한다. 성공한 push의 새 OID를 영속화한다.
- 실제 원격 OID가 저장된 마지막 채택 OID와 다르거나 게시 증거, branch·worktree 소유권, 로컬 rebase 계보 또는 PR 상태를 증명할 수 없으면 기존 안전 차단을 유지한다. 기대 OID가 없는 강제 push, 원격 ref 덮어쓰기, 다른 계획의 checkpoint 채택과 검증 생략을 허용하지 않는다.
- 이미 `publication_ownership_uncertain`으로 `PROJECT_BLOCKED`인 동일 유형의 기록은 게시 checkpoint·현재 원격 ref·실행 checkpoint·관리 worktree와 로컬 HEAD의 일치하는 증거를 모두 검증할 때에만 원래 실행 단계로 복원한다. 기존 ExecPlan 상태·세션·작업물과 별도 `PLAN_BLOCKED` 원인은 보존하고, 증명이 부족하면 차단 사유를 남긴다. 운영자가 SQLite 값을 수동으로 고치거나 임의로 강제 push할 필요가 없어야 한다.

## Implementation

- `loop_manager.py`의 `PLAN_BLOCKED` 해제 및 rebase 경계와 `runtime_state.py`의 재활성화 전이에서 게시 checkpoint의 소유 원격 OID와 rebase 전 HEAD를 검증 가능한 형태로 유지한다. 기존 `blocked_checkpoints`와 `execution_checkpoints`의 증거를 우선 사용하고, 단순 관측값을 소유권으로 승격하지 않는다.
- `execution_checkpoint.py`와 `execution_handlers.py`의 실행 재개 판정이 동일 계획의 검증된 게시 checkpoint OID를 기대 원격값으로 받게 한다. `publication_resume.py`와 게시 경계도 같은 기준을 사용해 검증 후 `--force-with-lease` 경로로 진행하며 기존 PR과 외부 원격 변경은 별도로 거부한다.
- `loop_manager.py`의 프로젝트 안전 gate에 위 증거를 확인하는 좁은 복구 경로를 추가한다. 현재 G-077 사고를 코드에 ID나 commit으로 고정하지 않고 재현 시나리오로 검증한다.

## Validation

- Worker는 수정한 경계에 직접 대응하는 `tests/test_execution_checkpoint.py`, `tests/test_execution_handlers.py`, `tests/test_publication_resume.py`, `tests/test_scoped_blocking.py`와 `tests/test_loop_manager.py`의 새 targeted unit case만 실행한다.
- 게시 checkpoint → Review 재시도 한도 차단 → 입력 변경 해제 → 최신 base rebase → 재시작 → 실행 checkpoint → 검증·Review → 명시적 lease push → PR 생성 순서를 검증한다. checkpoint OID와 rebase 후 로컬 HEAD가 다른 경우에도 원격이 마지막 채택 OID이면 정상 진행하며, 검증 전 push와 중복 checkpoint 게시가 없음을 확인한다.
- 원격 head의 외부 갱신·삭제·재작성, 누락되거나 위조된 게시 증거, 다른 계획·branch·worktree의 기록, 예상 밖 PR, rebase 증거 불일치 및 lease 경쟁은 차단한다. 이미 차단된 동일 유형의 복구와 증거 부족 시 차단 보존을 검증하고, 전체 verify는 기존 원격 소유권·PR·guidance checkpoint 안전 규칙의 회귀가 없어야 한다.

## Progress

- [x] 게시 checkpoint 소유 OID의 차단 해제·rebase·재활성화 전이 보존
- [x] 실행 checkpoint와 PR 전 게시 판정의 공통 소유권 검증 및 lease 연결
- [x] 기존 프로젝트 오탐의 증거 기반 복구와 targeted 회귀 사례

## Decision Log

- G-077은 guidance checkpoint의 게시·범위 재검사와 기존 G-058 이중 차단 복구를 다룬다. 이 계획은 게시 후 `PLAN_BLOCKED` 해제·rebase에서 실행 checkpoint와 publication 경계로 소유 OID가 이어지지 않는 별도 전이를 다루므로 G-077의 Merge를 선행 조건으로 두지 않는다.
- 확인된 사고에서는 원격 G-077 checkpoint와 `blocked_checkpoints.accepted_remote_oid`가 `aa155d4`로 일치했고, 관리 rebase 뒤 로컬 HEAD는 `b2dbcaa`였다. 해제 경로에서 `publication_remote_oid`가 초기화됐으며 실행 checkpoint의 `reactivated_from_head`는 `aa155d4`였다. 이 값은 재현 증거이며 구현 상수로 사용하지 않는다.
- 기존 PRODUCT는 PR 없는 소유 branch의 rebase 복구에 정확한 `--force-with-lease`만 허용한다. branch·commit·push 안전 경계의 변경이 ARCHITECTURE.md의 Architecture Change Policy에 해당하면 구현 전에 사용자 승인을 확인한다. `PROJECT_BLOCKED`가 유지되는 동안 자동 Worker나 Git 쓰기를 시작하지 않는다.

- 검토 피드백 반영: 최초 checkpoint identity의 원인·증거를 별도 nullable 컬럼에 보존한다. 기존 행은 재분류 전에 원래 입력을 보존하며, identity 검증을 생략하지 않는다. SQLite 변경은 기존 데이터 삭제 없는 호환 확장이다.
- push 성공 직후 중단은 저장된 push intent, 이전 원격 OID, 현재 로컬 HEAD와 live ref가 모두 일치할 때 복구하고 새 OID를 intent 해제 전에 저장한다.
- rebase 전에 작업 snapshot을 저장하고 중단 복구 시 파일 signature·경로·index와 설정을 대조한다. autostash로 풀린 staging만 검증 후 복원하며, 새 변경이나 증거 누락은 차단을 유지한다.
- Worker 검증은 변경 경계의 개별 unit test로 제한한다. 전체 verify와 최종 Reviewer 판정은 Loop Manager 단계에 남긴다.
