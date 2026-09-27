# Architecture

## Overview

Loop Manager는 Windows와 Linux에서 동작하는 Python 3 프로그램이다. 외부 스케줄러가 프로그램을 반복 실행하며, 프로젝트 문서와 GitHub 상태를 확인하고 AI Agent CLI를 호출해 계획, 구현, 검증과 PR 생성을 진행한다.

하나의 프로젝트에는 여러 READY ExecPlan을 등록할 수 있지만 한 번에 하나만 처리한다. 서로 다른 프로젝트는 `max_workers` 범위에서 병렬로 처리한다.

## 실행 환경과 시작

- Codex 또는 Claude Code CLI 중 최소 하나가 설치되고 인증돼있어야 한다
- Git과 gh(GitHub CLI)가 설치되고 대상 저장소에 접근할 수 있어야 한다
- Python 3 venv를 만들 수 있어야 한다
- Windows와 Linux용 실행 진입점을 제공한다
- 같은 사용자 상태 디렉터리를 사용하는 중복 실행은 `<state-dir>/loop-manager.lock`으로 차단한다
- `run.sh`와 `run.ps1`은 Git을 실행하지 않고 source checkout의 Python 부트스트랩만 시작한다
- 부트스트랩은 다른 Git 작업 전에 process lock을 획득하고, Loop Manager 저장소의 `origin/main`을 fetch해 exact SHA의 detached runtime worktree를 선택한 뒤 같은 lock을 유지한 채 runtime 자식을 실행한다
- runtime worktree는 source checkout의 object database를 공유하며 사용자 `main`의 파일, index, HEAD와 로컬 전용 commit을 사용하지 않는다. push된 새 SHA는 다음 실행에서만 선택한다
- source checkout의 bootstrap은 기존 `config/config.ini` 또는 명시된 `--config`를 source checkout 기준의 절대 경로로 확정해 runtime 자식에 전달하며 설정 파일을 runtime worktree에 복사·이동·symlink하지 않는다
- launcher가 선택한 source checkout의 Python interpreter를 runtime 자식도 사용한다. 그 밖의 ignored 파일과 cache는 runtime worktree로 전달하지 않는다
- `config/config.ini`가 없거나 유효하지 않으면 설정 방법을 안내하고 작업을 시작하지 않는다
- 최초 설정은 사용자가 설정 모드를 명시적으로 실행해 SQLite를 생성한다
- 일반 Loop 모드에서 SQLite가 없거나 손상됐으면 자동으로 초기화하지 않고 복원 또는 명시적 재설정을 안내한 뒤 중단한다

프로그램은 한 번 실행할 때 처리 가능한 작업을 진행한 뒤 종료한다. cron이나 Windows 작업 스케줄러가 다음 실행을 시작한다.

## 설정

Python 표준 라이브러리 `configparser`로 `config/config.ini`를 읽는다.

- `[loop].max_workers`: 동시에 처리할 프로젝트 수, 기본값 3인 양의 정수
- provider별 Model alias와 Effort
- Planner, Reviewer profile
- Worker `level1`부터 `level7`까지의 Model과 Effort
- Telegram Bot token과 chat ID

실제 `config/config.ini`는 Git에서 제외하고 현재 사용자만 읽을 수 있도록 권장한다. 기본 설정 파일은 source checkout에 그대로 두며 bootstrap이 해당 경로를 runtime 자식에 명시적으로 전달한다. 사용자가 `--config`를 지정하면 bootstrap은 상대 경로를 source checkout 기준으로 절대화한 뒤 전달한다. runtime은 전달된 파일을 시작 시 한 번 읽고 runtime `ROOT`를 기준으로 기본 경로를 다시 계산하지 않는다. 설정 파일을 SHA별 runtime에 복제하지 않으며 Telegram token과 chat ID가 모두 비어 있으면 알림을 비활성화하고, 하나만 설정돼 있으면 설정 오류로 처리한다. 비밀값은 자식 명령행, 로그, AI Agent prompt와 오류 메시지에 포함하지 않는다.

ignored 상태는 runtime 전달 근거가 아니다. `.venv`는 launcher가 선택한 interpreter의 절대 경로를 자식 프로세스에도 사용해 재사용하고, `__pycache__`와 도구 cache 같은 파생 파일은 전달하지 않는다. 다른 ignored 파일이 필수 운영 입력이 되면 파일별 외부 경로, 소유권, 권한과 전달 방식을 Architecture에 먼저 정의하며 일반 복사나 runtime clean 검증 예외를 추가하지 않는다.

런타임 상태 디렉터리는 source checkout 밖의 운영체제 사용자 상태 경로를 사용한다.

- Windows: `%LOCALAPPDATA%\loop-manager\`
- Linux: `${XDG_STATE_HOME:-~/.local/state}/loop-manager/`

SQLite, manager runtime worktree, 프로젝트 관리 worktree, 로그, 임시 파일과 프로세스 lock은 모두 이 경로 아래에 둔다.

SQLite 손실이나 손상에서의 자동 상태 재구성은 지원하지 않는다. 사용자는 상태 디렉터리를 백업하고, 문제 발생 시 백업을 복원하거나 명시적 재설정 후 프로젝트를 다시 등록한다.

## 프로젝트 등록

- 대상 경로가 Git 프로젝트이고 origin이 GitHub 저장소인지 확인한다
- 기준 branch를 확인해 프로젝트 설정에 저장한다
- `AGENTS.md`, `docs/PLANS.md`, `docs/ARCHITECTURE.md` 존재 여부를 확인한다
- `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래 Requirement가 하나 이상 있어야 한다
- `docs/PLANS.md`의 SHA-256 해시가 Loop Manager 기준 파일과 다르면 등록을 거부한다
- 변경되지 않은 PRODUCT 또는 ARCHITECTURE 템플릿만 있으면 작업 가능 상태로 등록하지 않는다
- `AGENTS.md`는 줄바꿈을 정규화한 뒤 Loop Manager 기준 내용이 연속해서 포함돼있는지 코드로 확인한다
- 기준 내용이 없으면 경고하고, 사용자가 승인한 경우에만 현재 파일 해시를 저장해 강제 등록한다
- `AGENTS.md`와 `docs/PLANS.md` 검사는 등록할 때만 수행하며 자동 Loop에서는 다시 검사하지 않는다
- READY, IMPLEMENTING, VERIFYING, REVIEWING, PR_OPEN 또는 BLOCKED ExecPlan이 있거나 해결되지 않은 `planning_error`가 있으면 프로젝트 등록 해제를 거부한다
- 등록 해제는 프로젝트 파일, branch, worktree와 PR을 삭제하지 않는다

공통 파일 검사는 AI가 아니라 프로그램 코드가 수행한다.

## 문서와 계획의 역할

- `docs/PRODUCT.md`: 현재 Merge된 제품 동작과 장기 규칙
- `docs/requirements/*.md`: 사용자가 원하는 제품 변화와 Acceptance Criteria
- `docs/ARCHITECTURE.md`: 제품 동작을 보장하는 프로젝트별 기술 설계
- `docs/PLANS.md`: 모든 프로젝트가 공유하는 ExecPlan 형식
- `docs/plans/*.md`: Planner가 작성한 구현 계획
- `docs/FUTURE_IDEAS.md`: 현재 PRODUCT/Requirement 범위 밖이지만 보존 가치가 있는 아이디어. Planner 입력이나 실행 상태로 사용하지 않는다
- `.agents/skills/specification-docs/SKILL.md`: 자동 Loop와 분리된 대화형 문서 작성 도구

Requirement는 frontmatter나 상태 없이 순수 Markdown 문서로 유지한다. `docs/requirements/` 바로 아래의 모든 `*.md`를 활성 Planner 입력으로 사용하고 `archive/`는 읽지 않는다. 파일의 프로젝트 상대 경로를 Requirement 식별자로 사용한다.

ExecPlan frontmatter에는 다음 고정 메타데이터만 둔다.

```yaml
id: G-002
title: ExecPlan Title
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-001] Requirement Title.md
depends_on:
  - G-001
complexity: medium
risk: high
priority: high
```

프로그램은 전체 YAML을 지원하지 않고 위 key/value, `sources` 목록과 `depends_on` 목록만 파싱한다. 선행 작업이 없으면 `depends_on: []`로 기록한다. `id`, `title`, 하나 이상의 `sources`, `depends_on`, 유효한 `complexity`, `risk`와 `priority`가 없으면 새 ExecPlan을 등록하지 않는다. 존재하지 않는 ID, 자기 의존과 순환 의존도 거부한다. 실행 상태와 재시도 횟수는 frontmatter에 쓰지 않는다.

priority는 `critical`, `high`, `normal`, `low` 순이며 안전한 무인 실행 차단 또는 반복적인 수동 복구는 `critical`, 정상 경로의 사용자 개입을 없애는 핵심 자동화는 `high`, 그 밖의 Current Goal과 필수 유지보수는 `normal`, 선택적 편의와 최적화는 `low`로 기록한다. 이 기능 전에 작성된 ExecPlan에는 `normal`을 적용해 호환성을 유지한다. priority 형식은 공통 `docs/PLANS.md`와 파서를 같은 구현 변경에서 함께 갱신한다.

## Source of Truth

- PRODUCT, Requirement, ARCHITECTURE와 ExecPlan 내용: 프로젝트 Git 파일
- ExecPlan 실행 상태, 의존성 충족 여부, 재시도 횟수, 세션, worktree, branch와 PR 연결: SQLite
- PR Open/Closed/Merged 상태: GitHub
- Model, Effort, 동시 실행 수와 Telegram 설정: `config/config.ini`

SQLite는 `<state-dir>/loop-manager.db`에 저장하며 상태 변경은 transaction으로 기록한다. 문서 본문이나 ExecPlan 본문은 SQLite에 복제하지 않고 경로와 내용 해시만 저장한다.

SQLite에는 등록 프로젝트, 기준 branch, 선택한 provider, 등록 시 `AGENTS.md` 승인 해시, `AGENTS.md` 변경 제안에 대한 대상 경로·사용자 결정·확정 patch 해시, 마지막 성공 Planner의 명세·전체 계획 입력 지문과 `planning_error`, 복구 가능한 planning publication intent, ExecPlan 메타데이터와 의존성, 최초 실행 여부, 실행 상태와 재시도 횟수, AI Agent 세션, worktree, branch, PR, 삭제된 미착수 ExecPlan의 ID 예약·감사 기록, `cleanup_pending`, 알림과 프로젝트 완료 주기를 저장한다.

실행 시작 시 SQLite, Git worktree/branch와 GitHub PR 상태를 대조한다. 서로 다르면 GitHub Merge 상태, 프로젝트 파일, SQLite 실행 정보 순으로 각 소유 영역의 원본을 적용한다.

## 계획 변경 감지

프로젝트별 명세 지문은 최신 `origin/<base-branch>`의 다음 사용자 소유 제품 문서에서 계산한다.

- `docs/PRODUCT.md`
- `docs/requirements/` 바로 아래의 `*.md`
- `docs/ARCHITECTURE.md`

프로젝트별 전체 계획 입력 지문은 같은 원격 revision의 다음 내용에서 계산한다.

- `AGENTS.md`
- `docs/PRODUCT.md`
- `docs/requirements/` 바로 아래의 `*.md`
- `docs/ARCHITECTURE.md`
- archive, `docs/plans/`와 `docs/FUTURE_IDEAS.md`를 제외한 소스, 테스트와 설정 파일

성공한 일반 Planner 주기는 새 계획 게시, 미착수 계획 개정·삭제 게시, FUTURE_IDEAS-only 게시와 추가 계획 없음 판단을 포함하며, 완료할 때 관측한 명세 지문과 전체 계획 입력 지문을 함께 저장한다. 자동 Requirement archive의 내용 보존 이동만으로는 두 기준을 무효화하거나 새 작업 주기와 프로젝트 완료 알림을 활성화하지 않는다.

활성 변경 단계와 maintenance publication이 없는 안전한 선택 경계에서 명세 지문이 마지막 성공 기준과 다르면 남아 있는 READY 또는 안정된 대기 상태보다 일반 Planner를 한 번 먼저 실행한다. 명세가 같으면 실행 가능한 READY를 priority 순으로 시작하고, 실행 가능한 READY가 없을 때만 전체 계획 입력 지문을 비교해 달라진 경우 일반 Planner를 한 번 실행한다. 성공한 두 지문이 최신 입력과 같으면 빈 catalog 또는 dependency 대기 상태에서도 Planner를 반복 실행하지 않는다.

## Git과 worktree 실행 모델

Loop Manager 자동 작업의 기준은 `git fetch`로 갱신한 `origin/<base-branch>`이며 등록 프로젝트 기본 worktree의 내용은 입력으로 사용하지 않는다. fetch 직후 기본 worktree에 tracked·staged·non-ignored untracked 변경이 없고 저장된 base branch를 checkout했으며 로컬 base가 원격 base와 같거나 그 조상인 경우에만, 이미 fetch한 ref에 대해 `git merge --ff-only origin/<base-branch>`와 동등한 update를 수행한다. 이는 중복 fetch 없이 `git pull --ff-only`와 같은 최종 checkout을 만든다. ignored 파일은 원격 변경 경로와 겹치지 않을 때만 보존한 채 허용한다. 그 밖에는 기본 worktree에서 checkout, pull, rebase, reset, stash, clean, commit 또는 파일 생성을 수행하지 않는다.

Loop Manager 프로그램 자체의 실행 revision은 다음 순서로 고정한다.

1. source checkout의 좁은 Python 부트스트랩이 `<state-dir>/loop-manager.lock`을 획득한다
2. lock을 보유한 부모만 source checkout을 anchor로 `git fetch origin`을 실행한다
3. fetch된 `origin/main`을 immutable commit SHA로 resolve한다
4. `<state-dir>/runtimes/<repository-id>/<sha>/`의 linked worktree가 같은 repository, clean detached HEAD와 exact SHA인지 검증하고, 없을 때만 `git worktree add --detach`로 만든다
5. 부모가 source checkout 기준의 절대 config 경로와 내부 runtime 표식을 전달하고, 현재 Python interpreter로 runtime worktree의 `loop_manager.py`를 자식 실행해 종료 코드를 전달한다
6. 부모는 자식 종료 뒤 lock을 해제한다. bootstrap은 실행 중인 runtime worktree를 이동하거나 사용자 `main`을 갱신하지 않는다. 다만 runtime 자식이 Loop Manager 저장소 자체를 등록 프로젝트로 처리하면 아래의 일반 safe fast-forward 조건을 적용할 수 있다

runtime 경로가 존재하지만 소유권, repository, detached HEAD, SHA 또는 clean 상태를 증명할 수 없으면 reset, clean, 제거 또는 덮어쓰기 없이 중단한다. 별도 clone은 만들지 않으며 SHA별 runtime 정리는 활성 실행과 분리된 검증된 관리 작업으로만 수행한다.

기본 worktree의 상태는 다음과 같이 처리한다.

- tracked·staged·non-ignored untracked 변경이 없는 저장 base branch가 원격과 같으면 동기화는 no-op이며, behind-only이면 HEAD, index와 tracked 파일을 원격 base로 fast-forward한다
- tracked 수정, staged, non-ignored untracked, 다른 branch, detached HEAD, ahead, diverged 또는 진행 중인 Git operation이면 동기화를 건너뛰고 기존 checkout을 보존한다
- ignored 파일은 원격이 변경할 tracked 경로와 같거나 부모·자식으로 겹칠 때만 동기화를 건너뛰며 그 밖에는 그대로 유지한다
- eligibility 확인 뒤 동시 사용자 변경, Git lock 또는 fast-forward 실패가 발생하면 reset·clean·stash·강제 checkout이나 잠금 제거 없이 skip 이유를 기록한다
- 동기화 수행·skip·failure와 관계없이 자동 작업은 fetch된 원격 SHA를 사용해 계속하며 기본 worktree 상태를 실행 게이트나 입력으로 사용하지 않는다
- 로컬에만 있는 문서와 commit은 자동 작업 입력으로 사용하지 않으며 사용자가 commit하고 push해야 반영한다
- GitHub PR 상태 동기화와 알림 같은 읽기 전용 처리는 기본 worktree 상태와 관계없이 수행한다
- Worker 실행 중 사용자가 기본 worktree의 파일, index, HEAD와 로컬 base ref를 변경해도 관리 worktree와 Loop Manager 소유 branch가 그대로면 Agent 범위 위반으로 귀속하지 않는다

ExecPlan 하나의 실행 흐름은 다음과 같다.

1. 실행 중인 변경, PR 충돌 복구, 일반·maintenance planning publication 또는 `PROJECT_BLOCKED`가 있으면 먼저 기존 상태를 복구·완료한다. 그 밖의 안전한 선택 경계에서는 최신 `origin/<base-branch>`의 명세 지문을 계산한다. 마지막 성공 기준과 다르면 READY·PLAN_BLOCKED·안정된 PR_OPEN·dependency 대기보다 일반 Planner를 우선하고, 같으면 dependency를 충족한 READY를 priority 순으로 선택한다. 명세가 같고 실행 가능한 READY가 없을 때만 전체 계획 입력 지문을 비교해 달라진 경우 일반 Planner를 시작한다
2. Planner가 필요하면 `<state-dir>/worktrees/<project-id>/planning/<input-fingerprint>/`에 해당 원격 SHA 기준의 detached planning worktree를 만든다. 프로그램은 비종료 ExecPlan compact catalog와 함께 SQLite가 `READY`, 미실행 marker, branch·worktree·reservation·Agent·검증·retry·PR 정보 없음으로 확인한 미착수 READY의 bounded snapshot을 만든다
3. Planner는 현재 입력에서 식별 가능한 미충족 작업 전체를 독립적인 새 ExecPlan으로 나누어 priority와 `depends_on`을 포함해 일괄 작성할 수 있다. 또한 snapshot의 미착수 READY만 같은 ID로 수정·rename하거나 삭제할 수 있다. 현재 범위 밖의 보존 가치가 있는 아이디어는 같은 계획 주기의 ExecPlan으로 만들지 않고 `docs/FUTURE_IDEAS.md`에 분리할 수 있다
4. 프로그램이 Planner 변경을 새 계획, snapshot에 결합된 개정·rename·삭제와 선택적인 `docs/FUTURE_IDEAS.md` 변경으로 분리한다. snapshot 밖 기존 계획과 그 밖의 파일 변경은 거부하고, 적용 후 전체 활성 catalog의 ID·경로, sources와 dependency graph를 검증한다. 삭제 ID는 취소 감사 대상으로 예약하며 참조 dependency가 같은 batch에서 모두 해소되지 않으면 거부한다
5. 검증에 실패하면 오류를 같은 Planner 세션에 전달하고 최대 3회까지 수정과 재검증을 수행한다
6. 계속 실패하면 원인과 두 계획 입력 지문을 프로젝트의 `planning_error`에 저장하고 planning worktree와 세션을 보존한 뒤 처리를 중단한다
7. 같은 실패 지문에서는 Planner를 다시 실행하지 않는다. 입력 지문이 바뀌면 검증 오류와 diff를 로그에 보존하고, 소유권을 확인한 이전 planning worktree를 제거한 뒤 `planning_error`를 지우고 최신 지문의 planning worktree에서 다시 시작한다
8. 계획과 `docs/FUTURE_IDEAS.md` 변경이 모두 없으면 `planning_error`를 지우고 명세·전체 계획 입력 지문을 저장한 뒤 planning worktree를 제거한다
9. 유효한 생성·개정·rename·삭제와 `docs/FUTURE_IDEAS.md` 변경을 planning worktree에서 하나의 planning commit으로 만든다. FUTURE_IDEAS만 변경된 결과는 ExecPlan 실행 상태를 만들지 않는다
10. `git fetch` 후 `origin/<base-branch>`가 planning worktree의 시작 commit과 다르거나 대상 READY snapshot의 상태·소유권·revision이 달라졌으면 계획 commit을 게시하지 않고 planning worktree를 정리한 뒤 최신 기준으로 다시 계획한다
11. 기준과 snapshot이 그대로면 planning commit과 적용 manifest를 publication intent로 SQLite에 저장한 뒤 `origin/<base-branch>`에 fast-forward push한다. force push는 사용하지 않는다
12. push된 commit을 원격에서 확인한 뒤에만 생성 row 등록, 개정 row cache 교체, 삭제 row 제거와 ID 취소 감사 기록, 성공 지문 및 planning 상태 정리를 하나의 SQLite transaction으로 적용한다. push 후 transaction 전에 중단되면 intent와 원격 commit을 검증해 같은 batch를 멱등하게 복구한다
13. planning worktree를 제거하고, 다음 안전한 선택에서 갱신된 전체 catalog의 모든 `depends_on`이 MERGED인 READY 중 priority가 가장 높은 계획을 선택하며 같은 priority에서는 숫자 ID가 가장 작은 계획을 선택한다
14. 갱신된 `origin/<base-branch>`에서 `loop-manager/<execplan-id>` branch와 `<state-dir>/worktrees/<project-id>/<execplan-id>/` worktree를 만든다
15. branch나 worktree가 이미 있으면 새로 만들지 않고 SQLite 및 Git 상태와 대조해 복구한다
16. Worker가 해당 worktree에서 구현, 관련 테스트와 verify 스크립트를 작성하고 필요한 PRODUCT, 선택된 ExecPlan 기록과 FUTURE_IDEAS를 갱신한다
17. 프로그램이 변경을 commit한 뒤 `git fetch`로 원격 기준 branch를 다시 갱신한다
18. branch가 최신 원격 기준 branch보다 뒤처졌으면 프로그램이 관리 worktree에서 `git rebase origin/<base-branch>`를 실행한다
19. rebase 충돌이 발생하면 충돌 내용을 같은 Worker 세션에 전달한다. Worker가 관리 worktree 안에서 충돌을 해결하면 프로그램이 변경 범위를 검증하고 stage한 뒤 rebase를 계속하며, 해결할 수 없으면 BLOCKED로 전이한다
20. rebase가 끝난 commit을 기준으로 프로그램이 전체 검증을 실행하고 읽기 전용 Reviewer를 호출한다
21. PR 생성 직전에 원격 기준 branch가 다시 변경됐으면 17~20단계를 반복한다
22. 프로그램이 같은 head branch의 GitHub PR을 조회해 기존 PR을 복구하고, 없을 때만 branch를 push한 뒤 PR을 생성한다
23. GitHub Merge를 확인한 뒤에만 SQLite 상태를 MERGED로 바꾸고, 다음 실행 가능한 READY ExecPlan은 갱신된 원격 기준 branch에서 새로운 전용 branch와 worktree를 만든다

ExecPlan 파일은 구현 PR에 처음 포함하지 않는다. Planner 단계에서 먼저 base branch에 게시되며, 각 구현 branch는 해당 ExecPlan이 포함된 원격 기준에서 시작한다.

사용자가 BLOCKED의 근거 PRODUCT/Requirement를 기본 branch에서 수정한 경우 변경을 commit하고 push해야 한다. Loop Manager는 변경된 `origin/<base-branch>`를 관리 branch에 rebase한 뒤 재개한다. rebase는 관리 worktree에서만 수행하며, 등록 프로젝트 동기화가 별도로 safe fast-forward 조건을 만족하지 않는 한 기본 worktree의 파일, index와 HEAD는 바꾸지 않는다. 사용자는 안내된 관리 worktree의 ExecPlan을 직접 수정할 수도 있다.

## 동시 실행

- 하나의 프로젝트에는 여러 READY ExecPlan이 존재할 수 있지만 활성 ExecPlan, branch, worktree와 Worker는 하나만 존재한다
- 활성 변경 작업은 새 계획 입력이 선점하지 않는다. 다만 마지막 성공 Planner 뒤의 명세 변경은 다음 안전한 선택 경계에서 남아 있는 READY보다 먼저 한 번 평가한다
- READY ExecPlan은 모든 `depends_on`이 MERGED일 때만 실행 후보가 된다
- 명세 변경이 없고 dependency를 충족하지 못한 READY만 남으면 전체 계획 입력 지문이 바뀐 경우에만 Planner를 한 번 실행하고, 성공 지문과 같으면 해당 프로젝트만 대기한다
- 실행 가능한 READY ExecPlan이 여럿이면 priority가 높은 계획을 먼저 선택하고 같은 priority에서는 숫자 ExecPlan ID가 가장 작은 계획을 선택한다
- 각 ExecPlan은 `loop-manager/<execplan-id>` 형식의 고유한 branch와 별도 관리 worktree를 사용한다
- 안정된 PR_OPEN 또는 PLAN_BLOCKED는 독립 READY 실행과 안전한 일반 Planner 지문 평가를 막지 않는다. PROJECT_BLOCKED와 현재 변경 slot을 점유하는 복구는 계속 모든 새 Agent 시작을 막는다
- 서로 다른 프로젝트만 병렬로 처리한다
- 전체 동시 프로젝트 수는 `max_workers`로 제한한다
- Planner 호출은 한 번에 하나만 실행한다

같은 프로젝트를 직렬화하므로 파일 범위 충돌 판정과 병렬 PR 재정렬은 지원하지 않는다. `depends_on`은 병렬 스케줄링이 아니라 선행 PR Merge를 강제하는 실행 게이트로 사용한다.

## AI Agent 실행

- Planner, Worker와 Reviewer prompt는 Loop Manager에 내장한다
- 프로젝트별 prompt 재정의는 지원하지 않는다
- 프로그램은 전체 ExecPlan을 코드로 파싱하되 Planner에게는 비종료 계획의 ID, 제목, 상태, sources, depends_on과 priority만 compact catalog로 전달한다
- Planner는 PRODUCT, 활성 Requirement, ARCHITECTURE, 관련 코드·테스트, 비종료 ExecPlan compact catalog와 프로그램이 제공한 미착수 READY snapshot을 읽고 현재 식별 가능한 미충족 작업 전체를 한 계획 주기에 반영하며, 범위 밖의 보존 가치가 있는 아이디어는 `docs/FUTURE_IDEAS.md`에 분리한다
- Planner는 MERGED ExecPlan 메타데이터와 본문을 기본 입력으로 받거나 읽지 않는다. 프로그램만 ID 고유성, 번호 할당, 의존성 충족과 감사 이력을 위해 이를 유지한다
- Planner는 안전한 무인 실행 차단과 반복적인 사용자 복구 제거를 최우선으로 priority를 정하고, 정상 경로 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순으로 계획한다
- Planner는 새 `docs/plans/*.md`를 생성하고 `docs/FUTURE_IDEAS.md`를 생성하거나 수정할 수 있다. 프로그램이 snapshot으로 지정한 미착수 READY만 같은 ID로 수정·rename·삭제할 수 있으며, 실행된 계획, snapshot 밖 기존 계획과 그 밖의 파일은 변경할 수 없다. 프로그램은 Agent 실행 전후 diff와 SQLite snapshot으로 범위를 검증한다
- Worker는 ExecPlan을 구현하고 관련 unit test와 전체 검증 스크립트를 작성하거나 갱신한다. 장기 제품 규칙이 바뀔 때만 PRODUCT를, 진행 기록이 필요할 때만 선택된 ExecPlan을 수정할 수 있으며 범위 밖 아이디어는 FUTURE_IDEAS에 기록할 수 있다
- Worker는 수정한 코드와 새로 작성하거나 변경한 테스트를 직접 검증하는 최소 범위의 targeted unit test만 실행한다. 실패하면 같은 세션에서 수정하고 해당 테스트를 다시 실행하며, 실행 가능한 관련 unit test가 없으면 그 사유를 결과에 포함한다
- Worker는 전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check와 전체 검증 스크립트를 직접 실행하지 않는다
- Reviewer는 Worker의 targeted unit test와 프로그램의 전체 verify가 통과한 뒤 실행되며 테스트 command나 verify 스크립트를 다시 실행하지 않는다. 파일을 수정하지 않고 코드, 테스트, verify 스크립트의 diff와 전달된 결과를 읽어 요구사항 충족, 회귀, 테스트 누락과 verify 약화를 포함한 blocking issue를 확인한다
- 프로그램은 각 Agent 실행 전후 diff를 검사한다. Planner의 범위 위반은 `planning_error` 흐름으로 처리하고, Worker 또는 Reviewer의 범위 위반은 해당 ExecPlan을 BLOCKED로 처리한다
- Worker와 Reviewer는 ExecPlan이 PR 생성 준비를 마칠 때까지 각자의 세션을 재사용한다
- 세션을 재개할 수 없으면 같은 worktree, ExecPlan과 저장된 실패 내용을 전달해 새 세션을 시작한다
- 상태 분류, 설정 검증, Git/GitHub 명령과 verify 실행은 프로그램 코드가 담당한다
- Worker는 commit, fetch, pull, rebase, push, worktree 등록과 PR 생성을 수행하지 않는다. Worker 반환 뒤의 모든 Git 쓰기는 Agent 호출 밖의 프로그램 코드가 소유한다
- AI Agent와 verify subprocess에는 역할 수행에 필요한 환경 변수만 전달하고 Telegram token을 포함한 Loop Manager 비밀값을 상속하지 않는다

### R-023: workspace 쓰기 제한과 자동 승인 검토 (구현 예정)

2026-09-26 사용자 결정으로 provider의 내장 workspace 쓰기 제한을 우선 사용하고, Codex·Claude의 자동 승인 검토를 함께 유지하는 설계를 채택한다. 이 절은 R-023의 구현 대상 설계이며 현재 Merge된 동작의 구현 완료를 뜻하지 않는다. Agent 호출 밖에서 Manager가 Git 쓰기를 소유하는 기존 경계는 유지한다.

provider adapter는 실제 호출 직전에 선택된 canonical worktree, Git worktree 등록과 repository identity를 확인하고, 신규 session과 재개 호출에 동일한 쓰기 정책을 명시한다. 허용 경로는 선택된 workspace와 실행별로 소유권을 확인한 최소 임시 경로로 제한한다. 사용자·프로젝트·session 설정의 병합으로 다른 worktree, source checkout, shared Git metadata 또는 넓은 임시 경로가 추가되면 이를 격리 확인으로 인정하지 않는다. workspace 정책과 별도로 기존 Planner·Worker·Reviewer·유지보수 Planner의 허용 파일 및 변경 범위 검사를 유지한다.

- Codex는 선택된 관리 worktree를 `-C`로 지정하고, 쓰기 역할에는 `workspace-write`, Reviewer에는 `read-only`를 적용한다. `approvals_reviewer="auto_review"`를 유지하면서 `approval_policy.granular`의 `sandbox_approval=false`, `request_permissions=false`로 sandbox 밖 실행과 권한 확대 요청을 거부한다. 필요한 나머지 승인 항목은 검토 대상으로 유지한다. `approval_policy="never"`로 전체 자동 검토를 비활성화하거나, 편의 CLI 옵션이 명시한 granular 정책을 덮어쓰게 하지 않는다.
- Codex의 추가 `writable_roots`는 Manager가 허용한 최소 경로만 포함한다. 기본 `/tmp` 및 `$TMPDIR` 쓰기 범위는 `exclude_slash_tmp`, `exclude_tmpdir_env_var`로 제외하고 필요 시 실행별 임시 경로만 추가한다. `.git` pointer file과 그 resolved Git directory를 포함한 보호 경로 및 shared Git directory는 쓰기를 허용하지 않는다. 상속된 profile·규칙·확장 기능이 경계를 우회할 수 없는지도 확인한다.
- Claude의 쓰기 역할은 `--permission-mode auto`를 명시하고 지원 모델·계정·CLI 버전에서 실제 적용 여부를 확인한다. `acceptEdits`는 편집 자동 승인 모드이므로 자동 검토 유지의 대체 설정으로 사용하지 않는다. Reviewer는 기존 읽기 전용 역할과 편집 도구 금지를 유지하고, 해당 역할에서도 지원되는 자동 검토 경로를 확인한다. 지원되지 않으면 권한 모드를 완화하지 않고 원인을 기록한다.
- Claude는 `sandbox.enabled=true`, `failIfUnavailable=true`, `allowUnsandboxedCommands=false`를 적용하고 `excludedCommands`를 비워 sandbox 밖 재실행을 막는다. sandbox 안 명령을 자동 검토 없이 일괄 승인하지 않도록 `autoAllowBashIfSandboxed=false`를 사용해 permission mode의 검토 흐름을 유지한다. 실제 검토 대상은 provider 정책에 따르며 모든 읽기·편집·명령이 검토된다고 표시하지 않는다.
- Claude의 linked worktree는 기본 sandbox에서 main repository의 shared `.git` 쓰기가 추가 허용될 수 있으므로 resolved shared Git directory와 worktree Git directory를 `sandbox.filesystem.denyWrite`로 명시적으로 거부한다. 설정 병합으로 추가 workspace나 우회 예외가 생기지 않도록 확인한다. Bash sandbox 밖에서 처리하는 Edit·Write·NotebookEdit 등 파일 도구에도 같은 쓰기 경계를 강제한다. Reviewer는 선택된 worktree의 쓰기까지 거부한다.
- 내장 sandbox가 없는 Claude native Windows는 미지원으로 진단하고 해당 Agent 호출을 시작하지 않는다. Linux와 Codex native Windows도 필요한 CLI 기능과 sandbox 초기화를 확인할 수 없으면 동일하게 중단한다. WSL·컨테이너 전환이나 새로운 외부 격리 infrastructure는 이 설계에 포함하지 않는다.

격리 정책 작성과 실제 효력 확인은 provider 호출 경계의 독립 책임으로 두고, 기존 Agent invocation·session·snapshot 저장과 연결한다. SQLite에는 호환 가능한 추가 저장만 사용하며, 개별 호출마다 provider, CLI 버전, 실행 환경, 역할, canonical worktree, repository identity, 유효한 격리 정책의 지문, session/호출 identity와 확인·거부 결과를 보존한다. 증거 저장이나 확인이 실패하면 검증된 실행으로 취급하지 않는다. 재개·provider 전환·설정 변경·중단 뒤에는 이전 증거를 새 호출의 증거로 재사용하지 않는다. 비밀값, 인증 자료와 source checkout의 로컬 파일 내용은 저장하지 않는다.

단순 CLI 옵션, cwd 지정, 자동 검토의 승인 또는 사후 Git diff만으로 호출 전 격리를 증명하지 않는다. 실제 적용되는 정책과 허용 경로 쓰기·금지 경로 쓰기 거부를 확인하며, source checkout의 로컬 상태를 바꾸지 않고 증명해야 한다. provider 자체의 Git 임시 ref·파일 도구·session 처리까지 분리해 증명할 수 없으면 미확인 결과를 남기고 무관한 Git 변화의 검사 범위를 축소하지 않는다. 신규 호출에서 요구 정책을 적용·확인할 수 없거나 기존 허용 작업과 양립하지 않으면 원인과 기존 작업물·session을 보존한 채 중단한다. 격리 증거가 없는 과거 snapshot과 session의 판정은 기존 엄격한 shared ref·다른 worktree 감시를 유지한다. Loop Manager의 Worker 이후 Reviewer 검토는 provider의 실행 전 자동 승인 검토와 별도로 유지한다.

설정 의미와 지원 범위는 구현 시 설치된 CLI와 공식 문서를 대조한다: [OpenAI 승인 정책 설정](https://learn.chatgpt.com/docs/config-file/config-reference), [Codex 자동 검토](https://learn.chatgpt.com/docs/sandboxing/auto-review), [Claude sandbox](https://code.claude.com/docs/en/sandboxing), [Claude permission mode](https://code.claude.com/docs/en/permission-modes). 지원되지 않는 key를 무시한 실행을 격리 확인으로 기록하지 않는다.

### AGENTS.md 변경 제안과 승인

- Worker는 현재 ExecPlan에 필요한 `AGENTS.md` 변경을 발견해도 파일을 직접 수정하지 않고 선택된 ExecPlan의 `Decision Log`에 대상 경로, 프로젝트 전용·공통 범위, 이유, 영향과 정확한 patch를 기록한다
- 일반 등록 프로젝트의 Worker는 현재 프로젝트에만 적용되는 `AGENTS.md` 변경을 `project` scope로 제안할 수 있다
- Loop Manager 자체 저장소의 루트 `AGENTS.md`는 향후 프로젝트 설정에 사용하는 공통 원본이므로 Loop Manager Worker는 `common` scope만 제안할 수 있고 `project` scope 제안은 거부한다. Loop Manager에만 적용되는 제품 동작, 정책과 기술 설계는 `docs/PRODUCT.md`, 활성 Requirement와 `docs/ARCHITECTURE.md`에 기록하며 `AGENTS.md`에 기록하지 않는다
- 프로그램은 Worker 결과와 ExecPlan diff에서 완전한 제안을 확인한 뒤 제안이 포함된 ExecPlan 및 기존 구현 변경을 보존하고, commit과 검증 전에 ExecPlan을 BLOCKED로 전환한다
- AGENTS 변경 승인 BLOCKED에는 일반적인 source 내용 해시 해제를 적용하지 않는다. 사용자는 보존된 관리 worktree의 같은 ExecPlan에서 해당 제안의 `User Decision`만 수정해 승인, 거절 또는 정확한 대체 patch를 기록한다
- 프로그램은 결정이 참조하는 제안, 대상 경로와 확정 patch를 정규화해 hash하고 SQLite 승인 기록과 결합한다. 결정란 밖의 문서 변경은 승인으로 해석하지 않는다
- 승인 또는 대체 결정이면 같은 Worker 세션을 재개해 확정 patch를 적용하고, 거절이면 `AGENTS.md`를 변경하지 않은 채 재개한다
- Agent 실행 후 실제 `AGENTS.md` diff를 승인된 대상과 patch에 결정적으로 대조한다. 차이가 있으면 게시하지 않고 승인을 무효화한 뒤 최신 patch 제안을 요구한다
- 승인된 변경을 적용한 뒤에는 일반 Worker 변경과 동일하게 targeted test, 전체 verify와 읽기 전용 Review를 거쳐 같은 구현 PR에 포함한다
- Planner와 Reviewer의 `AGENTS.md` 변경, 승인 전 Worker의 직접 변경 및 승인 대상 밖의 변경은 제안 흐름이 아니라 역할 범위 위반으로 처리한다
- 승인된 기준 `AGENTS.md`는 이후 프로젝트 설정의 공통 원본으로만 사용하고 기존 등록 프로젝트에는 자동 배포하지 않는다

## Model과 Effort

기본 Model alias는 다음 계열을 사용한다.

- Codex: Sol(strong), Terra(balanced), Luna(fast)
- Claude Code: Opus(strong), Sonnet(balanced), Haiku(fast)

정확한 Model 이름은 `config/config.ini`에서 관리한다. 프로그램은 complexity에 low=1, medium=3, high=5를, risk에 low=0, medium=1, high=2, critical=3을 부여하고 합계를 최대 7로 제한해 Worker level을 선택한다.

| complexity | risk | Worker level |
|---|---|---:|
| low | low / medium / high / critical | 1 / 2 / 3 / 4 |
| medium | low / medium / high / critical | 3 / 4 / 5 / 6 |
| high | low / medium / high / critical | 5 / 6 / 7 / 7 |

설정된 provider, Model 또는 Effort가 없거나 CLI가 지원하지 않으면 Worker를 시작하지 않고 설정 오류를 보고한다.

## 상태 전이와 복구

ExecPlan 상태는 SQLite에서 다음 순서로 전이한다.

```text
READY → IMPLEMENTING → VERIFYING → REVIEWING → PR_OPEN → MERGED
VERIFYING 또는 REVIEWING → IMPLEMENTING
READY, IMPLEMENTING, VERIFYING 또는 REVIEWING → BLOCKED
BLOCKED → READY
PR_OPEN → BLOCKED
BLOCKED → PR_OPEN
```

- 프로그램 코드만 상태를 변경한다
- 작업을 시작하기 전에 다음 상태와 소유 worktree를 하나의 transaction으로 기록한다
- 프로세스 중단 후에는 저장된 상태와 worktree에서 재개한다
- worktree가 없고 branch가 있으면 worktree를 다시 만든다
- worktree와 branch를 모두 복구할 수 없으면 BLOCKED로 변경한다
- PR_OPEN은 GitHub 상태를 확인해 MERGED 또는 BLOCKED로 전이한다
- 닫힌 PR로 인한 BLOCKED는 같은 PR이 다시 Open됐을 때만 PR_OPEN으로 전이한다

일반적인 BLOCKED 전환 시 ExecPlan과 `sources` 문서의 내용 해시를 저장한다. 이후 어느 하나가 바뀌면 재시도 횟수를 초기화하고 READY로 전이한다. 내용이 같으면 BLOCKED를 유지한다. 닫힌 PR과 `AGENTS.md` 변경 승인을 기다리는 BLOCKED에는 이 규칙을 적용하지 않는다. `AGENTS.md` 변경 승인은 선택된 ExecPlan의 대응하는 `User Decision`에 유효한 결정이 기록되고 대상 경로 및 확정 patch hash가 저장된 경우에만 READY로 전이한다.

`planning_error`는 ExecPlan 생성 전 프로젝트 단위 오류다. 해결되지 않은 동안 같은 계획 입력 지문으로 Planner를 다시 실행하거나 프로젝트 완료 처리하지 않는다. `cleanup_pending`은 MERGED된 ExecPlan의 worktree를 보존했다는 경고이며 ExecPlan 상태 전이를 만들거나 다음 작업과 프로젝트 완료를 막지 않는다.

미착수 READY가 일반 Planner batch에서 삭제되면 실행 상태 전이를 만들지 않고 활성 `exec_plans` row를 제거하며, 같은 transaction에서 별도 취소 감사 기록에 ID, 마지막 선언과 삭제 planning commit을 남긴다. 이 ID는 이후 새 계획 번호 계산과 고유성 검증에 계속 포함한다. `READY → IMPLEMENTING` activation은 최초 실행 marker를 같은 transaction에서 영구 설정하며 이후 READY로 복귀해도 일반 Planner 변경 대상이 되지 않는다.

### 관리 rebase의 단계별 증거와 완료 복구 — R-045 목표 설계

이 절은 `docs/requirements/[R-045] Phase-Bound Managed Rebase Validation and Recovery.md`를 구현하기 위한 목표 설계다. 구현·Merge 전의 요구를 현재 완료된 제품 동작으로 해석하지 않는다. 기존 ExecPlan 상태 모델, Manager의 Git 쓰기 권한과 게시 경계를 유지하며, rebase 내부 단계의 증거와 복구 판정을 구체화한다.

#### 증거의 적용 시점

충돌 해결의 승인 대상은 Manager가 검증해 stage한 결과와 그 결과를 반영한 재생 commit이다. 최종 HEAD에는 나머지 commit이 추가로 반영될 수 있으므로, 승인 시점의 파일 해시를 전체 rebase 완료 시점의 작업 폴더와 비교하지 않는다. 대신 원본 commit, 충돌 해결 commit과 최종 HEAD를 연결하는 재생 근거를 검증한다.

| 검증 단계 | 보존할 증거 | 비교 대상 |
|---|---|---|
| 실행 전 소유권 | repository·plan·branch·worktree identity, 실행 ID·generation, 시작 HEAD, target base, 원격 기대 OID·PR head, 원본 재생 대상과 순서 | Git 실행 직전의 실제 소유권·ref·등록·operation 상태 |
| Worker 충돌 교정 | 원본 commit, 재생 위치와 현재 parent HEAD, Worker invocation·session·결과 선언, 허용 경로와 승인된 내용·mode, 범위 검사 결과 | stage 직전의 파일·index 상태와 승인 내용 |
| stage 확정 | 해당 교정 identity, parent HEAD, 정확한 staged tree OID와 path·blob·mode, 실행 직전 snapshot | 충돌 해결을 반영한 해당 commit의 tree와 parent |
| 후속 commit 재생 | 원본에서 실제 재생 commit으로의 대응, 순서·parent 관계, 각 충돌 해결의 증거, 기존 정책이 허용한 무변경·skip 근거 | 실제 재생 이력 및 소유 실행에 결합된 Git 결과 |
| 전체 완료 | 완료 실행 표식, 최종 HEAD·소유 branch ref·등록 상태, target base 관계, 원격 기대 OID, 남은 index·작업 파일·Git operation 상태 | 완료 시점의 실제 Git 상태 |

표의 단계는 journal과 checkpoint 내부의 구분이며 새 ExecPlan 실행 상태를 추가하지 않는다. Git 실행과 결과 조회·기록은 Agent 호출 밖에서 Manager가 수행한다. Worker가 제출한 결과나 clean 상태만으로 Git 실행 소유권을 증명하지 않는다.

#### 책임과 저장 경계

`execution_handlers.py`는 stage, rebase 계속과 기존 실행 phase 전이를 조정한다. 소유 실행의 시작·단계별 결과·완료 기록은 `manager_git_journal.py`의 기존 journal 경계가 담당하고, 단계별 증거 대조는 부작용 없는 검증 경계로 분리한다. 하나의 함수에서 Git 실행, 증거 판정과 오류 상태 저장을 함께 처리하지 않는다.

`rebase_correction.py`와 `rebase_correction_state.py`의 교정 identity·결과 선언·범위 검증을 stage 증거에 연결한다. `execution_checkpoint_state.py`는 미완료 하위 단계와 journal 결과 identity를 보존하고, `runtime_state.py`는 확인된 완료를 계획 실행 row와 프로젝트 투영에 반영한다. 신규 실행·재시작·legacy 복구가 같은 판정 함수를 사용해 검사 기준의 차이를 없앤다.

저장은 기존 `<state-dir>` journal과 SQLite에 호환 가능한 필드·기록을 추가하는 방식으로 확장한다. 기존 기록과 schema를 삭제·재작성하지 않는다. 증거 버전을 명시하고 원래 rebase 실행 ID, 하위 Git 호출 ID, plan·repository·canonical worktree, Worker 교정 invocation과 session, SQLite generation에 결합한다. 세션만 같다는 이유로 서로 다른 교정 turn의 증거를 재사용하지 않는다. 내용은 commit/tree/blob OID, 허용 경로·mode·필요한 지문과 실행 결과 식별자로 보존하며 인증 자료와 source checkout 파일 내용은 기록하지 않는다.

#### 충돌 해결과 재생 검증

1. rebase 시작 전에 실제 소유권과 최신으로 관측한 원격 기대값을 확인하고, 현재 Git 방식이 재생할 원본 commit 집합과 순서를 고정한다. 이후 다른 원본 이력이나 target base를 같은 intent에 끼워 넣지 않는다.
2. 충돌 때 원본 commit과 현재 재생 위치를 기록한다. Worker 결과 선언과 허용 파일·index 범위를 검증한 뒤 Manager가 stage한다. stage 전후의 변경을 대조하고 정확한 staged tree를 Git 계속 호출 전에 저장한다. 미해결 stage, 예상 밖 flag·blob·mode 또는 승인 뒤 내용 변화가 남으면 계속하지 않는다.
3. `rebase --continue`가 해결 commit과 여러 후속 commit을 한 호출에서 생성하더라도, 저장한 parent·재생 위치·원본 commit으로 해결 commit을 식별한다. 그 commit의 tree가 승인된 staged tree와 같은지 확인한다. 항상 target base 다음의 첫 commit이라고 가정하지 않는다.
4. 나머지 재생 commit은 원본 commit·순서·parent 관계와 소유 Git 실행 결과를 대조한다. 충돌 없는 재생은 원본 patch의 보존 근거를, 충돌을 거친 재생은 그 commit에 결합된 승인 tree를 적용한다. 같은 파일을 변경하는 뒤의 원본 commit도 이 대응 관계로 검증한다. 전체 `range-diff`가 모두 동일하다는 조건이나 commit 수의 동일성만을 완료 근거로 사용하지 않는다.
5. 기존 정책이 허용한 무변경·skip은 처리된 원본 commit과 사유·검증 근거를 재생 대응에 남긴다. 설명되지 않는 누락, 추가 또는 순서 변경은 거부한다. 하나의 확인된 해결로 이후 충돌 해결이나 다른 commit 변경을 승인하지 않는다. 재생 대응을 유일하게 확인할 수 없으면 증거 부족으로 판정한다.
6. 전체 완료에서 현재 branch·HEAD·branch ref·worktree 등록, target base 관계와 최신 원격 기대값을 다시 확인한다. 실행 표식은 해당 소유 호출의 최신 결과와 연결하고 과거 reflog 전체에서 같은 문구를 찾은 사실만으로 인정하지 않는다. 남은 미해결 operation·index가 없고 최종 tracked·untracked·ignored 상태가 허용 범위인지 확인한다. 기존 autostash 경로는 저장된 stash identity와 보존 대상에 결합해 별도로 검증하며 미해결 복원은 기존 교정 경로로 넘긴다.

이 과정은 Manager가 이미 수행한 Git 전이의 증명을 다룬다. Agent 격리가 없는 호출의 shared ref·외부 worktree 변화를 자동 허용하거나 Agent 범위 검사를 줄이는 근거가 되지 않는다. rebase 없이 같은 HEAD에서 재개하는 실행은 일반 소유권·허용 변경 검사로 처리하고, 수행하지 않은 rebase의 intent나 해결 기록을 요구·생성하지 않는다.

#### 완료 확정과 중단 후 복구

검증된 journal 결과를 먼저 durable하게 확정하고, 그 결과 identity와 generation을 확인한 뒤 실행 checkpoint의 하위 단계를 정리하고 ExecPlan·프로젝트 투영을 같은 SQLite transaction에서 갱신한다. Git, 파일 journal과 SQLite를 하나의 원자 transaction으로 간주하지 않는다. 각 저장 경계 사이의 중단은 다음 실행에서 실제 Git 상태와 이미 저장된 증거를 다시 대조해 복구한다.

| 중단 경계 | 복구 판정 |
|---|---|
| Worker 결과 저장 뒤, 검증 또는 stage 전 | invocation·결과 선언·범위를 재검증한다. 미검증 결과를 legacy 성공으로 채택하지 않고 저장된 동일 교정을 이어간다. |
| stage 뒤, Git 계속 호출 전 | 저장된 staged tree·parent·재생 위치와 실제 index를 대조한다. 동일하면 같은 단계에서 계속하고 달라졌으면 자동 재사용하지 않는다. |
| 해결 commit 뒤, 후속 재생 중 | 해결 commit의 tree와 남은 재생 위치를 확인한다. 이미 반영된 해결을 다시 stage하거나 같은 Worker 교정을 중복 호출하지 않는다. 새 충돌은 별도 교정 identity로 처리한다. |
| Git 완료 뒤, journal 확정 전 | 실행 표식·해결 tree·재생 대응·최종 소유권을 재검증해 같은 호출의 완료 결과를 기록한다. 이미 완료된 `--continue`를 다시 실행하지 않는다. |
| journal 확정 뒤, SQLite 반영 전 | 저장 결과가 현재 Git과 여전히 일치하는지 확인하고 같은 generation의 완료 반영만 멱등하게 수행한다. |

target base나 원격 OID가 달라졌으면 저장된 작업의 완료 사실과 새 기준의 적용 필요를 구분한다. 이전 완료를 다른 target의 완료로 해석하지 않으며, 원격 변화는 기존 소유권·경쟁·lease 규칙에 따라 재관측 또는 차단한다. 필요한 새 rebase는 이전 작업의 확정 뒤 별도 intent로 시작한다.

구 형식 기록은 삭제하지 않는다. G-085처럼 승인된 staged tree와 해당 실행 표식이 남아 있으면 실제 해결 commit과 나머지 원본 재생을 유일하게 대응시키는 호환 검증을 수행한다. 증거 버전이 부족하다는 이유만으로 과거 실행을 승인하지 않으며, clean 상태·base ancestry·최종 OID 일치만으로 누락된 증거를 채우지 않는다. 확인된 증거와 판정 근거만 별도로 보존한다.

저장된 `status_failed` 재시도 또는 관련 `PLAN_BLOCKED`·`PROJECT_BLOCKED`는 같은 단계별 검증으로 재평가한다. 이 결함의 원인만 해제하고 독립적인 Review 실패, 사용자 결정, 다른 계획과 planning 상태를 보존한다. 후보 revision 변경에 따른 검증 증거를 무효화하고 필요한 기존 Worker 확인·targeted test·Manager 전체 verify·Review를 다시 수행한다. 완료 증거의 복구 자체는 구현 완료나 게시 승인이 아니다.

#### 진단과 검증

실패 진단에는 안정적인 원인 code, 단계, intent·교정 identity, 원본/재생 commit, 관련 경로와 기대·관측 지문을 기록한다. 해결 tree 불일치, 재생 대응 누락·변조, 최종 branch/ref/등록 불일치, 원격 lease 변화와 legacy 증거 부족을 구분한다. 큰 index·파일 본문·인증 자료를 로그에 출력하지 않으며 기존 실패 사건 및 알림 중복 제거를 유지한다.

직접 변경한 판정·저장 경계에는 targeted unit test를 두고, Manager가 실행하는 전체 verify에서는 실제 Git 이력의 충돌 해결·여러 후속 commit의 동일 파일 변경·다중 충돌·기존 PR 및 PR 없는 checkpoint를 확인한다. 각 저장 경계의 중단을 주입해 완료된 단계가 반복되지 않고 이후 Worker 교정·verify·Review까지 진행되는지 확인한다. 해결 tree 변조, 범위 밖 stage, 후속 commit 누락·변조, 다른 invocation의 증거, 원격 OID 변경과 불완전한 legacy 기록은 계속 거부해야 한다. 이 검증 책임은 기존 Worker·Manager·Reviewer 분담을 따른다.

## 검증과 재시도

- Worker는 구현과 관련 targeted unit test를 작성하고, 수정 범위에 직접 관련된 최소 unit test command만 실행한다
- targeted unit test 실패는 Worker가 같은 세션에서 수정하고 재실행한다. 실행 가능한 관련 unit test가 없는 변경은 사유를 남기며 전체 검증을 생략하는 근거로 사용하지 않는다
- Worker는 현재 운영체제용 verify 스크립트가 없으면 생성하고 테스트 명령이 바뀌면 갱신한다
  - Windows: `scripts/verify.ps1`
  - Linux: `scripts/verify.sh`
- verify 스크립트는 프로젝트 전체 검증을 수행하고 실패 시 0이 아닌 종료 코드를 반환한다
- 전체 test suite, integration test, end-to-end test, 프로젝트 전체 lint·build·type check는 verify 스크립트만 실행한다
- 프로그램은 verify 스크립트를 AI Agent 밖에서 실행한다
- 프로그램은 verify 성공 시 Worker를 다시 호출하거나 성공 출력을 전달하지 않는다
- verify 실패 시에만 종료 코드와 필요한 출력 내용을 같은 Worker 세션에 전달한다
- Reviewer는 성공한 Worker targeted unit test와 전체 verify 결과를 넘겨받고 테스트를 다시 실행하지 않는다
- Reviewer는 코드와 테스트의 diff 및 verify 스크립트 변경이 검증 범위를 약화하지 않았는지 읽기 전용으로 확인한다

전체 검증과 Review는 최초 실패 후 단계별로 최대 3회 재시도한다. 횟수는 SQLite에 저장해 프로그램 재실행으로 초기화되지 않으며, 성공하거나 BLOCKED 원인 문서가 변경됐을 때 초기화한다. 3회 재시도 후에도 실패하면 BLOCKED로 전이한다.

외부 CLI의 실행 실패, timeout과 일시적 서비스 오류는 프로그램 실행당 최대 3회 재시도한다. 모두 실패하면 현재 상태를 유지하고 해당 프로젝트를 건너뛴다. 다음 Loop Manager 실행에서는 다시 최대 3회 시도한다. AI 사용 한도 소진이 확인되면 현재 실행에서는 재시도하지 않는다.

## GitHub와 PR

- branch, commit, push와 PR 생성은 프로그램 코드가 수행한다
- Planner의 검증된 새 ExecPlan, snapshot에 결합된 미착수 계획 개정·rename·삭제와 `docs/FUTURE_IDEAS.md` 변경은 하나의 planning commit으로 `origin/<base-branch>`에 fast-forward push하며 별도 PR을 만들지 않는다
- 하나의 ExecPlan에는 하나의 결정적 branch와 하나의 PR만 연결한다
- PR 생성 전 같은 head branch의 Open, Closed 또는 Merged PR을 조회하고 기존 PR이 있으면 SQLite에 연결한다
- PR 생성 직후 SQLite 기록 전에 중단돼도 다음 실행에서 기존 PR을 찾아 복구하며 중복 PR을 만들지 않는다
- 원격 branch는 있지만 PR이 없는 복구 상황에서 rebase로 commit이 바뀌면, SQLite에 저장된 동일한 Loop Manager 소유 branch임을 확인한 경우에만 `--force-with-lease`로 갱신한다
- PR 생성 전 관련 테스트, 전체 검증과 Reviewer가 모두 성공해야 한다
- PR 생성 직전에 최신 원격 기준 branch를 rebase로 반영하고, 기준이 바뀌었다면 전체 검증과 Review를 다시 수행한다
- PR 생성 알림에는 GitHub PR 링크를 포함한다
- 프로그램은 PR을 Merge하지 않는다
- PR이 Merge 없이 닫히면 BLOCKED로 전이한다
- 닫힌 PR은 프로그램이 새 PR로 대체하지 않으며 사용자가 같은 PR을 다시 열어야 한다
- PR Merge 후 ExecPlan을 MERGED로 전이하고 `git fetch`로 `origin/<base-branch>`가 갱신된 것을 확인한 뒤 의존성이 충족된 다음 READY 계획을 시작한다
- PR의 Merge 상태와 head commit을 기준으로 관리 worktree의 commit이 모두 PR에 push됐는지 확인한다. base branch의 commit ancestry는 Merge commit에서만 성립하므로 push 여부 판단에 사용하지 않는다
- Merge commit, Squash merge와 Rebase merge 모두에서 worktree가 clean하고 로컬 HEAD가 PR head보다 앞서지 않았으면 원격 head branch가 삭제됐더라도 관리 worktree를 자동 제거한다
- 미커밋 변경, PR head에 없는 로컬 commit 또는 안전 여부를 확인할 수 없는 상태가 남아 있으면 제거하지 않고 `cleanup_pending`에 경로와 구체적인 원인을 기록한다

## Telegram 알림

Telegram token과 chat ID가 모두 설정된 경우에만 알림 이벤트와 전송 여부를 SQLite에 저장하고 전송한다. 둘 다 비어 있으면 알림을 생성하거나 전송하지 않는다.

- PR 생성: 프로젝트명, ExecPlan과 PR 링크
- BLOCKED: 프로젝트명, ExecPlan 또는 계획 단계, 원인과 수정할 파일 경로
- 프로젝트 완료: 프로젝트명과 마지막 완료 작업

동일 이벤트는 고유 키로 중복을 막는다. 전송 실패는 작업 상태와 분리해 다음 실행에서 재시도한다. 프로젝트 완료 여부는 프로젝트별로 관리하며 새 PRODUCT/Requirement 작업 주기가 완료되면 다시 알린다. Requirement archive 이동만으로는 완료 알림을 재활성화하지 않는다.

## Repository Safety and Security

- 등록 프로젝트 기본 worktree의 로컬 변경, ahead·diverged 이력과 checkout branch는 원격 SHA 기반 자동 작업을 막지 않는다. safe fast-forward 조건을 만족하지 않으면 기본 worktree 동기화만 건너뛴다
- 사용자 파일 삭제, reset, 일반적인 강제 push와 자동 Merge를 수행하지 않는다
- 계획 commit의 base branch 직접 push는 fast-forward만 허용한다. branch protection 등으로 거부되면 우회하지 않고 계획 오류로 보존한다
- 기본 worktree에서는 검증된 `git merge --ff-only origin/<base-branch>` 외의 파일 쓰기, checkout, pull, rebase, reset, stash, clean과 commit을 수행하지 않는다
- 관리 worktree, SQLite, 임시 파일, 로그와 lock은 source checkout 밖의 `<state-dir>`에만 만든다
- 모든 계획과 구현 변경은 Loop Manager가 만든 관리 worktree 안에서만 수행한다
- AI Agent가 접근하고 수정할 수 있는 경로를 해당 관리 worktree로 제한한다
- Worker가 제안한 `AGENTS.md` 변경은 승인 전 실제 파일에 적용하지 않으며, 승인 후에도 사용자 결정에 결합된 대상과 patch만 허용한다
- `git fetch`와 `git worktree add/remove`는 공유 Git metadata만 변경한다. 기본 worktree의 파일, index와 HEAD 변경은 검증된 safe fast-forward synchronizer만 수행한다
- 관리 worktree는 미커밋 변경과 PR head에 없는 로컬 commit이 없다고 확인된 때만 강제 옵션 없이 제거한다. base branch ancestry나 GitHub Merge 방식은 push 여부 판정에 사용하지 않는다
- PR head 정보나 commit 관계를 확인할 수 없으면 worktree를 보존하고 `cleanup_pending`으로 기록한다
- `cleanup_pending` worktree가 이후 clean해지거나 경로가 사라지면 기록을 지우고 강제 옵션 없이 정리한다
- planning worktree는 Planner만 수정할 수 있으므로 검증 실패 기록과 diff를 보존한 뒤 새 계획 입력으로 재시작할 때 폐기할 수 있다
- Merge된 로컬 관리 branch만 비강제 삭제할 수 있으며 원격 branch는 자동으로 삭제하지 않는다
- 예외적으로 PR이 아직 없는 동일한 Loop Manager 소유 branch의 rebase 복구에만 `--force-with-lease`를 사용할 수 있다
- 공통 파일 복사 스크립트는 사용자가 명시적으로 실행하는 초기 설정 도구이며 자동 Loop에서 호출하지 않는다
- 등록 프로젝트의 문서와 verify 스크립트는 사용자가 신뢰한 입력으로 취급한다
- Telegram token과 다른 자격 증명을 AI prompt, subprocess 인자와 로그에 노출하지 않는다
- subprocess는 인자 배열로 실행하고 필요한 경우에만 운영체제 shell을 사용한다

## Architecture Change Policy

다음 변경은 ExecPlan을 BLOCKED로 만들고 사용자 승인을 받은 뒤 진행한다.

- SQLite schema 또는 상태 모델의 비호환 변경
- worktree, branch, commit, push와 PR 생성 경계 변경
- 인증 또는 비밀값 저장 방식 변경
- 새로운 외부 infrastructure나 비용 발생 서비스 도입
- 데이터 삭제 또는 비가역 migration
- 같은 프로젝트의 병렬 실행 도입
