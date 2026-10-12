---
id: G-252
title: verify 가드의 프로세스 단일 설치와 환경 준비
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
depends_on: []
complexity: high
risk: critical
priority: high
---

## Purpose

sitecustomize의 runpy 로딩과 일반 import가 같은 프로세스에서 가드를 중복 설치하는 경로와 동일 subprocess 환경의 반복 준비를 정리한다. 실제 Agent/API 차단·중첩 자식 보호·인증값 제외와 후보 worktree 선택을 모두 유지한다.

## Acceptance Criteria

- 같은 프로세스에서 runpy·일반 import·반복 설치가 함께 발생해도 audit hook과 Popen wrapper가 한 번만 설치된다. 서로 다른 프로세스는 각각 보호되며 동명 다른 worktree 모듈이나 테스트 재진입 때문에 보호가 사라지지 않는다.
- 실제 자식에 전달할 같은 환경을 wrapper/audit에서 불필요하게 반복 준비하지 않는다. 명령/API 거부 검사는 매 실제 실행 경계에서 적용하고 안전 판단을 cache하거나 명령 변경을 과거 검사로 통과시키지 않는다.
- 명시/상속 env·중첩 Python 자식에서 marker·가드 경로·선택 worktree package·bytecode 정책을 유지하고 조율 인증값은 실제 전달 환경에서 제외한다. Windows key 대소문자와 POSIX bytes alias를 포함한다. 부모 env와 호출자 제공 mapping은 보존한다.
- 정제 오류·프로세스 시작 실패·timeout/cancel에서도 기존 미시작/종료 확인·예약·진단을 유지한다. 부분 설치나 설치 실패를 성공으로 cache하지 않으며 보호 없는 테스트 진행을 허용하지 않는다.
- 가드 설치·준비 진단에 비밀값·전체 env를 노출하지 않는다. ARCHITECTURE·docs/VERIFY.md의 Python 가드 책임을 갱신하되 비Python 또는 모든 외부 subprocess 보호를 새로 보장하지 않는다.

## Implementation

verification/guard.py·child_guard/sitecustomize.py와 runner/unittest_adapter의 설치 호출, worker_environment·Popen/audit 경계를 재사용한다. 함수 인스턴스별 lru_cache 대신 다른 로딩 경로가 공유할 최소 프로세스 설치 근거를 정하고 설치 완료/실패와 실제 가드 원본·프로젝트 선택 책임을 구분한다. 기존 wrapper 표식·환경 복사/정제와 실행 검사 계약을 함께 대조해 보안 검사 생략 없이 준비 중복만 없앤다. 새 일반 cache 서비스·환경 원본·가드 배포 경로나 인증 저장 체계를 만들지 않는다.

## Validation

- Worker는 test_verify_runtime.py의 가드/환경 최소 unit case로 다중 로딩의 단일 hook/wrapper, 반복 준비 횟수, 실패/부분 설치 재진입과 명령 변경의 매번 거부를 검사한다. 실제 codex/claude/API는 금지된 double이나 audit 입력으로 확인한다.
- test_verify_contract.py의 기존 새 interpreter·다른 후보 worktree 연결과 실제 자식 환경 회귀를 재사용해 runpy+import·명시/상속 env·중첩 Python·인증값 미전달·부모/호출자 불변·bytecode 정책을 확인한다. 인증값이 로그/summary에도 없고 정제/시작 오류의 종료·예약 경계가 유지되는지 검사한다.
- Manager 전체 verify는 Agent/API 차단·process/Job 정리·후보 package·fixture 결과/수집 계약을 유지한다. 영향 모듈의 동일 환경/범위·계측/cache·부하/worker 수 비용을 반복 비교해 중앙값을 남기고 전체 검증 기록과 구분한다. Reviewer는 설치 근거 위조·부분 실패의 보호 누락·실제 env 인증값 누출 및 검사 cache 우회를 blocking issue로 확인한다.

## Progress

- [x] 2026-10-10 50c699c8f9c149cecb0ddf9b081527942d8e2289의 R-067과 runpy sitecustomize·함수별 lru_cache·Popen wrapper/audit 환경 준비 및 관련 회귀를 정적으로 대조했다. 구현·테스트·verify는 실행하지 않았다.
- [x] 단일 설치/준비와 보호 회귀·targeted unit 검증
- [ ] Manager 전체 verify·비용 비교·읽기 전용 Review·사용자 Merge

### 구현 기록

- `guard.py`의 함수별 cache를 interpreter 공유 설치 근거·lock·실제 audit probe와 wrapper identity로 교체했다. 확인 중단·wrapper 설치 실패는 이미 등록한 hook을 중복 등록하지 않고 재진입하며, 등록 거부를 성공으로 저장하지 않는다. sitecustomize 설치 실패는 비밀값 없는 고정 진단 후 종료한다.
- Popen 호출 범위의 thread-local dict identity·내용 사본으로 wrapper/audit의 동일 환경 준비만 생략한다. 변경된 환경은 다시 정제하고 명령/API 검사는 매 경계에서 수행한다. Windows 대소문자·POSIX bytes 별칭의 후보 root·Python 환경 정책과 조율 인증값 제외를 유지한다.
- `GuardInstallationTests`와 기존 환경 unit case, `CandidateEnvironmentTests`의 새 interpreter·다른 후보 package·중첩 자식 case, command 정제/시작 실패·종료 불명 예약 case를 확인했다. Python/Linux에서 15개 targeted test가 통과했고 확인 중단 재진입 보완 뒤 영향 범위 8개 targeted test가 통과했다(고유 case 총 16개). 전체 suite·verify·비용 비교·Windows 실환경 검증은 수행하지 않았다.
- Manager verify 피드백 `verify:93c1a34cb7f227d620da9715f08a90ceff2989e1e65140413a06b843c1b8ff83`의 새 interpreter 실패를 고정 runtime의 이전 가드를 먼저 설치한 동일 개별 case로 재현했다. 부모 wrapper가 이전 runtime의 sitecustomize 경로를 선점하는 fixture 의존성을 제거하기 위해 `-I -B` harness에서 현재 가드를 먼저 설치한 뒤 실제 Python 자식의 자동 sitecustomize 로딩을 검사한다. 기존 다중 로딩·후보 package·중첩 자식·명령/API 거부·인증값 제외·환경 준비 횟수·bytecode assertion과 timeout은 유지한다. 수정 후 같은 targeted command의 1개 case가 통과했으며 전체 verify는 재실행하지 않았다. 후속 Worker 결과 검증 오류는 보고에서 최초 실패 결과를 생략하고 재실행 결과 뒤에 실패 표현을 넣은 것이 원인이었다. `FAILED_COMMAND` → 최초 `FAIL` → 동일 `RERUN` → 최종 `PASS` 형식의 실제 보고를 기존 `parse_worker_result`로 검사해 통과했다. 이미 통과한 targeted case는 반복 실행하지 않았다.

- Review 지적 `diagnostic-failure-bypasses-exit`에 따라 sitecustomize의 고정 진단 출력을 `try`에 두고 `finally`에서 `_exit(1)`을 호출하도록 수정했다. 기존 종료 unit case를 출력 성공·`os.write`의 `OSError` 두 subtest로 확장해 종료 코드 1과 종료 호출을 확인했다. `python3 -m unittest tests.test_verify_runtime.GuardInstallationTests.test_sitecustomize_failure_stops_instead_of_running_unprotected`의 1개 case가 통과했다. 전체 verify는 Worker가 실행하지 않았다.


## Decision Log

- install_guard의 cache는 runpy로 만든 함수와 일반 import 함수 사이에서 공유되지 않는다. wrapper의 worker_environment 뒤 audit의 worker_environment가 같은 자식 환경을 다시 정제한다. wrapper 단일 설치 표식만으로 audit hook 중복을 막지 못한다.
- complexity high: 다중 로딩의 설치 원본·완료/실패 identity와 process 전역 hook 수명, 부모/실제 전달 env·후보 import 경계를 함께 설계해야 한다. risk critical: 환경 정제 누락으로 조율 인증값이 보호 경계 밖 자식에 전달·노출되면 이미 전달한 비밀값을 회수하거나 코드 rollback으로 복구할 수 없다. 설치 누락은 실제 Agent/API 실행도 허용할 수 있다. 실행 전 거부·명시/상속/중첩 자식의 인증값 제외·로그 비노출·부분 설치 실패를 Acceptance Criteria와 Validation의 필수 시나리오로 두고 Reviewer가 확인한다. 노출 발생 시 관련 호출을 중단하고 보존 진단을 바탕으로 사용자 수동 대응이 필요하며 자동 복구 성공을 주장하지 않는다. priority high다.
- G-186의 다른 언어 가드·G-187의 전체 OS 적합성과 구분해 현재 Python 가드만 교정한다. 보안 제품/인증 저장·같은 프로젝트 병렬 정책 변경은 범위에 없다.
