# [R-089] setup PATH 저장과 bootstrap 전 실행 환경 적용

## Context

Linux의 cron 실행 환경에서는 정상 터미널에서 사용하는 사용자 설치 디렉터리가 PATH에 없어 Codex 등 외부 프로그램을 찾지 못할 수 있다. 현재 `scripts/setup.sh`는 가상환경과 Python 의존성만 준비하고, `run.sh`는 가상환경 Python을 절대 경로로 실행한다. `loop_manager.py`의 bootstrap은 부모 PATH를 그대로 사용하며 runtime 자식과 Agent도 이를 이어받는다. 따라서 가상환경 Python을 찾는 것만으로 Git·gh·Agent CLI·Node와 verify 내부 프로그램의 탐색 문제가 해결되지는 않는다.

setup에서 정상 터미널의 PATH를 설치별 캐시로 저장하고, `loop_manager.py`가 bootstrap의 첫 외부 명령보다 먼저 적용한다. 저장 파일이 없으면 현재 실행 환경의 PATH와 기본 시스템 후보를 합쳐 기존 설치와 PATH를 명시한 cron 실행을 지원한다. 캐시는 source checkout의 `config` 아래에서 다른 로컬 설정과 함께 관리하고 폴더 전체를 Git 제외 대상으로 둔다. 기존에 추적하는 `config.ini.example`은 설정 예시로 계속 관리한다. 프로그램 설치 디렉터리를 cron 실행 때마다 추측하거나 모든 호출을 개별 절대 경로로 바꾸지 않는다.

## Requirement

### setup에서 정상 PATH 수집·저장

- 이 변경의 적용 대상은 Linux다. 사용자는 필요한 CLI가 정상적으로 발견되는 같은 실행 계정의 터미널에서 기존 `bash scripts/setup.sh`를 실행한다. setup은 호출 환경의 PATH를 수집하며 별도 로그인 셸·개인 셸 설정 파일을 실행해 환경을 복원하지 않는다.
- 수집값은 디렉터리 순서를 유지하고 중복을 제거한다. 빈 항목·`.`·상대 경로·존재하지 않는 디렉터리는 제외한다. 경로에 포함된 공백은 보존하며 심볼릭 링크를 실제 대상 경로로 치환해 사용자가 선택한 설치 경로를 바꾸지 않는다. 환경 변수 확장·명령 치환·셸 코드 실행으로 값을 해석하지 않는다.
- 저장 전 정리한 PATH에서 Git·gh와 지원 Agent CLI 중 최소 하나가 실행 가능한 파일로 발견되는지 확인한다. 발견되지 않거나 정리 결과가 비어 있으면 저장하지 않고 정상 터미널에서 설치·PATH를 확인하도록 안내한다. 이 확인 때문에 로그인·모델 호출·프로젝트 작업을 시작하지 않는다.
- 저장 위치는 setup 대상 source checkout의 `config/execution-path.txt`다. `.gitignore`에서 source checkout의 `config` 폴더 전체를 제외하며 이미 추적 중인 `config.ini.example`은 추적을 유지한다. 캐시에는 PATH만 담으며 전체 환경 변수·인증값·개인 CLI 설정은 저장하지 않는다. `config.ini`·설정 예시·DB·사용자의 셸 설정은 수정하지 않고 가상환경 재생성만으로 캐시를 삭제하지 않는다.
- setup을 다시 실행하면 현재 정상 PATH로 저장값을 갱신한다. 같은 값의 재실행은 같은 결과를 만들고, 다른 프로그램 설치나 버전 관리 도구의 경로 변경은 정상 터미널에서 setup을 다시 실행해 반영한다. 수집·검사·setup 준비·저장 실패에서는 기존 저장값을 보존하고 실패를 알린다. 저장 중단이나 실행과의 경쟁으로 부분 파일이 읽히지 않아야 한다.

### bootstrap 전 적용과 실행 중 일관성

- `loop_manager.py`의 source 진입에서 인자를 해석한 뒤, bootstrap의 Git 조회·fetch를 포함한 첫 외부 프로그램 호출 전에 최종 PATH를 확정한다. `run.sh`에만 적용하지 않으며 가상환경 Python으로 직접 실행하는 등록·복구·수동 작업 등에도 같은 정책을 사용한다.
- 최종 PATH는 **저장한 PATH → 현재 프로세스 PATH의 추가 디렉터리 → 기본 시스템 디렉터리** 순서다. 기본 시스템 후보는 `/usr/local/bin`, `/usr/bin`, `/bin`, `/usr/local/sbin`, `/usr/sbin`, `/sbin`이며 실제 존재하는 디렉터리만 사용한다. 전체에서 위 수집 규칙으로 중복과 부적절한 항목을 제거한다. 저장값과 현재값의 앞뒤 순서를 바꾸거나 설치된 여러 버전 중 임의의 최신 버전을 선택하지 않는다.
- source checkout 기준의 PATH 캐시를 사용한다. 실행 cwd·선택 state_dir·별도 `--config` 위치는 이 설치별 저장 위치를 바꾸지 않는다. 서로 다른 source checkout의 캐시를 공유하거나 runtime worktree의 `config` 경로에서 다시 찾지 않는다.
- 적용은 현재 Manager 프로세스의 PATH와 그 자식 환경에 한정한다. 호출한 터미널·crontab·사용자의 셸 설정과 다른 환경 변수는 바꾸지 않는다. 기존 가상환경 Python의 절대 경로와 Agent의 비밀값 필터·인증 및 설정 홈·격리·권한 정책은 유지한다.
- 부모가 확정한 PATH는 bootstrap과 runtime 자식으로 전달되고 같은 실행의 Agent 신규 호출·session 재개·재시도·verify에 사용한다. runtime 자식은 저장 파일을 다시 읽거나 PATH를 재조립하지 않는다. 실행 도중 setup이 저장값을 갱신해도 진행 중인 실행은 처음 확정한 값을 유지하고 다음 source 실행부터 새 값을 사용한다.
- 설치 검사·인증 확인·모델 조회·추가 지침 및 격리 사전 검사와 실제 외부 명령은 같은 유효 PATH를 사용한다. 공통 명령 경계를 거치지 않는 직접 subprocess와 verify 내부의 외부 명령에도 적용한다. PATH 변경의 검증·결과 재사용 판단은 기존 실제 실행 환경 관측을 따르며 새 PATH를 과거 성공 근거에 소급 적용하지 않는다.

### 기존 설치와 실패·복구

- 저장 파일이 없는 Linux 실행은 현재 실행 환경의 PATH를 먼저 사용하고 기본 시스템 후보를 뒤에 보완한다. cron에서 사용자가 명시한 PATH도 같은 입력으로 보존한다. 캐시 부재만으로 setup 재실행을 요구하거나 작업을 중단하지 않으며 필요한 프로그램을 찾지 못할 때만 기존 실패 흐름과 함께 정상 터미널에서 setup을 실행하는 복구 방법을 안내한다.
- 저장 파일이 존재하지만 읽을 수 없거나 비어 있거나 유효한 PATH 설정으로 해석할 수 없으면 bootstrap 전에 오류를 알리고 중단한다. NUL·값 내부 줄바꿈 등 잘못된 입력은 실행하지 않으며 현재 PATH로 조용히 대체하거나 파일을 자동 덮어쓰지 않는다. 정상 터미널의 setup 재실행으로 복구할 수 있어야 한다.
- 저장 뒤 일부 설치 디렉터리가 없어지면 최종 PATH에서 제외한다. 필요한 CLI나 npm 설치판 Codex가 사용하는 Node 등 실행 의존성이 누락된 경우 기존 해당 명령의 오류를 전달하고 설치 경로 확인·setup 재실행을 안내한다. 다른 provider나 버전으로 임의 전환하지 않는다.
- PATH 실패·갱신은 기존 DB·진행 작업·branch·worktree·session·실패 및 검증 이력을 초기화하지 않는다. 저장한 PATH는 source·runtime 소유권이나 Agent 쓰기 범위를 허용하는 근거가 아니다.
- 구현 시 README와 운영 안내에 정상 터미널의 setup 실행, 저장 위치·우선순위, 기존 설치의 미저장 동작, 설치 경로 변경 뒤 갱신과 실패 복구를 설명한다. 기본 `run.sh`·직접 CLI 실행을 계속 사용하며 crontab을 자동 편집하지 않는다.

## Acceptance Criteria

- Linux setup fixture의 PATH에 사용자 CLI 디렉터리, 중복, 빈 항목, `.`, 상대 경로, 존재하지 않는 경로와 공백·심볼릭 링크가 있는 디렉터리를 포함하면 순서와 유효 경로를 보존한 값이 `config/execution-path.txt`에 저장된다. 저장값에는 PATH 외의 환경 변수나 비밀값이 없다. 캐시와 `config` 아래의 다른 미추적 로컬 파일은 폴더 전체의 Git 제외 규칙에 적용되고 기존 `config.ini.example`의 추적은 유지된다. `.venv` 재생성은 캐시를 삭제하지 않는다.
- Git·gh 또는 지원 provider 모두를 찾을 수 없거나 수집·setup 준비·저장이 실패한 경우 setup은 실패를 알리고 기존 저장값을 보존한다. 재실행은 정상 PATH로 값을 갱신하며 중단·경쟁 읽기에서 기존 완전한 값 또는 새 완전한 값만 관측된다. 로그인·모델 호출은 발생하지 않는다.
- PATH가 `/usr/bin:/bin`뿐인 cron 모의 환경에서도 저장된 사용자 설치 디렉터리의 Git·gh·선택 provider와 필요한 Node가 발견된다. 저장한 경로가 현재 PATH보다 우선하고 중복·제외 항목이 없는 최종 값이 bootstrap의 첫 외부 명령부터 적용된다.
- 같은 source에서 `run.sh` 진입과 직접 CLI 진입, 기본 및 별도 `--config`, cwd·state_dir 변경은 같은 저장 원본을 사용한다. 다른 source는 자신의 저장값을 사용한다. runtime worktree 안의 다른 PATH 파일은 읽지 않는다.
- runtime 자식과 Agent의 인증·모델·지침·격리 확인, 실제 신규·재개·재시도 호출, 직접 Git subprocess 및 verify 자식은 같은 유효 PATH를 사용한다. 실행 중 저장 파일을 바꿔도 현재 실행의 PATH는 유지되고 다음 source 실행에만 반영된다.
- 캐시가 없는 기존 설치와 cron 모의 환경에서 현재 PATH에 명시된 사용자 CLI 디렉터리는 순서대로 보존되고 기본 시스템 후보가 뒤에 보완된다. setup 재실행 없이 bootstrap과 자식의 명령 탐색에 사용된다. 읽기 실패·빈 파일·손상은 Git이나 Agent 호출 전에 중단하며 자동 대체·덮어쓰기를 하지 않는다. 사라진 설치 경로와 누락된 실행 의존성의 진단 뒤 정상 setup 재실행으로 복구한다.
- 기존 가상환경 interpreter·자식 인자·lock·종료 코드와 provider 비밀값 필터·설정 홈·격리·권한 경계가 유지된다. PATH 변경은 기존 환경 근거 검사에 반영되지만 작업·session·DB 및 과거 기록을 삭제하지 않는다. macOS·Windows의 기존 setup과 PATH 처리에는 변경이 없다.
- 최소 범위 unit test는 임시 디렉터리·설정과 모의 외부 명령으로 저장·정리·우선순위·시점·상속·실패·보존을 확인한다. Linux에서 실제 외부 명령 탐색을 검증한다면 최소 PATH와 임시 실행 파일로 효과를 확인하고 실계정 Agent나 `loop_manager.py run`을 실행하지 않는다. 실제 cron 운영 검증은 수행 환경과 범위를 따로 기록하며 모의 검증을 운영 성공으로 간주하지 않는다.

## Product Impact

- Linux 자동 실행의 프로그램 탐색 환경은 setup에서 저장한 정상 PATH를 우선하며 source 진입부터 runtime·Agent·verify까지 한 실행 안에서 일관된다.
- 설치별 PATH는 Git에서 제외되는 source checkout의 `config` 아래에 다른 로컬 설정과 함께 보존하고 정상 setup 재실행으로 갱신한다. 기존 설정 예시는 계속 Git에서 관리한다. 캐시가 없으면 cron의 명시적 PATH를 포함한 현재 실행 환경과 시스템 후보로 실행할 수 있다.

## Non-Goals

- macOS·Windows의 PATH 저장·적용 지원 확대, 새 CLI 설정 명령이나 별도 PATH 관리 도구.
- crontab·사용자 셸 설정 자동 수정, 매 실행의 로그인 셸·`.bashrc`·`.profile` 실행, 파일시스템 전체 탐색과 버전 관리 도구의 최신 버전 자동 선택.
- 프로그램 자동 설치·로그인, 호출별 절대 경로 전환, provider 설정·인증 홈 정책 변경 또는 Agent 권한 완화.
- 설정·DB·작업·session 초기화, 저장 파일의 runtime 복제 또는 저장한 PATH를 근거로 한 검증 생략.

## Open Questions

- 없음.
