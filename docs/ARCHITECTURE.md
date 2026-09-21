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

SQLite에는 등록 프로젝트, 기준 branch, 선택한 provider, 등록 시 `AGENTS.md` 승인 해시, `AGENTS.md` 변경 제안에 대한 대상 경로·사용자 결정·확정 patch 해시, 계획 입력 지문과 `planning_error`, ExecPlan 메타데이터와 의존성, 실행 상태와 재시도 횟수, AI Agent 세션, worktree, branch, PR, `cleanup_pending`, 알림과 프로젝트 완료 주기를 저장한다.

실행 시작 시 SQLite, Git worktree/branch와 GitHub PR 상태를 대조한다. 서로 다르면 GitHub Merge 상태, 프로젝트 파일, SQLite 실행 정보 순으로 각 소유 영역의 원본을 적용한다.

## 계획 변경 감지

프로젝트별 계획 입력 지문은 최신 `origin/<base-branch>`의 추적 파일을 기준으로 다음 내용에서 계산한다.

- `AGENTS.md`
- `docs/PRODUCT.md`
- `docs/requirements/` 바로 아래의 `*.md`
- `docs/ARCHITECTURE.md`
- archive, `docs/plans/`와 `docs/FUTURE_IDEAS.md`를 제외한 소스, 테스트와 설정 파일

Planner가 추가 ExecPlan이 필요 없다고 판단하면 현재 지문을 저장한다. 지문이 같고 새로 MERGED된 ExecPlan도 없으면 Planner를 다시 실행하지 않는다. Requirement archive 이동만으로는 새 작업 주기나 프로젝트 완료 알림을 활성화하지 않는다.

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

1. 실행 중인 작업이 없고 계획 입력이 변경됐으면 READY ExecPlan의 존재와 관계없이 `<state-dir>/worktrees/<project-id>/planning/<input-fingerprint>/`에 `origin/<base-branch>` 기준의 detached planning worktree를 만든다
2. 프로그램이 비종료 ExecPlan의 compact catalog를 만들고, Planner가 이를 현재 입력 및 관련 코드·테스트와 대조해 식별 가능한 미충족 작업 전체를 독립적인 새 ExecPlan으로 나누어 priority와 `depends_on`을 포함해 일괄 작성한다. 현재 범위 밖의 보존 가치가 있는 아이디어는 같은 계획 주기의 ExecPlan으로 만들지 않고 `docs/FUTURE_IDEAS.md`에 분리할 수 있다
3. 프로그램이 Planner 변경을 새 ExecPlan과 선택적인 `docs/FUTURE_IDEAS.md` 변경으로 분리한다. 다른 경로, 기존 ExecPlan 변경·삭제 또는 범위 밖 아이디어를 현재 ExecPlan에 포함한 결과는 거부하고, 새 ExecPlan만 메타데이터와 의존성 그래프 검증 및 SQLite 등록 대상으로 사용한다
4. 검증에 실패하면 오류를 같은 Planner 세션에 전달하고 최대 3회까지 수정과 재검증을 수행한다
5. 계속 실패하면 원인과 계획 입력 지문을 프로젝트의 `planning_error`에 저장하고 planning worktree와 세션을 보존한 뒤 처리를 중단한다
6. 같은 지문에서는 Planner를 다시 실행하지 않는다. 계획 입력 지문이 바뀌면 검증 오류와 diff를 로그에 보존하고, Planner만 수정할 수 있었던 이전 planning worktree를 제거한 뒤 `planning_error`를 지우고 새 지문의 planning worktree에서 다시 시작한다
7. 새 ExecPlan과 `docs/FUTURE_IDEAS.md` 변경이 모두 없으면 `planning_error`를 지우고 입력 지문을 저장한 뒤 planning worktree를 제거한다
8. 유효한 새 ExecPlan과 `docs/FUTURE_IDEAS.md` 변경을 planning worktree에서 하나의 planning commit으로 만든다. FUTURE_IDEAS만 변경된 결과는 ExecPlan이나 SQLite 실행 상태를 만들지 않는다
9. `git fetch` 후 `origin/<base-branch>`가 planning worktree의 시작 commit과 다르면 계획 commit을 rebase하지 않고 planning worktree를 정리한 뒤 최신 기준으로 다시 계획한다
10. 기준이 그대로면 계획 commit을 `origin/<base-branch>`에 fast-forward push한다. force push는 사용하지 않는다
11. push된 commit을 원격에서 확인한 뒤에만 새 ExecPlan을 SQLite에 READY 상태로 등록하고 `planning_error`를 지운다. `docs/FUTURE_IDEAS.md`는 등록·파싱 대상에서 제외한다. push 후 SQLite 기록 전에 중단된 경우 원격의 새 ExecPlan을 다시 파싱해 복구한다
12. planning worktree를 제거한다
13. 모든 `depends_on`이 MERGED인 READY ExecPlan 중 priority가 가장 높은 계획을 선택하고, 같은 priority에서는 숫자 ID가 가장 작은 계획을 선택한다
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
- 계획 입력이 변경되면 활성 작업을 선점하지 않으며, 다음 Worker 선택 전에 기존 READY 계획을 포함한 전체 카탈로그를 갱신한다
- READY ExecPlan은 모든 `depends_on`이 MERGED일 때만 실행 후보가 된다
- 실행 가능한 READY ExecPlan이 여럿이면 priority가 높은 계획을 먼저 선택하고 같은 priority에서는 숫자 ExecPlan ID가 가장 작은 계획을 선택한다
- 각 ExecPlan은 `loop-manager/<execplan-id>` 형식의 고유한 branch와 별도 관리 worktree를 사용한다
- PR_OPEN 또는 BLOCKED 프로젝트에서는 다른 ExecPlan을 시작하거나 Planner를 다시 실행하지 않는다
- 서로 다른 프로젝트만 병렬로 처리한다
- 전체 동시 프로젝트 수는 `max_workers`로 제한한다
- Planner 호출은 한 번에 하나만 실행한다

같은 프로젝트를 직렬화하므로 파일 범위 충돌 판정과 병렬 PR 재정렬은 지원하지 않는다. `depends_on`은 병렬 스케줄링이 아니라 선행 PR Merge를 강제하는 실행 게이트로 사용한다.

## AI Agent 실행

- Planner, Worker와 Reviewer prompt는 Loop Manager에 내장한다
- 프로젝트별 prompt 재정의는 지원하지 않는다
- 프로그램은 전체 ExecPlan을 코드로 파싱하되 Planner에게는 비종료 계획의 ID, 제목, 상태, sources, depends_on과 priority만 compact catalog로 전달한다
- Planner는 PRODUCT, 활성 Requirement, ARCHITECTURE, 관련 코드·테스트와 비종료 ExecPlan compact catalog를 읽고 현재 식별 가능한 미충족 작업 전체를 한 계획 주기에 새 ExecPlan으로 작성하며, 범위 밖의 보존 가치가 있는 아이디어는 `docs/FUTURE_IDEAS.md`에 분리한다
- Planner는 MERGED ExecPlan 메타데이터와 본문을 기본 입력으로 받거나 읽지 않는다. 프로그램만 ID 고유성, 번호 할당, 의존성 충족과 감사 이력을 위해 이를 유지한다
- Planner는 안전한 무인 실행 차단과 반복적인 사용자 복구 제거를 최우선으로 priority를 정하고, 정상 경로 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순으로 계획한다
- Planner는 새 `docs/plans/*.md`를 생성하고 `docs/FUTURE_IDEAS.md`를 생성하거나 수정할 수 있다. 기존 ExecPlan과 그 밖의 파일은 변경할 수 없으며 프로그램이 실행 전후 diff로 범위를 검증한다
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

### AGENTS.md 변경 제안과 승인

- Worker는 현재 ExecPlan에 필요한 `AGENTS.md` 변경을 발견해도 파일을 직접 수정하지 않고 선택된 ExecPlan의 `Decision Log`에 대상 경로, 프로젝트 전용·공통 범위, 이유, 영향과 정확한 patch를 기록한다
- 현재 프로젝트의 `AGENTS.md`는 해당 프로젝트 Worker가 제안할 수 있다. 향후 프로젝트 설정에 사용하는 기준 `AGENTS.md`의 공통 규칙은 Loop Manager 자체 저장소의 Worker만 제안할 수 있다
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
- Planner의 검증된 새 ExecPlan과 `docs/FUTURE_IDEAS.md` 변경은 하나의 planning commit으로 `origin/<base-branch>`에 fast-forward push하며 별도 PR을 만들지 않는다
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
