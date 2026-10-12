---
id: G-250
title: verify 연속 배정과 비교 가능한 모듈 시간 순서
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: medium
risk: medium
priority: normal
---

## Purpose

coordinator가 허용하는 독립 모듈을 연속 배정해 모듈별 고정 50ms 대기를 없애고, 비교 가능한 기존 모듈 시간으로 긴 모듈을 먼저 실행한다. 수집/결과 identity와 단계 순서는 유지한다.

## Acceptance Criteria

- 완료 모듈을 수집한 뒤 자원과 cap이 허용하는 동안 복수 pending 모듈을 연속 배정한다. 매 배정마다 acquire·예약·PID bind 등 기존 절차를 적용하고 다른 verify의 공정성·미반영 CPU/메모리 예약을 지킨다.
- 즉시 수집/배정할 일이 있으면 고정 sleep을 넣지 않는다. 진행할 일이 없거나 배정이 거절되면 bounded 대기해 busy polling을 피한다. 짧은/즉시 완료 모듈에서도 불필요한 50ms 간격이 누적되지 않는다.
- 현재 수집 단위와 실행 문맥에 비교 가능한 완전한 과거 duration 자료가 있을 때만 긴 모듈 우선 큐를 만든다. 동률은 수집 순서이며 자료 부재·불완전·손상·비교 불가능에는 전체 수집 순서를 유지한다. 누락 duration을 합성하지 않는다.
- R-086 부분 모드에서는 선정된 실행 모듈만 배정하고 전체 discovery의 NOT_SELECTED 기록을 보존한다. 부분/전체 또는 선택 집합이 다른 자료를 같은 실행 범위로 인정하지 않는다. 부분 선정·Review 순서·전체 verify 생략 기능을 이 작업에 추가하지 않는다.
- 배정 큐 외 manifest·테스트/fixture identity·필수 단계 순서·serial 의미와 native 결과는 바꾸지 않는다. 과거 시간은 비용 정보일 뿐 현재 성공이나 실행 생략 근거가 아니다.
- 시작 실패·결과 오류·timeout/cancel·남은 자식 생존/종료 불명에서도 기존 예약/프로세스 보존을 유지한다. 배정 처리 개선이 worker 재사용·종료 확인을 앞당기지 않는다. ARCHITECTURE·docs/VERIFY.md의 배정/시간 자료 책임을 필요한 절에 반영한다.

## Implementation

verification/core.py의 수집→pending 배정→대기 경계와 runner의 실행 큐 준비를 기존 coordinator·timing 자료에 연결한다. timing.comparison과 기존 manifest/환경 identity로 과거 시간의 비교 가능성을 확인하고 검증 실패에는 수집 순서를 사용한다. stage 사이 순서를 바꾸거나 새 cache/IPC/worker 내부 pool을 만들지 않는다. G-248이 보강할 시점 이벤트와 관계없이 기존 이벤트 계약을 유지한다.

## Validation

- Worker는 test_verify_runtime.py의 pool fixture와 test_verify_resources.py의 coordinator double로 복수 연속 grant, 거절 뒤 대기, 즉시 완료·실행 중 cap·다중 session 공정성·시작 실패를 최소 unit으로 검사한다. fake clock/sleep·process를 제공하고 실제 배정/예약 판단을 확인한다.
- 고정 시간 자료로 긴 모듈 우선·동률 안정·부재/손상/불완전/다른 환경 fallback과 동일 manifest·coverage/fixture·command 단계 순서를 검사한다. partial/중복 결과·자식 생존·종료 불명/timeout 회귀를 유지한다.
- Manager 전체 verify는 1/auto의 같은 범위·결과·compile과 예약/정리를 유지한다. 영향 모듈·필수 연관 회귀를 같은 대상·Python/Git·계측/cache·실제 worker/자원·동시 부하·임시 경로에서 반복 비교해 중앙값을 남긴다. 국소 개선을 전체 시간으로 환산하지 않는다.

## Progress

- [x] 2026-10-10 기준 50c699c8f9c149cecb0ddf9b081527942d8e2289의 core 단일 acquire 후 sleep, 수집 순서 pending과 기존 timing/예약·pool 회귀를 정적으로 확인했다. 구현·테스트·비교·verify는 실행하지 않았다.
- [ ] 실행 큐·연속 배정·설명 정합화와 targeted unit 검증
- [ ] Manager 전체 verify·같은 조건 비용 비교·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-12 사용자 요청의 문서 정합화: R-086 선택 집합·미선택 상태와 비교 범위의 호환 조건을 명시했다. 연속 배정/시간 순서의 원래 목표·dependency·metadata·기록을 유지하며 부분 검증이나 Reviewer 병렬 실행을 중복 구현하지 않는다.

- core.execute_manifest는 반복마다 pending 하나만 배정하고 남은 작업이 있으면 sleep(0.05)를 호출한다. list(modules)의 수집 순서만 사용하며 기존 timing 자료를 배정 큐에 연결하지 않는다.
- complexity medium: 실행 큐/과거 자료 유효성과 기존 수집·예약·대기 책임이 명확하다. risk medium: 잘못된 연속 배정은 cap·공정성·정리 회귀와 자원 압력을 일으킬 수 있다. 기존 coordinator 거절·예약 보존 및 안전 재실행으로 복구한다. priority normal이다.
- G-249의 Windows 관측·G-251의 최초 import 분산과 독립이다. R-065의 비용 우선순위를 새로운 선행 Merge dependency로 바꾸지 않는다.
