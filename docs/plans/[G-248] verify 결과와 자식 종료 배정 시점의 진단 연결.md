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
- 기존 단독 .cache/verify/<run>/summary.json과 Manager <state-dir>/logs/verify/<run>/summary.json에서 배정 변화·당시 실행량/관측 근거·Git 호출 수/누적 시간·수집/준비/테스트/정리 및 전체 경과를 대조할 수 있다. 부분 run의 전체 discovery·영향 선정·선택 모듈 실행과 후속 전체 run을 기존 후보 증거로 연결하되 각 실행의 시간을 구분한다. NOT_SELECTED·FALLBACK·일반 선행 FAIL 뒤 전체 verify 미호출에는 완료 이벤트나 성공 시간을 합성하지 않는다. 비밀값·전체 env는 출력하지 않는다.
- 첫 run_start의 busy=None과 이후 관측 실패를 구분한다. resource_wait가 상한 도달·예약 압력·미확인 관측을 포함함을 보존하며 이를 전부 유휴 시간으로 합산하지 않는다. PID별 누적 시간·부모에 포함된 자식 시간과 전체 경과를 더하지 않는다.
- 기존 version 1·unittest manifest/결과 의미·전체 실행의 필수 compile·timeout과 진단 위치를 유지한다. 불완전한 legacy 로그에는 확인 가능한 자료만 표시한다. ARCHITECTURE·docs/VERIFY.md의 진단 설명을 갱신하며 OS 실측·성공 재사용 완료를 선언하지 않는다.

## Implementation

verification/core.py의 결과 수집·Windows Job 관측·unit_start와 기존 stop_worker phase, timing.py의 이벤트 집계 및 runner/manager_adapter의 metadata를 연결한다. runner의 mode·partial-result와 preliminary_reviews/verify_receipts의 기존 증거 연결, discovery·impact_selection phase를 재사용해 부분/전체 run을 구분한다. 이미 남기는 allocation·Git·fixture·시간 자료를 재사용하고 필요한 누락 시점만 보강한다. 기존 이벤트/요약 계약의 호환 확장으로 처리하고 별도 로그 저장소나 모니터링 서비스를 만들지 않는다. 실제 임시 경로의 선택·관측과 summary 연결은 G-253이 담당한다.

## Validation

- Worker는 test_verify_runtime.py·test_verify_contract.py의 최소 unit case로 결과 파일 있음/자식 생존, 확인 후 다음 배정, 종료 오류·재사용 금지와 run/stage/worker/module 연결을 검사한다. fake clock·process·Job을 주고 실제 core·집계 로직을 실행한다.
- timing/manager adapter 회귀와 test_partial_verify_runner.py·test_preliminary_review.py의 기존 fixture로 standalone/Manager 동일 summary 경로와 Git/phase 자료 연결, 부분/전체 run 구분·미선택/fallback/전체 미호출, legacy 불완전 로그·비밀값 비노출·누적/경과 구분을 검사한다. 기존 실제 Windows 자식 종료 회귀는 유지하고 mock 결과를 실제 OS 근거로 확대하지 않는다.
- Manager 전체 verify는 종료/예약·timeout/cancel·결과 완결성과 진단 보존을 유지한다. Manager가 실제 Windows 영향 모듈을 측정할 때 revision·범위·Python/Git·affinity/메모리·동시 부하·worker 수·cache·checkout/임시 파일시스템을 남겨 결과/자식/배정 시간을 구분한다. Windows와 WSL은 같은 PC에서 한 번에 하나씩 비교하고 미확인 환경 원인·절감률을 확정하지 않는다.

## Progress

- [x] 2026-10-10 기준 50c699c8f9c149cecb0ddf9b081527942d8e2289의 R-065/R-067·core의 Job active 대기·timing summary와 runner metadata를 정적으로 확인했다. 구현·테스트·OS 측정·verify는 실행하지 않았다.
- [ ] 진단 연결·문서 정합화·targeted unit 검증
- [ ] Manager 전체 verify·필요한 실제 Windows 자료·읽기 전용 Review·사용자 Merge

## Decision Log

- core는 Job active()>1이면 결과 수집 전에 continue하며 module_result·idle을 나중에 남긴다. timing.summarize는 module_result/unit_start의 연결 시점을 별도 집계하지 않는다. 기존 Git/phase 집계 자체는 재구현할 대상이 아니다. runner.run_start의 실제 임시 경로 누락은 G-253의 launcher·실효 경로 책임에 통합해 중복 구현하지 않는다.
- complexity medium: core·runner·summary 사이의 확인된 시점과 실행 문맥을 연결한다. risk low: 기록·집계 오류는 보존 원본과 코드 교정으로 복구할 수 있으며 프로세스 종료·예약 정책은 바꾸지 않는다. priority normal이다.
- G-187의 두 언어/두 OS 전체 적합성·직렬/auto/동시 실측과 구분한다. 이 계획은 새 진단 누락만 보완하며 비Python adapter 완료를 선행 요구하지 않는다. R-065의 비용 우선순위는 새 필수 dependency가 아니다.
