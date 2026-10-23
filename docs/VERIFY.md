# 공통 verify 연결과 adapter 계약

Loop Manager 자체 검증은 `scripts/verify.json`으로 공통 runner를 선택한다. Manager가 전체 verify를 Agent 밖에서 실행하며 Worker는 변경한 동작의 최소 unit test만 실행한다. 미연결 프로젝트는 기존 verify를 유지한다. 연결 파일의 오류·미지원 버전·adapter·실행 도구 부재는 실패다.

## 실행

자체 저장소에서는 기존 `scripts/verify.sh` 또는 `scripts/verify.ps1`을 사용한다. Manager는 자신의 Python과 절대 경로 runner, protocol version, session·timeout·조율 정보를 launcher에 전달한다. launcher가 선택한 프로젝트 Python으로 수집·테스트·compile을 실행하고, 자원 배정은 Manager runtime에서 수행한다. 프로젝트에 Manager 패키지나 설정·비밀값을 복사하지 않는다. venv interpreter의 symlink를 실제 시스템 executable로 바꾸지 않아 프로젝트 의존성 환경을 유지한다.

단독 호출은 설치해 둔 Loop Manager runtime의 위치와 Python을 명시한다. 자동 설치나 새로운 PATH 탐색은 하지 않는다. 선택된 프로젝트 루트에서 다음과 같이 호출한다.

```sh
/manager/runtime/python /manager/runtime/loop_manager_internal/verification/entrypoint.py --version
/manager/runtime/python /manager/runtime/loop_manager_internal/verification/entrypoint.py --protocol-version 1 --project-python /project/.venv/bin/python --parallelism 1 --timeout 600
```

Windows에서도 Python과 `entrypoint.py`의 절대 경로로 같은 인수를 전달한다. Manager 호출과 단독 호출은 같은 선언·수집·단계·결과 검증을 사용한다. 단독 호출은 로컬 자원 관측만 사용하고, 다른 Manager나 단독 실행과 예약을 공유하지 않는다. timeout은 준비·수집·테스트·명령·결과 처리·정리의 실제 경과 시간을 포함하며 확인된 순번·자원 배정 대기는 제외한다. 구형 Manager가 조율 session만 전달하고 시간 상한을 전달하지 않으면 runner·명령·자식 대기는 바깥 Manager의 기존 상한을 따르며 별도의 600초 상한을 만들지 않는다. 명시된 상한은 그대로 적용하고 단독 실행의 기본 상한은 600초(자체 설정이 있으면 해당 값)다. 자식 종료 불명은 성공으로 처리하지 않고 예약 진단을 보존한다.

## 순번 대기와 시간 예산

Manager의 verify 호출 대기는 `verify_concurrency` 자리 배정 뒤 시간 측정을 시작한다. runner의 내부 자원 대기는 coordinator가 해당 session에 실행 중이거나 종료 불명인 자식이 없고 정상 heartbeat가 이어지는 구간만 확인해 누계로 남긴다. 다른 자식이 실행 중이거나 runner가 멈춘 구간은 제외하지 않는다. 부모의 경과 시간에서 확인된 대기만 빼며 병렬 자식 시간을 더하지 않는다.

`LOOP_MANAGER_VERIFY_BUDGET_DIR`는 같은 검증의 Manager·launcher·runner가 누계를 공유하는 내부 환경값이다. 단계 사이에서 예산을 초기화하지 않고, 테스트 worker에는 이 조율 환경을 제거해 검증 대상이 부모 시간을 조작하지 않도록 한다. Windows·WSL 두 단계도 같은 실제 시간 예산을 사용하며 Windows Job 종료와 Linux 자식 종료·복사본 정리 근거를 함께 확인한다. WSL 종료가 불명이면 lifecycle receipt와 소유 예약을 보존하고 실행 자리를 임의로 회수하지 않는다.

## 선언 version 1

실제 선언 예시는 [scripts/verify.json](../scripts/verify.json)이다.

| 필드 | 계약 |
|---|---|
| `version` | 정수 `1`; 다른 버전과 중복 JSON key는 거부 |
| `adapter` | 정적 registry의 `unittest` 또는 `perl-tap`; 자동 도구 탐색 없음 |
| `environment.python` | unittest 전용: worktree 상대 interpreter 경로 또는 `launcher`; 후자는 launcher/명시적 `--project-python`이 선택한 interpreter |
| `environment.perl` | perl-tap 전용: 존재하는 Perl executable의 절대 경로; Manager Python과 별도로 선택 |
| `stages` | 중복 없는 `id`, `kind`, worktree 안의 `cwd`를 가진 순서 있는 필수 단계 |
| tests 단계 | `collection.directory`, `collection.pattern`, `execution` (`independent` 또는 `serial`) |
| command 단계 | 비어 있지 않은 `argv` 배열; shell 문자열 해석 없음 |

command의 독립 인수 `{python}`과 `{runner}`는 각각 프로젝트 interpreter와 Manager runtime 진입점으로 치환한다. perl-tap command의 첫 인수 `{perl}`는 선언한 Perl과 검증 guard preload 인수로 치환한다. Perl compile은 이 형식을 사용해 BEGIN 단계에도 guard를 적용한다. 일반 문자열의 보간이나 범용 DAG·resource lock 설정은 제공하지 않는다. command 단계는 직렬이며 테스트별 결과를 만들지 않는다. 자체 compile은 명시된 `loop_manager.py`와 `loop_manager_internal/**/*.py` 전체를 프로젝트 Python으로 검사한다.

수집한 manifest는 version·run·adapter identity, 프로젝트 도구 버전과 각 단계의 단위 ID·argv·cwd·실행 조건·테스트 ID를 포함한다. Perl 단계는 Manager Python worker argv와 별도로 native 도구 버전을 `tool`에 기록한다. 정적 선언에 테스트 ID를 쓰지 않는다. 단계 실패 뒤의 단계는 시작하지 않는다. `auto`와 `1`은 같은 필수 범위를 사용한다.

## unittest 지원과 적합성

adapter version 1의 범위는 Python 3.11–3.14 stdlib unittest이다. [공식 unittest 계약](https://docs.python.org/3.12/library/unittest.html)의 discovery·`load_tests`, module/class fixture, skip·expected failure·unexpected success·subtest를 유지한다. 모듈별 독립 프로세스를 배정하고 도구 내부 병렬은 사용하지 않는다. module 사이에 공유 fixture·DB·파일·포트·순서 의존이 있으면 tests 단계를 `serial`로 선언한다. 수집 시 결정한 테스트 ID·fixture 소유자를 실행 시 다시 대조한다. fixture skip은 원래 소유자 근거로 기록하고 실행하지 않은 테스트의 완료를 만들지 않는다.

공통 core는 자원 배정·예약·프로세스 종료 확인을 담당하고, unittest adapter는 프로젝트 interpreter에서의 수집·재수집·reporter·fixture 의미를 담당한다. 테스트 import와 자식의 Python 경로는 후보 worktree를 선택하며 Manager runtime 소스가 후보 구현을 대신하지 않는다. 실제 Agent CLI/API 호출은 기존 guard로 차단하고 테스트에서는 mock/stub을 사용한다. Python 가드는 runpy·일반 import가 프로세스 설치 근거를 공유하며 확인된 audit hook과 Popen wrapper를 한 번 설치한다. 부분 실패는 재시도하고 자식 sitecustomize 설치 실패는 고정 진단 후 종료한다. wrapper가 준비한 동일 환경은 해당 호출의 audit에서 재사용하되 변경된 환경과 명령/API는 매 실행 경계에서 다시 검사한다. 명시/상속 환경의 조율 인증값 제외·부모/호출자 환경 보존·중첩 Python 보호·후보 package 선택·bytecode 정책과 Windows 대소문자/POSIX bytes 별칭을 유지한다. 이 Python guard가 임의 언어의 모든 subprocess를 차단한다고 보장하지 않는다.

완료 모듈 수집 뒤 coordinator가 허용하는 모듈은 매번 acquire·예약·PID bind를 거쳐 연속 배정한다. 즉시 완료 report는 배정 중 다른 모듈의 결과가 준비되거나 배정이 거절된 뒤에도 대기 없이 수집한다. 진행이 없거나 배정이 거절되고 즉시 수집할 report도 없으면 50ms 대기한다. report가 있어도 살아 있는 자식의 종료 확인을 생략하거나 worker를 조기에 재사용하지 않는다.

같은 로그 부모 디렉터리의 가장 최근 진단에 현재 실행 범위·환경과 비교 가능한 완전한 모듈 시간이 있으면 독립 단계의 실행 큐만 긴 모듈 우선으로 정렬한다. 동률은 수집 순서다. 큐 재사용은 manifest·fixture 소유자·pattern·선택 상태·부분/전체 모드와 전체 단계 선언·Python·병렬도/조율·임시 경로·Python 경로·계측/bytecode 정책이 일치해야 한다. 성공·완결된 run의 모든 실행 모듈에서 유효한 단일 duration과 report identity·coverage를 확인하며 종료 불명 예약도 거부한다. 자료 부재·불완전·손상·다른 문맥은 전체 수집 순서로 실행한다. 시간 진단은 같은 실행 범위·부분/전체 모드·관측 환경·bytecode 정책·실행 context를 비교하되 context의 parallelism 값만 제외해 `1`과 `auto`의 비용 비교를 지원한다. 이 비교는 모든 context 필드가 일치해야 하는 큐 재사용 판정과 별개다. 새로운 시간 cache나 누락 duration을 만들지 않는다. serial·필수 명령 순서와 전체 discovery의 NOT_SELECTED는 유지하고, 과거 비용은 현재 실행의 성공·생략 근거로 사용하지 않는다.

적합성 unit test는 [test_verify_contract.py](../tests/test_verify_contract.py)와 기존 [test_verify_runtime.py](../tests/test_verify_runtime.py)의 fixture·native 결과·pool 경계를 사용한다. 선언·manifest 누락/중복/버전, run·단계·단위 identity, 부분 결과, 직렬 예산, 프로젝트 interpreter 전달, 필수 명령 종료 코드, 시작 실패·종료 불명·진단 보존을 확인한다. 합성 결과와 mock 통과는 실제 프로세스·OS 격리·언어 독립성의 근거가 아니다. 로컬 targeted 검증 환경은 Python 3.12/Linux이며 다른 Python 버전·Windows/macOS의 실제 실행, 동시 verify·timeout/cancel·성능 비교는 Manager의 후속 검증으로 확인한다.

## Perl TAP 지원과 두 언어 예제

`perl-tap` version 1은 구현 환경에서 확인한 Perl 5.38.2, Test::More 1.302194,
TAP::Parser 3.44, JSON::PP의 Linux native 실행을 지원한다. 다른 버전·도구 부재는 probe 실패이며 skip이나 기존
verify로 전환하지 않는다. 공식 근거는 [Test::More](https://perldoc.perl.org/5.38.2/Test::More)의
plan·done_testing·skip·TODO·subtest와 [TAP::Parser](https://perldoc.perl.org/5.38.2/TAP::Parser)의
argv 실행·native result·plan·parse_errors·exit·wait·spool 계약이다. 자동 설치는 제공하지 않는다.

수집은 collection directory 아래 pattern에 맞는 파일을 정렬해 재귀 조회한다. 각 worktree 상대
파일 ID가 하나의 suite test ID이자 독립 프로세스 단위다. assertion을 미리 실행하거나 가짜 assertion
목록을 만들지 않는다. 실행 전 파일 목록을 다시 대조하며 실행 중 native TAP assertion의 번호와
plan을 대조한다. `파일 ID#번호` 결과는 해당 suite에 속하고, 연속 번호·native plan·parser 오류·
종료 코드·signal을 모두 확인해야 suite가 완결된다. 파일 전체 skip은 native skip-all 근거로
완결되며, fixture 오류·bailout·plan 누락·중복/누락 assertion·비정상 종료는 성공이 아니다.
[TAP 중단 계약](https://testanything.org/tap-version-13-specification.html#bail-out)에 따라
완료된 plan 뒤의 bailout도 종료 코드 0이나 parser의 정상 plan 판정과 관계없이 완료/PASS를 거부한다.
Test::More의 TODO 성공/실패 의미와 nested subtest의 부모 결과를 유지한다. nested assertion은
원본 spool에 보존하고 별도의 최상위 수집 ID로 합성하지 않는다.

Python worker IPC와 공통 core의 acquire·PID bind·idle·종료 후 release를 재사용한다. Perl
reporter가 테스트 subprocess를 기다린 뒤에만 단위 report를 만든다. serial 선언은 worker 하나를
재사용하고 independent는 파일별 프로세스를 배정한다. native JSON Lines는 `module-N.native.ndjson`에, 공통 EventWriter 이벤트는 `*.jsonl`에
저장한다. 진단 집계는 공통 이벤트만 읽으며 native 원본을 공통 이벤트로 해석하지 않는다.
native 로그는 즉시 flush하고 TAP spool도 flush하므로 중단 전 결과·관측 위치를 보존한다. 파일 suite의 시작·소요 시간은 공통 phase에,
assertion 결과의 reporter 관측 elapsed는 native JSONL에 기록한다. 관측 elapsed를 assertion
본문의 정밀 실행 시간으로 주장하지 않는다. 강제 종료·부분 JSONL에는 완료를 합성하지 않으며
기존 외부 timeout/프로세스 경계와 종료 불명 예약 진단을 사용한다. Perl의 AST 부분 선정과 과거
모듈 시간 기반 큐 재사용은 지원하지 않고 부분 요청은 명시적 FALLBACK이다.

Perl guard는 테스트를 compile하기 전에 preload하며 일반 Perl의 system·exec·readpipe·pipe open,
fork·socket·connect를 거부한다. 따라서 실제 Agent CLI/API와 내부 fork 병렬은 mock/stub으로
대체해야 한다. [공식 CORE::GLOBAL override 계약](https://perldoc.perl.org/5.38.2/perlsub)에
기반한 신뢰된 테스트 경계다. 명시적 CORE 호출·XS/FFI·ithreads·guard를 바꾸는 preload는
지원하지 않으며 악의적인 코드의 OS sandbox라고 주장하지 않는다. Python guard와 별도다.

예제 루트는 [Python](../tests/support/verify_examples/python/scripts/verify.json)과
[Perl](../tests/support/verify_examples/perl/scripts/verify.json)이다. 둘 다 compile → tests의
필수 순서와 fixture·skip·expected failure/TODO 예제를 제공하며 같은 version 1 core를 사용한다.
Python 예제는 launcher가 선택한 Python 3.11–3.14를, Perl 예제는 `/usr/bin/perl`을 명시한다.
다른 설치 위치에서는 environment.perl을 실제 절대 경로로 바꾼다. 샘플을 프로젝트로 복사할 때
scripts와 tests를 함께 복사한다. 단독 실행은 해당 예제 루트에서 다음처럼 호출한다.

```sh
export LOOP_MANAGER_VERIFY_RUNTIME_PYTHON=/manager/runtime/python
export LOOP_MANAGER_VERIFY_RUNNER=/manager/runtime/loop_manager_internal/verification/entrypoint.py
# Python 프로젝트 의존성 환경이 다르면 절대 경로로 지정한다.
export LOOP_MANAGER_EXAMPLE_PYTHON=/project/.venv/bin/python
bash scripts/verify.sh
```

Windows launcher는 같은 환경값으로 `scripts/verify.ps1`을 호출한다. Perl의 Windows native 실행은
이번 지원 근거에 포함되지 않는다. Manager에는 같은 예제 project root와 verify launcher를
연결한다. Manager가 runtime·protocol·session·coordinator·timeout을 전달하며 단독 호출과 같은
선언과 결과 validator를 사용한다. 자체 검증의 native Perl case와 Perl 예제 통합 회귀는
Linux 지원 범위에서 실행하며 테스트 준비가 프로젝트 PATH의 실제 Perl 절대 경로를 선언에 넣는다.
Linux에서는 위 설치 도구의 부재·버전 불일치를 성공/skip으로 숨기지 않는다. Windows는 미지원
Perl native case만 명시적으로 skip하고 Python 예제와 플랫폼 독립 계약·IPC 검사를 유지한다.
순수 계약/IPC 검사는 임시 도구 파일과 mock을 사용하며 native 지원 근거로 취급하지 않는다.
이 테스트 수집 구분은 명시적으로 연결한 프로젝트의 도구 부재 실패 계약을 바꾸지 않는다.
예제 Manager 호출의 shell 선택은 기존 verify_arguments의 pwsh/powershell 경로를 따른다.

[test_verify_perl_adapter.py](../tests/test_verify_perl_adapter.py)는 native assertion·plan·skip·TODO·
subtest·오류, 수집/결과 identity·범위·부분 결과·통신 실패·guard·serial 예산을 검사한다.
[test_verify_language_examples.py](../tests/test_verify_language_examples.py)는 실제 두 예제의
단독/Manager 성공·실패와 실제 조율 모드·session/run identity·공통 manifest·예약 회수를 확인한다.
단독/Manager 호출 fixture는 guard가 자식 환경에서 제거한 테스트 예산 경로를 임시 파일로
runner 진입점에 전달하며 Manager 호출에는 해당 fixture의 session도 전달한다.
같은 wrapper에서 중첩 runner의 자원 관측도 고정해 호스트 부하와 분리한다.
프로젝트 test worker의 인증값 제거와 Agent guard는 유지한다.
이 전달 계약은 subprocess를 mock한 unit test로 확인하며 native 통합 회귀는 Worker가 실행하지 않는다.
로컬 native targeted 근거는 Linux/Perl 5.38.2이며 실제 강제 종료,
Windows/Linux 격리와 동시 실행 실측은 전체 verify·후속 Manager 검증으로 남는다.

## 영향 기반 부분 실행

Manager의 `[loop] verify_partial = true`는 자체 저장소의 일반 작업에서 부분 verify → 일반 선행 Reviewer → 실제 전체 verify를 선택한다. 기본값은 true이며 옵션 생략 시에도 사용한다. `[loop] verify_partial = false`로 끌 수 있다. 수동 제출·waiver·문서 draft·R-082 문서 변경 선행 Review는 기존 경로를 유지한다. 실제 Reviewer instruction/provider/model/effort·환경/정책 identity를 제공하는 검증된 관측 port가 없으면 부분 실행 전에 기존 전체 verify → Reviewer로 fallback한다. provider 조립은 G-262의 `provider_execution.observe`에 현재 준비 입력과 지정한 실제 invocation 조회를 연결한다. CLI·인증·지침·격리·완료 기록을 확인하지 못하면 fallback하며 정적 profile이나 격리 버전 이름으로 identity를 합성하지 않는다. G-263/G-264의 인증 전용 source 변경과 Claude의 안전 거부 해제는 이 연결의 완료를 뜻하지 않는다.

runner의 `--partial-base <commit>` 또는 Manager가 전달하는 `LOOP_MANAGER_VERIFY_PARTIAL_BASE`는 같은 전체 discovery manifest에서 선정한 회귀 검사 모듈만 실행한다. 검증 설정은 기존 `scripts/verify.json`을 사용한다. 지원하는 단계 배치는 처음에 하나의 unittest tests 단계와 그 뒤의 command 단계다. 전체 검증의 후속 필수 command는 나중의 전체 실행에서 수행한다. 일반 전체 실행의 Manager는 상속된 부분 실행 환경값을 제거한다.

선택기 버전 4는 기준/후보 커밋 양쪽의 모든 추적 Python 본문을 AST로 분석하고 변경 테스트와 직접·역방향 간접 참조를 수집 명세의 모듈로 정규화한다. 삭제·이름 변경·패키지 이동 전의 관계와 패키지 초기화의 암묵적 의존성도 포함한다. 공개 API·공통 지원 코드 변경은 확인한 사용 테스트를 확대한다. 파일명 유사도와 요청 파일만의 역방향 관계는 사용하지 않는다. `load_tests`의 직접·간접 입력에 범위를 확정할 수 없는 파일 조회가 있으면 Python 파일 추가·삭제·이동 또는 해당 수집 입력 변경을 `uncertain_collection_input`으로 기록하고 실행 없이 전체 경로로 전환한다. 일반 구현 본문 변경이나 수집 경계와 무관한 불확실성은 이 전환 사유가 아니다.

현재 해소하는 동적 참조는 고정된 절대 모듈 이름·문자열 결합 또는 리터럴 목록을 순회하는 import, 가져온 모듈의 고정 속성 조회, 고정 이름/자기 모듈의 registry 조회, Git/GitHub CLI의 고정 버전 조회와 고정된 Python `-m` 대상이다. 별칭·재바인딩·로더 함수의 다른 변수 전달·shell/추가 실행 옵션·대상 불명·registry 변경·이름공간/코드 실행은 보수적으로 처리한다. 원본 코드맵의 불확실성은 지우지 않으며 해소한 참조를 부분 그래프에 추가한다. 공통 진입점의 직접 재노출은 초기화 부작용 없는 선언·정적 별칭·속성 사용을 확인한 경우에만 원래 구현 모듈로 연결한다. 부작용·순환·동적 접근·모듈 객체 전달을 확인하지 못하면 기존 넓은 관계를 유지한다.

부분 선정은 영향 범위 전체의 증명 대신 Review 전 빠른 회귀 검사를 위한 우선순위다. 변경 테스트는 필수이며 직접 참조·가까운 간접 참조·실제 재노출 API 사용을 우선해 기본 24개 모듈을 선정한다. 변경 테스트는 예산을 초과해도 제외하지 않는다. 불확실성·고위험 공통 코드·문서 소비 불명 또는 관련 대상이 없으면 수집 명세의 `test_preliminary_review`, `test_managed_configuration`, `test_notification_events`를 공통 검사로 보완한다. 고정된 파일 읽기가 변경 문서·자료를 가리키면 그 소비자도 관련 후보로 추가한다. 자료·지침·설정의 비영향을 확인하지 못한 경우 그 한계를 남기며 전체 변경과 Review 입력을 유지한다.

전역 동적 참조·미해결 의존성·문서 병행 수정·공개 API·Git/SQLite/복구·패키지 초기화·루트 구현 진입점 변경과 선택 비율 50% 이상만으로 전체 경로로 전환하지 않는다. 검증 모듈·verify/unittest 스크립트·검증 실행 환경 선언/설정과 공통 core·runner·Manager adapter가 사용하는 `execution/verify_process.py`(실행·timeout·프로세스 트리 종료) 및 `execution/windows_job.py`(Job 격리·자식 종료) 구현, `load_tests` 수집 계약 변경, 수집 명세의 소스 불일치 또는 그래프 읽기 실패는 전체 경로로 전환한다. 프로세스 관련 이름이나 `execution/`의 다른 파일은 이 조건에 포함하지 않는다. 실행 가능한 대상이 없거나 변경 테스트 필수 대상이 전체 수집 명세를 차지해도 전환하며 부분 모드에서 전체 테스트를 중복 실행하지 않는다.

`partial-result.json`은 `kind: partial_verify`, run identity, PASS/FAIL/FALLBACK/ERROR, candidate/base, 선택 모듈/테스트 ID·이유, 전체 대비 규모, 알고리즘/설정/manifest identity, 시간과 진단 경로를 보존한다. `selection.analysis`는 문서 역할·내용/모드 식별 정보, 대상 확대·문서 소비 근거·남은 불확실성·관련 후보 중 미선택 모듈과 실행 예산을 구분한다. PASS는 선택 모듈의 성공만 뜻하며 영향 범위 전체의 성공으로 해석하지 않는다. Reviewer에게는 이 한계·선택 규모·불확실성 종류/수·미선택 수와 불완전한 표본임을 명시한 요약을 전달한다. 전체 결과는 로그와 저장된 부분 증거에 보존하고 요약에 선택 식별 정보와 진단 경로를 제공한다. 이전 알고리즘 또는 다른 수집 명세의 선택 결과는 거부한다. 미선택 manifest 모듈은 NOT_SELECTED이며 실행 완료나 skip을 합성하지 않는다. FALLBACK은 discovery 이후에도 테스트 모듈과 command를 실행하지 않는다. 기존 core·adapter가 fixture·skip·identity/필수 결과·serial/independent·guard·예약·자식 종료를 검사하며 timeout과 취소도 전체 검증과 같은 외부 프로세스 경계를 사용한다.

부분 PASS는 전체 verify token/receipt와 분리한다. 일반 선행 Review의 최종 승격도 모든 후보·검토 입력·환경/정책과 해당 선행 승인 뒤 실행한 실제 전체 verify receipt ID가 확인될 때만 가능하며 게시 직전 재확인한다. 관측 port는 현재 준비된 입력뿐 아니라 `invocation_id`가 주어진 경우 그 실제 Reviewer 호출의 고정 instruction/profile·실행 환경/정책을 증명해야 한다. 현재 설정을 과거 호출의 identity로 소급 채택하지 않는다. 실제 호출 identity·사전/사후 현재 identity가 모두 같아야 하며 호출 증거 부재·실패·변경·중단은 기존 경로로 처리한다. 이 경로는 G-183의 최종 승인 재사용, G-241의 전체 검증 생략, G-250 병렬 최적화를 구현하지 않는다.

실행 자료는 verify 로그와 SQLite `preliminary_reviews`, 기존 Agent invocation/사용량 ledger에 결합된다. 부분·전체 시간, 모듈/테스트 규모, 부분 PASS 이후 전체 FAIL, 선행 FAIL의 전체 verify 미호출과 fallback 사유를 구분한다. 선행 호출 purpose는 `preliminary_review`이며 실제 실행/토큰과 재사용을 구분한다. 실제 운영 기록 없이 절감 시간·토큰 또는 완료 시간 개선률을 만들지 않는다.

## adapter 추가 경계

필요한 프로젝트의 계획에서 도구의 공식 수집·native reporter·지원 버전·독립 단위·내부 병렬 제어 근거를 먼저 확인한다. 정적 registry metadata와 adapter 구현을 추가하고 공통 core의 자원 계산·예약·정리 코드를 재사용한다. adapter는 수집된 고유 단위/테스트 identity를 제공하며 run·단계·단위 version을 결합한 결과의 완결성과 native 실패·오류·skip 의미를 검증한다. 실행 중 등록되는 테스트가 있는 도구는 예상 범위와 등록된 결과의 관계를 별도 검증해야 한다. 자유 형식 stdout 추측이나 command 종료 코드로 테스트 범위 검증을 대신하지 않는다.

unittest와 perl-tap 이외의 미연결 도구는 기존 verify를 유지하며 필요한 adapter·외부 프로젝트 연결은 해당 프로젝트의 계획으로 진행한다. 명시적으로 연결한 미지원 도구는 실패다. R-067의 전체 완료에는 Windows/Linux 격리 및 같은 revision·환경의 직렬/auto·동시 실행 실측이 남아 있다.

## 진단과 입력

run별 `manifest.json`, 단계별 수집 요청/결과·단위 결과·worker 로그와 JSONL 이벤트, 전체 `summary.json`을 보존한다. version 1 이벤트는 run·stage·unit·worker·test identity와 `clock_domain`을 담으며, 이전 로그에 없는 필드는 확인되지 않은 값으로 남긴다. `clock_domain`은 이벤트를 기록한 Python의 `sys.platform` 문자열이다. monotonic `at`·`start`·`duration`은 같은 clock domain 안에서만 비교한다. 종료·정리 판단은 Manager/core가 소유하고 summary나 운영 진행 표시는 이를 바꾸지 않는다. 실패 피드백의 `[VERIFY_DIAGNOSTIC]` 경로로 원본 진단을 찾을 수 있다. 예약은 session·단위·PID와 프로세스 시작 identity에 결합한다. 종료가 확인된 자식/미시작 예약만 회수하고 종료 불명은 별도 journal에 보존한다.

core는 `result_file_observed`에서 report 경로를 처음 발견한 parent 시각을 기록한다. Windows Job에서 `active_processes > 1`이면 `job_child_wait`와 변경된 process count를 남기고, worker만 남았음을 확인한 `job_child_exit_confirmed` 뒤에 report를 소비한다. 조회 오류는 `job_child_exit_unconfirmed`다. `module_result`는 결과 검증·집계 뒤에 기록하며 다음 `unit_start`에는 `worker_id`, `previous_unit_id`, 직전 결과 뒤 경과 시간이 있다. 이 대기와 coordinator `resource_wait`, `worker_shutdown:<worker>`는 따로 집계한다. 진단 이벤트는 worker 재사용·idle·예약 회수 정책을 완화하지 않는다.

Manager의 30초 진행 관측은 현재 run ID와 일치하는 완성된 JSONL 행만 읽고 파일별 커서로 한 번 소비한다. `stage-N/worker-*.jsonl`의 test 시작·종료·중단과 runner의 단계·unit·allocation 이벤트를 연결한다. Windows parent는 `snapshot.json`이 가리키는 live WSL bundle log와 native sibling log를 읽고 WSL `parent_run_id`·부모 `result.json`·snapshot revision을 함께 표시한다. WSL·native의 PID·monotonic time·run-local result를 합치지 않는다. partial row는 newline 뒤에만 소비하며 stage/run 종료 뒤 남은 active 표시를 지우고 읽기 실패·불완전 legacy 데이터는 미확인으로 남긴다.

`summary.json`은 같은 run·clock domain의 `run_start`/`run_end` 차이로 완료 run의 전체 경과를 계산한다. `run_end`가 없으면 현재 플랫폼과 clock domain이 일치할 때만 summary 관측 시점까지의 경과를 표시하고, 다른 clock domain은 미확인으로 둔다. assignment 시각·report 관측/검증·Job child 대기·allocation 당시 실행량과 resource snapshot·phase별 prepare/collection/test/cleanup·Git invocation 및 누적 시간을 원 이벤트와 연결한다. Windows parent의 `windows_pipeline`은 `result.json`·`snapshot.json`과 `<run>-wsl/summary.json`, `<run>-windows/summary.json`의 run ID·clock domain·결과·elapsed·요약 경로를 연결하며 OS별 상세 자료는 각 sibling summary에 둔다. `partial_result`·`partial_selections`는 선택 범위와 실제 partial 결과를 보존한다. 미실행 단계·NOT_SELECTED·FALLBACK·선행 FAIL 뒤의 미호출 full verify에는 완료 이벤트나 성공 시간을 만들지 않는다.

자체 `scripts/verify-inputs.json`은 `loop_manager.py`, `loop_manager_internal/**`, `tests/**`,
`requirements.txt`, `scripts/verify.json`, 두 verify 스크립트, Windows driver·선택 목록, `scripts/setup.sh`, 두 copy 스크립트와 소비되는
`run.sh`·`run.ps1`과 실행 코드 `.agents/skills/specification-docs/scripts/**`를
관측한다. 복사 회귀는 이 조회 도구와 `loop_manager_internal/catalog/frontmatter.py`의 실제 bytes를
샘플 source에 배치하고 배포 결과·조회 동작을 확인한다. 선언 파일은 실제 adapter·interpreter·수집·필수 compile 단계를
정하므로 목록에 포함한다. Perl adapter의 `.py`·`.pl`·`.pm`과 두 언어 예제·launcher도 기존 `loop_manager_internal/**`·
`tests/**`에 포함된다. `tests/**` 안의 고정 샘플이 문서·Skill·설정 예제 원본을 대체하며,
실제 copy 스크립트는 이 샘플 source에서 실행해 결과 bytes·경로·보존·덮어쓰기 거부를 확인한다.
`AGENTS.md`, README, docs와 `.agents`의 prose, `config/config.ini.example`·운영 설정, 소비하지 않는 setup·ignore·attributes는
자체 verify의 prose/예제 입력에서 제외한다. 이 범위는 문서·예제의 품질 검증을 뜻하지 않으며
필수 unittest 수집과 전체 compile을 유지한다. Linux 실행 경로 회귀가 실제 `scripts/setup.sh`를
임시 source에 복사해 실행하므로 이 파일의 bytes·kind·mode 변경은 입력 지문과 기존 제외 근거를 바꾼다.
소비하지 않는 다른 플랫폼 setup은 추가하지 않는다. Windows driver·선택 목록의 코드 입력 관측은
WSL/native 환경의 재현성 보장과 구분한다.

성공 재사용의 채택 기준은 같은 프로젝트·ExecPlan·작업 주기의 실제 전체 성공과 저장소 코드 입력의
동일성이다. 소스·테스트·fixture·의존성 선언·검증 스크립트/선언과 선택 진입점·목록의 bytes·경로·
kind·mode를 비교하고 미커밋/untracked·목록 밖 코드 누락도 확인한다. 설치 interpreter·도구·
환경변수·사용자/시스템 설정·외부 동적 입력·Git revision 값은 비교 범위 밖이며 그 미확인만으로
전체 실행이나 별도 infrastructure를 요구하지 않는다. 환경 재현성을 보장하는 정책은 아니다.

일반 스크립트와 연결 runner는 코드 기준으로 관측하며 runtime identity 부재는 재사용을 막지 않는다.
선택 진입점·연결 선언이 목록에 포함돼야 하고 관측/실행/receipt 판정 코드 bytes도 결합한다.
문서 제외는 현재 bytes·kind·mode와 실제 코드 소비 관계를 다시 확인한다. 고정 POSIX launcher·unittest의
제한된 정적 증명 외에, 일반/native 진입점은 Worker가 실제 소비를 검토한 명시적 제외 근거를
기존 Worker 결과에 제출한다. 최종 코드에서 프로젝트 Python으로 다음 읽기 전용 명령을 사용한다.

```sh
python /manager/runtime/loop_manager_internal/verification/inputs.py --review-request . --entrypoint scripts/verify.sh --operating-path config/config.ini --operating-path config/config.ini.example
```

실제로 검토한 경우에만 Windows 진입점도 `--entrypoint scripts/verify.ps1`로 추가한다. 출력 JSON은
identity 요청 자료이며 승인이나 verify 성공이 아니다. 소비하지 않는 정확한 목록 밖 경로가 어떤 내용이어도
검증에 영향을 주지 않는다는 실제 소비 검토 근거 `basis`를 추가해 최종 결과에
`VERIFY_INPUT_EXCLUSIONS: <JSON>` 한 줄을 제출한다. 형식은
[ARCHITECTURE의 관측 계약](ARCHITECTURE.md#운영-입력-관측-adapter)을 따른다.
운영/예제 설정만 정확 경로로 제외하며 검증이 소비하는 고정 자료는 manifest에 포함한다.
현재 코드/진입점/규칙·경로 집합과 근거가 맞을 때 문서 내용·mode를 재관측해 completeness를 갱신한다.
새 코드·경로·실제 소비·불명확한 관계·비정상 kind·비교 오류는 기존 Worker 확인과 전체 실행을 유지한다.
코드 입력 변경·목록/비교 오류·누락 의심·
유효한 전체 성공 부재에는 전체 실행을 유지한다. 새 실패·중단을 과거 성공으로 덮지 않으며,
부분 PASS·선행 Review·수동 면제와 실제 전체 run은 구분한다. 일반 선행 PASS 뒤 실제 전체 실행과
Windows의 WSL 전체 검증+native 핵심 회귀 계약은 유지한다.
