---
id: G-273
title: 연속 배정 중 다른 worker의 완료 결과 우선 수집
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
depends_on: []
complexity: low
risk: medium
priority: normal
---

## Purpose

이미 구현된 verify 연속 배정에서 다른 worker의 즉시 완료 결과도 다음 배정 전에 수집한다. 현재 루프는 방금 배정한 worker의 report만 확인하므로 두 번째 배정 중 첫 worker가 완료돼도, acquire가 계속 허용되면 idle 전환·메모리 결과 반영 없이 새 worker 배정을 이어간다. 이 잔여 조건만 교정하고 기존 실행 큐와 coordinator를 재사용한다.

## Acceptance Criteria

- 복수 pending 모듈과 여유 cap이 있는 연속 배정 중 먼저 배정한 다른 worker의 report가 준비되면 다음 acquire 전에 기존 결과 검증·coverage·메모리 반영·idle 처리를 수행한다. 추가 프로세스가 필요 없는 입력에서는 확인된 idle worker를 재사용하고, 수집 가능한 결과를 배정 burst 끝이나 cap 거절까지 미루지 않는다.
- 바로 수집할 결과가 없으면 기존 연속 배정을 유지한다. 각 배정의 acquire·예약·PID bind·자원/사용자 상한·다중 session 공정성을 유지하며, 진행 가능한 일이 없거나 거절되고 수집 가능한 결과도 없으면 기존 bounded 대기를 사용한다.
- report가 있어도 Windows Job의 잔여 자식 등 기존 수집·재사용 조건을 충족하지 못하면 idle 전환을 앞당기지 않는다. 이 상태가 수집 루프의 무진전 반복을 만들지 않으며, 종료 불명 예약·시작 실패의 미시작 확인·실제 종료 뒤 release와 실패 진단을 유지한다.
- 결과 누락·중복·identity 오류·runner 오류를 기존 방식으로 처리한다. 부분 실행에서는 선정된 모듈만 배정하고 NOT_SELECTED를 유지하며, serial·단계·필수 command 순서와 fixture/skip 의미를 변경하지 않는다.
- R-065에 따라 정확성·안전성 검증으로 이 교정을 완료할 수 있다. 성능 실측·중앙값은 후속 확인이며 미측정 개선율을 보고하거나 측정 누락을 사용자 판단 대기로 바꾸지 않는다.

## Implementation

`verification/core.py`의 완료 수집과 연속 pending 배정 사이에서 전체 실행 중 worker의 준비 상태를 확인한다. 기존 결과 수집·Job 확인과 대기 판단이 같은 조건을 사용하도록 필요한 국소 변경만 하고, 별도 polling thread·worker pool·자원 정책이나 새로운 cache를 추가하지 않는다. `tests/test_verify_runtime.py`의 pool fixture와 실제 `VerifyCoordinator`를 재사용한다.

현재 ARCHITECTURE·VERIFY의 즉시 결과 수집 계약과 구현을 대조한다. G-250의 실행 큐·연속 배정 전체를 다시 구현하지 않으며 G-248의 새 시점 진단과 G-187의 실제 OS/언어 비교는 해당 계획에 남긴다.

## Validation

- Worker는 기존 pool fixture로 cap에 아직 여유가 있고 두 번째 배정 중 첫 worker가 완료되는 최소 unit case를 확인한다. 요청·결과 처리 순서, 새 프로세스 수, idle 재사용과 보고된 메모리를 다음 acquire에 반영하는 실제 core/coordinator 결과를 검사한다. cap 거절 후 수집하는 기존 case만으로 이 조건을 대신하지 않는다.
- 즉시 완료 없음·방금 배정한 worker 완료·다른 worker 완료·report와 살아 있는 Job 자식·수집 오류의 관련 최소 case를 확인한다. fake process/clock으로 불필요한 sleep과 무진전 polling, 조기 idle/release를 검출하고 기존 실패/종료 불명 회귀를 유지한다.
- Manager 전체 verify는 연속 배정·다중 session 공정성·시작 실패·자식 종료·완결된 결과와 부분 NOT_SELECTED/전체 compile을 유지해야 한다. 실제 OS 효과와 성능 개선은 mock 결과로 완료했다고 주장하지 않는다.

## Progress

- [x] 2026-10-13 delta `20e6f0fbfdb6ceb5384d42c83f3904490b441356` → `f56739c31a49ec8fb5547a459688bfb02772586e`의 R-065·core·pool 회귀를 정적으로 대조했다. 다른 worker의 완료를 검사하는 기존 case는 cap 거절 뒤 수집을 확인하며, 여유 cap에서 다음 배정 전 수집은 검사하지 않는다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 전체 worker의 즉시 결과 수집 연결과 최소 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity low: 기존 수집·배정·Job 준비 조건을 국소적으로 연결하며 새로운 자원 설계가 없다. risk medium: 잘못된 준비 판정은 조기 worker 재사용이나 배정 정체를 만들 수 있다. 기존 자식/예약 보호와 진단을 유지하고 해당 실행의 안전한 재검증으로 복구하며 살아 있는 자식·거절·수집 오류를 Validation에서 확인한다.
- 기존 catalog는 이 동작 교정을 맡지 않는다. G-248은 시점 진단, G-187은 실제 적합성 자료이며 Merge된 G-250을 소급 계획하거나 그 성능 실측을 새 필수 조건으로 만들지 않는다. priority normal이며 이번 판단은 delta 범위의 잔여 조건이다.
