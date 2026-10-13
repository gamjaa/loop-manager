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

완료 모듈 수집 뒤 coordinator가 허용하는 모듈은 매번 acquire·예약·PID bind를 거쳐 연속 배정한다. 즉시 완료 report는 배정 중 다른 모듈의 결과가 준비되거나 배정이 거절된 뒤에도 대기 없이 수집한다. 진행이 없거나 배정이 거절되고 즉시 수집할 report도 없으면 50ms 대기한다. report가 있어도 살아 있는 자식의 종료 확인을 생략하거나 worker를 조기에 재사용하지 않는다.

같은 로그 부모 디렉터리의 가장 최근 진단에 현재 실행 범위·환경과 비교 가능한 완전한 모듈 시간이 있으면 독립 단계의 실행 큐만 긴 모듈 우선으로 정렬한다. 동률은 수집 순서다. 큐 재사용은 manifest·fixture 소유자·pattern·선택 상태·부분/전체 모드와 전체 단계 선언·Python·병렬도/조율·임시 경로·Python 경로·계측/bytecode 정책이 일치해야 한다. 성공·완결된 run의 모든 실행 모듈에서 유효한 단일 duration과 report identity·coverage를 확인하며 종료 불명 예약도 거부한다. 자료 부재·불완전·손상·다른 문맥은 전체 수집 순서로 실행한다. 시간 진단은 같은 실행 범위·부분/전체 모드·관측 환경·bytecode 정책·실행 context를 비교하되 context의 parallelism 값만 제외해 `1`과 `auto`의 비용 비교를 지원한다. 이 비교는 모든 context 필드가 일치해야 하는 큐 재사용 판정과 별개다. 새로운 시간 cache나 누락 duration을 만들지 않는다. serial·필수 명령 순서와 전체 discovery의 NOT_SELECTED는 유지하고, 과거 비용은 현재 실행의 성공·생략 근거로 사용하지 않는다.

적합성 unit test는 [test_verify_contract.py](../tests/test_verify_contract.py)와 기존 [test_verify_runtime.py](../tests/test_verify_runtime.py)의 fixture·native 결과·pool 경계를 사용한다. 선언·manifest 누락/중복/버전, run·단계·단위 identity, 부분 결과, 직렬 예산, 프로젝트 interpreter 전달, 필수 명령 종료 코드, 시작 실패·종료 불명·진단 보존을 확인한다. 합성 결과와 mock 통과는 실제 프로세스·OS 격리·언어 독립성의 근거가 아니다. 로컬 targeted 검증 환경은 Python 3.12/Linux이며 다른 Python 버전·Windows/macOS의 실제 실행, 동시 verify·timeout/cancel·성능 비교는 Manager의 후속 검증으로 확인한다.

## 영향 기반 부분 실행

Manager의 `[loop] verify_partial = true`는 자체 저장소의 일반 작업에서 부분 verify → 일반 선행 Reviewer → 실제 전체 verify를 선택한다. 기본값은 true이며 옵션 생략 시에도 사용한다. `[loop] verify_partial = false`로 끌 수 있다. 수동 제출·waiver·문서 draft·R-082 문서 변경 선행 Review는 기존 경로를 유지한다. 실제 Reviewer instruction/provider/model/effort·환경/정책 identity를 제공하는 검증된 관측 port가 없으면 부분 실행 전에 기존 전체 verify → Reviewer로 fallback한다. provider 조립은 G-262의 `provider_execution.observe`에 현재 준비 입력과 지정한 실제 invocation 조회를 연결한다. CLI·인증·지침·격리·완료 기록을 확인하지 못하면 fallback하며 정적 profile이나 격리 버전 이름으로 identity를 합성하지 않는다. G-263/G-264의 인증 전용 source 변경과 Claude의 안전 거부 해제는 이 연결의 완료를 뜻하지 않는다.

runner의 `--partial-base <commit>` 또는 Manager가 전달하는 `LOOP_MANAGER_VERIFY_PARTIAL_BASE`는 같은 전체 discovery manifest에서 선정한 회귀 검사 모듈만 실행한다. 검증 설정은 기존 `scripts/verify.json`을 사용한다. 지원하는 단계 배치는 처음에 하나의 unittest tests 단계와 그 뒤의 command 단계다. 전체 검증의 후속 필수 command는 나중의 전체 실행에서 수행한다. 일반 전체 실행의 Manager는 상속된 부분 실행 환경값을 제거한다.

선택기 버전 3은 기준/후보 커밋 양쪽의 모든 추적 Python 본문을 AST로 분석하고 변경 테스트와 직접·역방향 간접 참조를 수집 명세의 모듈로 정규화한다. 삭제·이름 변경·패키지 이동 전의 관계와 패키지 초기화의 암묵적 의존성도 포함한다. 공개 API·공통 지원 코드 변경은 확인한 사용 테스트를 확대한다. 파일명 유사도와 요청 파일만의 역방향 관계는 사용하지 않는다.

현재 해소하는 동적 참조는 고정된 절대 모듈 이름·문자열 결합 또는 리터럴 목록을 순회하는 import, 가져온 모듈의 고정 속성 조회, 고정 이름/자기 모듈의 registry 조회, Git/GitHub CLI의 고정 버전 조회와 고정된 Python `-m` 대상이다. 별칭·재바인딩·로더 함수의 다른 변수 전달·shell/추가 실행 옵션·대상 불명·registry 변경·이름공간/코드 실행은 보수적으로 처리한다. 원본 코드맵의 불확실성은 지우지 않으며 해소한 참조를 부분 그래프에 추가한다. 공통 진입점의 직접 재노출은 초기화 부작용 없는 선언·정적 별칭·속성 사용을 확인한 경우에만 원래 구현 모듈로 연결한다. 부작용·순환·동적 접근·모듈 객체 전달을 확인하지 못하면 기존 넓은 관계를 유지한다.

부분 선정은 영향 범위 전체의 증명 대신 Review 전 빠른 회귀 검사를 위한 우선순위다. 변경 테스트는 필수이며 직접 참조·가까운 간접 참조·실제 재노출 API 사용을 우선해 기본 24개 모듈을 선정한다. 변경 테스트는 예산을 초과해도 제외하지 않는다. 불확실성·고위험 공통 코드·문서 소비 불명 또는 관련 대상이 없으면 수집 명세의 `test_preliminary_review`, `test_managed_configuration`, `test_notification_events`를 공통 검사로 보완한다. 고정된 파일 읽기가 변경 문서·자료를 가리키면 그 소비자도 관련 후보로 추가한다. 자료·지침·설정의 비영향을 확인하지 못한 경우 그 한계를 남기며 전체 변경과 Review 입력을 유지한다.

전역 동적 참조·미해결 의존성·문서 병행 수정·공개 API·Git/SQLite/복구·패키지 초기화·루트 구현 진입점 변경과 선택 비율 50% 이상만으로 전체 경로로 전환하지 않는다. 검증 모듈·verify/unittest 스크립트·검증 실행 환경 선언/설정, `load_tests` 수집 계약 변경, 수집 명세의 소스 불일치 또는 그래프 읽기 실패는 전체 경로로 전환한다. 실행 가능한 대상이 없거나 변경 테스트 필수 대상이 전체 수집 명세를 차지해도 전환하며 부분 모드에서 전체 테스트를 중복 실행하지 않는다.

`partial-result.json`은 `kind: partial_verify`, run identity, PASS/FAIL/FALLBACK/ERROR, candidate/base, 선택 모듈/테스트 ID·이유, 전체 대비 규모, 알고리즘/설정/manifest identity, 시간과 진단 경로를 보존한다. `selection.analysis`는 문서 역할·내용/모드 식별 정보, 대상 확대·문서 소비 근거·남은 불확실성·관련 후보 중 미선택 모듈과 실행 예산을 구분한다. PASS는 선택 모듈의 성공만 뜻하며 영향 범위 전체의 성공으로 해석하지 않는다. Reviewer에게는 이 한계·선택 규모·불확실성 종류/수·미선택 수와 불완전한 표본임을 명시한 요약을 전달한다. 전체 결과는 로그와 저장된 부분 증거에 보존하고 요약에 선택 식별 정보와 진단 경로를 제공한다. 이전 알고리즘 또는 다른 수집 명세의 선택 결과는 거부한다. 미선택 manifest 모듈은 NOT_SELECTED이며 실행 완료나 skip을 합성하지 않는다. FALLBACK은 discovery 이후에도 테스트 모듈과 command를 실행하지 않는다. 기존 core·adapter가 fixture·skip·identity/필수 결과·serial/independent·guard·예약·자식 종료를 검사하며 timeout과 취소도 전체 검증과 같은 외부 프로세스 경계를 사용한다.

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
