---
id: G-248
title: verify 결과와 자식 종료 배정 시점의 진단 연결
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: low
priority: normal
---

## Purpose

모듈 결과 도착·남은 자식 종료 확인·다음 배정을 구분해 Windows 병목과 실제 자원 대기를 진단한다. 기존 JSONL과 summary에 확인한 시점을 연결하며 진단 부족을 실행량이나 종료 보호 완화로 해결하지 않는다.

## Acceptance Criteria

- 부분/전체 mode·후보·실제 선택 범위와 run·stage·worker/PID·module identity에 결과 파일을 처음 확인한 시점, 자식 종료 대기/확인과 다음 배정 시점을 연결한다. Windows Job의 active child 대기는 테스트 본체·coordinator의 배정 대기·worker 종료 phase와 구분한다. 종료 불명에는 확인 시간을 합성하지 않는다.
- 자식이 살아 있거나 종료를 확인하지 못하면 worker 재사용·idle/예약 회수를 앞당기지 않는다. 실패·timeout·취소에서도 완료 항목과 남은 위치·종료 불명 진단을 보존하고 다른 run의 예약/프로세스를 변경하지 않는다.
- 기존 단독 .cache/verify/<run>/summary.json과 Manager <state-dir>/logs/verify/<run>/summary.json에서 배정 변화·당시 실행량/관측 근거·Git 호출 수/누적 시간·수집/준비/테스트/정리 및 전체 경과를 대조할 수 있다. 자체 Windows launcher의 `result.json`·`snapshot.json`과 형제 `<run>-wsl`·`<run>-windows`의 단계별 JSONL/summary도 해당 부모 run에 연결해 WSL 전체·native 핵심 회귀와 미실행을 구분한다. 부분 run의 전체 discovery·영향 선정·선택 모듈 실행과 후속 전체 run을 기존 후보 증거로 연결하되 각 실행의 시간을 구분한다. NOT_SELECTED·FALLBACK·일반 선행 FAIL 뒤 전체 verify 미호출에는 완료 이벤트나 성공 시간을 합성하지 않는다. 비밀값·전체 env는 출력하지 않는다.
- 첫 run_start의 busy=None과 이후 관측 실패를 구분한다. resource_wait가 상한 도달·예약 압력·미확인 관측을 포함함을 보존하며 이를 전부 유휴 시간으로 합산하지 않는다. PID별 누적 시간·부모에 포함된 자식 시간과 전체 경과를 더하지 않는다.
- WSL과 Windows의 PID·monotonic clock은 OS별 문맥으로 식별한다. 서로 다른 clock 값을 빼거나 같은 숫자 PID를 합쳐 종료·배정 시간을 만들지 않는다. 부모의 두 단계 경과와 각 OS 내부의 테스트·자식·예약 시간은 각각 보존하며 WSL의 독립 coordinator와 native의 Manager 조율을 하나의 예약 session으로 오인하지 않는다.
- 기존 version 1·unittest manifest/결과 의미·전체 실행의 필수 compile·timeout과 진단 위치를 유지한다. 불완전한 legacy 로그에는 확인 가능한 자료만 표시한다. ARCHITECTURE·docs/VERIFY.md의 진단 설명을 갱신하며 OS 실측·성공 재사용 완료를 선언하지 않는다.
- PRODUCT 운영 로그: Manager의 주기적 표시는 현재 run의 실제 단계별 JSONL에서 관측한 테스트 진행·자원 대기를 연결한다. `stage-N/worker-*.jsonl`의 시작·종료·중단을 놓치지 않고 부분 행은 완성된 뒤 한 번만 소비한다. run·stage·unit·test를 구분하고 단계 종료·중단 뒤의 항목을 현재 실행 중으로 표시하지 않는다. 수집·fixture·필수 command·자식 종료 대기는 테스트 실행과 구분하며 관측 불가를 실제 완료 수·성공·진척률로 합성하지 않는다. 다른 run·native NDJSON·TAP 원문은 집계하지 않는다.

## Implementation

verification/core.py의 결과 수집·Windows Job 관측·unit_start와 기존 stop_worker phase, timing.py의 이벤트 집계 및 runner/manager_adapter의 metadata를 연결한다. runner의 mode·partial-result와 preliminary_reviews/verify_receipts의 기존 증거 연결, discovery·impact_selection phase를 재사용해 부분/전체 run을 구분한다. 이미 남기는 allocation·Git·fixture·시간 자료를 재사용하고 필요한 누락 시점만 보강한다. 기존 이벤트/요약 계약의 호환 확장으로 처리하고 별도 로그 저장소나 모니터링 서비스를 만들지 않는다. 실제 임시 경로의 선택·관측과 summary 연결은 G-253이 담당한다.

- [x] 직접 구현된 `verification/manager_adapter.py:progress_observation()`의 단계별 `rglob("*.jsonl")`·파일별 커서·부분 행/중단 처리를 재사용해 run/stage/unit identity·단계 종료와 Windows live WSL·형제 로그/부모 result 연결을 보완했다. `runner.py:run()`·`unittest_adapter.py:worker_main()`·`timing.py:TimedResult`·`scripts/verify_windows.py`의 소유 실행 배치를 사용하며, 다른 run과 parent가 다른 WSL 자료를 제외하고 읽기 실패는 미확인으로 남긴다. 추가 Git·DB·프로세스·모델 조회는 하지 않았다.
- [x] 기존 `runtime/operation_events.py:progress()` reporter와 중첩 표시 억제·문맥 복사·종료를 재사용했다. 파일 관측 실패는 미확인으로 설명하고 verify 결과·timeout·예약·실패 집계에는 영향을 주지 않는다. 별도 reporter·새 전역 상태를 만들지 않았다.

## Validation

- Worker는 test_verify_runtime.py·test_verify_contract.py의 최소 unit case로 결과 파일 있음/자식 생존, 확인 후 다음 배정, 종료 오류·재사용 금지와 run/stage/worker/module 연결을 검사한다. fake clock·process·Job을 주고 실제 core·집계 로직을 실행한다.
- timing/manager adapter 회귀와 test_partial_verify_runner.py·test_preliminary_review.py의 기존 fixture로 standalone/Manager 동일 summary 경로와 Git/phase 자료 연결, 부분/전체 run 구분·미선택/fallback/전체 미호출, legacy 불완전 로그·비밀값 비노출·누적/경과 구분을 검사한다. 기존 실제 Windows 자식 종료 회귀는 유지하고 mock 결과를 실제 OS 근거로 확대하지 않는다.
- Worker targeted unit은 `python3 -m unittest tests.test_operation_observability.OperationObservabilityTests`와 `tests/test_verify_runtime.py`·`tests/test_verify_contract.py`에서 이번 변경에 직접 관련된 case만 실행한다. 새 최소 fixture는 실제 `stage-0/worker-0.jsonl`과 루트 `runner.jsonl`에 version·run/stage/unit/test identity를 넣는다. 실행 중 append·UTF-8 부분 행·재관측·두 단계의 같은 test ID·중단·파일 읽기 실패와 자원 대기 해소에서 중복 완료·다른 run 혼합·낡은 실행 중 표시가 없어야 한다. 기존 최상위 단일 파일 fixture만으로 실제 writer 배치의 연결을 통과시키지 않는다. reporter 종료·오류가 verify 결과와 예약 상태를 바꾸지 않는지 모의 실행으로 확인한다.
- 자체 Windows fixture는 실제 `<run>-wsl`·`<run>-windows` 배치와 부모 `result.json`·단계 시작/종료를 사용한다. WSL PASS 뒤 native 진행/실패, WSL 실패 뒤 native 미호출, 서로 다른 OS의 같은 PID·test ID·상이한 clock, 부분 FALLBACK·정리 불명과 다른 run의 유사 경로에서 혼합 집계·가짜 완료가 없어야 한다. `tests/test_verify_windows_launcher.py`의 기존 fake process/결과 fixture를 재사용하고 이 관측 회귀에서 실제 WSL·전체 verify를 실행하지 않는다.
- Manager 전체 verify는 종료/예약·timeout/cancel·결과 완결성과 진단 보존을 유지한다. Manager가 실제 Windows 영향 모듈을 측정할 때 revision·범위·Python/Git·affinity/메모리·동시 부하·worker 수·cache·checkout/임시 파일시스템을 남겨 결과/자식/배정 시간을 구분한다. Windows와 WSL은 같은 PC에서 한 번에 하나씩 비교하고 미확인 환경 원인·절감률을 확정하지 않는다.

## Progress

- [x] 2026-10-10 기준 50c699c8f9c149cecb0ddf9b081527942d8e2289의 R-065/R-067·core의 Job active 대기·timing summary와 runner metadata를 정적으로 확인했다. 구현·테스트·OS 측정·verify는 실행하지 않았다.
- [x] 2026-10-16 원격 코드의 단계별 이벤트 탐색·파일 커서·중단 처리와 최소 배치 회귀를 정적으로 확인했다. 테스트 실행 성공은 확인하지 않았다.
- [x] 남은 실행 identity·단계 종료·Windows 형제 로그의 운영 진행 연결
- [x] 진단 연결·문서 정합화·targeted unit 검증
- [ ] Manager 전체 verify·필요한 실제 Windows 자료·읽기 전용 Review·사용자 Merge
- [x] 2026-10-23 targeted unit 11개를 통과시켰다. WSL cleanup 회귀는 `/tmp` 요청을 assertion으로 유지하고 test run root를 writable temporary fixture로 배정해 child 종료 후 정리를 확인했다. bundle live-log 연결도 mock 회귀로 확인했다. 실제 Windows/WSL 실행과 Manager 전체 verify는 실행하지 않았다.
- [x] 2026-10-23 rebase base `c411905d6910a4794394d14dfb3f9e185a19eb07` 이후 현재 base-relative 변경 14개 파일의 전체 diff를 재검토했다. 관련 targeted unit 11개를 새로 실행해 통과했고 `git diff --check`도 통과했다. 실제 Windows/WSL 실행과 Manager 전체 verify는 실행하지 않았다.

## Decision Log

- 2026-10-16 delta: `1f8b97e6c04868281cd23672396f1fd3d47ee98e`의 재귀 탐색과 단계 fixture는 직접 구현으로 소비한다. 과거 reader가 루트만 읽는다는 조사 기록은 최신 상태 설명이 아니다. 결과 확인/종료/다음 배정의 시점·run identity·Windows 형제 로그 목표는 유지하며 `ExecutionClock`의 확인된 대기 누계는 진단에서 재사용한다. 시간 예산 구현은 G-295에 남기고 resource_wait 전체를 timeout 제외 시간으로 합산하지 않는다.

- 최신 `bbce8cd25cdaa34a547b736cfc2f5d20f96a2a6d`의 직접 구현은 자체 Windows 결과를 부모와 WSL/native 형제 경로에 나눈다. 기존 진행·진단 소비 목표를 이 실제 배치에 맞추며 새 launcher·자원 조율·timeout은 구현하지 않는다. G-253은 실효 임시 경로, G-295는 전체 시간 상한, G-187은 실제 OS·부하 근거를 담당한다. 기존 수행 기록·분류·priority·dependency는 유지한다.
- 2026-10-14 efc71a72f4e2ce152c9c4355e6d08bdc4ee242ac 재평가: 보존된 단계별 JSONL reader 보완과 이후 Worker 문서 권한 변경을 대조했다. 실제 writer 배치·reader·회귀 fixture의 누락은 그대로이며 관련 제품·설계 설명만 허용 문서에서 갱신한다. Requirement·다른 ExecPlan 수정이나 frontmatter 복원 기능을 추가하지 않고 ID·metadata·dependency·기존 수행 기록을 보존한다. 테스트·OS 실측·verify는 실행하지 않았다.
- 2026-10-14 운영 로그 delta: `b72b0f7ba7be68d3977151c2a4ecbbba20af89aa` → `58814a9afa875bf5c440d610afe4e58725826921`에서 공통 로그·주기적 관측이 직접 구현됐다. 새 진행 reader는 run 루트만 읽지만 실제 unittest writer는 `stage-N/worker-*.jsonl`에 기록하므로 테스트 진행을 놓친다. `tests/test_operation_observability.py`는 루트의 단일 JSONL만 사용한다. 기존 진단 연결 목표의 소비 경계에 이 누락과 실제 배치 회귀를 통합하며 공통 포맷·reporter·이미 구현된 이벤트를 재구현하지 않는다. snapshot의 ID·경로·sources·dependency·complexity/risk·priority와 기존 기록을 보존한다. 정적 검토만 수행했으며 테스트·OS 실측·최종 제품 전문 확인 성공은 기록하지 않는다.
- core는 Job active()>1이면 결과 수집 전에 continue하며 module_result·idle을 나중에 남긴다. timing.summarize는 module_result/unit_start의 연결 시점을 별도 집계하지 않는다. 기존 Git/phase 집계 자체는 재구현할 대상이 아니다. runner.run_start의 실제 임시 경로 누락은 G-253의 launcher·실효 경로 책임에 통합해 중복 구현하지 않는다.
- complexity medium: core·runner·summary 사이의 확인된 시점과 실행 문맥을 연결한다. risk low: 기록·집계 오류는 보존 원본과 코드 교정으로 복구할 수 있으며 프로세스 종료·예약 정책은 바꾸지 않는다. priority normal이다.
- G-187의 두 언어/두 OS 전체 적합성·직렬/auto/동시 실측과 구분한다. 이 계획은 새 진단 누락만 보완하며 비Python adapter 완료를 선행 요구하지 않는다. R-065의 비용 우선순위는 새 필수 dependency가 아니다.
