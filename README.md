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

복사 스크립트는 Specification Skill·문서 템플릿과 빈 작업 폴더, PRODUCT·ARCHITECTURE 초안을 준비한다. `.agents`·`docs` 충돌은 중단한다. 자동 Loop에서는 호출하지 않는다.

기존 프로젝트에는 필요한 Skill·문서 파일(`.agents/skills/`, `docs/PLANS.md`, `docs/*.template.md`)만 선택적으로 병합한다.

### 2. 프로젝트 문서 작성·게시

| 문서 | 작성할 내용 |
|---|---|
| `docs/PRODUCT.md` | 현재 제품의 동작·경계·장기 규칙 |
| `docs/requirements/*.md` | 새 기능·정책 변경과 Acceptance Criteria |
| `docs/ARCHITECTURE.md` | 제품 규칙을 보장하는 구조·기술 결정·복구 방식 |

PRODUCT와 활성 Requirement 중 하나 이상은 반드시 있어야 한다. 초기 제품은 PRODUCT로 정의하고 운영 중 변경은 Requirement로 작성한다. Requirement는 frontmatter·상태 없이 `docs/requirements/` 바로 아래에 두며 모두 Planner 입력이 된다.

새 기능·정책 변경을 요청받은 대화형 Agent는 별도 문서 작성 요청을 기다리지 않고 관련 Requirement를 먼저 작성·갱신한다. 기존 활성 Requirement가 요청과 Acceptance Criteria를 충분히 담으면 재사용한다. Specification Skill은 필요한 명세와 누락된 조건을 정리하며 ExecPlan 생성·계획 작성은 매니저가 호출하는 Planner의 자동 흐름에서 담당한다. 작성이 끝나면 commit·push한다. Loop Manager는 최신 원격 base만 입력으로 사용하므로 로컬 전용 파일·commit은 자동 작업에 포함되지 않는다.

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

`config/config.ini`에 모델·Effort, Telegram과 동시 실행 수를 설정한다. `[codex.roles]`와 `[claude.roles]`의 `planner_check = Model alias,Effort`는 읽기 전용 점검 Planner에 독립 적용된다. 선택 provider의 `planner`·`planner_check`와 Worker level 1~8 및 참조 alias를 모두 직접 지정해야 하며 누락·빈 값·미지원 조합은 실행 전에 거부한다. 유지보수 Planner는 기존 일반 Planner 연결을 유지한다. 예시는 운영 설정의 fallback으로 읽지 않으며 Claude 예시에는 Claude Code 2.1.284 이상이 필요하다. `max_workers` 기본값은 3, 전체 verify의 `verify_timeout_seconds`는 600초다. 검증이 정상 진행 중인데 상한이 부족한 경우 `[loop]`에서 조정한다.

Loop Manager 자체 verify는 `[loop] verify_parallelism = auto`가 기본이다. 코드는 허용 CPU·실제 부하·메모리 여유와 아직 관측에 반영되지 않은 예약을 확인해 새 테스트 모듈을 배정한다. 양의 정수는 verify별 상한이며 `1`은 직렬이다. 상한 안에서도 자원 부족 시 배정을 기다리고, 관측 실패 시 직렬로 축소한다. `max_workers`의 프로젝트 실행 수와 별개이며 다른 등록 프로젝트의 verify 스크립트에는 적용하지 않는다.

Manager 실행의 시간 이벤트·worker 원문·manifest·집계는 상태 디렉터리 `logs/verify/<실행 ID>/`에 남는다. 단독 스크립트는 `.cache/verify/<실행 ID>/`를 사용한다. 기존 두 OS 스크립트가 같은 runner로 전체 unittest와 compile을 수행하며 실제 Agent CLI·외부 API 연결은 테스트 프로세스에서 차단된다. 시간 집계만 읽으려면 `python -m loop_manager_internal.verification.runner --report <직렬 기록 디렉터리> <auto 기록 디렉터리>`를 사용한다. 이 명령은 테스트를 실행하지 않는다. 비교 결과의 범위·관측 환경 일치와 원본의 부하·메모리 제한을 확인한 뒤 시간을 해석한다.

실제 config에는 비밀값이 들어갈 수 있으므로 Git에 commit하지 않는다. 상태·PR·session은 source checkout 밖 사용자 상태 디렉터리의 SQLite에 저장한다. 공통·역할별 지침 원본 snapshot은 같은 디렉터리의 `agent-guidance/<내용 SHA-256>.json`에 저장하며 SQLite의 작업·session 결합과 함께 백업한다. 역할별로 합친 실행용 `.md` 파일도 이 디렉터리에서만 생성하며 커밋하지 않는다. 합친 파일은 고정 원본에서 재생성할 수 있지만 누락·손상된 원본 snapshot을 최신 지침으로 바꿔 재개하지 않는다. 기존 작업의 단일 `.md` snapshot은 원래 내용으로 유지한다.

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

init은 한 번 실행하고 프로젝트마다 add한다. provider를 생략하면 codex를 사용한다. 등록 검사는 fetch한 원격 base의 필수 문서·provider를 기준으로 하며 로컬 전용 편집은 반영하지 않는다.

진행 중인 계획·프로젝트 차단·미해결 planning 오류가 없으면 `config remove <path>`로 등록만 해제하며 파일·branch·worktree·PR·MERGED 감사는 보존한다.

Planner는 검증된 계획을 base branch에 fast-forward push하므로 Loop Manager 계정의 직접 push가 허용돼야 한다. 구현은 `loop-manager/<execplan-id>` branch에서 진행하고 PR로 제출한다.

### 5. 실행과 외부 스케줄러 등록

```powershell
& .\.venv\Scripts\python.exe loop_manager.py run
```

```sh
.venv/bin/python loop_manager.py run
```

저장소 인자 없이 등록된 프로젝트를 처리한다. 한 프로젝트의 차단·일시 실패는 다른 프로젝트를 막지 않는다. 안정된 열린 PR이나 계획별 차단이 있어도 독립 READY는 처리할 수 있으며, 의존 작업은 선행 PR Merge를 기다린다. 같은 성공 입력에서 할 일이 없으면 Agent를 반복 호출하지 않는다.

등록 프로젝트의 로컬 설정·인증·도구·권한·timeout을 고친 뒤에는 원격 변경 없이 선택한 원인을 다시 검사할 수 있다.

```powershell
& .\.venv\Scripts\python.exe loop_manager.py recover D:\repo\my-project
& .\.venv\Scripts\python.exe loop_manager.py recover D:\repo\my-project --plan G-001
```

```sh
.venv/bin/python loop_manager.py recover /path/to/my-project
.venv/bin/python loop_manager.py recover /path/to/my-project --plan G-001
```

계획을 지정하면 해당 계획만 검사한다. 로컬 문제를 고친 뒤 재개할 때 사용하며, 원격 코드·문서 변경은 기존 `run`의 자동 복구 경로에서 처리한다. 다른 작업 중에도 요청을 저장할 수 있으며, 실제 복구는 같은 상태 디렉터리의 전역 실행 잠금을 얻은 뒤 진행한다. 즉시 실행할 수 없으면 요청 identity와 대기 안내를 출력하고 다음 `run`에서 이어진다. 같은 범위의 반복 요청은 미완료 요청을 재사용하고 다른 범위의 미완료 요청이 있으면 거부한다. 잘못된 대상·미해결 원인·실행 자리 대기는 0이 아닌 종료 코드로 보고한다.

cron 또는 Windows 작업 스케줄러에는 `run --skip-if-running`을 등록한다. 정상적인 중복 실행은 조용히 건너뛴다. 옵션 없이 전역 lock이 충돌하면 시각이 포함된 오류와 종료 코드 1을 반환하며, lock 외 오류는 그대로 보고한다.

bootstrap은 최신 `origin/main`의 정확한 SHA에서 runtime을 실행한다. source checkout의 로컬 main·미커밋 변경·로컬 전용 commit은 실행 revision이 아니다. 기본 프로젝트 checkout은 안전한 경우에만 fast-forward하고 그 밖의 로컬 상태는 보존한다. [실행·동기화 구조](docs/ARCHITECTURE.md#git과-worktree-실행-모델)

자동 실행 지침은 선택 runtime의 `docs/AGENTS.common.md`와 역할별 파일을 합쳐 Codex의 추가 developer 지침 또는 Claude의 append system prompt 파일로 전달한다. 공통에는 모든 역할의 작업·승인·품질 기준을, 역할별 파일에는 권한·금지사항과 절차를 기록한다.

| 호출 역할 | 공통 지침에 합칠 파일 |
|---|---|
| Planner·유지보수 Planner | `docs/AGENTS.planner.md` |
| 읽기 전용 점검 Planner | `docs/AGENTS.planner_check.md` |
| Worker | `docs/AGENTS.worker.md` |
| Reviewer | `docs/AGENTS.reviewer.md` |

작업 시작 시 모든 지침 원본을 함께 고정한다. 같은 ExecPlan의 Worker·Reviewer는 같은 원본 버전에서 각 역할의 내용만 합치며 재시도·새 세션·재개도 이 버전을 유지한다. 새 작업만 최신 원본을 선택하고 역할별 내용 변경도 planning·검증 결과 재사용의 입력에 반영한다. 기본 지침·기존 추가 지침·프로젝트 규칙과 격리 검증 gate는 보존하며 파일 오류·지원되지 않는 CLI·설정 충돌·알 수 없는 역할은 모델 실행 전에 중단한다.

작업 worktree의 공통·역할별 `docs/AGENTS.*.md`는 일반 파일 권한으로 수정하고 PR에 최종 경로·diff·이유·영향을 보고한다. 후보 파일은 현재 실행 지침을 바꾸지 않는다. 이 저장소 루트 AGENTS에는 Loop Manager 개발 지침을 유지한다.

## 수동 구현과 제출

기존 G를 다른 기기에서 구현하려면 등록된 저장소와 계획 ID를 지정한다.

```sh
python loop_manager.py manual start /path/to/project G-012
```

선행 작업의 실제 원격 base Merge를 확인한 뒤 Manager 소유 branch를 생성·push한다. 안내된 branch를 다른 기기에서 checkout해 코드·테스트와 필요한 R 변경을 함께 push한다. 수동 작업 대기는 실행 자리를 점유하지 않으며 독립 G는 계속 처리한다.

```sh
python loop_manager.py manual complete /path/to/project G-012
```

G 없이 R부터 구현까지 올린 경우에는 원격 source branch와 R 경로를 제출한다. `--requirement`는 반복할 수 있다.

```sh
python loop_manager.py manual submit /path/to/project \
  --branch feature/manual --requirement 'docs/requirements/[R-001] Feature.md'
```

Manager가 완성된 G를 생성하고 별도 소유 branch에 source 이력과 함께 보존한다. 복수 G는 같은 최종 후보·PR을 공유하며 내부 선행 Merge가 필요한 묶음은 분리 제출해야 한다. 이후 수정은 안내된 소유 branch로 push한다. 최초 source의 새 변경은 같은 source·R 범위로 submit을 다시 실행해야 반영된다.

complete·submit은 기본적으로 전체 verify와 읽기 전용 Review를 수행하고 실패하면 자동 교정·재검증을 이어간다. 외부 검증을 생략 근거로 쓰려면 `--skip-verify`와 `--skip-review`를 각각 지정하고, 원격 commit의 전체 SHA인 `--expected-head`와 `--skip-reason`을 함께 제공한다. 생략은 `SKIPPED_BY_USER`로 기록하며 실제 PASS와 구분한다. SHA·base·G/R·환경 또는 관련 단계 입력이 달라지면 기본 검증을 다시 실행한다. submit에서 생성되는 G commit도 SHA를 바꾸므로 source SHA의 생략 승인을 무효화한다.

종료 코드 0은 수동 준비 또는 PR 연결 완료를 뜻한다. 수락 후 대기·자동 복구 중이면 1이며 저장된 요청은 다음 run에서 이어진다. 인자 오류는 2다. PR 생성이나 생략은 완료 근거가 아니며 저장된 동일 branch·PR의 실제 Merge를 확인해야 구성 G 전체가 MERGED가 된다.

## 실행 후 사용자가 할 일

| 상황 | 조치 |
|---|---|
| PR 생성 | 출력·Telegram의 링크로 검토하고 Merge한다. Merge 없이 닫았으면 같은 PR을 다시 연다. |
| 차단 | 안내된 원인·보존 경로를 확인하고 필요한 문서·사용자 결정 또는 작업물을 수정한다. 원격 입력 변경은 commit·push한다. |
| SQLite 없음·손상 | 일반 run은 자동 초기화하지 않는다. 백업을 복원하거나 손상 DB를 별도로 보관한 뒤 명시적으로 config init·재등록한다. |
| cleanup_pending | 미커밋·미게시 작업 또는 불확실한 worktree가 보존돼 있다. 안전 조건 충족·경로 부재 시 자동 해제되며 다음 작업·완료를 막지 않는다. |

정상 실행에서는 코드·테스트·ExecPlan·SQLite 상태를 직접 작성할 필요가 없다. Planner가 계획을 만들고 Worker가 구현·targeted test·verify 스크립트를 준비한다. 전체 verify는 Manager가 실행하고 실패 출력만 Worker에게 전달한다. Reviewer는 읽기 전용으로 검토하고 사용자가 PR을 Merge한다.

직접 작업과 대화형 Agent의 로컬 구현은 관련 Requirement 작성·갱신 또는 충분한 기존 활성 Requirement의 재사용으로 시작하고 ExecPlan을 만들지 않는다. 자동 Worker 역할·SQLite 전이·자동 완료 절차도 요구하지 않는다. 직접 구현과 활성 Requirement를 원격 base에 게시하면 매니저의 Planner는 실제 미충족 작업만 재점검하며 소급·중복 계획이나 자동 검증 성공 기록을 만들지 않는다.

Merge 확인 뒤 worktree가 clean하고 PR head에 없는 로컬 commit이 없으면 Merge·Squash·Rebase 방식과 관계없이 정리한다. 안전성을 확인할 수 없는 작업 공간은 보존한다.

## 상세 기준

- 제품 동작·상태·완료 조건: [PRODUCT.md](docs/PRODUCT.md)
- 책임·저장·실행 격리·복구: [ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 공통 ExecPlan 형식: [PLANS.md](docs/PLANS.md)
- 문서 작성·검토: [Specification Skill](.agents/skills/specification-docs/SKILL.md)

PLANS와 템플릿은 복사된 공통 파일을 사용한다. 계획별 phase·branch·worktree·PR·재시도는 Manager가 SQLite에서 관리한다.
