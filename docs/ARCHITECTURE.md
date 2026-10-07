# Architecture

## Overview

Loop Manager는 Windows와 Linux에서 동작하는 Python 3 프로그램이다. 외부 스케줄러가 프로그램을 반복 실행하며, 프로젝트 문서와 GitHub 상태를 확인하고 AI Agent CLI를 호출해 계획, 구현, 검증과 PR 생성을 진행한다.

하나의 프로젝트에는 여러 READY ExecPlan을 등록할 수 있지만 한 번에 하나만 처리한다. 서로 다른 프로젝트는 `max_workers` 범위에서 병렬로 처리한다.

## 문서 경계와 책임

| 기준 | 담당 내용 |
|---|---|
| [README.md](../README.md) | 설치·설정·실행·사용자 조치 |
| [PRODUCT.md](PRODUCT.md)와 활성 Requirement | 현재 제품 동작·장기 규칙과 요청된 변화 |
| 이 문서 | 책임 경계·정보 원본·저장·실행 격리·검증·복구 방식 |
| [PLANS.md](PLANS.md) | 공통 ExecPlan 형식·메타데이터 |
| [Specification Skill](../.agents/skills/specification-docs/SKILL.md) | 제품 문서 작성·검토 기준 |

상태의 의미와 허용 정책은 PRODUCT를 기준으로 하며 여기서는 이를 적용·보존하는 방법을 설명한다. 활성 Requirement의 미구현 설계는 구현 예정임을 구분한다.

현행 구조와 활성 변경 요청의 적용 범위는 [PRODUCT의 변경 요구사항 절](PRODUCT.md#활성-변경-요구사항과-현재-동작)에서 연결한다. 아래의 로컬 fast-forward 제한, PR 없는 checkpoint, Planner의 FUTURE_IDEAS 작성과 Worker 문서 제한은 현행 설명이며, 각각 R-056·R-060·R-061의 구현 목표를 거부하는 제약으로 사용하지 않는다. 구현 전 Agent의 실제 권한과 실행 gate를 문서만으로 변경하지 않는다.

[R-063](requirements/%5BR-063%5D%20Manager-Owned%20Deterministic%20Work%20and%20Minimal%20Model%20Use.md)은 규칙 기반 편집·결과 형식 처리의 Manager 이관과 모델 호출 축소를 요청한다. 아래의 후보 revision 변경 후 Worker·verify·Review 재수행은 현행 설명이며, R-063 구현에서는 역할별 입력 동일성이 증명된 성공 결과의 재사용 범위에 한해 변경한다. 실제 변경과 비교 근거가 없는 경우의 재검증·최신 원격 게시 gate는 유지한다.

## 실행 환경과 시작

- Codex 또는 Claude Code CLI 중 최소 하나가 설치되고 인증돼있어야 한다
- Git과 gh(GitHub CLI)가 설치되고 대상 저장소에 접근할 수 있어야 한다
- Python 3 venv를 만들 수 있어야 한다
- Windows와 Linux용 실행 진입점을 제공한다
- 같은 사용자 상태 디렉터리의 자동 실행과 실제 복구 실행은 `<state-dir>/loop-manager.lock`으로 직렬화한다. 수동 준비와 복구 요청 접수는 별도 command runtime과 자원 잠금을 사용한다
- `run.sh`와 `run.ps1`은 Git을 실행하지 않고 source checkout의 Python 부트스트랩만 시작한다
- 부트스트랩은 다른 Git 작업 전에 부모 process lock을 획득하고, Loop Manager 저장소의 `origin/main`을 fetch해 exact SHA의 detached runtime worktree를 선택한 뒤 같은 lock을 유지한 채 runtime 자식을 실행한다. `manual`과 `recover`는 command별 부모 lock과 고유 runtime 경로로 자동 실행과 분리한다
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
- `docs/PLANS.md`, `docs/ARCHITECTURE.md` 존재 여부를 확인한다
- `docs/PRODUCT.md` 또는 `docs/requirements/` 바로 아래 Requirement가 하나 이상 있어야 한다
- `docs/PLANS.md`의 SHA-256 해시가 Loop Manager 기준 파일과 다르면 등록을 거부한다
- 변경되지 않은 PRODUCT 또는 ARCHITECTURE 템플릿만 있으면 작업 가능 상태로 등록하지 않는다
- `docs/PLANS.md` 검사는 등록할 때만 수행하며 자동 Loop에서는 다시 검사하지 않는다
- 등록 해제의 비종료 계획·프로젝트 차단·planning 오류 gate는 PRODUCT를 따른다. 해제는 registry만 변경한다
- 등록 해제는 프로젝트 파일, branch, worktree와 PR을 삭제하지 않는다

등록 조건과 runtime 실행 지침 원본 검사는 AI가 아니라 프로그램 코드가 수행한다.

## 문서 입력과 ExecPlan 파싱

프로젝트 문서·계획의 자동 입력은 fetch로 확인한 `origin/<base-branch>`의 특정 commit에서 읽는다. 등록 프로젝트의 기본 checkout에만 있는 파일·수정·commit은 사용자가 push하기 전에는 입력에 포함하지 않는다. 진행 작업은 이 원격 기준에서 만든 관리 worktree의 결과를 보존하며, 선택된 원격 작업 branch의 사용자 수정은 검증 후 반영한다.

ExecPlan의 `Progress` 체크 항목과 본문은 구현 내용·판단·검증 기록이다. 체크가 끝났다는 이유로 `READY`, `PLAN_BLOCKED`, `MERGED` 같은 실행 상태를 바꾸지 않으며, 단계·재시도·session은 SQLite, 실제 PR Merge는 GitHub 확인을 기준으로 한다. 본문 변경은 재평가 입력이고 명시적 `User Decision`은 승인 판정 입력이므로, 본문을 읽지 않거나 변경을 무시한다는 뜻은 아니다.

활성 Requirement 경로는 안정적인 식별자이고 `docs/requirements/` 바로 아래 문서만 새 계획 입력이다. archive는 기존 ExecPlan source의 조회·검증 호환성에만 사용하며 재평가하거나 활성 경로로 자동 복원하지 않는다. FUTURE_IDEAS는 계획 입력·실행 상태가 아니다.

Requirement 최종 판단은 source로 연결된 비종료 계획·draft와 수동 제출이 없는 시점에 일반 Planner가 담당한다. 관련 계획이 있으면 모두 실제 MERGED여야 하며 연결 계획 없는 직접 구현도 코드·검증·관련 제품·설계 문서로 판단한다. Manager는 유효한 기준별 충족 판단과 입력 지문·source 관계를 확인한 원문만 내용 보존 이동으로 게시한다. 미충족이면 후속 ExecPlan을 연결하며, 원격 확인 전 입력 변경에 따른 이동 철회와 원격 확인 후 archive를 구분한다.

과거 Requirement 감사 row와 저장된 복원 manifest 필드는 읽기 호환성과 이력 보존을 위해 유지한다. 새 감사·복원 판단과 완료 대기를 만들지 않으며 복원 manifest는 새 게시 전 거부한다. 미게시 Manager 복원 영수증은 해당 worktree에서 원문·원격 archive·경로 소유 범위를 확인해 archive로 철회하고 활성 입력을 재평가한다. index reset 중 중단도 같은 영수증으로 재개하고 사용자 수정·충돌·게시된 후보는 덮어쓰지 않는다.

ExecPlan은 [PLANS.md](PLANS.md)의 고정 key/value와 `sources`·`depends_on` 목록만 파싱하며 전체 YAML을 지원하지 않는다. 필수 메타데이터, source, ID 고유성 및 dependency graph를 검증한다. 새 계획에는 priority가 필수이고 도입 전 계획의 생략 값은 normal로 해석한다. priority 형식 변경은 공통 PLANS와 파서를 같은 변경에서 갱신한다.

## Source of Truth

- PRODUCT, Requirement, ARCHITECTURE와 ExecPlan의 게시된 내용: 검증한 원격 commit의 Git 파일. 진행 중인 미게시 변경은 해당 관리 worktree에 보존한다.
- ExecPlan 실행 상태, 의존성 충족 여부, 재시도 횟수, 세션, worktree, branch와 PR 연결: SQLite
- PR Open/Closed/Merged 상태: GitHub
- Model, Effort, 동시 실행 수와 Telegram 설정: `config/config.ini`

SQLite는 `<state-dir>/loop-manager.db`에 저장하며 상태 변경은 transaction으로 기록한다. 문서 본문이나 ExecPlan 본문은 SQLite에 복제하지 않고 경로와 내용 해시만 저장한다.

SQLite에는 등록 프로젝트, 기준 branch, 선택한 provider, 과거 등록·승인 감사, 마지막 성공 Planner의 명세·전체 계획 입력 지문과 `planning_error`, 복구 가능한 planning publication intent, ExecPlan 메타데이터와 의존성, 최초 실행 여부, 실행 상태와 재시도 횟수, AI Agent 세션, worktree, branch, PR, 삭제된 미착수 ExecPlan의 ID 예약·감사 기록, `cleanup_pending`, 알림과 프로젝트 완료 주기를 저장한다.

실행 시작 시 SQLite, Git worktree/branch와 GitHub PR 상태를 대조한다. 서로 다르면 GitHub Merge 상태, 프로젝트 파일, SQLite 실행 정보 순으로 각 소유 영역의 원본을 적용한다.

### 원격 catalog와 실행 이력의 분리

최신으로 검증한 `origin/<base-branch>`의 active 및 archived ExecPlan 파일은 ID를 제외한 현재 계획 선언의 원본이다. SQLite는 최초 게시 path·hash·SHA를 불변 감사 정보로 보존하고 현재 path·title·sources·depends_on·complexity·risk·priority·hash·revision을 별도 cache로 동기화한다

유효한 ExecPlan 선언 변경은 계획 또는 프로젝트 차단 사유가 아니며, catalog 전체가 유효하고 원격 revision과 SQLite 상태·소유권 snapshot이 그대로일 때만 모든 현재 cache delta를 한 transaction으로 반영한다

ExecPlan 번호는 연속일 필요가 없다. 번호 공백을 누락 계획으로 추정하거나 메우지 않으며 사용한 ID는 재사용하지 않는다. 현행 catalog 동기화는 SQLite 실행·취소 이력에 대응하는 원격 파일이 없거나 형식·source·ID·dependency graph가 유효하지 않으면, 다른 계획 상태를 바꾸지 않고 원인 파일을 포함한 프로젝트 범위 진단을 남긴다.

[R-055 구현 예정](requirements/%5BR-055%5D%20Blocking%20Path%20Audit%20and%20Minimal%20Interruption.md): 사용자 필요에 따른 파일 삭제는 정상적인 선언 변경으로 반영한다. 원격 변경·실행 이력을 대조해 삭제 목적과 영향받는 계획·dependency를 재평가하고, 허용된 계획 개정·관계 정리를 통한 자동 복구를 차단보다 먼저 시도한다. 이력을 삭제하거나 삭제된 계획을 Merge 완료로 간주하지 않으며, 해결하지 못한 영향 범위만 보존·중단하고 검증된 독립 계획은 계속한다.

일반·유지보수 planning의 미해결 오류와 입력 지문은 다른 ExecPlan의 차단 해제·PR 상태 변경·project 실행 상태 정리와 독립적으로 보존한다. worktree 경로 부재나 관련 입력이 같은 원격 base 전진만으로 해제하지 않으며, 같은 입력의 반복 실행·재시작에서는 Planner를 다시 호출하지 않는다

관련 planning 입력이 바뀌면 저장된 소유권·workspace identity를 확인하고 관리 worktree에 최신 원격 base를 반영한다. 오류는 입력 전진만으로 해제하지 않고 보존된 결과의 교정·재검증 또는 게시 복구로 원인이 실제 해소된 뒤에만 해제한다. 소유 증거 없는 경로와 복구 실패는 Git 등록·HEAD·index·파일 및 진단·session·작업물을 보존한다

ExecPlan ID만 Git 선언과 SQLite 실행 이력을 잇는 안정적인 identity다. 같은 ID의 유효한 원격 파일에서 path·title·sources·depends_on·complexity·risk·priority 또는 본문이 바뀌면 현재 cache를 갱신하고 상태·실패 이력·Agent session·branch·worktree·workspace·PR 연결은 보존한다

현재 path·title은 compact catalog·로그·조회에 사용한다. 기존 PR의 ExecPlan 링크는 최초 게시 SHA·active path를 유지한다. cache 갱신은 실행 상태·session·branch·worktree·workspace·PR과 최초 게시 감사를 교체하지 않는다.

## 계획 변경 감지

프로젝트별 명세 지문은 최신 `origin/<base-branch>`의 다음 사용자 소유 제품 문서에서 계산한다.

- `docs/PRODUCT.md`
- `docs/requirements/` 바로 아래의 `*.md`
- `docs/ARCHITECTURE.md`

프로젝트별 전체 계획 입력 지문은 같은 원격 revision의 다음 내용에서 계산한다.

- `docs/PRODUCT.md`
- `docs/requirements/` 바로 아래의 `*.md`
- `docs/ARCHITECTURE.md`
- archive, `docs/plans/`와 `docs/FUTURE_IDEAS.md`를 제외한 소스, 테스트와 설정 파일

성공한 일반 Planner 주기는 새 계획 게시, 미착수 계획 개정·삭제 게시, FUTURE_IDEAS-only 게시와 추가 계획 없음 판단을 포함하며, 완료할 때 관측한 명세 지문과 전체 계획 입력 지문을 함께 저장한다. 자동 Requirement archive의 내용 보존 이동만으로는 두 기준을 무효화하거나 새 작업 주기와 프로젝트 완료 알림을 활성화하지 않는다.

활성 변경 단계와 maintenance publication이 없는 안전한 선택 경계에서 명세 또는 전체 계획 입력 지문이 마지막 성공 기준과 다르면, PRODUCT의 유지보수·역할 선택 순서를 적용해 READY보다 먼저 필요한 Planner를 한 번 실행한다. 입력이 같으면 활성 유지보수 prerequisite·리팩터링 DAG의 선택을 먼저 적용하고 일반 READY는 priority와 숫자 ID 순으로 시작한다. 성공한 두 지문이 최신 입력과 같으면 빈 catalog 또는 dependency 대기 상태에서도 Planner를 반복 실행하지 않는다.

[R-054 구현 예정](requirements/%5BR-054%5D%20Due%20Refactoring%20Priority.md): Merge 대기 PR이 없는 due 유지보수는 평가가 아직 계획을 게시하지 않았어도 일반 Planner·빠른 점검·일반 READY보다 먼저 수행한다. 진행 작업은 선점하지 않으며 평가와 필요한 리팩터링 cycle이 끝난 뒤 일반 작업 선택으로 돌아간다.

## Git과 worktree 실행 모델

자동 입력과 시작·rebase 기준은 fetch된 `origin/<base-branch>` SHA다. 로컬 base가 미게시 commit으로 앞서 있어도 자동 작업은 원격 SHA에서 시작하며 로컬 commit을 자동 push하거나 입력으로 채택하지 않는다. 기본 worktree는 사용자 checkout으로 보존한다.

작업·동기화·복구가 관여하는 branch는 등록한 base와 소유권을 확인한 Loop Manager 생성 branch다. 그 밖의 사용자 branch는 생성·수정·삭제를 되돌리거나 자동 복구 대상으로 삼지 않으며, 그 변화만으로 계획·프로젝트를 차단하지 않는다. 이름이 비슷하다는 이유로 관리 branch로 간주하지 않는다. 실제 사용 중인 origin·관리 branch·worktree 등 공유 자원의 안전 검사는 유지한다.

### 기본 worktree 동기화

| 상태 | 처리 |
|---|---|
| 저장 base branch·Git operation 없음·HEAD가 원격과 equal | dirty 상태도 변경하지 않고 no-op |
| behind-only이며 Git operation·비정상 index·경로 겹침 없음 | 로컬 변경과 index를 보존하는 safe fast-forward |
| ahead-only이며 로컬에 미게시 commit이 있음 | 로컬 commit·checkout을 보존하고 동기화만 건너뜀. 자동 작업은 원격 SHA에서 계속 |
| 다른 branch·detached·diverged·operation·비정상 index·겹침·동시 변경 | 이유를 기록하고 동기화만 건너뜀 |

경로 겹침은 원격이 변경할 tracked 경로와 staged·unstaged·untracked·ignored 로컬 경로를 대소문자 및 부모·자식 관계까지 비교한다. eligibility 뒤 사용자 변경·Git lock·실패가 있으면 reset·clean·stash·강제 checkout이나 잠금 제거로 우회하지 않는다. safe fast-forward는 이미 fetch한 원격 ref에 대한 `git merge --ff-only` 상당의 적용이며 중복 fetch를 하지 않는다.

동기화 수행·skip·실패와 관계없이 자동 작업은 원격 SHA에서 계속하고 PR 관측·알림도 유지한다. 로컬 전용 문서·commit은 사용자가 push한 뒤 입력이 된다. 기본 worktree의 사용자 변경은 별도 관리 worktree와 소유 branch가 유지되는 한 Agent 위반으로 귀속하지 않는다.

### 자체 runtime 선택과 handoff

자동 명령의 실행 revision은 다음 순서로 고정한다. `manual`과 `recover`의 병행 command runtime도 같은 원격 SHA·소유권 검증을 적용하며, 전역 잠금 대신 고유 부모 잠금과 `commands/<mutation-id>/<sha>/` 경로를 사용한다.

1. source checkout의 좁은 Python 부트스트랩이 `<state-dir>/loop-manager.lock`을 획득한다
2. lock을 보유한 부모만 source checkout을 anchor로 `git fetch origin`을 실행한다
3. fetch된 `origin/main`을 immutable commit SHA로 resolve한다
4. `<state-dir>/runtimes/<repository-id>/<sha>/`의 linked worktree가 같은 repository, clean detached HEAD와 exact SHA인지 검증하고, 없을 때만 `git worktree add --detach`로 만든다
5. 부모가 source checkout 기준의 절대 config 경로와 내부 runtime 표식을 전달하고, 현재 Python interpreter로 runtime worktree의 `loop_manager.py`를 자식 실행해 종료 코드를 전달한다
6. 부모는 자식 종료 뒤 lock을 해제한다. bootstrap은 실행 중인 runtime worktree를 이동하거나 사용자 `main`을 갱신하지 않는다. 다만 runtime 자식이 Loop Manager 저장소 자체를 등록 프로젝트로 처리하면 아래의 일반 safe fast-forward 조건을 적용할 수 있다

runtime 경로가 존재하지만 소유권, repository, detached HEAD, SHA 또는 clean 상태를 증명할 수 없으면 reset, clean, 제거 또는 덮어쓰기 없이 중단한다. 별도 clone은 만들지 않으며 SHA별 runtime 정리는 활성 실행과 분리된 검증된 관리 작업으로만 수행한다.

runtime 자식은 병렬 프로젝트 작업을 제출하기 전에 등록된 self source checkout의 대기 PR을 동기화한다. PR의 실제 base branch가 등록 base와 같음을 확인한 새 self PR Merge만 handoff 근거로 삼는다. Merge 전이·cleanup·알림 outbox를 영속화하고 등록 base의 최신 fetch와 source checkout 안전 동기화를 평가한다. 원격 SHA가 현재 runtime SHA와 같으면 계속 실행하고 fast-forward 후속이면 source 동기화 결과와 관계없이 정상 종료해 다음 외부 실행의 bootstrap에 제어를 넘긴다. handoff 판정 뒤에는 새 Agent·verify·publication 또는 다른 프로젝트 작업을 시작하지 않는다. self Merge가 없거나 다른 프로젝트의 Merge 또는 일반 push만 있으면 handoff하지 않는다. repository·origin·PR base identity, fetch 또는 ancestry를 확인할 수 없으면 성공 handoff로 처리하지 않고 기존 작업 상태를 보존한다

### 선택 전 상태 동기화

1. 프로그램이 `PROJECT_BLOCKED`의 저장소 안전을 확인하고 저장된 모든 Merge 대기 PR의 GitHub identity와 Open·Merged·Closed 상태를 조회한다. 안전 gate가 허용하는 Merge·Close 전이와 worktree cleanup 판정 및 알림 outbox를 기본 worktree 동기화보다 먼저 SQLite에 기록한다. 프로젝트 안전이 증명되지 않으면 기본 worktree 동기화와 일반 저장소 변경 단계를 실행하지 않는다. 계획과 연결된 차단의 검증 가능한 대상 작업 복구·보존은 아래의 계획별 실행 자리와 현재 상태 검증 경계에서만 수행한다. 단, 원격 ExecPlan catalog 오류 차단은 원인 push를 fetch하고 재검증해야 하므로 이 저장소 안전 gate와 별도로 처리한다
2. 프로젝트 안전 gate가 열려 있으면 새 Merge 뒤 `origin/<base-branch>`를 다시 fetch하고 최신 ExecPlan·project 상태를 읽는다. 가능한 ExecPlan archive 게시를 먼저 완료한 뒤 최종 원격 SHA를 기준으로 기본 worktree의 safe fast-forward eligibility와 catalog를 평가한다. 저장된 `PR_OPEN` mirror, 다른 안정된 Open PR 또는 충돌 marker만으로 평가를 생략하지 않는다. 그 뒤 각 `PLAN_BLOCKED`의 계획별 해제 조건 및 `planning_error`의 근거 입력 변경 여부를 독립적으로 확인한다

기존 Open PR도 [GitHub와 PR](#github와-pr)의 Merge 가능 여부를 확인한다. 충돌이 확정된 PR은 안전·dependency·실행 자리 조건을 확인한 뒤 관리 branch를 최신 원격 base에 rebase해 같은 PR을 갱신한다.

그 뒤 실행 중인 변경을 재개하거나 확정 PR 충돌 복구·유지보수·일반 Planner·READY를 선택한다. dependency와 순서는 [PRODUCT의 작업 선택 정책](PRODUCT.md#작업-선택과-동시-실행)을 따른다. Worker·검증·Review·PR 생성 순서는 [제품 처리 흐름](PRODUCT.md#execplan-생성과-자동-처리)을 따른다.

### 계획 게시와 작업 공간

계획을 실행 이력에 적용하기 전에 원격 게시와 catalog 전체의 유효성을 확인한다. 계획·FUTURE_IDEAS 변경이 없으면 관측한 두 입력 지문을 저장하고 planning worktree를 정리해 완료 여부를 확인한다.

1. 생성·개정·rename·삭제·FUTURE_IDEAS 변경을 분류하고 snapshot 원본, SQLite의 현재 무소유 상태 및 적용 후 catalog의 ID·source·dependency graph를 검증한다.
2. 실패는 같은 Planner 세션에서 최대 3회 교정한다. 소진하면 원인·입력 지문을 planning_error에 보존하고 같은 실패 입력의 신규 Planner 호출을 반복하지 않는다. 현행 실행 경로는 이 오류에서 프로젝트 처리를 중단한다. [R-055](requirements/%5BR-055%5D%20Blocking%20Path%20Audit%20and%20Minimal%20Interruption.md)는 안전하게 실행 자리를 비운 뒤 이미 게시된 독립 계획의 순차 구현·PR 관측을 계속하도록 변경한다.
3. 검증된 전체 변경을 planning worktree의 한 commit으로 만든다.
4. 아직 게시하지 않은 동안 원격 base가 전진하면 변경·session을 보존해 반영하고 최신 catalog·대상 권한과 관련 문서를 재검증한다. 충돌·원격 재작성·소유권 불명확은 작업을 보존해 복구 경계로 보낸다.
5. 기준과 mutable READY snapshot이 그대로면 복구 가능한 publication intent를 저장하고 base에 fast-forward push한다.
6. 원격 commit을 확인한 뒤 생성 row 등록·개정 cache 갱신·삭제 row 제거와 ID 취소 감사·두 성공 지문·planning 정리를 한 SQLite transaction으로 반영한다. FUTURE_IDEAS-only 변경은 실행 상태를 만들지 않는다.

planning worktree는 `<state-dir>/worktrees/<project-id>/planning/<input-fingerprint>/`, 구현 worktree는 `<state-dir>/worktrees/<project-id>/<execplan-id>/`에 둔다. 구현 branch 이름은 `loop-manager/<execplan-id>`다. 계획은 Worker 시작 전에 base에 게시하며 구현 PR에서 처음 생성하지 않는다.

미착수 snapshot은 READY·미실행 marker와 branch·worktree·reservation·Agent·검증·retry·PR 정보 부재를 확인해 생성한다. 계획 삭제는 활성 row 제거와 ID 취소 감사를 같은 transaction에서 기록하며 ID를 재사용하지 않는다. 최초 activation marker는 이후 READY로 돌아와도 유지한다.

사용자가 BLOCKED의 근거 PRODUCT/Requirement를 기본 branch에서 수정한 경우 변경을 commit하고 push해야 한다. Loop Manager는 변경된 `origin/<base-branch>`를 관리 branch에 rebase한 뒤 재개한다. rebase는 관리 worktree에서만 수행하며, 등록 프로젝트 동기화가 별도로 safe fast-forward 조건을 만족하지 않는 한 기본 worktree의 파일, index와 HEAD는 바꾸지 않는다. 사용자는 안내된 관리 worktree의 ExecPlan을 직접 수정할 수도 있다.

원격 관측은 Agent·verify가 worktree를 사용하는 동안 파일을 바꾸지 않고 다음 안전한 경계에서 적용한다. planning rebase는 작업물과 stash를 보존한 채 Git 상태를 복구하고 새 base를 SQLite에 기록한 뒤 보존 stash를 정리한다. 관련 문서 판별은 Planner의 현재 계획 sources·dependency 및 활성 평가 입력, Worker·Reviewer의 현재 ExecPlan 내용에 한정한다. 후보 HEAD가 바뀐 실행은 기존 검증·Review 증거를 무효화하고 최종 후보에서 다시 통과한다. 게시 직전 다시 원격을 확인하며 planning은 fast-forward, 구현은 기존 lease·PR·checkpoint 정책을 따른다.

진행 중인 일반·유지보수 planning은 원격 base 전진 시 staged·unstaged·untracked 변경과 세션을 관리 worktree에 보존해 반영하고 최신 catalog·SQLite 권한에서 재검증한다. 시작 revision의 READY 원본과 Planner가 작성한 결과를 구분하며, 관련 근거·의존 문서의 실제 내용이 바뀐 경우에만 해당 계획을 다시 확인한다. 무관한 문서 변경과 내용 보존 이동은 재확인을 만들지 않는다

Worker·Reviewer의 문서 재확인은 현재 ExecPlan 내용 변경에 한정한다. 후보 HEAD가 바뀌면 문서 재확인 여부와 관계없이 Worker의 필요한 targeted test, Manager 전체 verify와 읽기 전용 Review를 최종 후보에서 다시 통과한다

## 실행 자리와 유지보수

프로젝트마다 SQLite 연결과 하나의 변경 실행 자리를 사용한다. 여러 READY·PLAN_BLOCKED·안정된 PR_OPEN의 branch·worktree는 함께 보존할 수 있지만 변경 실행과 PR 충돌 복구는 하나만 진행한다. 서로 다른 프로젝트만 max_workers 안에서 병렬 제출하며 Planner 호출은 전체 프로세스에서 직렬화한다.

dependency·priority·명세 변경·유지보수 DAG의 선택 순서는 PRODUCT를 따른다. 같은 프로젝트의 병렬 파일 충돌 판정·PR 재정렬은 지원하지 않는다.

일반·유지보수 planning이 진행 중이거나 시도·오류·게시 결과를 보존하는 동안 다른 ExecPlan의 차단 복구는 project 실행 투영을 교체하거나 정리하지 않는다. Worker·검증·Review·게시와 Git 변경 복구는 planning이 실행 자리를 유효하게 비운 뒤 재개한다. 안전 gate가 허용한 PR Merge·Close 확인과 해당 계획의 상태·알림 기록은 계속할 수 있다

[R-055 구현 예정](requirements/%5BR-055%5D%20Blocking%20Path%20Audit%20and%20Minimal%20Interruption.md): 보존된 planning 오류 자체는 변경 실행 자리의 점유 근거가 아니다. planning worktree·session·미게시 결과·오류는 별도로 보존하고, 진행 중 operation·미확정 게시·실제 공유 안전 문제가 없으면 실행 자리를 비워 기존 독립 계획을 계속한다. 새 Planner 주기와 프로젝트 완료 선언은 오류가 해소될 때까지 보류한다.

### 유지보수 분석과 성공 근거

Manager는 최신 원격 추적 파일의 경로·내용 hash·mode와 분석 버전에 결합된 정적 사실을 저장한다. 변경된 파일만 다시 파싱하되 현재 파일 집합에서 관계를 다시 계산한다. 정적 코드맵·읽기 전용 조회는 심벌·import·직접 참조·관련 테스트·분석 불확실성을 제공하며 의미 검토 성공 증거는 아니다. 정적 사실은 `docs/maintenance/code-map.jsonl`과 Agent 조회용 별도 snapshot에 제공하고, Manager의 운영 SQLite·session·상태를 Agent 입력으로 내보내지 않는다.

PRODUCT의 `fast`·`targeted`·`full` 선택은 정적 비교와 마지막 성공한 전체 정밀 점검의 Merge 기준을 사용한다. `fast`에는 모델 검토 묶음이나 의미 검토 receipt를 만들지 않는다. 정밀 점검은 관련 파일을 모듈 단위의 제한된 묶음으로 구성하고, 원본·mode·정적 사실·분석 버전·평가 조건에 결합된 묶음 identity의 성공 결과를 재사용한다. 전체 점검의 성공 Merge 기준과 최신 정적·부분 평가 기준은 따로 보존한다.

모듈 의미 검토는 읽기 전용 Planner 호출로 필요한 원본과 보존 조건을 확인한다. Manager는 묶음 범위·판단·발견과 실제 원본 인용을 검증해 잠정 결과로 보존한다. 발견이 없고 일반 계획도 필요하지 않으면 추가 종합·계획 호출 없이 no-change 결과를 준비한다. 발견이 있으면 저장된 검토를 입력으로 계획 또는 근거 있는 제외에 연결하며, 파일별 검토·관계 확인·종합 페이지를 다시 기본 단계로 실행하지 않는다.

유지보수 Planner의 barrier dependency·사후 구조 참조 개정은 계속 활성 mode와 무소유 READY snapshot에 한정한다. 새 단계별 평가의 일반 계획은 별도 필요한 계획 주기에서 수행한다. 일반 계획 결합 여부와 종전 결과를 이미 보존한 cycle은 해당 mode·게시 검증을 유지해 복구한다.

planning batch의 원격 게시·catalog 반영과 현재 원격 입력 일치를 확인한 뒤 정적·모듈 평가 근거와 해당 성공 기준을 SQLite transaction에서 확정한다. 미게시·검증 실패·원격 source 변경은 성공 기준을 전진시키지 않으며 잠정 근거·publication intent를 보존해 복구한다. 새 리팩터링 계획은 기존 DAG·장벽·사후 재검증 경로를 따르고, 사후 재검증은 cycle 완료를 담당한다.

종전 파일별 평가 경로는 이미 보존된 결과·게시의 복구를 위해 유지한다. 실제 검토 파일의 ledger·의미 코드맵·파일별 성공 기준은 그 경로에서만 원본·관계·게시 검증을 거쳐 확정하며, 정적 코드맵이나 새 모듈 평가를 파일별 의미 검토 완료로 기록하지 않는다.

## AI Agent 실행

- 사용자 요청의 새 기능·정책 변경은 대화형 Agent가 관련 Requirement를 먼저 작성·갱신하거나 충분한 기존 활성 Requirement를 재사용한다. 프로젝트로 복제하는 Specification Skill은 제품 명세 작성·검토만 안내하고 Manager 내부 실행 절차를 설명하지 않는다. 이 저장소 루트 AGENTS는 Skill을 참조하며 문서 작성 기준을 반복하지 않는다. 직접 작업에는 ExecPlan 작성을 요구하지 않으며 생성·계획 작성은 매니저가 호출하는 자동 Planner 흐름에서 수행한다
- 정적 자동 실행 규칙은 선택 runtime의 `docs/AGENTS.common.md`와 `docs/AGENTS.planner.md`·`docs/AGENTS.planner_check.md`·`docs/AGENTS.worker.md`·`docs/AGENTS.reviewer.md`에 둔다. common에는 모든 역할의 기준만, 역할 파일에는 해당 역할의 권한·금지사항·절차만 남긴다. Skill과 Manager가 프로젝트에서 쓰는 예약 경로는 PRODUCT의 프로젝트 예약 경로 목록을 따르며 공통 지침과 명세 작성 Skill에 위치 보존을 명시한다. runtime 전용 `docs/AGENTS.*.md`는 프로젝트 예약 경로가 아니다. Loop Manager 전용 새 루트 항목 제한·instruction 원본 수정 지침은 이 저장소 루트에 두며 runtime 공통·역할별 합성 대상에 포함하지 않는다
- Manager는 작업 시작 시 모든 원본을 묶음으로 고정하고 내용 해시·source revision·snapshot과 작업·session 관계를 상태 디렉터리와 SQLite에 보존한다. 일반·유지보수·수동 제출 Planner는 common+planner, 점검은 common+planner_check, Worker와 Reviewer는 common+각 역할을 사용한다. 재시도·새 세션·runtime 갱신에도 같은 작업의 원본 버전을 유지한다
- 실제 합친 내용을 Codex의 추가 developer instruction 또는 Claude의 append system prompt로 전달하고 실제 내용 해시를 호출 기록에 남긴다. 기본·기존 추가 지침·프로젝트 규칙과 실행 경계를 보존하며 원본·snapshot 오류, 미지원 전달 기능·설정 충돌·알 수 없는 역할은 모델 실행 전에 거부한다
- 원본 묶음 snapshot은 저장소 밖 Manager 상태 디렉터리의 JSON으로 보존하고 역할별 합친 Markdown은 같은 디렉터리의 실행용 파일로만 생성하며 커밋하지 않는다. 합친 파일은 고정 원본에서 재생성하지만 원본 누락·손상은 최신 내용으로 대체하지 않는다. 기존 단일 Markdown snapshot은 원래 내용으로 재개하며 최신 역할 지침을 덧붙이지 않는다. 관련 planning·성공 결과 재사용에는 모든 역할 원본을 포함한 묶음 해시를 사용한다
- Planner, Worker와 Reviewer의 작업별 prompt는 Loop Manager에 내장하며 목표·snapshot·승인 사실·피드백·결과 형식 등의 문맥을 구성한다. 정적 공통·역할 규칙을 이 사용자 메시지에 중복하지 않는다
- 프로젝트별 prompt 재정의는 지원하지 않으며 프로젝트 파일이나 작업 prompt로 자동 역할 권한을 확대하지 않는다
- 프로그램은 전체 ExecPlan을 코드로 파싱하되 Planner에게는 비종료 계획의 ID, 제목, 상태, sources, depends_on과 priority만 compact catalog로 전달한다
- Planner는 PRODUCT, 활성 Requirement, ARCHITECTURE, 관련 코드·테스트, 비종료 ExecPlan compact catalog와 프로그램이 제공한 미착수 READY snapshot을 읽고 현재 식별 가능한 미충족 작업 전체를 한 계획 주기에 반영하며, 범위 밖의 보존 가치가 있는 아이디어는 `docs/FUTURE_IDEAS.md`에 분리한다
- Planner는 MERGED ExecPlan 메타데이터와 본문을 기본 입력으로 받거나 읽지 않는다. 프로그램만 ID 고유성, 번호 할당, 의존성 충족과 감사 이력을 위해 이를 유지한다
- Planner는 안전한 무인 실행 차단과 반복적인 사용자 복구 제거를 최우선으로 priority를 정하고, 정상 경로 자동화, 그 밖의 Current Goal, 선택적 편의와 최적화 순으로 계획한다
- Planner는 새 `docs/plans/*.md`를 생성하고 `docs/FUTURE_IDEAS.md`를 생성하거나 수정할 수 있다. 프로그램이 snapshot으로 지정한 미착수 READY만 같은 ID로 수정·rename·삭제할 수 있으며, 실행된 계획, snapshot 밖 기존 계획과 그 밖의 파일은 변경할 수 없다. 프로그램은 Agent 실행 전후 diff와 SQLite snapshot으로 범위를 검증한다
- Worker는 ExecPlan·관련 unit test·verify 스크립트를 구현한다. Requirement·ExecPlan 구현으로 바뀌는 제품 동작·장기 규칙은 PRODUCT에, 설계·책임 경계·계약은 ARCHITECTURE에 같은 변경에서 반영하며 문서 경로만으로 설계 수정을 거부하지 않는다. 현재 구현에 필요한 대응표·구현 기록은 다른 기존 ExecPlan에도 갱신할 수 있다. 다른 계획의 목표를 구현하거나 상태를 문서에 기록하지 않는다. 범위 밖 아이디어는 FUTURE_IDEAS에 기록한다
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

Codex를 기본 provider와 기능·검증 수준의 기준으로 삼는다. Claude adapter는 같은 기능을 제공하는 전제로 해당 provider의 명칭·설정·호출 방식에 대응한다. 지원 구현의 검증에는 공식 정책 대조와 모의 호출 테스트를 사용하고, Claude CLI·이용 가능한 계정이 없는 개발 환경에서 실계정 호출 성공을 필수 gate로 요구하지 않는다. 실제 호출 검증은 가능한 환경에서 추가하며 미수행 범위를 기록한다. 이 설계는 Claude의 구현 완료나 실제 실행 성공을 선언하지 않는다.

provider adapter는 실제 호출 직전에 선택된 canonical worktree, Git worktree 등록과 repository identity를 확인하고, 신규 session과 재개 호출에 동일한 쓰기 정책을 명시한다. 허용 경로는 선택된 workspace와 실행별로 소유권을 확인한 최소 임시 경로로 제한한다. 사용자·프로젝트·session 설정의 병합으로 다른 worktree, source checkout, shared Git metadata 또는 넓은 임시 경로가 추가되면 이를 격리 확인으로 인정하지 않는다. workspace 정책과 별도로 기존 Planner·Worker·Reviewer·유지보수 Planner의 허용 파일 및 변경 범위 검사를 유지한다.

- Codex는 R-050에 따라 Manager가 `codex exec`를 직접 호출한다. 선택된 관리 worktree를 `-C`로 지정하고, 쓰기 역할에는 `workspace-write`, Reviewer 및 읽기 전용 역할에는 `read-only`를 적용한다. `-C`는 작업 경로이며 쓰기 제한은 내장 sandbox가 담당한다. 바깥 `codex sandbox` wrapper, `app-server` transport와 중첩 sandbox probe를 호출 조건으로 요구하지 않는다. `approvals_reviewer="auto_review"`를 유지하면서 `approval_policy.granular`의 `sandbox_approval=false`, `request_permissions=false`로 sandbox 밖 실행과 권한 확대 요청을 거부한다. 필요한 나머지 승인 항목은 검토 대상으로 유지한다. `approval_policy="never"`로 전체 자동 검토를 비활성화하거나, 편의 CLI 옵션이 명시한 granular 정책을 덮어쓰게 하지 않는다.
- Codex의 추가 `writable_roots`는 Manager가 허용한 최소 경로만 포함한다. 기본 `/tmp` 및 `$TMPDIR` 쓰기 범위는 `exclude_slash_tmp`, `exclude_tmpdir_env_var`로 제외하고 필요 시 실행별 임시 경로만 추가한다. `.git` pointer file과 그 resolved Git directory를 포함한 보호 경로 및 shared Git directory는 쓰기를 허용하지 않는다. 상속된 profile·규칙·확장 기능이 경계를 우회할 수 없는지도 확인한다.
- Claude의 쓰기 역할은 `--permission-mode auto`를 명시하고 공식 지원 모델·계정·CLI 조건과 유효 설정을 대조한다. 실제 호출에서 보고된 미지원은 해당 호출의 오류로 처리하며, 개발 환경의 계정 부재만으로 별도의 효력 시연을 요구하지 않는다. `acceptEdits`는 편집 자동 승인 모드이므로 자동 검토 유지의 대체 설정으로 사용하지 않는다. Reviewer는 기존 읽기 전용 역할과 편집 도구 금지를 유지하고, 해당 역할에서도 지원되는 자동 검토 경로를 확인한다. 지원되지 않으면 권한 모드를 완화하지 않고 원인을 기록한다.
- Claude는 `sandbox.enabled=true`, `failIfUnavailable=true`, `allowUnsandboxedCommands=false`를 적용하고 `excludedCommands`를 비워 sandbox 밖 재실행을 막는다. sandbox 안 명령을 자동 검토 없이 일괄 승인하지 않도록 `autoAllowBashIfSandboxed=false`를 사용해 permission mode의 검토 흐름을 유지한다. 실제 검토 대상은 provider 정책에 따르며 모든 읽기·편집·명령이 검토된다고 표시하지 않는다.
- Claude의 linked worktree는 기본 sandbox에서 main repository의 shared `.git` 쓰기가 추가 허용될 수 있으므로 resolved shared Git directory와 worktree Git directory를 `sandbox.filesystem.denyWrite`로 명시적으로 거부한다. 설정 병합으로 추가 workspace나 우회 예외가 생기지 않도록 확인한다. Bash sandbox 밖에서 처리하는 Edit·Write·NotebookEdit 등 파일 도구에도 같은 쓰기 경계를 강제한다. Reviewer는 선택된 worktree의 쓰기까지 거부한다.
- 내장 sandbox가 없는 Claude native Windows는 미지원으로 진단하고 해당 Agent 호출을 시작하지 않는다. Linux와 Codex native Windows도 필요한 CLI 기능과 sandbox 초기화를 확인할 수 없으면 동일하게 중단한다. WSL·컨테이너 전환이나 새로운 외부 격리 infrastructure는 이 설계에 포함하지 않는다.

격리 정책 작성과 유효 정책 확인은 provider 호출 경계의 독립 책임으로 두고, 기존 Agent invocation·session·snapshot 저장과 연결한다. SQLite에는 호환 가능한 추가 저장만 사용하며, 개별 호출마다 provider, CLI 버전, 실행 환경, 역할, canonical worktree, repository identity, 유효한 격리 정책의 지문, session/호출 identity와 확인·거부 결과를 보존한다. 증거 저장이나 확인이 실패하면 검증된 실행으로 취급하지 않는다. 재개·provider 전환·설정 변경·중단 뒤에는 이전 증거를 새 호출의 증거로 재사용하지 않는다. 비밀값, 인증 자료와 source checkout의 로컬 파일 내용은 저장하지 않는다.

단순 CLI 옵션, cwd 지정, 자동 검토의 승인 또는 사후 Git diff만으로 호출 전 격리를 증명하지 않는다. 실제 적용되는 정책과 허용 경로 쓰기·금지 경로 쓰기 거부를 확인하며, source checkout의 로컬 상태를 바꾸지 않고 증명해야 한다. Codex 확인은 Agent의 파일 변경·도구 실행에 적용되는 내장 정책을 대상으로 한다. 설치 CLI의 공식 지원 설정, 병합 우선순위와 역할별 실제 허용·거부 동작을 확인하며, exec JSONL에 유효 정책 전용 필드가 없다는 이유로 transport를 바꾸거나 바깥 wrapper를 추가하지 않는다. Codex 프로세스 전체의 mount·내부 쓰기 및 중첩 도구 실행 성공은 증명 조건이 아니다. 정상 인증·session·로그 저장과 PRODUCT의 `refs/codex/turn-diffs/*` 예외는 구분하며 그 밖의 Git·Agent 쓰기 권한은 확대하지 않는다. Claude도 같은 확인 수준을 적용한다. 공식 정책의 지원 조건·설정 병합·resolved Git 경로와 파일 도구 권한을 대조하고 신규·재개·fallback의 호출 구성을 모의 테스트로 검증한다. 계정이 필요한 모델 호출이나 금지 쓰기·자동 검토의 실시간 시연을 호출 전 필수 probe로 추가하지 않는다. 정책 검토·모의 테스트와 실제 호출 검증의 근거를 구분해 저장하고, 실제 호출에서 관측한 미지원·정책 충돌·초기화 실패는 해당 Claude 작업에만 반영한다. Claude의 개발 환경 부재나 선택 프로젝트의 인증 실패 때문에 독립 Codex 작업을 막거나 다른 provider로 임의 전환하지 않는다. 신규 호출에서 요구 정책을 적용·확인할 수 없거나 기존 허용 작업과 양립하지 않으면 원인과 기존 작업물·session을 보존한 채 중단한다. 격리 증거가 없는 과거 snapshot과 session의 판정은 기존 엄격한 shared ref·다른 worktree 감시를 유지한다. Loop Manager의 Worker 이후 Reviewer 검토는 provider의 실행 전 자동 승인 검토와 별도로 유지한다.

Reviewer의 `read-only`는 workspace 파일 쓰기를 제한한다. 검색·diff·기존 결과 읽기에 필요한 명령은 내장 sandbox 안에서 실행할 수 있다. 테스트 command와 verify를 재실행하지 않는 규칙은 고정된 common+Reviewer 역할 instruction에 유지하며, 쓰기 없는 테스트도 역할상 실행하지 않는다. Worker의 targeted test 결과와 Manager의 전체 verify 결과를 작업별 prompt로 전달하는 기존 흐름을 유지하고, read-only 정책이 테스트 명령을 모두 차단한다고 취급하지 않는다.

설정 의미와 지원 범위는 구현 시 설치된 CLI와 공식 문서를 대조한다: [OpenAI 승인 정책 설정](https://learn.chatgpt.com/docs/config-file/config-reference), [Codex 자동 검토](https://learn.chatgpt.com/docs/sandboxing/auto-review), [Claude sandbox](https://code.claude.com/docs/en/sandboxing), [Claude permission mode](https://code.claude.com/docs/en/permission-modes). 지원되지 않는 key를 무시한 실행을 격리 확인으로 기록하지 않는다.


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

## 상태 저장과 중단 복구

상태의 의미·허용 전이는 [PRODUCT.md](PRODUCT.md#states-and-transitions)를 따른다. 프로그램만 상태를 변경하고 다음 phase와 소유 worktree를 작업 시작 전에 한 transaction으로 저장한다. 중단 뒤 저장된 단계와 실제 Git 상태를 대조해 같은 작업을 재개한다.

worktree가 없고 소유 branch가 있으면 재등록할 수 있다. 둘 다 복구할 수 없거나 소유권을 확정할 수 없으면 작업물을 보존해 해당 scope로 차단한다. 원인·plan identity·판단 근거·해제 지문과 상태는 함께 저장하며, scope를 확인할 수 없는 legacy BLOCKED는 PROJECT_BLOCKED로 보수적으로 이관한다.

일반적인 PLAN_BLOCKED는 계획·원래 활성 sources의 내용 해시를 기준으로 재평가한다. 닫힌 PR은 같은 PR reopen을 요구한다. 과거 파일 승인 대기는 일반 작업·소유권 검사를 통과하면 기록을 보존한 채 재개한다. 독립 차단·planning 오류·실패 감사는 함께 지우지 않는다. planning_error는 실제 교정·재검증·게시 복구로 원인이 해소된 뒤에만 지운다. cleanup_pending과 ExecPlan archive pending은 상태 전이를 만들지 않는다.

Git·journal 파일·SQLite는 단일 원자 transaction이 아니다. 저장 intent, 현재 Git 결과와 SQLite generation을 대조해 이미 끝난 단계를 반복하지 않고 결과만 멱등하게 반영한다. 과거 증거·실패·알림 감사는 삭제하지 않는다.

### 관리 rebase와 후보 검증

현재 rebase 정책은 작업 수행 이력을 필수 증거로 삼던 R-045 설계를 대체한다. 자동 Worker 교정과 사용자 수동 해결·stage·continue·완료를 같은 현재 상태 판정으로 다룬다. 과거 기록은 감사 자료로 보존하며 누락 자체를 거부 조건으로 사용하지 않는다.

Worker 결과와 작업 범위를 확인한 뒤 최신 `origin/<base-branch>`를 fetch하고 소유 변경을 commit하여 필요한 rebase를 완료한 다음 게시 후보 HEAD에서 전체 검증과 Review를 수행한다. 기준이 안정적이면 한 Worker 결과의 전체 검증은 한 번 수행한다. 이후 base 또는 후보 HEAD가 바뀌면 기존 검증과 Review를 무효화하고 최종 revision을 다시 검증한다

모든 관리 rebase의 시작·충돌 해결·완료·중단 복구는 현재 repository·계획·canonical worktree·소유 branch·target base·원격 OID와 PR identity/head를 확인한다. Worker 교정 turn, 승인 stage tree, 명령 receipt, reflog 표식, 원본 commit별 replay와 journal 완전성은 완료 조건이 아니며 기존 기록과 실패 감사는 보존한다

자동 Worker 해결과 사용자의 수동 파일 해결·stage·continue·완료를 같은 현재 상태 판정으로 수용한다. 진행 중인 소유 rebase와 미해결 autostash 복원은 충돌 파일·index·operation을 보존하고 기존 Worker 교정 경계로 보낸다. Manager는 현재 범위가 확인된 필요한 path만 stage하고 이미 끝난 Git 단계를 반복하지 않는다

완료 결과는 HEAD·branch ref·worktree 등록·target ancestry, clean index·미해결 operation 부재, 현재 역할·선택 ExecPlan 및 archive 계약에 맞는 최종 변경과 정확한 원격 lease를 확인한 뒤 현재 상태·입력 지문과 SQLite generation에 결합해 한 번만 반영한다. 원본 commit 내용·개수·순서의 변화 자체를 거부하지 않으며 Git 완료 뒤 저장 전 중단도 같은 검사로 복구한다

PR 없는 동일 계획의 기존 원격 작업 branch를 관리 rebase한 경우, 현재 repository·계획·worktree·branch·HEAD·ref·등록·target base ancestry·index·operation·허용 변경과 마지막 채택 원격 OID를 확인한 뒤 완료 HEAD를 중간 checkpoint로 게시한다. 다음 Worker·전체 verify·Review·추가 base rebase보다 먼저 게시 또는 안전한 거부를 확정하며 base가 다시 전진해도 완료된 결과를 먼저 처리한다. 기존 PR·신규 원격 branch·다른 계획의 branch는 이 경로로 게시하지 않는다

중간 게시 intent는 정확한 기대 원격 OID와 후보 HEAD, 현재 상태 수용 identity·지문을 SQLite에 보존한다. 게시 직전 실제 ref와 PR 부재를 다시 확인하고 정확한 OID를 명시한 `--force-with-lease`로 같은 ref만 갱신한다. 원격 삭제·재작성·후속 commit·PR 출현·권한 또는 보호 정책 거부에서는 기존 ref와 작업물을 보존하며 lease를 새로 추정하지 않는다

push 전후 및 SQLite 반영 전 중단에서는 같은 intent와 실제 원격 OID·현재 소유 상태를 재대조한다. 원격이 이미 후보 HEAD면 push·commit을 반복하지 않고 완료 기록과 마지막 채택 원격 OID를 한 transaction으로 갱신한다. 일시적 실패는 같은 intent로 재개하며 미게시 rebase가 여러 개 쌓인 경우도 과거 journal·receipt·commit별 replay 연쇄 없이 동일한 현재 상태 판정을 사용한다. 중간 게시는 구현 완료·차단 해제·검증 성공·PR head 승인이 아니며 최종 후보의 Worker 확인·targeted test·전체 verify·Review·PR gate를 유지한다

PR 없는 게시 checkpoint의 원격 OID가 rebased HEAD의 조상이 아니어도 동일 계획의 게시 identity·마지막 채택 원격 OID·현재 branch/worktree·target·허용 최종 변경이 일치하면 소유권을 유지한다. 원격 후속 commit·삭제·재작성과 PR head 변경은 수용하지 않으며 journal/replay 완전성을 ownership 조건으로 다시 요구하지 않는다

rebase로 후보 HEAD 또는 ExecPlan·근거 입력이 바뀌면 이전 Worker 후보 확인·전체 verify·Review·publication 증거를 무효화한다. 기존 또는 정상 fallback Worker 세션이 현재 입력과 최종 diff를 재확인하고 관련 targeted test 결과를 반환한 뒤 Manager 전체 verify와 Reviewer를 다시 통과해야 한다. 같은 확인 후보에서는 Worker를 반복 호출하지 않는다. 수용 자체는 구현 완료·게시 승인·차단 해제가 아니며 중간 checkpoint 게시도 최종 성공을 대신하지 않는다

현재 결과는 선택 branch/worktree의 비교 기준선에만 반영한다. Agent 원본 snapshot·session·허용 범위와 과거 감사를 보존하고 수동 복구를 Manager 실행·Agent 격리 증거로 표시하지 않는다. 실제 Agent 범위 위반, 다른 Manager Git 작업의 증거와 별도 Review·사용자 결정·planning 차단은 기존 경계를 유지한다

저장된 rebase 충돌·완료·소유권 오탐 차단은 실제 원인과 대상 계획의 rebase를 연결하고 현재 Git 상태, checkpoint, 원격 branch·PR 소유권 및 허용 최종 변경을 다시 확인한 경우에만 해당 원인을 해제한다. 과거 작업 기록이나 세션 일치는 해제 조건이 아니다. 다른 Review·사용자 결정·Agent 범위·planning 원인과 실패·알림 감사는 보존한다. 해제 후 후보 revision에 필요한 Worker 확인, targeted test, 전체 verify와 Review를 다시 수행하며 게시 직전 최신 원격 lease를 확인한다

### 차단 작업의 원격 반영과 보존

흐름:
1. `PLAN_BLOCKED` 상태, scope, 구조화 원인과 판단 근거를 SQLite에 먼저 commit하고 PR이 없는 계획에 별도 durable checkpoint request를 기록한다
2. 저장된 branch·결정적 worktree·workspace 소유권·HEAD와 Worker 허용 변경 범위가 모두 일치하고 미해결 Git operation, symlink, ignored 파일, 범위 밖 staged entry, index flag 또는 공유 metadata 불확실성이 없을 때만 검증된 pathspec으로 checkpoint commit을 만든다. 이미 commit된 변경이나 변경 없음에는 빈 commit을 만들지 않는다
3. 같은 소유 branch의 원격이 없으면 정확한 부재 lease로 생성하고, 저장된 expected OID의 정확한 lease에 결합된 fast-forward만 갱신한다. fast-forward가 아닌 갱신은 PR 없는 동일 소유 branch의 검증된 관리 rebase에만 허용하며 예상하지 못한 원격 commit, branch protection과 권한 거부를 우회하지 않는다
4. commit·push phase와 local/remote OID를 저장해 중단 후 같은 checkpoint를 재개하고, 일시적 push 실패는 실행당 제한 횟수만 재시도한 뒤 local commit과 worktree를 보존한다. checkpoint 결과와 무관하게 ExecPlan은 `PLAN_BLOCKED`로 남고 독립 READY 선택 자리를 점유하지 않는다
5. `planning_error`, 등록·branch 생성 전 실패, 기존 PR 또는 현재 대상 작업의 소유권·범위를 증명할 수 없는 차단에는 원격 쓰기를 수행하지 않고 미게시 이유를 저장한다. `PROJECT_BLOCKED`에서는 검증 가능한 대상 계획의 복구·보존만 15번의 경계에서 허용한다. 차단 알림은 checkpoint 결과 확정 뒤 성공 시 commit OID와 immutable branch link를, 미게시 시 local-only 이유를 포함한다
6. 계획별 입력 변경, 사용자 결정 또는 닫힌 PR reopen 조건은 해당 `PLAN_BLOCKED`만 재평가한다. PR 없는 게시 checkpoint의 저장된 원격 branch가 달라지면 게시 OID와 마지막 채택 head를 포함하는 후속 commit인지 확인한다. 계획·branch·worktree·저장소 소유권, 로컬 HEAD·index·작업 파일·ignored 경로·Git operation 및 허용 변경 범위를 검증한 뒤 로컬 작업을 보존할 수 있을 때만 같은 branch를 fast-forward한다. 삭제·재작성·분기·소유권 불명확·충돌은 구체적인 이유로 계속 차단한다. 새 OID만으로 해제하지 않고 실제 변경과 원인별 조건을 재평가하며, 해결된 사용자 commit은 Worker 검토와 targeted test, 전체 verify, Review를 다시 거친다. 해결되지 않은 검사 결과는 원격 head와 함께 저장해 동일 입력의 반복 시도와 알림을 막는다. 게시 증거가 없는 legacy checkpoint와 닫힌 PR에는 이 경로를 적용하지 않는다
7. 해제 조건이 충족된 계획은 기존 branch와 worktree에서 재개하고 재시도 횟수와 verify·Review 실패 지문 이력을 초기화하며 effective Worker level과 승격 이력을 최초 level로 되돌린 뒤 READY 또는 PR_OPEN으로 변경한다. 같은 branch의 최초 PR은 최종 verify와 Review 성공 뒤에만 생성한다
8. `PROJECT_BLOCKED`는 기본 worktree, 허용되지 않은 공유 ref, 외부 worktree와 workspace 소유권을 다시 검증해 프로젝트 안전이 증명된 경우에만 해제한다. `refs/codex/turn-diffs/*`의 순변경은 해제 조건을 무효화하지 않는다
9. 프로젝트 차단이 해제돼도 별도 계획 원인이 남은 ExecPlan은 `PLAN_BLOCKED`를 유지하며, 다른 차단 계획의 fingerprint와 작업 공간은 변경하지 않는다
10. 저장된 `managed_worktree_conflict`와 계획에 연결된 rebase 완료·소유권 오탐은 같은 현재 상태 서비스로 재평가한다. 특정 구형 오류 문구, 과거 Worker 세션·교정 turn·stage 승인·receipt·journal 완전성이나 입력 변경 지문 부재만으로 복구를 거부하지 않는다. 저장 원인을 해당 계획의 rebase intent와 연결할 수 없으면 차단을 유지한다
11. 현재 repository·reservation·등록 worktree·소유 branch·target base·원격 기대값과 rebase metadata가 일치하면 일반 branch 검사 전에 detached rebase를 인식하고 충돌·사용자 stage를 보존한 채 같은 실행을 재개한다. 이미 완료됐으면 Git 단계를 반복하지 않고 결과를 채택한다. 관측을 transaction 안에서 다시 확인하고 대상 원인 해제와 후보 검증 무효화를 함께 기록한다. 다른 활성 실행·planning 또는 미충족 dependency가 있으면 투영·session·진단을 바꾸지 않고 대기한다
12. rebase 원인 해제는 별도 Review 실패·사용자 결정·PR reopen·실제 Agent 범위 위반과 다른 계획·planning·프로젝트 안전 원인을 해제하지 않는다. 원문·실패·알림 감사와 기존 branch·worktree·session을 보존하며 변경된 후보는 Worker의 현재 입력·최종 diff 확인과 targeted test, Manager 전체 verify, Reviewer 및 기존 게시 ownership·lease·PR identity 조건을 다시 통과해야 한다
13. 교정 소진으로 차단된 진행 중 rebase는 저장된 실제 차단 기준선과 현재 입력·선택 worktree를 비교할 수 있고 모두 같으면 차단을 유지해 동일 Worker 실패를 반복하지 않는다. 입력 또는 수동 해결·stage·continue 결과가 바뀌면 현재 상태를 다시 검사한다. 과거 기준선이나 실패 기록의 부재 자체는 복구 거부 조건이 아니다
14. `PLAN_BLOCKED`와 계획과 연결된 `PROJECT_BLOCKED`의 계획별 원격 관측 입력은 다른 활성 계획·PR 충돌 복구·planning publication·유지보수가 변경 실행 자리를 점유하는 동안 보존한다. 자리가 비었거나 차단된 동일 계획이 점유한 때에만 정확한 원격 ref·PR·계획·branch·worktree·작업 범위를 다시 확인하고, 안전한 작업 branch 후속 commit을 먼저 채택한 뒤 최신 base를 관리 worktree에 반영한다. 계획·Requirement 지문이 같아도 base 전진은 rebase 대상으로 처리한다. PR 없는 기존 branch의 완료 결과는 현재 상태 판정과 중간 checkpoint 게시를 먼저 마친 뒤 원래 원인만 재검사한다. 원인이 남으면 이유와 차단을 유지해 기존 checkpoint 경계로 현재 작업물을 게시하며, 같은 원격 HEAD에는 빈 commit·반복 push를 만들지 않는다. 원인이 해소된 원격 복구는 기존 session·branch·worktree와 실패·승격 감사를 보존하고 후보 검증 토큰을 무효화해 Worker 확인·targeted test·전체 verify·Review부터 다시 진행한다. 저장 단계와 실제 Git 결과를 대조해 중단을 복구하며 동일한 처리 입력과 원인의 검사·rebase·알림을 반복하지 않는다. 별도 사용자 결정·PR reopen·소유권·안전 차단과 다른 계획의 상태는 유지한다

15. 프로젝트 차단 중 기존 branch의 게시 identity와 마지막 채택 원격 OID가 확인되면 동일 소유 branch에 관측 OID의 정확한 lease로 checkpoint를 게시한다. fast-forward 또는 PR 없는 검증된 관리 rebase만 허용하며 원래 차단이 남으면 상태·원인을 보존한다. 게시 계보를 증명할 수 없어도 현재 repository·plan·worktree·branch·target·원격 기대값·허용 변경 범위와 rebase 완료를 검증한 작업물은 결정적 진단 branch에 보존한다. 진단 ref는 계획·원본 branch·base·내용 OID에 결합하며 부재 시에만 생성한다. 같은 OID는 재사용하고 다른 OID는 갱신하지 않는다. 원인·ref·OID와 게시 intent·확정 결과는 SQLite에 보존하며 PR 생성·원래 branch 소유권 승격·차단 해제 근거로 사용하지 않는다
16. 충돌 중이거나 현재 상태·작업 범위를 검증하지 못하면 commit·push하지 않고 작업물·session과 실제 local-only 이유를 보존한다. 원격 삭제·재작성·경쟁·권한·branch protection 오류를 예상 밖 ref의 force push로 우회하지 않는다. 과거 rebase turn·stage 승인·receipt·replay·journal 부재 자체는 현재 상태 판정의 거부 사유가 아니다. 진단 ref 생성·checkpoint commit·push·SQLite 확정 사이의 중단은 저장 intent와 실제 OID를 대조해 복구하며, 같은 입력·같은 결과의 rebase·게시·검사·알림을 반복하지 않는다. 알림에는 확인된 ref·OID 또는 실제 local-only 이유와 보존 경로를 포함한다

17. 저장된 scope 차단이 증명된 격리의 무관한 변화, 검증된 Manager 전이의 낡은 기준선 또는 정상 충돌 stage 해소만으로 생겼다면 입력 지문 변경 없이 재평가한다. 실제 Agent 전후 결과·현재 선택 worktree·사용 자원·checkpoint·게시 소유권을 다시 확인하고 SQLite transaction에서 상태와 Git 결과가 그대로일 때 해당 원인만 해제한다. 진행 rebase는 일반 branch 검사보다 먼저 공통 현재 상태 서비스로 판정하며 `allow_rebase=True`나 metadata 존재만으로 detached HEAD를 수용하지 않는다. 작업물·session·retry·승인·실패 감사와 다른 차단을 보존하고 바뀐 후보의 검증·Review를 무효화한다. source freshness·safe fast-forward는 해제 후 독립 gate로 판단하며 프로젝트 안전 gate가 닫힌 동안 일반 Worker·Git 쓰기를 시작하지 않는다

PR 없는 게시 checkpoint의 Review 횟수 차단을 rebase 없이 해제할 때는 checkpoint 요청 identity·게시 commit의 tree와 identity·마지막 채택 원격 OID 및 현재 원격 ref, 동일 계획의 branch·관리 worktree와 로컬 HEAD·ref·index·작업 파일·등록 상태를 확인한다. 실행 checkpoint에 게시 소유권 근거를 보존하고 재시작과 Worker 교정 뒤에도 다시 대조한다. 이 경로는 rebase 완료 표식을 만들지 않으며, 이후 실제 rebase가 필요하면 관리 rebase의 현재 상태 판정과 정확한 원격 lease를 요구한다. 게시 소유권 오탐으로 생긴 프로젝트 차단은 같은 증거를 재검증한 경우에만 해제하고, 남아 있는 Review issue의 교정·검증과 다른 READY 계획의 선택을 계속한다.

원격 관측 기록은 origin identity·정확한 ref·OID·실패 원인·시각을 계획별로 보존한다. 조회 실패는 마지막 확정 OID를 덮지 않는다. 처리 키는 원래 원인·검사 지문·관측 base/work branch OID·로컬 작업 지문을 포함한다. 같은 키의 확정 결과를 재사용하며 관련 입력이 달라지면 재평가한다.

### 로컬 복구 요청의 접수와 실행

`recover`의 별도 runtime은 짧은 SQLite `BEGIN IMMEDIATE` transaction 안에서 `local_environment_recovery_requests`에 범위별 요청을 생성하거나 같은 범위의 pending 요청을 재사용한다. 기존 프로젝트별 단일 pending 인덱스를 프로젝트 전체·계획별 부분 unique 인덱스로 교체해 감사와 소비 기록을 보존한다. 접수 transaction을 commit한 뒤 전역 process lock을 비차단 방식으로 얻고, 선택한 계획의 실행 자리도 확인해 실행 가능할 때만 기존 복구 executor를 호출한다. 잠금·다른 실행 자리로 대기하면 요청을 완료하거나 제한 재검사를 소비하지 않는다.

등록 프로젝트 scheduler는 worker connection에서 DDL 없이 pending 요청을 접수 시각·삽입 순서로 읽고 실행 가능한 첫 요청을 선택한다. 즉시 명령 실행도 같은 선택을 적용해 앞선 실행 가능 요청이 있으면 자기 요청은 대기로 보존한다. 선택한 범위가 실행 가능하면 같은 executor에 저장된 요청 identity·계획 범위와 현재 설정, 공유 Planner mutex·verify coordinator·runtime mutation·단계 알림 경계를 전달한다. 앞선 요청이 다른 실행 소유자 때문에 대기하면 뒤의 실행 가능한 요청을 선택하고, 그런 요청도 없으면 일반 project runner를 호출하며 대기 요청은 보존한다. 프로젝트별 한 번의 scheduler 방문에서는 선택한 요청 하나를 처리한다. executor는 즉시 실행과 지연 실행의 결과·제한 재검사 소비를 동일하게 기록하며 완료한 요청은 다시 처리하지 않는다.

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

verify 지문 이력, Review 지문·level 승격 이력과 실행별 Worker 교정 횟수는 별도로 저장한다. 최종 반복·일시 중단·BLOCKED 해제 조건은 PRODUCT의 실패 정책을 따른다. verify 성공은 verify 이력만, Review 성공은 Review 이력만 초기화하고 effective level·승격 이력은 유지한다.

외부 CLI의 실행 실패, timeout과 일시적 서비스 오류는 프로그램 실행당 최대 3회 재시도한다. 모두 실패하면 현재 상태를 유지하고 해당 프로젝트를 건너뛴다. 다음 Loop Manager 실행에서는 다시 최대 3회 시도한다. AI 사용 한도 소진이 확인되면 현재 실행에서는 재시도하지 않는다.

## GitHub와 PR

등록된 Open PR은 매 상태 동기화에서 Merge 가능 여부도 확인한다. 결과가 미확정이면 다음 조회에서 확인하고, Git 충돌이 확정됐으면 관리 worktree에서 최신 원격 base를 rebase해 해소한다. 바뀐 후보의 Worker 확인·필요한 targeted test·전체 verify·읽기 전용 Review 뒤, 같은 PR identity와 원격 head를 확인해 기존 PR branch를 안전한 lease로 갱신한다. 승인·필수 check 대기는 rebase로 해소되는 충돌로 간주하지 않으며 새 PR을 중복 생성하거나 자동 Merge하지 않는다.

- branch, commit, push와 PR 생성은 프로그램 코드가 수행한다
- Planner의 검증된 새 ExecPlan, snapshot에 결합된 미착수 계획 개정·rename·삭제와 `docs/FUTURE_IDEAS.md` 변경은 하나의 planning commit으로 `origin/<base-branch>`에 fast-forward push하며 별도 PR을 만들지 않는다
- 하나의 ExecPlan에는 하나의 결정적 branch와 하나의 PR만 연결한다
- PR 생성 전 같은 head branch의 Open, Closed 또는 Merged PR을 조회하고 기존 PR이 있으면 SQLite에 연결한다
- PR 생성 직후 SQLite 기록 전에 중단돼도 다음 실행에서 기존 PR을 찾아 복구하며 중복 PR을 만들지 않는다
- 원격 branch는 있지만 PR이 없는 복구 상황에서 rebase로 commit이 바뀌면, SQLite에 저장된 동일한 Loop Manager 소유 branch임을 확인한 경우에만 `--force-with-lease`로 갱신한다
- PR 생성 전 관련 테스트, 전체 검증과 Reviewer가 모두 성공해야 한다
- PR 생성 직전에 최신 원격 기준 branch를 rebase로 반영하고, 기준이 바뀌었다면 전체 검증과 Review를 다시 수행한다

기존 PR identity와 Merge 상태는 GitHub가 원본이다. Closed PR은 같은 PR reopen을 기다리며 프로그램이 새 PR로 대체하거나 자동 Merge하지 않는다. Merge head와 관리 worktree를 대조해 clean·미게시 local commit 부재를 확인한 때만 강제 옵션 없이 정리한다. Merge 방식이나 base ancestry만으로 push 여부를 판단하지 않는다. 불확실한 정리는 cleanup_pending으로 보존하고 이후 안전 조건 충족 또는 경로 부재를 재평가한다.

### MERGED ExecPlan archive

ExecPlan은 GitHub Merge 확인과 SQLite MERGED 전이 뒤에만 별도 정리 대상으로 삼고, 최신 원격 base의 detached 임시 worktree에서 내용 보존 rename-only commit으로 일괄 archive한다

ExecPlan archive는 원본 hash, regular-file source, 비어 있는 destination과 원격 base 불변성을 검증해 fast-forward로 직접 게시하며, 후보별 충돌·원격 경쟁·push 실패는 MERGED 상태와 다른 작업·완료 처리를 막지 않는 archive pending으로 재시도한다

## Telegram 알림

알림 이벤트·payload·차단 해제와 대기·완료의 의미는 PRODUCT의 알림 정책을 따른다. token과 chat ID가 모두 있어야 SQLite outbox에 이벤트를 저장하고 전송한다. 두 값이 모두 없으면 생성·전송하지 않고 일부만 있으면 설정 오류다.

고유 사건 키로 중복을 방지하고 전송 결과·조각·실패 감사와 재시도 정보를 작업 상태와 분리해 보존한다. 실패한 전송만 다음 실행에서 재시도한다. planning 오류 해제와 해당 사건 해제·미전송 조각 취소는 한 transaction으로 반영하며 과거 전송 감사는 유지한다. 새 PRODUCT·Requirement 작업 주기와 내용 보존 archive를 구분한다.

## Repository Safety and Security

- 등록 프로젝트 기본 worktree의 로컬 변경, ahead·diverged 이력과 checkout branch는 원격 SHA 기반 자동 작업을 막지 않는다. safe fast-forward 조건을 만족하지 않으면 기본 worktree 동기화만 건너뛴다
- 사용자 파일 삭제, reset, 일반적인 강제 push와 자동 Merge를 수행하지 않는다
- 계획 commit의 base branch 직접 push는 fast-forward만 허용한다. branch protection 등으로 거부되면 우회하지 않고 계획 오류로 보존한다
- 기본 worktree에서는 검증된 `git merge --ff-only origin/<base-branch>` 외의 파일 쓰기, checkout, pull, rebase, reset, stash, clean과 commit을 수행하지 않는다
- 관리 worktree, SQLite, 임시 파일, 로그와 lock은 source checkout 밖의 `<state-dir>`에만 만든다
- 모든 계획과 구현 변경은 Loop Manager가 만든 관리 worktree 안에서만 수행한다
- AI Agent가 접근하고 수정할 수 있는 경로를 해당 관리 worktree로 제한한다
- `git fetch`와 `git worktree add/remove`는 공유 Git metadata만 변경한다. 기본 worktree의 파일, index와 HEAD 변경은 검증된 safe fast-forward synchronizer만 수행한다
- 관리 worktree는 미커밋 변경과 PR head에 없는 로컬 commit이 없다고 확인된 때만 강제 옵션 없이 제거한다. base branch ancestry나 GitHub Merge 방식은 push 여부 판정에 사용하지 않는다
- PR head 정보나 commit 관계를 확인할 수 없으면 worktree를 보존하고 `cleanup_pending`으로 기록한다
- `cleanup_pending` worktree가 이후 clean해지거나 경로가 사라지면 기록을 지우고 강제 옵션 없이 정리한다
- planning worktree·diff·session과 미게시 결과는 오류·게시 복구 동안 보존한다. 폐기·교체는 소유권과 보존 결과를 검증한 관리 경계에서만 수행한다
- Merge된 로컬 관리 branch만 비강제 삭제할 수 있으며 원격 branch는 자동으로 삭제하지 않는다
- 예외적으로 PR이 아직 없는 동일한 Loop Manager 소유 branch의 rebase 복구에만 `--force-with-lease`를 사용할 수 있다
- 공통 파일 복사 스크립트는 사용자가 명시적으로 실행하는 초기 설정 도구이며 자동 Loop에서 호출하지 않는다
- 등록 프로젝트의 문서와 verify 스크립트는 사용자가 신뢰한 입력으로 취급한다
- Telegram token과 다른 자격 증명을 AI prompt, subprocess 인자와 로그에 노출하지 않는다
- subprocess는 인자 배열로 실행하고 필요한 경우에만 운영체제 shell을 사용한다

### 실행별 Agent 범위와 Manager 전이

실행별 쓰기 격리 증거가 저장된 작업 기준선·호출·역할·session·repository 및 관리 worktree와 일치할 때만 Agent Git 감시를 선택 worktree의 identity·등록·branch·HEAD·index·허용 파일, 작업 소유 branch·원격 base·게시 ref와 영향을 주는 config·operation으로 좁힌다. 무관한 ref·다른 worktree 변화는 진단에 보존하며, 사용 중인 자원의 예상 밖 변화와 영향이 불확실한 config는 차단한다. 증거가 없거나 불완전한 과거 snapshot은 기존 엄격 비교를 유지하고, 이후 사용할 자원은 사용 직전에 실제 값·소유권·기대 OID를 재검증한다

Agent 시작 이후 Manager의 fetch·worktree 전이는 작업별 전후 기대값·소유 표식·checkpoint와 현재 Git 상태가 일치하는 자원만 비교 기준선에 반영한다. 관측 OID, SQLite 완료 상태나 예정된 cleanup만으로 전체 ref·worktree를 갱신하지 않으며, Git 변경 뒤 receipt 저장 전 중단에서도 같은 증거를 읽기 전용으로 확인한다. 원본 scope와 재개 호출의 비교 시작값을 별도로 보존해 후속 Manager 전이가 실제 Agent 위반을 덮지 않게 하며, Agent 허용 변경·session·격리 증거를 유지한다

현재 소유권과 허용 범위가 확인된 관리 rebase의 충돌 stage 1/2/3에서 stage 0으로의 해소(`ls-files -v`의 `M → H` 포함)는 보안 index flags 변경이 아니다. 신규 Worker 결과와 저장된 scope 차단에 같은 판정을 적용하되, `skip-worktree`·`assume-unchanged`의 예상 밖 설정·해제, 범위 밖 파일·index 변경과 소유권 불명확은 계속 차단한다

Codex가 Agent turn 추적을 위해 자동 생성·갱신·정리하는 `refs/codex/turn-diffs/*`는 게시 또는 workspace 소유권에 사용하지 않는 provider 소유의 비권위 임시 ref다. 그 namespace의 순변경만으로 Agent 범위 위반이나 `PROJECT_BLOCKED`를 만들지 않으며, 작업 소유 branch와 사용 중인 원격 ref는 계속 보호한다

Codex는 Manager가 `codex exec`로 직접 호출하며 선택 workspace의 내장 쓰기 제한과 granular 자동 승인 검토를 유지한다. Reviewer는 읽기 전용이고 테스트·verify 재실행 금지는 별도의 역할 지침으로 유지한다. Codex 프로세스 전체를 감싸는 바깥 sandbox와 중첩 sandbox 호환성은 호출 조건이 아니며, 정상 session·로그 저장을 Agent의 workspace 밖 쓰기 권한과 구분한다

## Verify 입력 관측과 생성 요청

`verification/inputs.py`는 version 1 목록과 작업 파일을 관측하고 `manager_adapter.observe_inputs`
경계로 제공한다. POSIX 상대 경로를 대소문자 구분하여 해석하며 `*`·`?`는 한 경로 segment,
독립된 `**` segment는 0개 이상의 segment와 일치한다. 숨김 파일도 포함한다. 절대 경로·
상위 이동·역슬래시·문자 class·중복 key/경로·미지원 버전은 거부하며 literal 누락은 오류,
glob의 빈 결과는 관측 가능한 빈 집합이다. 목록 자체와 실제 파일의 경로·내용·kind·mode,
실제 명령·환경·측정한 runtime·Git/외부 입력 identity를 결합한다. symlink를 따라가지 않으며
확인되지 않은 입력은 재사용할 수 없다. Git 내부·bytecode는 생성물이고 `.venv`는 별도로
측정한 runtime/외부 의존성 identity로 확인한다.

목록 밖 파일은 경로·내용·kind·mode 및 검증 설정에 결합한 completeness token으로 제한된
제외 검토가 필요하다. verify 성공은 이 근거를 만들지 않는다. 관측 API는 실행·성공 저장·
재사용 정책과 분리하며, 목록 부재/오류는 기존 전체 verify를 유지한다.

일반 planning의 prompt 준비는 최신 `origin/<base>`의 고정 revision과 실제 목록을 조회한다.
오래된 planning branch는 새 누락 요청을 만들지 않는다. 원격 활성 계획(draft 포함)과 SQLite의
미종료 계획(PR 포함)에 관련 작업이 있으면 요청을 생략한다. Manager 요청은 additive SQLite
`verify_input_requests`에 프로젝트·revision·목적·규칙 원문/버전을 보존하며 같은 요청을 재사용한다.
`manager:verify-inputs` 계획은 요청 선언과 정확한 생성 Purpose를 유지해야 한다. Planner 결과와
원격 catalog 등록에서 실제 등록 요청과 대조하며 임의 예약어·제품 목표 혼합을 거부한다.
예약어는 문서 조회와 Requirement retirement에서 제외하고 규칙 내용을 source 지문에 결합한다.
이는 성공 결과 재사용이나 일반 runner 도입을 구현하지 않는다.

## Architecture Change Policy

다음 변경은 ExecPlan을 BLOCKED로 만들고 사용자 승인을 받은 뒤 진행한다.

- SQLite schema 또는 상태 모델의 비호환 변경
- worktree, branch, commit, push와 PR 생성 경계 변경
- 인증 또는 비밀값 저장 방식 변경
- 새로운 외부 infrastructure나 비용 발생 서비스 도입
- 데이터 삭제 또는 비가역 migration
- 같은 프로젝트의 병렬 실행 도입
