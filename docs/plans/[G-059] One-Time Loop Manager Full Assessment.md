---
id: G-059
title: One-Time Loop Manager Full Assessment
sources:
  - docs/requirements/[R-027] Shared Quality Guidance and Full Project Refactoring Assessment.md
depends_on:
  - G-051
  - G-058
complexity: medium
risk: medium
priority: normal
---

## Purpose

현재 유지보수 평가는 Merge 수를 기준으로 주기적으로 수행된다. R-027은 Loop Manager 저장소에 한정해 기존 코드·테스트와 활성 제품 문서를 한 번 전수 평가하고 중단 뒤에도 결과와 남은 작업을 추적하도록 요구한다.

## Acceptance Criteria

- self-hosted Loop Manager에만 최초 전수 평가를 한 번 수행하며 코드, 테스트, PRODUCT, 활성 Requirement와 ARCHITECTURE를 포함한다. 생성물, 의존성 및 archive는 제외한다.
- 책임·상태 전이·외부 연동 혼합, 이름·제어 흐름·결합도, 중복과 후속 변경 충돌을 조사하고 근거 있는 발견·처리 상태·남은 작업을 durable하게 기록한다. 파일 길이나 변경 횟수만으로 후속 작업을 만들지 않는다.
- 중단 뒤 같은 평가를 재개하고 완료 기록이 있으면 재실행하지 않는다. 다른 등록 프로젝트의 기존 주기적 평가 조건과 SQLite 실행 상태·저장 형식은 보존한다.
- 발견된 변경은 현재 PRODUCT/Requirement 범위와 유지보수 권한에 맞는 계획으로만 이어진다. 범위 밖 아이디어는 실행 근거로 사용하지 않는다.

## Implementation

- `maintenance_state.py`와 유지보수 coordinator에 self 프로젝트 전용 일회성 marker 및 bounded 평가 결과를 기존 cycle 감사와 결합한다. 현재 assessment 결과가 있을 경우 중복 평가 대신 이어서 기록하도록 한다.
- 대상 파일 inventory와 제외 기준을 결정적으로 산출하고 Planner의 결과를 source revision에 결합한다. 후속 계획 ID와 남은 검토 범위를 저장해 재시작 후에도 추적한다.
- 현재 barrier assessment에서 확인된 Agent scope 혼합 문제와 G-051을 평가 결과의 첫 사례로 연결한다.

## Validation

- Worker는 `tests/test_maintenance_state.py`, `tests/test_maintenance_orchestration.py`, `tests/test_maintenance_planning.py`의 일회성 평가 관련 targeted unit test만 실행한다.
- self-only 1회 실행, 중단·재개, source revision drift, 기존 assessment 결과 재사용, archive·생성물 제외와 타 프로젝트 주기 불변을 검증한다.
- 전체 verify에서 maintenance barrier, post-refactor revalidation, 다중 프로젝트 scheduler와 계획 publication 회귀가 없어야 한다.

## Progress

- [ ] 전수 평가 대상과 durable 결과 경계 구현
- [ ] self-only·중단 재개 targeted test 추가

## Decision Log

- 이 계획은 R-027의 일회성 전수 평가 실행·추적을 담당한다. G-058은 공통 품질 지침에 한정한다.
