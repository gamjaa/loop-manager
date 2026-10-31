---
id: G-187
title: 공통 verify의 실제 OS 격리와 부하 비교
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-186
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
  - G-265
  - G-266
  - G-267
  - G-268
  - G-295
  - G-297
  - G-309
  - G-327
  - G-329
  - G-331
  - G-332
  - G-333
  - G-334
  - G-335
complexity: medium
risk: high
priority: normal
---

## Purpose

이미 구현된 자원 coordinator와 unittest/Perl TAP 공통 runner를 실제 프로젝트·OS·부하에서 검증할 Manager 실행 자료를 제공한다. G-309의 신뢰된 필수 통합 단계·상하위 실행 예산을 소비해 남은 OS 종료/격리·동시 부하·동일 범위 실측을 담당한다. 두 언어 예제·native 결과 변환과 자체 Windows의 WSL 전체 → Windows 핵심 회귀 launcher는 재구현하지 않는다.

## Acceptance Criteria

- R-067/R-091: 현재 필수 `runner-integration`의 고정 9개 case(두 언어 × standalone/coordinated × 정상/native 실패와 partial/full)를 Manager·단독 전체 실행 자료에서 확인한다. 지원 플랫폼의 case 완료·run/stage/revision·고유 child run과 실제 session·예약·PID/시작 identity·최종 종료 응답을 대조한다. 일반 unittest 자식의 중첩 runner를 복원하지 않고 Agent/API 거부·인증값/예산 제어값 격리를 유지한다. 단계 누락·중복·timeout·인프라/fixture 실패를 전체 성공이나 의도된 native 실패로 처리하지 않는다.
- Windows·Linux의 실제 공통 프로세스/예약 경계와 지원되는 두 언어 예제에서 auto와 1이 같은 수집·테스트/compile 범위를 수행한다. Python은 선언한 프로젝트 interpreter를 사용하고 Perl native 실행은 현재 VERIFY의 Linux/Perl 5.38.2·Test::More 1.302194·TAP::Parser 3.44 범위에서 확인한다. Windows에는 Python 실제 실행과 플랫폼 독립 Perl 계약/IPC 회귀를 유지하며 이 결과를 Windows native Perl 지원으로 표시하지 않는다. 고부하 대기·동시 두 프로젝트·새 작업 진입에서 실제 관측과 미반영 예약을 함께 계산한다.
- R-086이 지원하는 자체 unittest 부분 실행에서도 기존 자식/예약 격리·timeout·취소·종료 불명 보호를 확인한다. 현재 Windows launcher의 부분 요청은 `self_windows_requires_full` FALLBACK이며 두 단계를 실행하지 않는다. 이를 Linux 부분 실행의 성공이나 Windows 부분 지원으로 표시하지 않고 이후 실제 전체 경로와 연결한다. 부분 실행의 미선택 상태와 실행 없는 fallback을 전체 필수 검증 완료와 구분하며 Python/비Python의 기존 전체 적합성 범위는 유지한다.
- timeout·취소·시작/통신/결과 오류·강제 종료에서 해당 자식 종료와 예약 해제를 확인한다. 종료 미확인 예약은 함부로 풀지 않고 다른 프로젝트의 자식/예약/작업을 변경하지 않는다.
- R-091: Manager의 기본 `verify_concurrency=1`과 명시적 2에서 실제 호출 수·대기·Agent 병행을 구분한다. 동시 두 프로젝트의 부하/예약 비교는 2로 명시하고 단독 runner의 기존 조율 범위와 구별한다. 순번·자원 대기를 제외한 실제 실행 예산과 wall 경과·대기 누계를 별도로 기록한다. 종료 불명은 관련 호출/관리 자리를 보존하며 자체 Windows 두 단계와 정리는 같은 verify 자리 하나를 유지한다.
- 동일 revision·도구 버전·부하 조건에서 병렬도 설정·실제 worker 수를 기록하고 같은 환경의 직렬/auto 실제 전체 verify 시간을 비교한다. Windows 개발 환경의 자체 전체 verify는 WSL에서 수행하고 Windows native는 관련 최소 회귀로 확인한다. Windows/WSL 비용 비교는 같은 PC·코드·검증 범위의 대상만 순차 측정해 WSL 전체 검증 자료와 구분한다. 부분 실행 자료는 같은 mode·실제 선택 집합끼리 별도로 비교하며 전체/부분 시간을 같은 범위의 병렬 성능으로 해석하지 않는다. 테스트/fixture/정리·자원 대기·개별 누적과 전체 경과를 구분하며 미측정 속도 개선을 주장하지 않는다.
- Worker는 좁은 관련 unit case만 실행하고 전체 실제 OS/언어 검증·성능 비교는 Manager가 Agent 밖에서 실행한다. 미실행 플랫폼/미완료 결과를 성공으로 표시하지 않는다.
- R-086의 부분 실행 안내는 실제 선택기 algorithm identity와 일치한다. 현재 `verification/impact.py`의 선택기 버전 5와 공통 protocol·adapter 버전 1을 구별하며, 과거 선택을 거부하는 기존 계약을 유지한다.

## Implementation

- [ ] 기존 `loop_manager_internal/verification/resources.py`·`coordination.py`·`timing.py`의 진단과 확인한 `tests/support/verify_examples/python/`·`perl/`의 선언/launcher를 재사용해 bounded 실제 검증/비교 진입을 기존 verify 경계에 연결한다. 현재 `verification/integration.py:execute()`·`validate_example()`·`partial_case()`의 고정 suite와 `scripts/verify.json`의 필수 단계가 이전한 정상/native 실패·partial/full assertion을 소유한다. 삭제된 `tests/test_verify_language_examples.py`·`tests/support/verify_fixture.py`와 고정 sampler를 복원하거나 같은 실제 실행을 unittest에 추가하지 않는다. 통합 단계·상하위 시간 계산은 원격에 반영된 G-309 구현을 소비하며 별도 상시 benchmark 서비스나 새 루트 launcher를 만들지 않는다.
- [ ] 확인한 `loop_manager_internal/verification/contract.py`·`runner.py`·`perl_adapter.py`의 version 1 dispatch·파일 suite/동적 TAP assertion·serial/independent·부분 FALLBACK을 실제 실행 자료에 연결한다. `perl_reporter.pl`·`LoopManagerVerifyGuard.pm`의 native 로그/TAP spool·BEGIN 이전 Agent/fork/network 거부를 보존한다. Perl의 미지원 AST 부분 선정·시간 큐 재사용이나 Windows native 지원을 새 목표로 추가하지 않는다.
- [ ] 확인한 `scripts/verify.ps1`·`scripts/verify_windows.py`·`scripts/verify-windows-tests.json`, `tests/test_verify_windows_launcher.py`·`tests/test_verify_windows.py`를 실제 Windows 자료 수집의 기존 진입으로 사용한다. WSL의 독립 Linux coordinator와 native의 Manager Windows 예약 조율을 구분하고, WSL 프로젝트 snapshot bytes·원래 후보 revision과 재구성한 Linux Git revision의 관계를 보존한다. 두 단계/정리의 전체 시간 상한 보완은 G-295를 소비하며 timeout 구현을 이 실측 작업에서 중복하지 않는다.
- 시간 비교는 G-216의 `git_fixture_seconds` 중첩 집계와 현재 `timing.summarize()`의 root journal/manifest run identity를 재사용한다. 상위 summary의 `integrations`와 child별 원래 summary·native report·console/coordination/process 기록을 연결하고, 하위의 의도된 실패·fixture 비용이 상위 결과/집계를 덮지 않게 한다. phase event·개별 duration을 보존하고 같은 PID의 완전히 포함된 하위 구간만 fixture 총계에서 제외하며, 서로 다른 clock 원점이나 병렬 child duration을 더해 상위 경과 시간으로 만들지 않는다.
- [ ] 검증 자료에 환경·revision·run identity·원래 결과·실제 worker 수·종료 확인·비교 조건을 남기고 시간 상한 증가/필수 테스트 축소로 성능을 만들지 않는다. 명시적으로 연결한 Perl 도구의 부재/버전 불일치는 실패이며 native 검증을 mock 또는 skip으로 대체해 성공을 만들지 않는다.
- [ ] `docs/VERIFY.md`가 소유한 지원 범위·Manager/단독 호출·timeout 상속 계약에 실제 자료와 미실행 한계를 반영한다. R-086의 부분 실행 절에 남은 선택기 버전 4 표기를 확인한 `verification/impact.py:VERSION`·`Selection.algorithm`·`selected_modules()`와 `verification/runner.py`의 실제 결과 identity에 맞춘다. 두 외부 프로세스 경로의 변경·삭제·이동 감지와 과거 선택 거부는 이미 반영된 구현을 재사용한다. unittest의 기존 targeted 근거와 새 Linux Perl native 회귀·예제 통합, mock Windows Job/IPC 적합성을 실제 Windows 격리·동시 부하·성능 근거로 확대하지 않는다. Windows의 공통 종료/예약 검증은 계속 필수이며 언어 지원과 OS 격리 자료를 별도로 보고한다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- Worker는 resource reservation·process cleanup·diagnostic aggregation에서 변경한 경계의 최소 unit case만 실행한다. `tests/test_verify_runtime.py`·`test_verify_contract.py`·`test_verify_perl_adapter.py`와 현재 `tests/test_verify_integration.py`의 결과/소유권·종료 보존 unit을 재사용한다. 고정 suite의 실제 실행은 전체 verify에 유지하고 결과 확인/배정 진단은 G-248, Windows 부하 관측은 G-249의 목표와 중복하지 않는다.
- 선택기 버전 안내는 위 구현의 선언·선택 직렬화·이전 algorithm 거부·runner 결과 기록과 정적으로 대조한다. `tests/test_verify_impact.py`의 기존 계약 변경·삭제/rename·algorithm identity 회귀와 `tests/test_partial_verify_runner.py`의 실행 없는 FALLBACK·NOT_SELECTED 회귀를 보존하며, 안내 문구만의 교정에 고정 prose assertion을 추가하지 않는다.
- Manager의 실제 실행에서 Windows·Linux timeout/취소·시작 실패·강제 종료 뒤 해당 자식/예약만 정리하고 다른 실행을 보존하는지 확인한다. Linux Perl reporter·테스트 자식의 부분 NDJSON/TAP spool과 suite 미완료, Windows 공통 Job 종료 불명·예약 보존을 실제 효과에 연결한다. Windows native는 이 효과를 확인할 최소 실행과 경로·인자·중첩 Python guard 회귀로 한정하고 자체 전체 verify·대규모 Git/CLI 통합은 WSL에서 수행한다. G-309의 별도 필수 통합 단계가 Manager·단독 전체 실행에서 같은 범위를 확인하며 Worker targeted command에는 넣지 않는다. WSL 실행은 AGENTS.md의 실행별 `/tmp/` 복사본·유효한 Git 메타데이터·임시 환경 변수·종료 후 정리 기준을 따른다. WSL 전체 PASS와 Windows 핵심 회귀를 Windows 전체 verify PASS로 합치지 않는다.
- 동일 revision·도구/환경·수집/fixture/필수 compile·계측으로 직렬/auto 및 단일/동시 자료를 비교한다. 두 언어 결과·부분 FALLBACK/NOT_SELECTED·비교 범위·실제 자식 종료를 대조하며 미실행 플랫폼은 한계와 잔여 필수 확인으로 남긴다. 해당 자료 없이 R-065/R-067의 실측 완료를 선언하지 않는다.
- G-297의 작업별 대기·종류별 실행 한도와 G-295의 launcher 예산, G-309의 상하위 대기 관측을 재사용해 실제 Manager 연결에서 기본 직렬·명시적 동시 2·세 번째 대기와 독립 구현/Review 진행을 확인한다. 상한 10초의 대기 30초·실제 2초는 하위 runner 생존 중에도 확인된 순수 대기만 제외하고 실제 12초 처리는 실패해야 한다. 상위 처리·형제 실행의 겹침·heartbeat 단절·종료 불명과 중복 공제 거부, 단계/재개 뒤 소비 예산 보존을 실제 진단과 대조한다. 같은 예산의 Windows 두 단계/정리와 종료 불명 예약 보존을 실제 OS 근거로 구분한다.
- 대기 누계의 손상·역행·소실은 현재 `ExecutionClockTests`와 `ManagerDeadlineHandoffTests`의 오류/최종 성공 거부 회귀를 재사용하고, 필요한 실제 실행 연결에서 `budget-error/`·소유 누계 및 원래 하위 진단의 보존을 확인한다. `finish_child` 최종 응답 부재·거부·형식 오류에는 stopped=true나 복사본 정리를 확정하지 않으며 독립 run 예약은 유지돼야 한다. 공통 오류 행렬을 실제 OS마다 반복하지 않는다.
- Windows 실제 실행은 현재 launcher의 원본 snapshot·WSL 전체 manifest/필수 compile·native 선택 manifest와 두 결과를 각각 확인한다. `test_real_launcher_runs_both_phases_and_preserves_native_failure`·`test_killed_windows_launcher_stops_linux_children_and_removes_its_copy`의 실제 OS 근거를 재사용하되 테스트 존재나 mock PASS를 실행 근거로 채택하지 않는다. WSL 부재·실패·native 실패·정리 불명은 전체 조합 성공이 아니며 해당 실행의 `/tmp/`·자식/예약만 정리되고 다른 실행은 보존돼야 한다. 이 조합과 Windows native 전체 suite의 범위를 구분한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-09 `bac6f84e`와 mutable snapshot token `72f8256fbf8d111d1e3b5471dfeb314312d188d8eeec9fbcb704063918d9c50a`의 공통 runner·지원 문서·종료/예약 회귀를 읽었다. 두 언어·Windows/Linux 실제 격리·직렬/auto 동일 조건 실측 목표는 유지하며 구현·테스트·verify를 실행하지 않았다.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- revision `c233021f56362b22f6fb10257eb2734c2664e23e`의 고정 통합 suite·관측 전용 session·공통 대기 누계·최종 종료 응답과 예산 오류 보존은 원격 구현으로 소비한다. 삭제된 중첩 예제/고정 관측 helper의 정상·native 실패·partial/full 검증은 `integration.py`와 새 unit으로 이전됐으며 이 계획은 남은 실제 OS 격리·동시 부하·동일 범위 비교 자료만 담당한다. snapshot token `45bca81e68a942e7da99615278950c6c529c9c22a7eb905d863db25ae77fedca`의 원문 hash를 확인했고 기존 ID·경로·metadata·dependency·수행 기록을 보존한다. 실제 통합·Windows 실행·성능이나 프로젝트 최종 전문 확인의 성공을 새로 기록하지 않는다.
- 2026-10-16 delta: R-091의 직접 구현된 기본 verify 직렬·설정 가능한 동시 호출과 대기 제외 시간 기준을 실측 조건에 연결한다. G-297의 조율 보완 뒤 실제 연결을 확인하고 기존 OS/언어/부하 목표·수행 이력은 보존한다. 부분 unit/mock 근거를 실측이나 최종 전문 확인으로 확대하지 않는다.
- 이번 변경 검토에서 G-274의 두 프로세스 경로 감지·선택기 버전 5와 관련 회귀를 정적으로 확인했다. 남은 VERIFY의 버전 4 표기는 이 계획의 기존 지원 문서 정합화 범위에 통합하며 G-274를 중복 계획하거나 수정하지 않는다. ID·경로·sources·dependency·complexity·risk·priority와 수행 기록을 보존한다. 국소 안내 교정은 기존 OS 격리·실측의 난도·위험을 바꾸지 않으며 테스트·verify·Review 성공을 새로 확정하지 않는다.

- 기준 `bbce8cd25cdaa34a547b736cfc2f5d20f96a2a6d`의 자체 Windows 두 단계 launcher와 native 목록은 직접 구현으로 재사용한다. Windows의 실제 자식·예약 격리·동시 부하·성능 목표는 유지하고, WSL 전체와 native 핵심 회귀를 Windows native 전체 성공으로 합치지 않는다. G-295는 확인한 snapshot 준비·단계 전환·정리의 전체 시간 상한 보완이며 그 Merge 뒤 이 계획의 실제 timeout/정리 자료를 수집한다. 기존 dependency와 수행 기록은 보존하고 직접 구현에 소급 계획·검증 성공을 만들지 않는다.

- 변경 비교에서 선행 adapter 계획의 archive-only 이동은 같은 ID의 완료/조회 호환 경계로 취급한다. 기존 dependency는 보존하고 확인한 실제 Perl 코드·테스트와 두 예제를 구현 근거로 사용한다. Windows/Linux 공통 프로세스 안전과 현재 Linux 전용 native Perl 근거를 대조하며 Windows Perl 지원 확대를 이 검증 작업에 끼워 넣지 않는다. 실제 OS/동시 부하·성능 목표는 미충족으로 유지하고 검증 성공은 합성하지 않는다.

- R-058의 시간 기록·Agent 없는 테스트는 현재 충족된 검증 보호 기준으로 재사용한다. 이 계획의 미충족 목표는 R-065·R-067의 실제 부하/OS/언어 검증이며 R-058의 재구현이나 새 잔여 목표로 등록하지 않는다.

- 기존 core의 실제 검증 자료 연결로 complexity medium, 프로세스/예약 오정리와 범위 누락 영향으로 risk high다. R-065의 이미 구현된 배분 알고리즘은 중복 구현하지 않는다.
- 현재 snapshot의 ID·경로·metadata·dependency·기록을 보존한다. medium complexity는 기존 runner·예약·진단에 범위가 정해진 실제 실행 자료를 연결하는 작업이다. high risk는 다른 실행의 자식을 종료하거나 종료 불명 예약을 회수해 독립 작업을 손상하는 영향이며, 종료된 외부 프로세스를 코드 rollback으로 복구할 수 없다. 예약과 작업물을 보존하고 해당 실행의 수동 재개를 지원하며 Validation의 실제 자식/예약 격리·미확인 종료 보존을 Review 근거로 확인한다.
