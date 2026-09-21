# [R-022] Ignored-Only Managed Worktree Tolerance

## Context

Loop Manager는 Agent 실행 전후의 Git snapshot을 비교해 역할별 허용 범위 밖의 변경을 차단한다. 현재 snapshot은 `.gitignore`에 포함된 untracked 파일도 수집하므로 Planner, Reviewer 또는 Manager 검증이 자동 생성한 `__pycache__/*.pyc`, test cache와 임시 artifact까지 작업 결과와 동일한 범위 위반으로 취급할 수 있다.

R-016은 이 문제를 Planner의 Python bytecode 기록만 예방하는 방식으로 해결했다. 그러나 Worker의 targeted test, 전체 verify, Reviewer의 조사 명령과 다른 도구도 ignored artifact를 만들 수 있어 역할별 예방 설정을 계속 추가해야 하며, 새 도구가 생길 때마다 같은 문제가 반복된다. R-021에서도 전체 verify가 생성한 ignored bytecode 때문에 게시 가능한 변경이 없는 Merge 가능한 PR이 차단되는 사례가 확인됐다.

ignored untracked 파일은 강제로 index에 추가하지 않는 한 commit과 PR에 포함되지 않는다. 반면 tracked 파일, staged entry, non-ignored untracked 파일과 Git ref·worktree·config·operation 상태는 게시 결과나 작업 공간 소유권을 바꿀 수 있으므로 계속 엄격하게 검사해야 한다. 두 종류를 하나의 dirty 상태로 다루는 것은 저장소에 반영되는 변경의 안전성과 로컬 파생 artifact의 존재를 불필요하게 결합한다.

Planner와 Worker는 조사, targeted test, 전체 verify와 빌드 과정에서 필요에 따라 ignored 파일을 생성·수정·삭제할 수 있다. 이 로컬 작업 부산물을 저장소에 반영되는 결과와 동일하게 제한하면 정상적인 도구 실행이 역할 범위 위반이나 복구 불가능한 snapshot 차이를 만들 수 있다. 이 Requirement는 ignored-only 상태를 비권위 로컬 상태로 분류하고, 저장소에 반영되거나 Git 작업의 안전성에 영향을 주는 경계만 차단한다.

## Requirement

Loop Manager의 Agent 범위 검사와 관리 worktree 소유권·cleanliness 검사는 다음 상태를 권위 있는 변경으로 계속 비교하고 허용 범위를 벗어나면 차단해야 한다.

- tracked 파일과 index의 staged entry 및 non-default index flag
- non-ignored untracked 파일
- `HEAD`, branch, refs, 등록 worktree, Git config와 진행 중인 Git operation
- 역할별 허용 경로, 선택된 ExecPlan과 승인된 guidance 범위를 벗어난 게시 가능한 변경

권위 있는 변경을 검사한다는 것은 Planner와 Worker의 정상적인 파일 생성·수정·삭제를 금지한다는 뜻이 아니다. Planner는 새 ExecPlan과 허용된 `docs/FUTURE_IDEAS.md` 변경을, Worker는 선택된 ExecPlan 구현에 필요한 코드·테스트·문서 변경을 기존 역할별 경계 안에서 만들고 commit할 수 있다. Reviewer만 권위 있는 파일 변경을 남길 수 없다.

Git의 기준 규칙에 따라 ignored로 분류되고 index에 없는 파일의 생성·수정·삭제만으로는 Planner, Worker 또는 Reviewer의 범위 위반, planning error, ExecPlan BLOCKED, 관리 worktree 소유권 실패 또는 PR 상태 복구 실패를 만들지 않아야 한다. Python bytecode, test cache, build cache와 임의의 다른 ignored 파일에 파일명별 예외나 allowlist를 추가하지 않고 같은 ignored-only 규칙을 적용한다.

Planner와 Worker는 역할 수행에 필요한 ignored 파일을 자유롭게 생성·수정·삭제할 수 있으며 Agent 종료 전에 원래 상태로 복원할 의무가 없다. Worker의 targeted test, Loop Manager의 전체 verify와 Reviewer는 기존과 같이 같은 ExecPlan 관리 worktree에서 이어서 실행한다. ignored 상태를 격리하기 위한 별도 검증 worktree, 복제된 checkout 또는 추가 검증 단계를 만들지 않는다.

ignored 여부는 Agent 또는 Manager 단계가 시작될 때 적용된 ignore 규칙을 기준으로 판정한다. 실행 중 `.gitignore`, repository exclude 설정 또는 관련 Git config를 변경해 새 경로를 ignored로 숨길 수 없어야 한다. ignore 규칙 자체의 tracked·config 변경은 기존 범위 검사 대상이며, 시작 규칙으로 ignored임을 증명할 수 없는 새 경로는 non-ignored 변경과 동일하게 판단한다.

ignored 파일을 `git add -f` 등으로 index에 추가하면 더 이상 ignored-only 상태가 아니며 staged entry와 역할별 허용 경로를 기존과 같이 검사한다. ignored 파일과 tracked 경로가 같거나 부모·자식 관계로 겹쳐 checkout, rebase, merge 또는 다른 Manager Git 쓰기가 이를 덮어쓸 수 있으면 파일을 삭제하거나 덮어쓰지 않고 해당 Git 작업을 시작하지 않는다. 활성 작업 중 충돌하지 않는 ignored 파일을 복구 목적으로 clean, reset, stash, stage, commit 또는 push하지 않는다.

planning 결과가 게시됐거나 작업 없음으로 확정된 planning worktree, 그리고 PR Merge 뒤 모든 commit이 게시됐고 권위 있는 미커밋 변경이 없는 ExecPlan 관리 worktree는 정확한 Manager 소유권과 결정적 경로를 다시 검증한 뒤 전체 worktree 수명 종료 과정에서 제거할 수 있다. ignored-only 파일은 이 정상 정리를 막거나 `cleanup_pending`을 만들지 않는다. 이는 활성 작업의 개별 ignored 파일을 정리하는 권한이 아니며, 등록 프로젝트의 기본 worktree, 소유권이 불명확한 경로 또는 권위 있는 변경이 남은 worktree에는 적용하지 않는다.

이전 정책으로 저장된 Agent snapshot 또는 범위 오류도 같은 분류로 재평가한다. 저장된 기준과 현재 상태의 차이가 ignored-only 파일뿐이고 `HEAD`, branch, index, tracked·non-ignored untracked 상태와 공유 Git metadata가 모두 기존 기대값과 일치하면, ignored 경로를 snapshot 기준으로 복사하거나 삭제하지 않고 비권위 차이로 제외해 기존 세션, worktree, 검증 결과와 작업 단계를 멱등하게 재개한다. 권위 있는 변경이 하나라도 함께 있거나 시작 ignore 규칙을 재구성할 수 없으면 snapshot을 재설정하거나 전체 상태를 신뢰하지 않고 기존 차단을 유지한다.

R-016의 Planner bytecode 예방 설정은 기존 동작으로 남을 수 있지만 범위 안전성의 필수 조건으로 사용하지 않는다. Worker·Reviewer·Manager 진단과 프로젝트 전체에 `PYTHONDONTWRITEBYTECODE`를 확대하는 것은 이 Requirement의 완료 조건이 아니다.

## Acceptance Criteria

- Planner의 새 ExecPlan·허용된 FUTURE_IDEAS 변경과 Worker의 현재 구현 범위 안에 있는 tracked·non-ignored 파일 생성·수정·삭제는 기존 역할별 검사 후 정상 결과로 commit할 수 있다.
- Planner가 Python 모듈을 import해 ignored `__pycache__` 또는 `.pyc`를 생성·수정해도 유효한 새 ExecPlan과 `docs/FUTURE_IDEAS.md` 변경만 역할별 결과로 남으면 범위 위반이나 `planning_error`가 발생하지 않는다.
- Planner와 Worker가 역할 수행 중 임의의 ignored 파일을 생성·수정·삭제하고 복원하지 않아도 ignored-only 차이만으로 결과를 거부하지 않는다.
- Worker의 targeted test와 Loop Manager의 전체 verify가 ignored bytecode, test cache, coverage cache, log 또는 build artifact를 생성·수정·삭제해도 권위 있는 Git 상태가 유효하면 기존 구현·검증 흐름을 계속한다.
- Reviewer의 조사 명령이 ignored artifact만 남겨도 읽기 전용 검토 결과를 거부하거나 ExecPlan을 BLOCKED로 전환하지 않는다.
- Worker의 targeted test, 전체 verify와 Reviewer는 별도 검증 worktree 없이 기존 ExecPlan 관리 worktree에서 순서대로 실행하며 ignored 상태만을 이유로 추가 검증 단계를 만들지 않는다.
- ignored-only 변경은 특정 확장자나 디렉터리 allowlist 없이 동일하게 처리하며, 활성 작업에서 해당 파일을 복구 목적으로 삭제, clean, reset, stash, stage, commit 또는 push하지 않는다.
- ignored 파일을 강제로 stage하거나 tracked 파일로 전환하면 index와 역할별 경로 검사를 적용해 허용되지 않은 변경을 기존과 같이 거부한다.
- tracked·staged·non-ignored untracked 변경, non-default index flag, `HEAD`·branch·ref·worktree·Git config·operation 변경은 ignored artifact가 함께 존재해도 완화되지 않는다.
- 실행 중 변경된 `.gitignore`, repository exclude 설정 또는 Git config 때문에 새로 숨겨진 경로는 ignored-only 허용 대상으로 분류하지 않는다.
- checkout, rebase, merge 또는 Manager가 쓸 tracked 경로와 같거나 부모·자식으로 겹치는 ignored 파일이 있으면 파일을 보존하고 Git 쓰기를 시작하지 않으며 충돌 경로를 진단한다.
- persisted Agent snapshot과 현재 상태의 차이가 ignored-only 파일뿐인 기존 `planning_error` 또는 BLOCKED 작업은 사용자 cache 삭제나 SQLite 수정 없이 같은 세션, worktree, 결과와 단계에서 자동 재개한다.
- 기존 차단 상태에 tracked·staged·non-ignored untracked 또는 공유 Git metadata 차이가 함께 있거나 시작 ignore 규칙을 증명할 수 없으면 ignored 파일의 존재를 근거로 snapshot을 교체하거나 전체 상태를 정상으로 판정하지 않는다.
- 정상 종료 조건을 충족한 정확한 Manager 소유 planning 또는 ExecPlan worktree는 ignored-only 파일이 남아 있어도 전체 worktree를 정리하며, 이 상태만으로 `cleanup_pending`을 만들지 않는다.
- 등록 프로젝트의 기본 worktree, 소유권이 불명확한 경로, PR head에 없는 local commit 또는 권위 있는 미커밋 변경이 남은 worktree는 ignored-only 정리 규칙으로 제거하지 않는다.
- PR_OPEN의 `MERGEABLE`·`UNKNOWN` 판정과 `CONFLICTING` 복구 preflight는 R-021의 ignored artifact 보존 및 tracked 경로 충돌 규칙을 유지한다.
- 자동 테스트는 Planner·Worker·Reviewer와 전체 verify의 ignored-only 생성·수정·삭제, 같은 관리 worktree의 기존 실행 순서, 임의 확장자, 강제 stage, ignore 규칙 변경, tracked 경로 충돌, 권위 변경 혼합, persisted snapshot 자동 재개와 종료 worktree 정리의 안전 실패를 검증한다.
- 전체 verify에서 역할별 게시 가능 경로, shared Git metadata, workspace ownership, rebase·publication lease와 Reviewer 읽기 전용 결과의 기존 검사가 유지된다.

## Product Impact

- 자동 생성되는 ignored artifact 때문에 유효한 계획, 구현, 검증, Review와 PR 상태 동기화가 중단되지 않는다.
- 저장소와 PR에 반영될 수 있는 변경 및 공유 Git 상태는 기존과 같이 엄격하게 검사한다.
- 기존 ignored-only snapshot 차단은 사용자 cache 삭제나 DB 수정 없이 안전 조건을 확인한 뒤 재개할 수 있다.
- 역할과 도구마다 bytecode·cache 예방 환경을 추가하지 않아도 같은 정책을 일관되게 적용한다.
- 정상 종료된 Manager 소유 worktree는 ignored artifact 때문에 불필요하게 보존되지 않는다.

## Non-Goals

- ignored 파일을 stage, commit, push하거나 tracked 결과로 간주하지 않는다.
- 활성 작업의 ignored 파일을 개별 삭제하거나 worktree를 clean, reset, stash 또는 강제 checkout하지 않는다.
- tracked 경로와 충돌하는 ignored 파일을 덮어쓰거나 Git 쓰기 안전 조건을 완화하지 않는다.
- tracked·staged·non-ignored untracked 변경과 shared Git metadata의 역할별 허용 범위를 완화하지 않는다.
- ignored 상태를 격리하기 위한 별도 검증 worktree, 복제 checkout 또는 hermetic 실행 환경을 도입하지 않는다.
- 등록 프로젝트의 기본 worktree나 소유권이 불명확한 경로를 ignored-only 정리 대상으로 취급하지 않는다.
- 프로젝트 전체 Python bytecode 생성을 비활성화하거나 기존 R-016 역사 문서를 수정하지 않는다.

## Open Questions

- 없음
