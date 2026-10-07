---
id: G-187
title: 공통 verify의 실제 OS 격리와 부하 비교
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
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
complexity: medium
risk: high
priority: normal
---

## Purpose

이미 구현된 자원 coordinator와 G-148/G-186의 runner를 실제 프로젝트·OS·부하에서 검증할 Manager 실행 자료를 제공한다.

## Acceptance Criteria

- Windows·Linux의 실제 Python/비Python 프로젝트에서 auto와 1이 같은 수집·테스트/compile 범위를 수행한다. 고부하 대기·동시 두 프로젝트·새 작업 진입에서 실제 관측과 미반영 예약을 함께 계산한다.
- timeout·취소·시작/통신/결과 오류·강제 종료에서 해당 자식 종료와 예약 해제를 확인한다. 종료 미확인 예약은 함부로 풀지 않고 다른 프로젝트의 자식/예약/작업을 변경하지 않는다.
- 동일 revision·도구 버전·병렬도·부하 조건의 직렬/auto 실제 전체 verify 시간을 비교한다. 테스트/fixture/정리·자원 대기·개별 누적과 전체 경과를 구분하며 미측정 속도 개선을 주장하지 않는다.
- Worker는 좁은 관련 unit case만 실행하고 전체 실제 OS/언어 검증·성능 비교는 Manager가 Agent 밖에서 실행한다. 미실행 플랫폼/미완료 결과를 성공으로 표시하지 않는다.

## Implementation

- 현재 resource/coordination/timing 진단과 G-186 예제를 재사용해 bounded 실제 검증/비교 진입을 기존 verify 경계에 연결한다. 별도 상시 benchmark 서비스나 새 루트 launcher를 만들지 않는다.
- 검증 자료에 환경·revision·run identity·원래 결과·종료 확인·비교 조건을 남기고 시간 상한 증가/필수 테스트 축소로 성능을 만들지 않는다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- resource reservation·process cleanup·diagnostic aggregation에서 변경한 경계의 최소 unit case만 실행한다.
- Manager 전체 verify 및 동일 범위 비교 실행은 각 OS의 실제 자식/예약 격리·두 언어 결과·timeout 재개를 확인한다. 지원 OS에서 얻지 못한 증거는 한계와 필요한 실행을 남긴다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-058의 시간 기록·Agent 없는 테스트는 현재 충족된 검증 보호 기준으로 재사용한다. 이 계획의 미충족 목표는 R-065·R-067의 실제 부하/OS/언어 검증이며 R-058의 재구현이나 새 잔여 목표로 등록하지 않는다.

- 기존 core의 실제 검증 자료 연결로 complexity medium, 프로세스/예약 오정리와 범위 누락 영향으로 risk high다. R-065의 이미 구현된 배분 알고리즘은 중복 구현하지 않는다.
