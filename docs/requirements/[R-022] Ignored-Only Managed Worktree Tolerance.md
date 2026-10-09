# [R-022] Ignored-Only Managed Worktree Tolerance

## Context

Loop Manager는 Agent 실행 전후의 Git snapshot을 비교해 역할별 허용 범위 밖의 변경을 차단한다. ignored 파일의 관측 자료를 권위 있는 변경과 동일하게 비교하면 Planner, Reviewer 또는 Manager 검증이 자동 생성한 `__pycache__/*.pyc`, test cache와 임시 artifact까지 범위 위반이나 소유권 실패로 취급할 수 있다.

R-016은 이 문제를 Planner의 Python bytecode 기록만 예방하는 방식으로 해결했다. 그러나 Worker의 targeted test, 전체 verify, Reviewer의 조사 명령과 다른 도구도 ignored artifact를 만들 수 있어 역할별 예방 설정을 계속 추가해야 하며, 새 도구가 생길 때마다 같은 문제가 반복된다. R-021에서도 전체 verify가 생성한 ignored bytecode 때문에 게시 가능한 변경이 없는 Merge 가능한 PR이 차단되는 사례가 확인됐다.

ignored untracked 파일은 강제로 index에 추가하지 않는 한 commit과 PR에 포함되지 않는다. 반면 tracked 파일, staged entry, non-ignored untracked 파일과 Git ref·worktree·config·operation 상태는 게시 결과나 작업 공간 소유권을 바꿀 수 있으므로 계속 엄격하게 검사해야 한다. 두 종류를 하나의 dirty 상태로 다루는 것은 저장소에 반영되는 변경의 안전성과 로컬 파생 artifact의 존재를 불필요하게 결합한다.

Planner와 Worker는 조사, targeted test, 전체 verify와 빌드 과정에서 필요에 따라 ignored 파일을 생성·수정·삭제할 수 있다. 이 로컬 작업 부산물을 저장소에 반영되는 결과와 동일하게 제한하면 정상적인 도구 실행이 역할 범위 위반이나 복구 불가능한 snapshot 차이를 만들 수 있다. 이 Requirement는 ignored-only 상태를 비권위 로컬 상태로 분류하고, 저장소에 반영되거나 Git 작업의 안전성에 영향을 주는 경계만 차단한다.

기본 worktree 동기화는 경로 충돌 검사와 파일 보존을 위해 ignored 경로·내용 signature를 수집하지만, 같은 상태를 실행 직전 비교와 Manager Git journal의 기대 결과·중단 복구 비교에도 사용한다. 이 경계에서는 Git 전이와 작업 소유 표식이 맞아도 ignored 내용 변화 때문에 동기화나 미완료 journal 복구가 거부될 수 있다. 다른 소비자도 같은 비권위 분류를 적용하는지 확인하되, 기본 worktree에서 확인한 결함을 모든 관리 branch의 결함으로 일반화하지 않는다.

## Requirement

Loop Manager의 Agent 범위 검사와 관리 worktree 소유권·cleanliness 검사는 다음 상태를 권위 있는 변경으로 계속 비교하고 허용 범위를 벗어나면 차단해야 한다.

- tracked 파일과 index의 staged entry 및 non-default index flag
- non-ignored untracked 파일
- 실제 사용하는 HEAD·branch·refs·등록 worktree·Git config·진행 중인 Git operation. 정상 외부 fetch와 무관한 자원 변화·legacy 기본 checkout 제외는 R-055를 따른다.
- 역할별 허용 경로, 선택된 ExecPlan의 범위를 벗어난 게시 가능한 변경

권위 있는 변경을 검사한다는 것은 Planner와 Worker의 정상적인 파일 생성·수정·삭제를 금지한다는 뜻이 아니다. Planner는 새 ExecPlan과 허용된 `docs/FUTURE_IDEAS.md` 변경을, Worker는 선택된 ExecPlan 구현에 필요한 코드·테스트·문서 변경을 기존 역할별 경계 안에서 만들 수 있으며 commit·fetch·rebase·push는 Manager가 수행한다. Reviewer만 권위 있는 파일 변경을 남길 수 없다.

Git의 기준 규칙에 따라 ignored로 분류되고 index에 없는 파일의 생성·수정·삭제만으로는 Planner, Worker 또는 Reviewer의 범위 위반, planning error, ExecPlan BLOCKED, 관리 worktree 소유권 실패 또는 PR 상태 복구 실패를 만들지 않아야 한다. Python bytecode, test cache, build cache와 임의의 다른 ignored 파일에 파일명별 예외나 allowlist를 추가하지 않고 같은 ignored-only 규칙을 적용한다.

Planner와 Worker는 역할 수행에 필요한 ignored 파일을 자유롭게 생성·수정·삭제할 수 있으며 Agent 종료 전에 원래 상태로 복원할 의무가 없다. Worker의 targeted test, Loop Manager의 전체 verify와 Reviewer는 기존과 같이 같은 ExecPlan 관리 worktree에서 이어서 실행한다. ignored 상태를 격리하기 위한 별도 검증 worktree, 복제된 checkout 또는 추가 검증 단계를 만들지 않는다.

ignored 여부는 Agent 또는 Manager 단계가 시작될 때 적용된 ignore 규칙을 기준으로 판정한다. 실행 중 `.gitignore`, repository exclude 설정 또는 관련 Git config를 변경해 새 경로를 ignored로 숨길 수 없어야 한다. ignore 규칙 자체의 tracked·config 변경은 기존 범위 검사 대상이며, 시작 규칙으로 ignored임을 증명할 수 없는 새 경로는 non-ignored 변경과 동일하게 판단한다.

경로 충돌·파일 보존에 필요한 ignored 관측 자료와 범위·소유권·완료 판정에 사용하는 권위 상태를 구분한다. 정상 완료, 실행 직전 재검사와 중단·재시작 복구는 시작 규칙으로 증명된 ignored·index-free 차이만 비교에서 제외하고 실제 ref·HEAD·index·operation 전이와 작업 소유 증거를 계속 검증한다. 충돌 조건을 바꾸지 않는 ignored-only 차이만으로 동일 입력의 실패 재시도 금지가 풀리거나 새 Git 쓰기를 시작하지 않아야 한다. 원본 snapshot·intent·invocation·결과 감사는 덮어쓰지 않으며, 현재 ignore 판정이나 저장된 `ignored` 표기만으로 시작 규칙의 증명을 대신하지 않는다.

ignored 파일을 `git add -f` 등으로 index에 추가하면 더 이상 ignored-only 상태가 아니며 staged entry와 역할별 허용 경로를 기존과 같이 검사한다. ignored 파일과 tracked 경로가 같거나 부모·자식 관계로 겹쳐 checkout, rebase, merge 또는 다른 Manager Git 쓰기가 이를 덮어쓸 수 있으면 파일을 삭제하거나 덮어쓰지 않고 해당 Git 작업을 시작하지 않는다. 활성 작업 중 충돌하지 않는 ignored 파일을 복구 목적으로 clean, reset, stash, stage, commit 또는 push하지 않는다.

Git 쓰기 직전에는 현재 ignored 경로와 해당 작업이 쓸 tracked 경로의 겹침을 다시 검사한다. 사전 검사 뒤 생성된 ignored 파일도 충돌하면 보존·거부하며, 충돌하지 않는 ignored 파일의 생성·수정·삭제만으로 사전 판단을 무효화하지 않는다. 실제 Git 실행 중 경로 충돌이나 상태 변경으로 실패하면 파일을 보존하고 기존 실패 처리를 유지한다.

planning 결과가 게시됐거나 작업 없음으로 확정된 planning worktree, 그리고 PR Merge 뒤 모든 commit이 게시됐고 권위 있는 미커밋 변경이 없는 ExecPlan 관리 worktree는 정확한 Manager 소유권과 결정적 경로를 다시 검증한 뒤 전체 worktree 수명 종료 과정에서 제거할 수 있다. ignored-only 파일은 이 정상 정리를 막거나 `cleanup_pending`을 만들지 않는다. 이는 활성 작업의 개별 ignored 파일을 정리하는 권한이 아니며, 등록 프로젝트의 기본 worktree, 소유권이 불명확한 경로 또는 권위 있는 변경이 남은 worktree에는 적용하지 않는다.

이전 정책으로 저장된 Agent snapshot 또는 범위 오류도 같은 분류로 재평가한다. 저장된 기준과 현재 상태의 차이가 ignored-only 파일뿐이고 `HEAD`, branch, index, tracked·non-ignored untracked 상태와 R-055에 따라 실제 사용하는 공유 Git metadata의 현재 안전 조건이 충족되면, ignored 경로를 snapshot 기준으로 복사하거나 삭제하지 않고 비권위 차이로 제외해 기존 세션, worktree, 검증 결과와 작업 단계를 멱등하게 재개한다. 과거 Manager Git journal도 시작 ignore 규칙과 실제 전이·작업 소유 증거를 재구성할 수 있을 때만 같은 비교 기준으로 완료·복구한다. 완료 결과를 먼저 안전하게 기록한 뒤 활성 기록을 종료하며, 결과 기록 뒤 활성 기록 종료 전에 다시 중단돼도 기존 감사를 보존해 같은 복구를 이어간다. 권위 있는 변경이 하나라도 함께 있거나 시작 ignore 규칙·전이·소유 증거가 부족하면 snapshot이나 journal을 초기화하지 않고 기존 기록과 거부를 유지한다.

기본 worktree의 오래된 journal을 안전하게 종료한 뒤에는 현재 원격 base에 대한 기존 동기화 판단을 계속한다. source 동기화는 best-effort 경계이며, 복구 성공·실패가 별도 관리 worktree의 원격 runtime 실행이나 계획 게시 소유권 증명을 대신하거나 차단하지 않는다.

R-016이 추가한 Planner 전용 `PYTHONDONTWRITEBYTECODE=1` 강제 설정은 ignored artifact를 범위 위반으로 보던 이전 정책의 역할별 우회책이므로 제거한다. Codex와 Claude의 Planner 최초 호출 및 세션 재개에 더 이상 역할 전용 bytecode 환경값을 삽입하지 않고, bytecode 미생성을 요구하던 테스트는 ignored-only artifact 허용과 권위 있는 변경 차단을 검증하는 테스트로 대체한다. Worker·Reviewer·Manager 진단이나 프로젝트 전체에 같은 설정을 확대하지 않으며, 완료된 R-016 Requirement와 ExecPlan은 역사 기록으로 수정하지 않는다.

## Acceptance Criteria

- Planner의 새 ExecPlan·허용된 FUTURE_IDEAS 변경과 Worker의 현재 구현 범위 안에 있는 tracked·non-ignored 파일 생성·수정·삭제는 기존 역할별 검사 후 정상 결과로 commit할 수 있다.
- Planner가 Python 모듈을 import해 ignored `__pycache__` 또는 `.pyc`를 생성·수정해도 유효한 새 ExecPlan과 `docs/FUTURE_IDEAS.md` 변경만 역할별 결과로 남으면 범위 위반이나 `planning_error`가 발생하지 않는다.
- Codex와 Claude의 Planner 최초 호출 및 저장된 세션 재개 환경은 Planner 역할만을 위한 `PYTHONDONTWRITEBYTECODE` 값을 강제로 추가하거나 부모 값을 덮어쓰지 않는다.
- Planner import 뒤 bytecode가 생성되지 않아야 한다는 기존 테스트는 제거하고, 실제 ignored bytecode 생성 뒤에도 계획 결과를 허용하면서 staged·tracked·non-ignored 변경은 계속 거부하는 회귀 테스트로 대체한다.
- Planner와 Worker가 역할 수행 중 임의의 ignored 파일을 생성·수정·삭제하고 복원하지 않아도 ignored-only 차이만으로 결과를 거부하지 않는다.
- Worker의 targeted test와 Loop Manager의 전체 verify가 ignored bytecode, test cache, coverage cache, log 또는 build artifact를 생성·수정·삭제해도 권위 있는 Git 상태가 유효하면 기존 구현·검증 흐름을 계속한다.
- Reviewer의 조사 명령이 ignored artifact만 남겨도 읽기 전용 검토 결과를 거부하거나 ExecPlan을 BLOCKED로 전환하지 않는다.
- Worker의 targeted test, 전체 verify와 Reviewer는 별도 검증 worktree 없이 기존 ExecPlan 관리 worktree에서 순서대로 실행하며 ignored 상태만을 이유로 추가 검증 단계를 만들지 않는다.
- ignored-only 변경은 특정 확장자나 디렉터리 allowlist 없이 동일하게 처리하며, 활성 작업에서 해당 파일을 복구 목적으로 삭제, clean, reset, stash, stage, commit 또는 push하지 않는다.
- ignored 파일을 강제로 stage하거나 tracked 파일로 전환하면 index와 역할별 경로 검사를 적용해 허용되지 않은 변경을 기존과 같이 거부한다.
- tracked·staged·non-ignored untracked 변경, non-default index flag, `HEAD`·branch·ref·worktree·Git config·operation 변경은 ignored artifact가 함께 존재해도 완화되지 않는다.
- 실행 중 변경된 `.gitignore`, repository exclude 설정 또는 Git config 때문에 새로 숨겨진 경로는 ignored-only 허용 대상으로 분류하지 않는다.
- checkout, rebase, merge 또는 Manager가 쓸 tracked 경로와 같거나 부모·자식으로 겹치는 ignored 파일이 있으면 파일을 보존하고 Git 쓰기를 시작하지 않으며 충돌 경로를 진단한다.
- source 동기화의 사전 판단 뒤 충돌하지 않는 임의의 ignored 파일이 생성·수정·삭제돼도 권위 상태가 유효하면 fast-forward 또는 허용된 rebase를 계속한다. Git 쓰기 직전에 새로 생긴 동일·부모·자식 경로 충돌은 파일을 보존하고 거부한다.
- 새 Manager Git 작업의 정상 완료와 재시작 복구는 시작 규칙으로 증명된 ignored-only 차이에 영향받지 않는다. 실제 HEAD·ref·index·operation 전이 불일치, 권위 변경 혼합과 작업 소유 표식 누락은 계속 거부한다.
- 과거 source journal의 예상 ignored 상태와 현재 상태가 달라도 시작 규칙·index-free 분류와 실제 전이·소유 증거가 확인되면 Git 쓰기를 재실행하거나 ignored 파일을 복원하지 않고 완료한다. tracked ignore 규칙과 외부 exclude·config의 시작 증거를 재구성할 수 없는 사례는 원본 journal과 사용자 파일을 보존하고 거부한다.
- intent 기록 뒤 Git 실행 전, Git 성공 뒤 결과 기록 전, 결과 기록 뒤 활성 기록 종료 전의 중단에서 같은 증거로 멱등하게 복구한다. 충돌 조건을 바꾸지 않는 ignored-only 차이만으로 확인된 동일 입력의 실패 Git 쓰기를 반복하거나 기존 완료 감사를 덮어쓰지 않는다.
- 오래된 source journal이 안전하게 완료된 뒤 원격 base가 더 전진해 있으면 현재 원격에 대한 동기화 판단을 계속한다. source 복구 실패만으로 별도 관리 worktree의 원격 runtime 실행을 차단하지 않는다.
- persisted Agent snapshot과 현재 상태의 차이가 ignored-only 파일뿐인 기존 `planning_error` 또는 BLOCKED 작업은 사용자 cache 삭제나 SQLite 수정 없이 같은 세션, worktree, 결과와 단계에서 자동 재개한다.
- 기존 차단 상태에 tracked·staged·non-ignored untracked 또는 공유 Git metadata 차이가 함께 있거나 시작 ignore 규칙을 증명할 수 없으면 ignored 파일의 존재를 근거로 snapshot을 교체하거나 전체 상태를 정상으로 판정하지 않는다.
- 정상 종료 조건을 충족한 정확한 Manager 소유 planning 또는 ExecPlan worktree는 ignored-only 파일이 남아 있어도 전체 worktree를 정리하며, 이 상태만으로 `cleanup_pending`을 만들지 않는다.
- 등록 프로젝트의 기본 worktree, 소유권이 불명확한 경로, PR head에 없는 local commit 또는 권위 있는 미커밋 변경이 남은 worktree는 ignored-only 정리 규칙으로 제거하지 않는다.
- PR_OPEN의 `MERGEABLE`·`UNKNOWN` 판정과 `CONFLICTING` 복구 preflight는 R-021의 ignored artifact 보존 및 tracked 경로 충돌 규칙을 유지한다.
- 자동 테스트는 Planner·Worker·Reviewer와 전체 verify의 ignored-only 생성·수정·삭제, 같은 관리 worktree의 기존 실행 순서, 임의 확장자, 강제 stage, ignore 규칙 변경, tracked 경로 충돌, 권위 변경 혼합, persisted snapshot 자동 재개와 종료 worktree 정리의 안전 실패를 검증한다.
- Agent 결과, 저장 scope 재검사, checkpoint, source·관리 rebase, PR 복구와 cleanup의 ignored 비교 소비자를 확인하고 같은 분류와 기존 안전 경계가 유지되는 회귀 근거를 남긴다. 조건별 판단은 Git 관측 경계를 mock으로 제공해 실제 판단·저장·파일 보존을 검사하며, 실제 Git은 기존 회귀로 충족되지 않는 전이·소유 표식·경로 충돌 효과를 확인할 때만 사용한다.
- 전체 verify에서 역할별 게시 가능 경로, shared Git metadata, workspace ownership, rebase·publication lease와 Reviewer 읽기 전용 결과의 기존 검사가 유지된다.

## Product Impact

- 자동 생성되는 ignored artifact 때문에 유효한 계획, 구현, 검증, Review와 PR 상태 동기화가 중단되지 않는다.
- 저장소와 PR에 반영될 수 있는 변경 및 공유 Git 상태는 기존과 같이 엄격하게 검사한다.
- 기존 ignored-only snapshot 차단은 사용자 cache 삭제나 DB 수정 없이 안전 조건을 확인한 뒤 재개할 수 있다.
- 역할과 도구마다 bytecode·cache 예방 환경을 추가하지 않아도 같은 정책을 일관되게 적용한다.
- Planner 환경의 obsolete한 역할별 bytecode 예외와 전용 테스트를 제거해 Agent 실행 환경 계약을 단순화한다.
- 정상 종료된 Manager 소유 worktree는 ignored artifact 때문에 불필요하게 보존되지 않는다.

## Non-Goals

- ignored 파일을 stage, commit, push하거나 tracked 결과로 간주하지 않는다.
- 활성 작업의 ignored 파일을 개별 삭제하거나 worktree를 clean, reset, stash 또는 강제 checkout하지 않는다.
- tracked 경로와 충돌하는 ignored 파일을 덮어쓰거나 Git 쓰기 안전 조건을 완화하지 않는다.
- tracked·staged·non-ignored untracked 변경과 shared Git metadata의 역할별 허용 범위를 완화하지 않는다.
- ignored 상태를 격리하기 위한 별도 검증 worktree, 복제 checkout 또는 hermetic 실행 환경을 도입하지 않는다.
- 등록 프로젝트의 기본 worktree나 소유권이 불명확한 경로를 ignored-only 정리 대상으로 취급하지 않는다.
- Worker·Reviewer·Manager 진단이나 프로젝트 전체 Python bytecode 생성을 비활성화하지 않고, 기존 R-016 역사 문서를 수정하지 않는다.

## Open Questions

- 없음
