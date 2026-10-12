# 공통 verify 연결과 adapter 계약

Loop Manager 자체 검증은 `scripts/verify.json`으로 공통 runner를 선택한다. Manager가 전체 verify를 Agent 밖에서 실행하며 Worker는 변경한 동작의 최소 unit test만 실행한다. 미연결 프로젝트는 기존 verify를 유지한다. 연결 파일의 오류·미지원 버전·adapter·실행 도구 부재는 실패다.

## 실행

자체 저장소에서는 기존 `scripts/verify.sh` 또는 `scripts/verify.ps1`을 사용한다. Manager는 자신의 Python과 절대 경로 runner, protocol version, session·timeout·조율 정보를 launcher에 전달한다. launcher가 선택한 프로젝트 Python으로 수집·테스트·compile을 실행하고, 자원 배정은 Manager runtime에서 수행한다. 프로젝트에 Manager 패키지나 설정·비밀값을 복사하지 않는다. venv interpreter의 symlink를 실제 시스템 executable로 바꾸지 않아 프로젝트 의존성 환경을 유지한다.

단독 호출은 설치해 둔 Loop Manager runtime의 위치와 Python을 명시한다. 자동 설치나 새로운 PATH 탐색은 하지 않는다. 선택된 프로젝트 루트에서 다음과 같이 호출한다.

```sh
/manager/runtime/python /manager/runtime/loop_manager_internal/verification/entrypoint.py --version
/manager/runtime/python /manager/runtime/loop_manager_internal/verification/entrypoint.py --protocol-version 1 --project-python /project/.venv/bin/python --parallelism 1 --timeout 600
```

Windows에서도 Python과 `entrypoint.py`의 절대 경로로 같은 인수를 전달한다. Manager 호출과 단독 호출은 같은 선언·수집·단계·결과 검증을 사용한다. 단독 호출은 로컬 자원 관측만 사용하고, 다른 Manager나 단독 실행과 예약을 공유하지 않는다. timeout은 수집·대기·테스트·명령·정리를 포함한다. 구형 Manager가 조율 session만 전달하고 시간 상한을 전달하지 않으면 runner·명령·자식 대기는 바깥 Manager의 기존 상한을 따르며 별도의 600초 상한을 만들지 않는다. 명시된 상한은 그대로 적용하고 단독 실행의 기본 상한은 600초(자체 설정이 있으면 해당 값)다. 자식 종료 불명은 성공으로 처리하지 않고 예약 진단을 보존한다.

## 선언 version 1

실제 선언 예시는 [scripts/verify.json](../scripts/verify.json)이다.

| 필드 | 계약 |
|---|---|
| `version` | 정수 `1`; 다른 버전과 중복 JSON key는 거부 |
| `adapter` | 정적 registry의 `unittest`; 자동 도구 탐색 없음 |
| `environment.python` | worktree 상대 interpreter 경로 또는 `launcher`; 후자는 launcher/명시적 `--project-python`이 선택한 interpreter |
| `stages` | 중복 없는 `id`, `kind`, worktree 안의 `cwd`를 가진 순서 있는 필수 단계 |
| tests 단계 | `collection.directory`, `collection.pattern`, `execution` (`independent` 또는 `serial`) |
| command 단계 | 비어 있지 않은 `argv` 배열; shell 문자열 해석 없음 |

command의 독립 인수 `{python}`과 `{runner}`만 각각 프로젝트 interpreter와 Manager runtime 진입점으로 치환한다. 일반 문자열의 보간이나 범용 DAG·resource lock 설정은 제공하지 않는다. command 단계는 직렬이며 테스트별 결과를 만들지 않는다. 자체 compile은 명시된 `loop_manager.py`와 `loop_manager_internal/**/*.py` 전체를 프로젝트 Python으로 검사한다.

수집한 manifest는 version·run·adapter identity, 프로젝트 Python 버전과 각 단계의 단위 ID·argv·cwd·실행 조건·테스트 ID를 포함한다. 정적 선언에 테스트 ID를 쓰지 않는다. 단계 실패 뒤의 단계는 시작하지 않는다. `auto`와 `1`은 같은 필수 범위를 사용한다.

## unittest 지원과 적합성

adapter version 1의 범위는 Python 3.11–3.14 stdlib unittest이다. [공식 unittest 계약](https://docs.python.org/3.12/library/unittest.html)의 discovery·`load_tests`, module/class fixture, skip·expected failure·unexpected success·subtest를 유지한다. 모듈별 독립 프로세스를 배정하고 도구 내부 병렬은 사용하지 않는다. module 사이에 공유 fixture·DB·파일·포트·순서 의존이 있으면 tests 단계를 `serial`로 선언한다. 수집 시 결정한 테스트 ID·fixture 소유자를 실행 시 다시 대조한다. fixture skip은 원래 소유자 근거로 기록하고 실행하지 않은 테스트의 완료를 만들지 않는다.

공통 core는 자원 배정·예약·프로세스 종료 확인을 담당하고, unittest adapter는 프로젝트 interpreter에서의 수집·재수집·reporter·fixture 의미를 담당한다. 테스트 import와 자식의 Python 경로는 후보 worktree를 선택하며 Manager runtime 소스가 후보 구현을 대신하지 않는다. 실제 Agent CLI/API 호출은 기존 guard로 차단하고 테스트에서는 mock/stub을 사용한다. Python 가드는 runpy·일반 import가 프로세스 설치 근거를 공유하며 확인된 audit hook과 Popen wrapper를 한 번 설치한다. 부분 실패는 재시도하고 자식 sitecustomize 설치 실패는 고정 진단 후 종료한다. wrapper가 준비한 동일 환경은 해당 호출의 audit에서 재사용하되 변경된 환경과 명령/API는 매 실행 경계에서 다시 검사한다. 명시/상속 환경의 조율 인증값 제외·부모/호출자 환경 보존·중첩 Python 보호·후보 package 선택·bytecode 정책과 Windows 대소문자/POSIX bytes 별칭을 유지한다. 이 Python guard가 임의 언어의 모든 subprocess를 차단한다고 보장하지 않는다.

적합성 unit test는 [test_verify_contract.py](../tests/test_verify_contract.py)와 기존 [test_verify_runtime.py](../tests/test_verify_runtime.py)의 fixture·native 결과·pool 경계를 사용한다. 선언·manifest 누락/중복/버전, run·단계·단위 identity, 부분 결과, 직렬 예산, 프로젝트 interpreter 전달, 필수 명령 종료 코드, 시작 실패·종료 불명·진단 보존을 확인한다. 합성 결과와 mock 통과는 실제 프로세스·OS 격리·언어 독립성의 근거가 아니다. 로컬 targeted 검증 환경은 Python 3.12/Linux이며 다른 Python 버전·Windows/macOS의 실제 실행, 동시 verify·timeout/cancel·성능 비교는 Manager의 후속 검증으로 확인한다.

## 영향 기반 부분 실행

Manager의 `[loop] verify_partial = true`는 자체 저장소의 일반 작업에서 부분 verify → 일반 선행 Reviewer → 실제 전체 verify를 선택한다. 기본값은 true이며 옵션 생략 시에도 사용한다. `[loop] verify_partial = false`로 끌 수 있다. 수동 제출·waiver·문서 draft·R-082 문서 변경 선행 Review는 기존 경로를 유지한다. 실제 Reviewer instruction/provider/model/effort·환경/정책 identity를 제공하는 검증된 관측 port가 없으면 부분 실행 전에 기존 전체 verify → Reviewer로 fallback한다. 현재 provider 조립은 이 port를 제공하지 않으며 G-262/263/264와 연계한 실제 환경 관측이 필요하다. 정적 profile이나 격리 버전 이름으로 그 identity를 합성하지 않는다.

runner의 `--partial-base <commit>` 또는 Manager가 전달하는 `LOOP_MANAGER_VERIFY_PARTIAL_BASE`는 같은 전체 discovery manifest에서 영향받은 모듈만 실행한다. 검증 설정은 기존 `scripts/verify.json`을 사용한다. 지원하는 단계 배치는 처음에 하나의 unittest tests 단계와 그 뒤의 command 단계다. 전체 검증의 후속 필수 command는 나중의 전체 실행에서 수행한다. 일반 전체 실행의 Manager는 상속된 부분 실행 환경값을 제거한다.

선택기는 base/HEAD 양쪽의 모든 tracked Python body를 AST로 분석하고 변경 테스트와 직접·역방향 간접 소비자를 manifest 모듈로 정규화한다. 삭제·rename·패키지 이동의 과거 그래프도 포함한다. 어느 쪽 tree라도 동적 참조·reflection·미해결 참조가 있거나, 공유 support·고위험 공통 코드·공개 계약·패키지 초기화·진입점·비Python 입력이 변경되면 fallback한다. 테스트 모듈 수 또는 테스트 ID 수의 절반 이상을 고르거나 대상이 없어도 fallback한다. 이 규칙은 보수적인 실행 선택 기준이며 성능 성공 기준이 아니다. `query_worktree()`의 요청 파일만의 incoming 관계를 완전한 그래프로 사용하지 않는다.

`partial-result.json`은 `kind: partial_verify`, run identity, PASS/FAIL/FALLBACK/ERROR, candidate/base, 선택 모듈/테스트 ID·이유, 전체 대비 규모, 알고리즘/설정/manifest identity, 시간과 진단 경로를 보존한다. 미선택 manifest 모듈은 NOT_SELECTED이며 실행 완료나 skip을 합성하지 않는다. FALLBACK은 discovery 이후에도 테스트 모듈과 command를 실행하지 않는다. 기존 core·adapter가 fixture·skip·identity/필수 결과·serial/independent·guard·예약·자식 종료를 검사하며 timeout과 취소도 전체 검증과 같은 외부 프로세스 경계를 사용한다.

부분 PASS는 전체 verify token/receipt와 분리한다. 일반 선행 Review의 최종 승격도 모든 후보·검토 입력·환경/정책과 해당 선행 승인 뒤 실행한 실제 전체 verify receipt ID가 확인될 때만 가능하며 게시 직전 재확인한다. 관측 port는 현재 준비된 입력뿐 아니라 `invocation_id`가 주어진 경우 그 실제 Reviewer 호출의 고정 instruction/profile·실행 환경/정책을 증명해야 한다. 현재 설정을 과거 호출의 identity로 소급 채택하지 않는다. 실제 호출 identity·사전/사후 현재 identity가 모두 같아야 하며 호출 증거 부재·실패·변경·중단은 기존 경로로 처리한다. 이 경로는 G-183의 최종 승인 재사용, G-241의 전체 검증 생략, G-250 병렬 최적화를 구현하지 않는다.

실행 자료는 verify 로그와 SQLite `preliminary_reviews`, 기존 Agent invocation/사용량 ledger에 결합된다. 부분·전체 시간, 모듈/테스트 규모, 부분 PASS 이후 전체 FAIL, 선행 FAIL의 전체 verify 미호출과 fallback 사유를 구분한다. 선행 호출 purpose는 `preliminary_review`이며 실제 실행/토큰과 재사용을 구분한다. 실제 운영 기록 없이 절감 시간·토큰 또는 완료 시간 개선률을 만들지 않는다.

## adapter 추가 경계

필요한 프로젝트의 계획에서 도구의 공식 수집·native reporter·지원 버전·독립 단위·내부 병렬 제어 근거를 먼저 확인한다. 정적 registry metadata와 adapter 구현을 추가하고 공통 core의 자원 계산·예약·정리 코드를 재사용한다. adapter는 수집된 고유 단위/테스트 identity를 제공하며 run·단계·단위 version을 결합한 결과의 완결성과 native 실패·오류·skip 의미를 검증한다. 실행 중 등록되는 테스트가 있는 도구는 예상 범위와 등록된 결과의 관계를 별도 검증해야 한다. 자유 형식 stdout 추측이나 command 종료 코드로 테스트 범위 검증을 대신하지 않는다.

현재 비Python adapter와 외부 프로젝트 연결은 구현하지 않는다. 미지원 도구는 기존 verify를 유지하며 필요한 adapter·연결은 해당 프로젝트의 계획으로 진행한다. R-067의 전체 완료에는 두 번째 실제 언어·도구와 예제, Windows/Linux 격리 및 같은 revision·환경의 직렬/auto·동시 실행 실측이 남아 있다.

## 진단과 입력

run별 `manifest.json`, 단계별 수집 요청/결과·단위 결과·worker 로그와 JSONL 이벤트, 전체 `summary.json`을 보존한다. 이벤트는 version·run·단계·단위/테스트 identity를 결합하며 단계별 로그도 전체 진단에 포함한다. 실패 피드백의 `[VERIFY_DIAGNOSTIC]` 경로로 원본 진단을 찾을 수 있다. 예약은 session·단위·PID와 프로세스 시작 identity에 결합한다. 종료가 확인된 자식/미시작 예약만 회수하고 종료 불명은 별도 journal에 보존한다.

자체 `scripts/verify-inputs.json`은 `loop_manager.py`, `loop_manager_internal/**`, `tests/**`,
`requirements.txt`, `scripts/verify.json`, 두 verify 스크립트, 두 copy 스크립트와 소비되는
`run.sh`·`run.ps1`과 실행 코드 `.agents/skills/specification-docs/scripts/next_requirement_id.py`를
관측한다. 복사 회귀는 이 조회 도구와 `loop_manager_internal/catalog/frontmatter.py`의 실제 bytes를
샘플 source에 배치하고 배포 결과·조회 동작을 확인한다. 선언 파일은 실제 adapter·interpreter·수집·필수 compile 단계를
정하므로 목록에 포함한다. `tests/**` 안의 고정 샘플이 문서·Skill·설정 예제 원본을 대체하며,
실제 copy 스크립트는 이 샘플 source에서 실행해 결과 bytes·경로·보존·덮어쓰기 거부를 확인한다.
`AGENTS.md`, README, docs와 `.agents`의 prose, `config/config.ini.example`, setup·ignore·attributes는
자체 verify의 prose/예제 입력에서 제외한다. 이 범위는 문서·예제의 품질 검증을 뜻하지 않으며
필수 unittest 수집과 전체 compile을 유지한다. 이 정적 목록만으로 외부 interpreter·의존성·runtime·
Git 또는 동적 입력의 완전성을 주장하지 않으며, 공통 runner 연결의 성공 재사용 관측은 계속 unknown이다.
