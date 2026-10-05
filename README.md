# Loop Manager

Loop Manager는 여러 로컬 GitHub 프로젝트의 계획·구현·검증·Pull Request 생성을 관리한다. 사용자는 제품 문서를 작성하고 프로젝트를 등록한 뒤, 차단 시 필요한 결정을 내리고 PR을 검토·Merge한다.

외부 스케줄러가 반복 실행하며 상태와 Agent session은 중단 후에도 이어진다. 같은 프로젝트의 변경은 하나씩, 다른 프로젝트는 `max_workers` 안에서 병렬 처리한다. PR 생성·차단·프로젝트 완료는 Telegram으로 알린다.

## 시작하기

Python 3, Git, GitHub CLI와 프로젝트에서 선택한 인증된 Codex 또는 Claude Code CLI가 필요하다. 아래 명령은 Loop Manager 저장소 루트에서 실행한다.

Windows·macOS·Linux의 검증 진입점은 각각 PowerShell과 Bash를 사용한다. verify는 저장소의 `.venv`를 우선 사용하며 다른 디렉터리에서 호출해도 저장소 루트에서 실행한다. macOS에서 CPU·메모리 관측이 불가능하면 기존 안전 규칙에 따라 직렬 검증으로 축소한다.

Codex 자동 실행은 세 OS의 내장 sandbox 사전 검사를 통과해야 시작하며 CLI 0.159.3 이상이 필요하다. Windows에서는 Python 3.13 이상과 초기 설정을 마친 elevated sandbox가 필요하고 기존 `CODEX_HOME`의 인증·session 저장소를 사용한다. Linux/macOS는 역할별 runtime에서 원본 인증·설정에 연결한다. Windows는 ACL·Job Object, macOS는 Seatbelt 정책, Linux는 읽기 전용 mount와 프로세스 그룹을 사용하며 경계를 확인할 수 없으면 권한을 완화하지 않고 중단한다. Claude의 검증되지 않은 격리 호출은 계속 거부한다. [Codex 권한 정책](https://developers.openai.com/codex/permissions)

### 1. 관리할 프로젝트 준비

```powershell
pwsh -File scripts/copy-project-files.ps1 -TargetPath D:\repo\my-project
```

```sh
bash scripts/copy-project-files.sh /path/to/my-project
```

복사 스크립트는 공통 AGENTS·Specification Skill·문서 템플릿과 빈 작업 폴더, PRODUCT·ARCHITECTURE 초안을 준비한다. 대상에 `AGENTS.md`, `.agents` 또는 `docs`가 있으면 덮어쓰지 않고 중단한다. 자동 Loop에서는 호출하지 않는다.

기존 프로젝트에는 필요한 공통 파일(`AGENTS.md`, `.agents/skills/`, `docs/PLANS.md`, `docs/*.template.md`)만 선택적으로 병합한다.

### 2. 프로젝트 문서 작성·게시

| 문서 | 작성할 내용 |
|---|---|
| `docs/PRODUCT.md` | 현재 제품의 동작·경계·장기 규칙 |
| `docs/requirements/*.md` | 새 기능·정책 변경과 Acceptance Criteria |
| `docs/ARCHITECTURE.md` | 제품 규칙을 보장하는 구조·기술 결정·복구 방식 |
| `AGENTS.md` | 공통 작업 원칙을 유지하면서 필요한 프로젝트 작업 규칙 추가 |

PRODUCT와 활성 Requirement 중 하나 이상은 반드시 있어야 한다. 초기 제품은 PRODUCT로 정의하고 운영 중 변경은 Requirement로 작성한다. Requirement는 frontmatter·상태 없이 `docs/requirements/` 바로 아래에 두며 모두 Planner 입력이 된다.

Specification Skill로 역할에 맞는 초안을 작성하고 누락된 조건을 확인할 수 있다. 작성이 끝나면 commit·push한다. Loop Manager는 최신 원격 base만 입력으로 사용하므로 로컬 전용 파일·commit은 자동 작업에 포함되지 않는다.

### 3. 환경과 설정 준비

```powershell
pwsh -NoProfile -File scripts/setup.ps1
Copy-Item -LiteralPath config/config.ini.example -Destination config/config.ini
```

```sh
bash scripts/setup.sh
cp config/config.ini.example config/config.ini
```

setup은 기존 `.venv`를 재사용하고 `requirements.txt`의 패키지를 설치한다. 현재 외부 Python 의존성은 없으며 나중에 추가되면 같은 스크립트를 다시 실행한다.

`config/config.ini`에 모델·Effort, Telegram과 동시 실행 수를 설정한다. `[codex.roles]`와 `[claude.roles]`의 `planner_check = Model alias,Effort`는 읽기 전용 점검 Planner에 독립 적용되며 생략하면 같은 provider의 `planner` 값을 사용한다. `max_workers` 기본값은 3, 전체 verify의 `verify_timeout_seconds`는 600초다. 검증이 정상 진행 중인데 상한이 부족한 경우 `[loop]`에서 조정한다.

Loop Manager 자체 verify는 `[loop] verify_parallelism = auto`가 기본이다. 코드는 허용 CPU·실제 부하·메모리 여유와 아직 관측에 반영되지 않은 예약을 확인해 새 테스트 모듈을 배정한다. 양의 정수는 verify별 상한이며 `1`은 직렬이다. 상한 안에서도 자원 부족 시 배정을 기다리고, 관측 실패 시 직렬로 축소한다. `max_workers`의 프로젝트 실행 수와 별개이며 다른 등록 프로젝트의 verify 스크립트에는 적용하지 않는다.

Manager 실행의 시간 이벤트·worker 원문·manifest·집계는 상태 디렉터리 `logs/verify/<실행 ID>/`에 남는다. 단독 스크립트는 `.cache/verify/<실행 ID>/`를 사용한다. 기존 두 OS 스크립트가 같은 runner로 전체 unittest와 compile을 수행하며 실제 Agent CLI·외부 API 연결은 테스트 프로세스에서 차단된다. 시간 집계만 읽으려면 `python -m loop_manager_internal.verification.runner --report <직렬 기록 디렉터리> <auto 기록 디렉터리>`를 사용한다. 이 명령은 테스트를 실행하지 않는다. 비교 결과의 범위·관측 환경 일치와 원본의 부하·메모리 제한을 확인한 뒤 시간을 해석한다.

실제 config에는 비밀값이 들어갈 수 있으므로 Git에 commit하지 않는다. 상태·PR·session은 source checkout 밖 사용자 상태 디렉터리의 SQLite에 저장한다. 상태 디렉터리는 백업해 둔다.

### 4. 상태 초기화와 프로젝트 등록

```powershell
& .\.venv\Scripts\python.exe loop_manager.py config init
& .\.venv\Scripts\python.exe loop_manager.py config add D:\repo\my-project --provider claude
& .\.venv\Scripts\python.exe loop_manager.py config list
```

```sh
.venv/bin/python loop_manager.py config init
.venv/bin/python loop_manager.py config add /path/to/project --provider claude
.venv/bin/python loop_manager.py config list
```

init은 한 번 실행하고 프로젝트마다 add한다. provider를 생략하면 codex를 사용한다. 등록 검사는 fetch한 원격 base의 필수 문서·공통 파일·provider를 기준으로 하며 로컬 전용 편집은 반영하지 않는다.

공통 AGENTS 내용이 없으면 등록을 거부한다. 파일 내용을 확인한 경우에만 `--approve-agents-mismatch`로 해당 blob 해시에 결합된 승인을 저장할 수 있다. 진행 중인 계획·프로젝트 차단·미해결 planning 오류가 없으면 `config remove <path>`로 등록만 해제하며 파일·branch·worktree·PR·MERGED 감사는 보존한다.

Planner는 검증된 계획을 base branch에 fast-forward push하므로 Loop Manager 계정의 직접 push가 허용돼야 한다. 구현은 `loop-manager/<execplan-id>` branch에서 진행하고 PR로 제출한다.

### 5. 실행과 외부 스케줄러 등록

```powershell
& .\.venv\Scripts\python.exe loop_manager.py run
```

```sh
.venv/bin/python loop_manager.py run
```

저장소 인자 없이 등록된 프로젝트를 처리한다. 한 프로젝트의 차단·일시 실패는 다른 프로젝트를 막지 않는다. 안정된 열린 PR이나 계획별 차단이 있어도 독립 READY는 처리할 수 있으며, 의존 작업은 선행 PR Merge를 기다린다. 같은 성공 입력에서 할 일이 없으면 Agent를 반복 호출하지 않는다.

cron 또는 Windows 작업 스케줄러에는 `run --skip-if-running`을 등록한다. 정상적인 중복 실행은 조용히 건너뛴다. 옵션 없이 전역 lock이 충돌하면 시각이 포함된 오류와 종료 코드 1을 반환하며, lock 외 오류는 그대로 보고한다.

bootstrap은 최신 `origin/main`의 정확한 SHA에서 runtime을 실행한다. source checkout의 로컬 main·미커밋 변경·로컬 전용 commit은 실행 revision이 아니다. 기본 프로젝트 checkout은 안전한 경우에만 fast-forward하고 그 밖의 로컬 상태는 보존한다. [실행·동기화 구조](docs/ARCHITECTURE.md#git과-worktree-실행-모델)

## 실행 후 사용자가 할 일

| 상황 | 조치 |
|---|---|
| PR 생성 | 출력·Telegram의 링크로 검토하고 Merge한다. Merge 없이 닫았으면 같은 PR을 다시 연다. |
| 차단 | 안내된 원인·보존 경로를 확인하고 필요한 문서·사용자 결정 또는 작업물을 수정한다. 원격 입력 변경은 commit·push한다. |
| SQLite 없음·손상 | 일반 run은 자동 초기화하지 않는다. 백업을 복원하거나 손상 DB를 별도로 보관한 뒤 명시적으로 config init·재등록한다. |
| cleanup_pending | 미커밋·미게시 작업 또는 불확실한 worktree가 보존돼 있다. 안전 조건 충족·경로 부재 시 자동 해제되며 다음 작업·완료를 막지 않는다. |

정상 실행에서는 코드·테스트·ExecPlan·SQLite 상태를 직접 작성할 필요가 없다. Planner가 계획을 만들고 Worker가 구현·targeted test·verify 스크립트를 준비한다. 전체 verify는 Manager가 실행하고 실패 출력만 Worker에게 전달한다. Reviewer는 읽기 전용으로 검토하고 사용자가 PR을 Merge한다.

Merge 확인 뒤 worktree가 clean하고 PR head에 없는 로컬 commit이 없으면 Merge·Squash·Rebase 방식과 관계없이 정리한다. 안전성을 확인할 수 없는 작업 공간은 보존한다.

## 상세 기준

- 제품 동작·상태·완료 조건: [PRODUCT.md](docs/PRODUCT.md)
- 책임·저장·실행 격리·복구: [ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 공통 ExecPlan 형식: [PLANS.md](docs/PLANS.md)
- 문서 작성·검토: [Specification Skill](.agents/skills/specification-docs/SKILL.md)

PLANS와 템플릿은 복사된 공통 파일을 사용한다. 계획별 phase·branch·worktree·PR·재시도는 Manager가 SQLite에서 관리한다.
