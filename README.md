# Loop Manager

Loop Manager는 외부 스케줄러가 반복 실행하며 여러 로컬 GitHub 프로젝트의 계획, 구현, 검증과 Pull Request 생성을 관리하는 도구다.

프로젝트 문서에서 작업을 찾고 Planner, Worker, Reviewer를 순서대로 실행한다. 작업 상태와 AI Agent 세션은 재실행 후에도 이어지며 서로 다른 프로젝트를 병렬로 처리할 수 있다. 같은 프로젝트에서는 한 번에 하나의 작업만 실행하고, 의존 작업은 선행 PR이 Merge될 때까지 기다린다. PR 생성, 작업 차단과 프로젝트 완료는 Telegram으로 알린다.

## 주요 동작

- 여러 로컬 Git 프로젝트 등록 및 상태 관리
- Requirement를 바탕으로 ExecPlan 생성
- 작업 난이도에 맞는 Codex 또는 Claude 모델 선택
- ExecPlan별로 격리된 Git branch와 worktree에서 구현
- 프로젝트별 전체 검증과 AI Reviewer 실행
- Pull Request 생성 후 사용자의 Merge 대기
- 외부 스케줄러를 통한 반복 실행과 중단 상태 복구

세부 제품 동작은 [PRODUCT.md](docs/PRODUCT.md), 기술 설계는 [ARCHITECTURE.md](docs/ARCHITECTURE.md)에서 관리한다.

## 사용자가 할 일

### 1. 공통 파일 복사

Loop Manager 저장소 루트에서 관리할 프로젝트의 경로를 지정한다.

```powershell
pwsh -File scripts/copy-project-files.ps1 -TargetPath D:\repo\my-project
```

```sh
bash scripts/copy-project-files.sh /path/to/my-project
```

복사 스크립트는 사용자가 명시적으로 실행하는 초기 설정 도구다. 공통 `AGENTS.md`, Specification Skill, 문서 템플릿과 빈 작업 폴더를 준비하고 `PRODUCT.md`와 `ARCHITECTURE.md` 초안을 생성한다. 대상에 `AGENTS.md`, `.agents` 또는 `docs`가 이미 있으면 덮어쓰지 않고 중단하며, 자동 Loop에서는 이 스크립트를 호출하지 않는다.

기존 프로젝트에 적용할 때는 문서를 덮어쓰지 말고 다음 파일만 선택적으로 병합한다.

- `AGENTS.md`
- `.agents/skills/`
- `docs/PLANS.md`
- `docs/*.template.md`

### 2. 프로젝트 문서 작성

사용자는 대상 프로젝트에 맞게 다음 문서를 작성한다.

- `docs/PRODUCT.md`: 현재 제품에서 지켜야 할 동작과 장기 규칙
- `docs/ARCHITECTURE.md`: 제품 동작을 보장할 시스템 구조와 기술 결정
- `docs/requirements/*.md`: 새 기능이나 정책 변경이 있을 때 작성하는 Requirement
- `AGENTS.md`: 프로젝트별 AI 작업 규칙이 필요하면 공통 규칙을 유지한 채 추가

`PRODUCT.md`와 Requirement 중 하나 이상은 반드시 있어야 한다. 초기 제품은 `PRODUCT.md`로 정의하고, 운영 중 변경은 `REQUIREMENT.template.md`를 복사해 Requirement로 작성한다. Requirement는 별도 상태나 frontmatter 없이 작성하며 `docs/requirements/` 바로 아래에 둔 모든 문서가 Planner 입력이 된다.

Specification Skill을 사용하면 템플릿을 바탕으로 초안을 작성하고 누락된 흐름이나 조건을 함께 확인할 수 있다.

문서 작성이 끝나면 변경사항을 commit하고 push한다. Loop Manager는 최신 원격 branch를 자동 작업의 기준으로 사용하며 기본 worktree의 파일, index와 HEAD를 수정하지 않는다. 기본 worktree가 원격보다 뒤처진 상태는 허용하지만 dirty하거나 원격보다 앞선 변경은 자동 작업에 포함되지 않으므로 해당 프로젝트를 건너뛴다.

### 3. 실행 환경과 Loop Manager 설정

저장소를 받은 뒤 한 번만 환경 준비 스크립트를 실행한다. 기존 `.venv`가 있으면 재사용하고 `requirements.txt`의 패키지만 설치한다.

```powershell
pwsh -NoProfile -File scripts/setup.ps1
```

```sh
bash scripts/setup.sh
```

현재 최소 실행기는 외부 Python 패키지를 사용하지 않으므로 `requirements.txt`에는 의존성이 없다. 이후 패키지가 추가돼도 같은 스크립트를 다시 실행하면 된다.

`config/config.ini.example`을 `config/config.ini`로 복사한 뒤 사용할 AI Agent 모델, Telegram 설정과 동시 실행 수를 입력한다. `max_workers` 기본값은 3이다.

```powershell
Copy-Item -LiteralPath config/config.ini.example -Destination config/config.ini
```

```sh
cp config/config.ini.example config/config.ini
```

실제 `config.ini`는 향후 Telegram token을 포함할 수 있으므로 Git에 커밋하지 않는다. 작업 상태와 PR 정보는 source checkout 밖의 운영체제 사용자 상태 디렉터리에 있는 SQLite에 저장한다.

일반 실행 중 SQLite가 없거나 손상되면 자동으로 새 상태를 만들지 않고 중단한다. 상태 디렉터리를 백업해두고, 필요할 때 백업을 복원하거나 손상된 DB를 별도로 보관한 뒤 `init`으로 다시 만든다.

### 4. 초기화 및 반복 실행 설정

최소 실행기는 별도 프로젝트 등록 명령 없이 `run`에 전달된 저장소 하나를 처리한다. 공통 `AGENTS.md`가 포함되지 않은 저장소는 거부하며, 내용을 확인한 경우에만 `--allow-agents-mismatch`로 실행할 수 있다.

Planner가 만든 ExecPlan은 검증 후 대상 저장소의 base branch에 fast-forward push된다. 따라서 해당 branch는 Loop Manager 계정의 직접 push를 허용해야 한다. 이후 구현은 `loop-manager/<execplan-id>` branch에서 진행되고 PR로 제출된다.

상태 DB를 한 번 초기화한다.

```powershell
& .\.venv\Scripts\python.exe loop_manager.py init
```

```sh
.venv/bin/python loop_manager.py init
```

그다음 cron에서 대상 저장소를 지정해 반복 실행한다.

```powershell
& .\.venv\Scripts\python.exe loop_manager.py run D:\repo\my-project
```

```sh
.venv/bin/python loop_manager.py run /path/to/project
```

실행기는 한 번에 한 프로젝트의 작은 작업 하나만 별도 worktree에서 처리한다. PR을 생성하면 Merge될 때까지 새 작업을 시작하지 않으며, 현재 기준 commit에서 할 일이 없다고 판단한 뒤에는 commit이 바뀔 때까지 Codex를 다시 호출하지 않는다. 서버에는 Python 3, Git, GitHub CLI와 인증된 Codex CLI가 필요하다.

cron에는 같은 `run` 명령을 등록한다. 동시 실행은 전역 잠금으로 거부하므로 여러 프로젝트 병렬 처리나 같은 실행에서의 프로젝트 순회는 후속 구현에서 추가한다.

### 5. Pull Request 검토

최소 실행기는 생성한 PR URL을 표준 출력에 표시한다. 사용자는 PR을 검토하고 Merge하며, 열린 동안에는 다음 실행도 같은 PR을 기다린다. PR을 Merge하지 않고 닫았다면 같은 PR을 다시 열어야 한다.

GitHub가 Merge를 확인하면 PR head commit을 기준으로 로컬 전용 commit이 없는 깨끗한 관리 worktree를 Merge, Squash, Rebase 방식과 관계없이 자동 정리한다. 미커밋 변경, PR에 포함되지 않은 로컬 commit 또는 commit 관계를 확인할 수 없는 worktree는 삭제하지 않고 `cleanup_pending`에 경로와 원인을 기록한다. 이 경고는 다음 작업이나 프로젝트 완료를 막지 않으며, 이후 실행에서 안전 조건이 충족되거나 경로가 사라지면 자동으로 해제된다.

## 사용자가 직접 작성하지 않는 항목

- `docs/PLANS.md`와 `docs/*.template.md`는 복사된 공통 파일을 그대로 사용한다.
- `docs/plans/*.md`의 ExecPlan은 Planner가 생성한다.
- 현재 작업 phase, branch, worktree와 PR은 Loop Manager의 SQLite가 관리한다.
- Worker가 기능을 구현하고 관련 테스트를 작성한다.
- Worker가 프로젝트에 맞는 `scripts/verify.ps1` 또는 `scripts/verify.sh`를 생성하고 유지한다.
- Worker는 전체 검증 스크립트를 직접 실행하지 않는다.
- Loop Manager가 전체 검증을 실행하고 실패 출력만 Worker에게 전달한 뒤 읽기 전용 Reviewer와 Pull Request 생성을 진행한다.
- worktree, branch와 현재 작업 상태는 Loop Manager가 관리한다.

일반적인 실행 중에는 사용자가 코드, 테스트, ExecPlan이나 SQLite 상태를 수정할 필요가 없다. 단, BLOCKED 원인을 해소할 때는 ExecPlan 또는 제품 문서를 수정할 수 있다.
