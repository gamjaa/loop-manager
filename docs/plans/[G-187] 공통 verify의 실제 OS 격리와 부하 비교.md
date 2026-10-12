---
id: G-187
title: 공통 verify의 실제 OS 격리와 부하 비교
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-065] 실제 부하 기반 verify 병렬 실행.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
  - docs/requirements/[R-086] 영향 기반 부분 검증과 일반 선행 Review.md
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
complexity: medium
risk: high
priority: normal
---

## Purpose

이미 구현된 자원 coordinator와 G-148/G-186의 runner를 실제 프로젝트·OS·부하에서 검증할 Manager 실행 자료를 제공한다.

## Acceptance Criteria

- 전체 실행의 Windows·Linux 실제 Python/비Python 프로젝트에서 auto와 1이 같은 수집·테스트/compile 범위를 수행한다. 고부하 대기·동시 두 프로젝트·새 작업 진입에서 실제 관측과 미반영 예약을 함께 계산한다.
- R-086이 지원하는 자체 unittest 부분 실행에서도 기존 자식/예약 격리·timeout·취소·종료 불명 보호를 확인한다. 부분 실행의 미선택 상태와 실행 없는 fallback을 전체 필수 검증 완료와 구분하며 Python/비Python의 기존 전체 적합성 범위는 유지한다.
- timeout·취소·시작/통신/결과 오류·강제 종료에서 해당 자식 종료와 예약 해제를 확인한다. 종료 미확인 예약은 함부로 풀지 않고 다른 프로젝트의 자식/예약/작업을 변경하지 않는다.
- 동일 revision·도구 버전·부하 조건에서 병렬도 설정·실제 worker 수를 기록하고 직렬/auto 실제 전체 verify 시간을 비교한다. 부분 실행 자료는 같은 mode·실제 선택 집합끼리 별도로 비교하며 전체/부분 시간을 같은 범위의 병렬 성능으로 해석하지 않는다. 테스트/fixture/정리·자원 대기·개별 누적과 전체 경과를 구분하며 미측정 속도 개선을 주장하지 않는다.
- Worker는 좁은 관련 unit case만 실행하고 전체 실제 OS/언어 검증·성능 비교는 Manager가 Agent 밖에서 실행한다. 미실행 플랫폼/미완료 결과를 성공으로 표시하지 않는다.

## Implementation

- 현재 resource/coordination/timing 진단과 G-186 예제를 재사용해 bounded 실제 검증/비교 진입을 기존 verify 경계에 연결한다. 별도 상시 benchmark 서비스나 새 루트 launcher를 만들지 않는다.
- 시간 비교는 G-216의 `git_fixture_seconds` 중첩 집계를 재사용한다. phase event·개별 duration을 보존하고, 같은 PID의 완전히 포함된 하위 구간만 fixture 총계에서 제외한다.
- 검증 자료에 환경·revision·run identity·원래 결과·종료 확인·비교 조건을 남기고 시간 상한 증가/필수 테스트 축소로 성능을 만들지 않는다.
- `docs/VERIFY.md`가 소유한 version 1 선언·adapter 지원·Manager/단독 호출·timeout 상속 계약을 사용한다. 현재 unittest의 Python 3.12/Linux targeted 근거와 mock Windows Job/프로세스 적합성을 실제 OS 격리·다른 지원 버전·동시 실행·성능 근거로 확대하지 않는다. G-186의 추가 도구와 같은 필수 범위로 Manager가 얻은 실제 자료 및 미실행 한계를 지원 문서에 반영한다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- resource reservation·process cleanup·diagnostic aggregation에서 변경한 경계의 최소 unit case만 실행한다.
- Manager 전체 verify 및 동일 범위 비교 실행은 각 OS의 실제 자식/예약 격리·두 언어 결과·timeout 재개를 확인한다. 지원 OS에서 얻지 못한 증거는 한계와 필요한 실행을 남긴다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-09 `bac6f84e`와 mutable snapshot token `72f8256fbf8d111d1e3b5471dfeb314312d188d8eeec9fbcb704063918d9c50a`의 공통 runner·지원 문서·종료/예약 회귀를 읽었다. 두 언어·Windows/Linux 실제 격리·직렬/auto 동일 조건 실측 목표는 유지하며 구현·테스트·verify를 실행하지 않았다.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-058의 시간 기록·Agent 없는 테스트는 현재 충족된 검증 보호 기준으로 재사용한다. 이 계획의 미충족 목표는 R-065·R-067의 실제 부하/OS/언어 검증이며 R-058의 재구현이나 새 잔여 목표로 등록하지 않는다.

- 기존 core의 실제 검증 자료 연결로 complexity medium, 프로세스/예약 오정리와 범위 누락 영향으로 risk high다. R-065의 이미 구현된 배분 알고리즘은 중복 구현하지 않는다.
- 현재 snapshot의 ID·경로·metadata·dependency·기록을 보존한다. medium complexity는 기존 runner·예약·진단에 범위가 정해진 실제 실행 자료를 연결하는 작업이다. high risk는 다른 실행의 자식을 종료하거나 종료 불명 예약을 회수해 독립 작업을 손상하는 영향이며, 종료된 외부 프로세스를 코드 rollback으로 복구할 수 없다. 예약과 작업물을 보존하고 해당 실행의 수동 재개를 지원하며 Validation의 실제 자식/예약 격리·미확인 종료 보존을 Review 근거로 확인한다.
