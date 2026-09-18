# Loop Manager

Loop Manager는 외부 스케줄러가 반복 실행하며 여러 로컬 GitHub 프로젝트의 계획, 구현, 검증과 Pull Request 생성을 관리하는 도구다.

프로젝트 문서에서 작업을 찾고 Planner, Worker, Reviewer를 순서대로 실행한다. 작업 상태와 AI Agent 세션은 재실행 후에도 이어지며 서로 다른 프로젝트를 병렬로 처리할 수 있다. 같은 프로젝트에서는 한 번에 하나의 작업만 실행하고, 의존 작업은 선행 PR이 Merge될 때까지 기다린다. PR 생성, 작업 차단과 프로젝트 완료는 Telegram으로 알린다.

현재 저장소는 제품 명세와 공통 템플릿을 작성하는 단계다. 실행 CLI가 구현되면 실제 설치 및 실행 명령을 이 문서에 추가한다.

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

### 3. Loop Manager 설정

`config/config.ini.example`을 `config/config.ini`로 복사한 뒤 사용할 AI Agent 모델, Telegram 설정과 동시 실행 수를 입력한다. `max_workers` 기본값은 3이다.

```powershell
Copy-Item -LiteralPath config/config.ini.example -Destination config/config.ini
```

```sh
cp config/config.ini.example config/config.ini
```

실제 `config.ini`는 Telegram token을 포함할 수 있으므로 Git에 커밋하지 않는다. token과 chat ID를 모두 비워두면 Telegram 알림을 사용하지 않는다. 등록 프로젝트, 작업 상태, 세션과 PR 정보는 source checkout 밖의 운영체제 사용자 상태 디렉터리에 있는 SQLite에 저장한다.

일반 실행 중 SQLite가 없거나 손상되면 자동으로 새 상태를 만들지 않고 중단한다. 상태 디렉터리를 백업해두고, 필요할 때 백업을 복원하거나 설정 모드에서 명시적으로 재설정한다.

### 4. 프로젝트 등록 및 반복 실행 설정

1. Loop Manager 설정 모드에서 프로젝트 경로와 Codex 또는 Claude를 선택한다.
2. `AGENTS.md` 경고가 발생하면 공통 규칙을 병합하거나 현재 파일로 강제 등록할지 결정한다.
3. Loop Manager를 한 번 실행해 프로젝트 설정이 유효한지 확인한다.
4. cron 또는 Windows 작업 스케줄러로 같은 실행 명령을 반복하도록 설정한다.

진행 중이거나 대기 중인 ExecPlan 또는 해결되지 않은 계획 단계 오류가 있는 프로젝트는 등록 해제할 수 없다. `AGENTS.md`와 `docs/PLANS.md` 검사는 최초 등록 시에만 수행한다.

실제 설치, 설정 모드와 실행 명령은 CLI 구현 후 이 문서에 추가한다.

### 5. Pull Request 검토

Telegram 알림을 사용하는 경우 전달된 Pull Request를 검토하고 Merge한다. 선행 PR을 Merge하기 전에는 해당 PR에 의존하는 다음 작업이 시작되지 않는다. `BLOCKED`가 발생하면 안내된 worktree의 ExecPlan 또는 그 근거가 된 PRODUCT/Requirement를 수정한다. Planner가 유효한 ExecPlan을 만들지 못한 계획 단계 오류는 계획 입력이 변경된 뒤 다시 시도한다. PR을 Merge하지 않고 닫아서 BLOCKED가 됐다면 문서 수정이 아니라 같은 PR을 다시 열어야 한다.

Merge된 작업의 worktree에 미커밋 또는 미푸시 변경이 남아 있으면 삭제하지 않고 `cleanup_pending`으로 보존한다. 이는 다음 작업이나 프로젝트 완료를 막지 않는다.

## 사용자가 직접 작성하지 않는 항목

- `docs/PLANS.md`와 `docs/*.template.md`는 복사된 공통 파일을 그대로 사용한다.
- `docs/plans/*.md`의 ExecPlan은 Planner가 생성한다.
- ExecPlan 실행 상태와 재시도 횟수는 Loop Manager의 SQLite가 관리한다.
- Worker가 기능을 구현하고 관련 테스트를 작성한다.
- Worker가 프로젝트에 맞는 `scripts/verify.ps1` 또는 `scripts/verify.sh`를 생성하고 유지한다.
- Worker는 전체 검증 스크립트를 직접 실행하지 않는다.
- Loop Manager가 전체 검증을 실행하고 실패 출력만 Worker에게 전달한 뒤 읽기 전용 Reviewer와 Pull Request 생성을 진행한다.
- worktree, branch, 작업 상태와 AI Agent 세션은 Loop Manager가 관리한다.

일반적인 실행 중에는 사용자가 코드, 테스트, ExecPlan이나 SQLite 상태를 수정할 필요가 없다. 단, BLOCKED 원인을 해소할 때는 ExecPlan 또는 제품 문서를 수정할 수 있다.
