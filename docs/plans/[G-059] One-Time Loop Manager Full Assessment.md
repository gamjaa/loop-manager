---
id: G-059
title: One-Time Loop Manager Full Assessment
sources:
  - docs/requirements/[R-027] Shared Quality Guidance and Full Project Refactoring Assessment.md
depends_on:
  - G-051
  - G-058
  - G-076
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
- 근거가 확인된 코드 개선은 기존 비종료 계획과 대조해 중복 없이 독립 검증·Merge 가능한 리팩터링 ExecPlan으로 모두 연결한다. 문서 개선은 원본과 분리된 수정본으로 사용자에게 제시하고, 승인된 수정본에 대해서만 의미 보존 ExecPlan과 PR을 진행한다. 거절되거나 재검토가 필요한 발견도 결정과 이유를 남기며, 문제 없음도 근거와 함께 기록한다.
- 원본 PRODUCT·활성 Requirement·ARCHITECTURE와 공통 AGENTS.md는 평가 및 수정본 작성 중 변경하지 않는다. 수정본과 원본 revision·내용 지문, 전체 diff, 승인·거절 결정은 중단 뒤에도 보존한다. 승인 직전 원본과 비종료 계획의 source 관계를 재검증하고, 달라졌거나 의미 보존이 불확실하면 적용하지 않는다. 다른 등록 프로젝트에는 이 일회성 흐름을 적용하지 않는다.

## Implementation

- `maintenance_state.py`와 유지보수 coordinator에 self 프로젝트 전용 일회성 marker 및 bounded 평가 결과를 기존 cycle 감사와 결합한다. 현재 assessment 결과가 있을 경우 중복 평가 대신 이어서 기록하도록 한다.
- 대상 파일 inventory와 제외 기준을 결정적으로 산출하고 Planner의 결과를 source revision에 결합한다. 후속 계획 ID와 남은 검토 범위를 저장해 재시작 후에도 추적한다.
- 유지보수 Planner의 평가 기록과 dependency 장벽을 사용해 코드 리팩터링 DAG를 게시한다. 문서 발견은 원본 밖의 수정본과 사용자 결정을 먼저 보존하고, 승인된 정확한 수정본에 대해서만 별도 문서 계획·검증·Review·PR을 진행한다. 거절되거나 정책 선택이 필요한 발견은 원본문서를 바꾸지 않고 결정과 이유를 기록한다.
- 완료된 G-051의 Agent scope 책임 분리 결과를 이미 처리한 발견으로 평가 이력에 연결한다. 최신 코드·테스트와 활성 문서에서 남은 문제를 다시 확인하고 같은 범위의 리팩터링을 중복 계획하지 않는다.

## Validation

- Worker는 `tests/test_maintenance_state.py`, `tests/test_maintenance_orchestration.py`, `tests/test_maintenance_planning.py`의 일회성 평가 관련 targeted unit test만 실행한다.
- self-only 1회 실행, 중단·재개, source revision drift, 기존 assessment 결과 재사용, archive·생성물 제외와 타 프로젝트 주기 불변을 검증한다.
- 코드·문서 발견의 분리와 중복 제거, 문서 수정본의 지문·전체 diff·승인·거절·원본 변경 시 재승인, 사용자 결정 전 원본 불변, 승인 뒤 정확한 적용, 중단 복구와 계획 게시 후 재개를 검증한다.
- 전체 verify에서 maintenance barrier, post-refactor revalidation, 다중 프로젝트 scheduler와 계획 publication 회귀가 없어야 한다.

## Progress

- [ ] 전수 평가 대상과 durable 결과 경계 구현
- [ ] self-only·중단 재개 targeted test 추가

## Decision Log

- 이 계획은 R-027의 일회성 전수 평가 실행·추적을 담당한다. G-058은 공통 품질 지침에 한정한다.
- 문서 수정본 검토는 이 저장소의 이번 전수 평가에 한정하며 공통 AGENTS.md 변경을 전제로 하지 않는다.
- G-051은 이번 재검증 이전에 Merge된 구조 변경이다. 전수 평가는 그 결과를 현재 기준선으로 사용한다.
