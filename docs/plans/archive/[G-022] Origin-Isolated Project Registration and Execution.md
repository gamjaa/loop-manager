---
id: G-022
title: Origin-Isolated Project Registration and Execution
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-010] Origin-Isolated Local Worktree Operation.md
depends_on:
  - G-012
  - G-016
complexity: high
risk: critical
priority: high
---

## Purpose

Loop Manager의 시작, 프로젝트 등록과 자동 실행을 immutable `origin` commit 기준으로 일원화한다. Loop Manager 자체는 로컬 저장소가 공유하는 detached runtime worktree에서 실행하고, 대상 프로젝트는 최신 `origin/<base-branch>`만 자동화 입력으로 사용한다. 등록된 기본 worktree에 tracked·staged·non-ignored untracked 변경이 없는 base branch의 equal·behind-only 상태이면 원격 base로 안전하게 fast-forward하고, 로컬 변경, 별도 branch 또는 독립 이력이 있으면 그대로 보존하면서 계획과 구현을 계속한다.

## Acceptance Criteria

- `run.sh`와 `run.ps1`은 Loop Manager checkout에서 Git이나 다른 자동 갱신을 실행하지 않고 현재 설치된 Python 부트스트랩만 시작하며 모든 사용자 인자를 그대로 전달한다.
- 부트스트랩은 Git fetch나 worktree 등록보다 먼저 `<state-dir>/loop-manager.lock`을 획득하고 runtime 자식이 끝날 때까지 보유한다. `--skip-if-running`의 기존 조용한 종료 계약도 유지한다.
- 부트스트랩은 Loop Manager 저장소의 `origin/main`을 fetch해 exact commit SHA를 확정하고, 로컬 object database를 공유하는 `<state-dir>` 아래의 clean detached runtime worktree를 생성하거나 검증해 그 SHA의 `loop_manager.py`를 자식으로 실행한다. 독립 clone은 만들지 않는다.
- bootstrap은 Git에서 제외된 기존 source checkout의 `config/config.ini`를 이동·복사·symlink하지 않고 기본 설정 경로로 사용한다. 명시적인 `--config` 상대 경로도 source checkout 기준으로 절대화해 runtime 자식에 전달하며, 자식은 runtime `ROOT`를 기준으로 설정 경로를 다시 계산하지 않는다.
- runtime 준비는 ignored 파일 전체를 복제하지 않는다. `.venv`는 launcher가 선택한 동일 Python interpreter를 자식 실행에도 사용하고, `__pycache__`, 도구 cache와 그 밖의 ignored 파일은 runtime worktree로 전달하지 않는다.
- 로컬 `main`의 미commit 변경과 로컬 전용 commit은 runtime에 포함하지 않는다. push된 새 `origin/main` SHA는 다음 실행에서 새 runtime으로 선택하고 실행 중인 runtime HEAD는 바꾸지 않는다.
- 기존 runtime 경로의 소유권, detached HEAD, expected SHA 또는 clean 상태를 확인할 수 없으면 reset·clean·덮어쓰기 없이 실패한다.
- pinned runtime 이전에 도입된 정상 종료 후 source `main` fast-forward updater는 이 계획의 구현에서 제거한다. launcher와 bootstrap은 source checkout을 갱신하지 않으며, runtime 자식의 일반 등록 프로젝트 동기화만 Loop Manager 저장소 자체를 포함한 eligible 기본 worktree를 갱신할 수 있다.
- G-016의 프로젝트 등록 검사는 fetch로 확정한 `origin/<base-branch>` tree와 blob에서 필수 문서, 활성 PRODUCT/Requirement, 공통 `AGENTS.md` 내용, `docs/PLANS.md` 해시와 template-only 조건을 판정한다. 로컬 worktree에서만 추가·수정·삭제된 파일은 등록 성공이나 실패에 영향을 주지 않는다.
- 등록을 마친 프로젝트의 자동 Loop는 등록 시 전용 공통 파일 검사를 반복하지 않으며, 저장된 repository root, origin, base branch와 provider 설정을 사용한다.
- 대상 저장소 기본 worktree의 tracked 수정, staged 변경, untracked 파일과 현재 checkout branch는 계획, READY 선택, Worker 재개 또는 새 관리 worktree 생성을 차단하지 않는다.
- 로컬 base branch가 `origin/<base-branch>`보다 ahead, behind 또는 diverged여도 자동 입력과 실행 가능 여부는 달라지지 않는다. 로컬 전용 commit과 미commit 변경은 planning/implementation worktree에 복사하거나 자동 commit·push하지 않는다.
- 각 등록 프로젝트는 fetch 뒤 기본 worktree에 tracked·staged·non-ignored untracked 변경이 없고 저장된 base branch를 checkout했으며 로컬 base가 `origin/<base-branch>`와 같거나 그 조상인지 확인한다. behind-only이면 이미 fetch한 원격 ref로 `git merge --ff-only origin/<base-branch>`와 동등한 갱신을 수행하고 equal이면 no-op으로 끝낸다.
- tracked 수정, staged, non-ignored untracked, 다른 branch, detached HEAD, ahead·diverged 또는 진행 중인 Git operation이면 갱신 사유를 기록하고 건너뛴다. ignored 파일은 허용하되 fast-forward가 변경할 tracked 경로와 같거나 부모·자식으로 겹치면 덮어쓰지 않고 skip한다. 확인 뒤 동시 사용자 변경, Git lock 또는 fast-forward 실패가 발생해도 reset·clean·stash·강제 checkout이나 잠금 제거 없이 원격 기준 자동 작업을 계속한다.
- 계획 입력 지문은 `origin/<base-branch>` commit tree만 사용하고, planning worktree는 해당 원격 commit의 detached HEAD에서 생성되며, 새 ExecPlan branch와 구현 worktree도 같은 원격 기준에서 시작한다.
- 기존 활성 ExecPlan은 SQLite에 저장된 Loop Manager 소유 branch와 관리 worktree에서만 재개하며 기본 worktree의 HEAD나 파일을 복구 기준으로 사용하지 않는다.
- 계획 게시, 구현 PR Merge 또는 외부 push로 원격 base가 앞서면 다음 프로젝트 fetch에서 safe fast-forward를 다시 평가한다. eligible checkout만 HEAD, index와 tracked 파일을 원격 base로 함께 전진시키고 나머지 기본 worktree 상태는 변경하지 않는다.
- Loop Manager 코드는 `git fetch`, 검증된 `git worktree add/remove`와 safe fast-forward 조건을 만족하는 기본 worktree의 `git merge --ff-only origin/<base-branch>`를 수행할 수 있다. 갱신 결과를 자동화 입력으로 다시 읽지 않는다.
- Worker는 파일 변경과 targeted test만 수행하고 commit, fetch, pull, rebase, push, worktree 등록과 PR 생성은 Agent 호출 밖의 Loop Manager 코드가 수행한다. 실행 중 사용자가 기본 worktree 또는 로컬 base ref를 변경해도 관리 worktree와 소유 branch가 그대로면 Worker 범위 위반으로 판정하지 않는다.
- 동시 사용자 Git 작업 때문에 lock 충돌이 발생하면 dirty/ahead 판정이나 사용자 조치가 필요한 BLOCKED로 오인하지 않는다. 기존 외부 명령 재시도 정책에 따른 일시 오류로 상태를 보존하며 lock을 삭제·우회하거나 사용자 작업을 reset하지 않는다.
- Loop Manager 저장소 자체를 등록한 경우에도 같은 프로젝트 동기화 정책을 적용한다. clean `main`의 behind-only 상태만 갱신하고 로컬 문서 변경이나 branch 차이가 있으면 source checkout을 보존한 채 자동 작업을 계속한다.
- README와 장기 제품 규칙은 실행과 업데이트의 분리, 원격-only 자동화 입력, eligible 기본 worktree의 safe fast-forward와 그 밖의 로컬 상태 보존을 설명하고 더 이상 dirty/ahead 상태를 사전 차단 조건으로 안내하지 않는다.

## Implementation

- G-016이 분리한 설정·registry 경계에 revision tree reader를 두고, repository root와 GitHub origin을 확인한 뒤 base ref를 fetch·resolve해 등록용 파일 존재, 내용, 해시와 template 비교를 blob에서 수행한다. 경로 탐색에 기본 worktree를 사용할 수는 있지만 제품 입력 내용은 읽지 않는다.
- per-project orchestration에서 `ensure_safe_base_worktree`와 로컬 `HEAD...origin/<base>` 차이를 실행 게이트로 사용하는 경로를 제거한다. 새 workspace 예약과 생성에는 검증된 원격 commit SHA를 명시적으로 전달하고, 재개 경로에는 저장된 소유 branch/worktree identity를 사용한다.
- per-project fetch 직후 실행 게이트와 분리된 기본 worktree synchronizer를 호출한다. synchronizer는 repository identity, tracked·staged·non-ignored untracked 상태, ignored path와 remote 변경 path의 overlap, 현재 branch, operation state와 local/remote ancestry를 다시 확인해 equal·behind-only만 no-op 또는 fast-forward하고, skip·failure 이유를 구조화해 기록한 뒤 원격 SHA를 그대로 후속 단계에 전달한다.
- launcher를 현재 checkout의 가상환경과 bootstrap `loop_manager.py` 실행만 담당하도록 단순화한다. bootstrap 경로는 process lock 안에서 fetch, exact `origin/main` SHA resolve, SHA별 deterministic runtime 경로 검증 또는 detached `git worktree add`, 같은 Python interpreter를 사용한 runtime 자식 실행을 담당한다.
- bootstrap이 인자 파싱 시 기본 `config/config.ini`와 명시적인 `--config`를 source checkout 기준의 canonical absolute path로 확정하고 내부 runtime 표식과 함께 자식에 전달한다. 설정 파일의 내용은 복사하거나 명령행, 로그와 오류에 노출하지 않으며 기존 설정 loader가 자식 시작 시 한 번 읽는다.
- runtime 생성 경계는 tracked revision 파일만 `git worktree add`로 구성한다. ignored 파일을 열거·복사하는 일반 경로를 두지 않고, source `.venv`는 interpreter 경로 재사용으로만 연결하며 cache는 runtime 입력에서 제외한다.
- runtime 자식만 내부 실행 표식을 받아 lock을 중복 획득하지 않게 하고, 부모는 자식 종료 코드를 그대로 반환한다. 내부 표식 없이 시작한 모든 일반 실행은 반드시 bootstrap과 lock을 거친다.
- bootstrap 전환과 함께 임시 post-run updater, 관련 조건 분기와 테스트를 제거한다. pinned runtime 선택 자체가 최신 코드 반영 경계이며 source checkout 갱신을 fallback으로 남기지 않는다.
- runtime worktree 등록은 Agent 시작 전에 끝내고 실행 중에는 고정한다. Agent scope는 활성 관리 worktree와 Loop Manager 소유 branch의 mutation을 계속 차단하되 사용자 기본 worktree와 그 로컬 base ref의 독립 변경은 허용한다.
- Git lock 오류를 결정적으로 식별해 기존 bounded transient-command 처리에 연결하고, 오류 전후 SQLite 상태와 사용자 worktree snapshot을 보존한다. 기본 worktree synchronizer의 경쟁·lock·fast-forward 실패는 저장소를 reset하지 않고 skip 결과로 낮추되, 다른 Git 단계의 기존 오류 정책은 유지한다. lock 파일 제거, 강제 Git 명령 또는 dirty 상태 우회는 추가하지 않는다.
- 장기 동작으로 남는 PRODUCT 항목과 사용자 실행 안내를 구현과 같은 PR에서 정합화하되, Requirement나 ARCHITECTURE 문서는 수정하지 않는다.
- origin tree 검사, 실행 게이트와 workspace 생성 책임을 각각 테스트 가능한 함수로 유지해 G-016 registry와 G-017 scheduler가 로컬 파일 검사를 다시 도입하지 않도록 한다.

## Validation

- Worker는 새 origin-isolation 테스트 class와 launcher contract 테스트만 targeted unit test로 실행한다.
- 원격 tree에는 유효하지만 로컬에서 필수 문서·AGENTS·PLANS를 추가, 수정 또는 삭제한 경우와 그 반대 경우를 구성해 등록 결과가 원격 blob에만 따르는지 검증한다.
- 변경 없는 base branch의 equal·behind-only, tracked 수정, staged, non-ignored untracked, ahead, diverged, detached HEAD, 다른 checkout branch와 operation state 각각에서 synchronizer 결과를 검증한다. eligible behind-only는 HEAD, index와 tracked 파일이 원격 base로 전진하고 나머지 상태는 실행 전후 보존되며, 모든 경우 planning 또는 Worker 작업은 원격 commit 기준으로 시작해야 한다.
- ignored `config/config.ini`, `.venv`, cache와 임의 파일이 원격 변경 경로와 겹치지 않는 경우 및 같거나 부모·자식으로 겹치는 경우를 구성해, 전자는 갱신하고 후자는 ignored 파일을 보존한 채 skip하는지 검증한다.
- 기존 활성 ExecPlan 재개, 새 detached planning worktree와 새 구현 branch가 로컬 HEAD가 아니라 저장된 workspace 또는 `origin/<base-branch>` SHA를 사용하는지 검증한다.
- Linux와 PowerShell launcher에 Git 명령이 없고 Python bootstrap 실행 인자를 그대로 전달하는지 검증한다.
- fetch보다 process lock이 먼저 획득되고 runtime 자식 종료 뒤 해제되는지, 중복 실행이 runtime Git metadata를 바꾸기 전에 종료되는지 검증한다.
- 기본 설정과 상대·절대 `--config`가 source bootstrap에서 기대한 절대 경로로 자식에 전달되고, runtime `ROOT/config/config.ini`의 존재 여부와 무관하게 기존 설정을 읽는지 검증한다. 설정 내용과 비밀값이 자식 명령행, 로그와 오류에 포함되지 않는지도 확인한다.
- source checkout에 `config/config.ini`, `.venv`, cache와 임의의 ignored 파일이 있을 때 config는 외부 경로로 읽고 interpreter만 재사용하며, ignored 파일 어느 것도 runtime worktree에 복사·symlink되지 않는지 검증한다.
- local `main`이 dirty, ahead 또는 다른 commit으로 이동해도 runtime이 exact `origin/main` SHA의 detached HEAD이고, 실행 중 origin이 바뀌어도 현재 runtime이 움직이지 않으며 다음 실행에서만 새 SHA를 선택하는지 검증한다.
- 기존 runtime 경로가 dirty하거나 다른 SHA·branch·repository를 가리키면 보존한 채 실패하고, 정상 SHA별 runtime은 재사용하는지 검증한다.
- 기존 bootstrap 임시 updater 테스트를 pinned runtime 계약으로 교체해 launcher와 bootstrap이 source checkout을 변경하지 않는지 검증한다. 별도로 Loop Manager 저장소가 등록 프로젝트일 때 일반 synchronizer가 safe 조건에서만 그 source checkout을 갱신하는지 검증한다.
- eligibility 확인 직후 사용자 변경, Git lock과 fast-forward 명령 실패를 주입해 reset·clean·stash나 BLOCKED 전이가 없고 원격 SHA 기반 자동 작업이 계속되는지 검증한다.
- Worker 호출 중 기본 worktree의 파일·index·HEAD와 local base ref만 바뀌는 경우는 허용하지만 활성 관리 worktree, 소유 branch, Git config 또는 operation state 변경은 계속 차단하는 scope 테스트를 추가한다.
- 주입된 Git lock 충돌이 lock 삭제나 BLOCKED 전이를 만들지 않고 bounded transient 결과와 재개 가능한 상태를 남기는지 검증한다.
- 전체 verify에서 기존 Agent scope, planning fingerprint, latest-base publication, project registry와 scheduler 테스트가 유지돼야 한다.

## Progress

- [x] launcher의 자동 pull 제거와 실행 계약 테스트 추가 (`ec1be4e`)
- [ ] process lock을 소유한 origin/main SHA runtime bootstrap 구현
- [ ] 전환용 정상 종료 후 source main updater와 관련 테스트 제거
- [x] 사용자 기본 worktree·local base ref와 Worker 소유 Git 상태의 scope 분리
- [ ] origin tree 기반 등록 검사 구현
- [ ] 등록 프로젝트 기본 worktree safe fast-forward synchronizer 구현
- [ ] 기본 worktree 상태 게이트 제거와 원격 SHA 기반 workspace 경계 정리
- [ ] Git lock 일시 오류 보존 처리와 상태 행렬 테스트 추가
- [ ] PRODUCT와 README의 장기 동작 정합화

## Decision Log

- R-010은 기본 worktree와 원격 기준 경계를 변경하도록 명시하고 G-012를 선행 조건으로 지정하므로, Architecture Change Policy에서 요구하는 사용자 승인 근거로 사용한다.
- 프로젝트 등록 수명주기와 자동 Loop의 등록 검사 제거는 G-016이 제공하므로 그 계획이 Merge된 뒤 이 변경을 적용한다.
- launcher, 등록 검사와 실행 게이트를 한 계획에 묶는다. 셋 중 일부만 바꾸면 로컬 상태가 계속 실행을 막거나 로컬 전용 문서가 원격-only 등록 결과에 섞이는 중간 동작이 생긴다.
- 기본 worktree는 자동화 입력이 아니라 Git repository와 shared metadata에 접근하는 anchor다. 사용자 편의를 위해 명시된 safe fast-forward 조건에서만 동기화 대상이 되며 동기화 결과를 Planner나 Worker 입력으로 읽지 않는다.
- runtime은 별도 clone이 아니라 Loop Manager 로컬 저장소의 object database를 공유하는 detached linked worktree다. 로컬 `main`은 사용자 소유 ref로 남고 runtime 선택은 fetch된 `origin/main` SHA만 사용한다.
- Worker는 Git 게시 주체가 아니다. Worker가 반환한 commit 제목과 PR 설명을 사용해 commit, rebase, push와 PR 생성은 Loop Manager 코드가 수행하므로 사용자 기본 worktree 변화를 Agent 변경으로 귀속하지 않는다.
- launcher pull 제거는 반복 차단을 즉시 멈추는 안전 hotfix로 먼저 적용했다. runtime bootstrap과 origin-only 프로젝트 실행은 G-016이 MERGED된 뒤 이 계획의 나머지 범위에서 구현한다.
- 2026-09-20 사용자 결정: pinned runtime Merge 전에는 lock을 실제 획득해 정상 종료한 실행만 lock 해제 직전에 clean source `main`을 fast-forward하는 임시 updater를 허용한다. G-022는 아직 `READY`이고 구현 branch가 없으므로, 최종 runtime 전환에서 이 임시 경로를 제거하는 조건을 본 계획에 추가한다.
- 2026-09-20 운영 hotfix: Agent 실행 중 사용자 기본 worktree와 base refs의 독립 변경을 허용하되 관리 worktree와 Loop Manager 소유 branch 검사는 유지한다. Codex가 자체 생성·정리하는 `refs/codex/turn-diffs/`는 과거에 저장된 snapshot에도 적용되도록 비교 단계에서 제외한다.
- 같은 BLOCKED Agent를 선검증하다 새 결과 snapshot을 만들지 못한 경우에는 이미 저장된 Agent 결과를 유지한다. 시작 snapshot으로 대체해 보존된 Worker 변경의 귀속을 잃지 않는다.
- 2026-09-20 사용자 결정: Git에서 제외된 기존 `config/config.ini`는 runtime worktree로 이전하지 않고 source bootstrap이 절대 경로로 전달한다. ignored 파일은 일괄 복제하지 않으며 source `.venv`는 동일 interpreter 경로로만 재사용하고 cache는 전달하지 않는다.
- 2026-09-20 사용자 결정: G-022가 READY이고 branch, worktree, PR과 Worker session이 없는 상태에서 다른 등록 프로젝트의 기본 경로도 최신화하도록 범위를 보완한다. per-project fetch 뒤 clean base branch의 equal·behind-only checkout만 기존 원격 ref로 fast-forward하며, 그 밖의 상태와 갱신 실패는 보존하고 원격 SHA 기반 자동화는 계속한다.
