# Loop Manager

Requirement에서 계획 PR을 제안하고, base에 확정된 ExecPlan을 Worker → 필수 Verify → Reviewer → 구현 PR로 처리한다. 최종 Merge와 중요한 제품 결정은 사용자가 맡는다. 외부 스케줄러에서 실행하며 SQLite·원격 Git·GitHub로 중단된 작업을 재관측한다.

전체 AI 한도는 `max_workers`, 프로젝트별 Worker는 하나, 전체 Verify는 하나다. 독립 G의 서로 다른 단계는 겹쳐 실행한다. 자동 유지보수 작업은 생성하지 않는다.

## 설치

Python, Git, 인증된 GitHub CLI(`gh`), Codex 또는 Claude Code CLI가 필요하다. Windows는 Python 3.13 이상과 PowerShell 7, 이 저장소 자체 전체 검증에는 WSL이 필요하다. Python 외부 패키지는 사용하지 않는다.

```sh
bash scripts/setup.sh
cp config/config.ini.example config/config.ini
```

```powershell
pwsh -NoProfile -File scripts/setup.ps1
Copy-Item config/config.ini.example config/config.ini
```

선택 Provider의 모델·effort와 선택 사항인 Telegram을 설정한다. 실제 config는 커밋하지 않는다. Linux setup은 실행 PATH를 저장하므로 Git·gh·Agent CLI가 보이는 같은 계정의 터미널에서 실행한다.

## 프로젝트 준비

빈 문서 구조는 `bash scripts/copy-project-files.sh /path/to/project` 또는 `pwsh -File scripts/copy-project-files.ps1 -TargetPath D:\repo\project`로 복사한다. 기존 `.agents`나 `docs`가 있으면 중단하므로 기존 프로젝트에는 필요한 파일을 직접 병합한다.

`docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, 활성 `docs/requirements/*.md`를 작성해 원격 base에 commit·push한다. Planner의 `docs/plans/*.md`는 계획 PR을 검토·Merge한 뒤 실행한다. 사용자가 base에 직접 반영한 유효한 G도 실행하며 `draft: true`인 G는 제외한다. 필수 검증 명령은 프로젝트가 제공한다. [Verify 계약](docs/VERIFY.md)

## 명령

아래 `python`은 `.venv`의 Python이다. Windows는 `.venv\Scripts\python.exe`, POSIX는 `.venv/bin/python`을 사용한다.

```sh
python loop_manager.py init
python loop_manager.py add /path/to/project --base develop --provider codex
python loop_manager.py list
python loop_manager.py status
python loop_manager.py run --skip-if-running
```

base 생략 시 등록 checkout의 `origin/HEAD`를 사용한다. 프로젝트 인자는 등록 경로나 `owner/repository`다. `--state-dir`, `--config`는 명령 앞에 둔다.

| 목적 | 명령 |
|---|---|
| 등록 해제 요청 | `remove <project>` |
| 전체 또는 프로젝트 신규 배정 중지 | `pause [project]` |
| 배정 재개 | `resume [project]` |
| 읽기 전용 조회 | `list`, `status [project]`, `logs [project]`, `usage [project]` |
| 차단 원인 실제 재평가 요청 | `unblock <project> G-001` |
| G별 수동 Git 준비 | `manual start <project> G-001` |
| 게시한 수동 구현 제출 | `manual complete <project> G-001` |
| 선택 프로젝트 실행 | `run <project> [project ...]` |

`manual start`가 반환한 경로에서 구현·commit·push한 뒤 `manual complete`를 접수하면 같은 G의 Verify·Reviewer·PR을 진행한다. 검증 생략 옵션은 없다. 접수만 반환한 명령은 상태 조회와 Telegram에서 최종 결과를 확인한다.

## 운영 경계

실행 코드는 자신의 등록 원격 base에서 선택한 정확한 SHA다. 자신의 등록이 없으면 origin 기본 브랜치를 사용한다. 사용자 checkout이나 G 작업 공간을 실행 코드 원본으로 사용하지 않는다. 최초 전환은 새 runtime을 해당 원격 base에 반영한 뒤 수행한다.

신규 DB 전용이며 이전 DB를 자동 마이그레이션하지 않는다. 기존 상태는 별도 보관하고 새 상태 경로에서 `init`·`add`한다. 새 환경 인수 전에는 이전 Writer를 종료하고 작업물을 원격에 게시한다. 과거 세션·토큰·검증 성공은 합성하지 않는다.

PR 생성은 완료가 아니다. 실제 Merge 후 G를 보관하고 R의 최신 Acceptance Criteria를 최종 재평가한다. 정리할 수 없는 사용자 변경은 보존하며 다른 작업은 계속한다. Force push·Rebase·Reset이나 원격 작업 브랜치 자동 삭제는 하지 않는다.

- [운영 안내](docs/OPERATIONS.md): 상태·설정·수동 작업·복구·runtime
- [검증](docs/VERIFY.md): 프로젝트 명령과 이 저장소의 WSL/Windows 검증
- [PRODUCT](docs/PRODUCT.md), [ARCHITECTURE](docs/ARCHITECTURE.md): 정책·설계 계약
- [PLANS](docs/PLANS.md), [Specification Skill](.agents/skills/specification-docs/SKILL.md): 문서 작성

Worker 품질 지침에는 [Ponytail](https://github.com/DietrichGebert/ponytail)을 매 호출 한 번 적용한다.
