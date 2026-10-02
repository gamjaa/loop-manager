---
id: G-120
title: Extract Bootstrap and State Foundations
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-119
complexity: high
risk: high
priority: critical
---

## Purpose

`loop_manager.py`의 process lock·runtime bootstrap과 schema·catalog migration·DB 접속은 후속 runtime·Agent·Git 기능이 모두 사용하는 기반이다. 먼저 서로 독립된 runtime 및 storage 경계로 추출해 내부 모듈이 거대한 진입점에 의존하는 원인을 줄인다.

## Acceptance Criteria

- 상태 디렉터리, lock·runtime identity와 공통 immutable 자료형은 필요한 책임의 leaf module에 둔다. storage·bootstrap service가 루트 진입점을 import하거나 실행 process 전체의 전역 객체를 조회하지 않는다.
- `SCHEMA`, `_ensure_execplans_schema`, `_migrate_execplan_catalog`, `connect_database`와 catalog cache/publication persistence의 책임은 기존 repository 계층으로 연결한다. 동일 row 해석·schema 보완·definition serialize가 중복된 경우 같은 저장 계약 안에서 재사용한다.
- SQLite schema·column/default·저장 JSON·감사 기록과 transaction/rollback 범위를 보존한다. 공통 SQL helper가 임의 commit을 수행하거나 서로 다른 event의 transaction을 합치지 않는다.
- lock 획득, 최신 원격 bootstrap SHA 확인, source 갱신 후 handoff, exact-SHA detached runtime 선택과 child 실행의 순서를 보존한다. 사용자 source 변경, config 절대 경로와 interpreter 선택, 실패 exit code를 유지한다.
- `tests/test_origin_isolation.py`의 bootstrap/runtime identity·source sync·registration case를 책임별로 나누고 G-119 support를 사용한다. root resource와 launcher 호출 계약을 유지한다.

## Implementation

1. G-060 대응표에서 공통 자료형·예외·identity와 각 schema/접속 함수의 실제 소비자를 확인한다. runtime과 storage가 서로를 불필요하게 import하지 않도록 작은 contract module 또는 명시적 인자를 사용한다.
2. bootstrap/process-lock/runtime 선택을 G-063 패키지의 runtime 영역으로, 접속·기존 migration orchestration을 storage 영역으로 추출한다. bootstrap의 Git 효과는 명시적인 adapter로 받고 아직 G-122가 담당할 Git 구현을 함께 재설계하지 않는다.
3. `runtime_state.py`, `execplan_catalog_state.py`, maintenance·notification repository와 중복되는 실제 저장 연산만 담당 repository로 모은다. row projection과 원본 실행 상태의 경계, Manager 밖 전체 verify 실행 금지를 유지한다.
4. 필요한 공개 root helper는 명시적 re-export로 보존하고 내부 소비자와 테스트는 실제 module을 사용한다. 저장소 루트·state·config identity를 전달하며 이동한 `__file__` 기준으로 resource 경로를 바꾸지 않는다.
5. import·compile 대상이 바뀐 부분만 verify에 반영하고 후속 추출 대상은 대응표에 남긴다. 무관한 schema 개선이나 runtime 정책 변경은 수행하지 않는다.

## Validation

- Worker는 접속·schema 재개·catalog projection과 bootstrap lock/runtime identity의 직접 관련 test method만 선택해 targeted unit test를 실행한다.
- schema 재적용의 멱등성, 실패 transaction rollback, 저장 JSON의 round trip, 같은 SHA의 정상 runtime 재생성 및 증거가 불충분한 재생성 거부를 검증한다.
- dirty source의 보존, config 경로·interpreter 전달과 resource lookup은 해당 경계의 unit test로 확인한다. 실제 다중 process·Git 흐름의 전체 regression은 Manager verify에 맡긴다.
- Manager 전체 verify에서 Windows/Linux launcher, exact runtime identity, 기존 database 호환성과 모든 origin-isolation 시나리오가 유지돼야 한다.

## Progress

- [ ] foundation 소비자와 transaction·bootstrap 순서 대응표 확인
- [ ] runtime·storage·leaf contract 추출 및 관련 중복 제거
- [ ] targeted unit test와 origin-isolation test 책임 분리
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-02: G-120~G-123은 같은 `loop_manager.py`의 기능을 순차 추출한다. PR별로 구현·검증·사용자 Merge를 끝내고 다음 변경의 기준선을 갱신한다.
- 이 계획은 기반 책임의 위치와 재사용을 바꾸며 상태 모델·저장 형식·bootstrap safety 정책을 바꾸지 않는다. 길이가 큰 repository라도 한 책임으로 응집돼 있으면 그 근거를 기록한다.

- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 먼저 진행하도록 priority를 `critical`로 지정한다. 리팩터링 내부의 선행 Merge 의존성과 검증·Review·사용자 Merge 조건은 유지한다.
