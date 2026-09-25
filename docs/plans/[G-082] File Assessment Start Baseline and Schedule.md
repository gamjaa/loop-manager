---
id: G-082
title: File Assessment Start Baseline and Schedule
sources:
  - docs/requirements/[R-042] Periodic Repository-Wide Code Assessment for Token-Efficient Refactoring.md
depends_on: []
complexity: high
risk: high
priority: normal
---

## Purpose

기존 유지보수 평가는 성공 이력이 없으면 곧바로 due가 되고 기본 주기가 5개다. 파일별 평가가 도입되기 전의 성공 이력을 첫 파일별 평가의 기준으로 오인하지 않도록, 등록 시점 또는 안전한 기존 프로젝트 전환 시점의 MERGED 수를 별도로 보존하고 파일별 성공 기준과 구분한다.

## Acceptance Criteria

- 공통 `refactoring_review_interval` 기본값은 20이며 기존 양의 정수 설정과 MERGED ExecPlan 수 계산을 유지한다. 설정 예시와 프로그램 진입점의 기본 인자도 일치한다.
- 새 등록 프로젝트는 등록 시점에 catalog와 대조해 확인한 MERGED 수를 첫 파일별 평가 시작 기준으로 한 번 기록한다. 기존 프로젝트는 진행 중인 기존 유지보수 주기를 완료하거나 복구한 뒤 안전한 선택 경계에서 그 시점의 수를 한 번 기록한다. 재시작·재등록·반복 실행이 기준을 덮어쓰지 않는다.
- 첫 파일별 성공 전에는 과거 일반 유지보수 성공 이력과 관계없이 시작 기준 이후 설정 주기만큼 새 MERGED가 쌓여야 파일별 정기 평가가 due다. 그 뒤에는 별도의 마지막 성공 파일별 평가 MERGED 수가 기준이다. 과거 성공 이력과 self 전용 일회성 전수 평가 기록은 보존한다.
- 완료 후보의 10개 안정화 조건은 첫 파일별 성공 전에는 적용하지 않는다. 활성 기존 cycle, Worker·PR·차단 및 READY 장벽의 안전한 선택 순서를 유지한다.
- 새 파일별 평가 실행 경로가 G-086에서 검증·연결되기 전에는 준비된 일정 정보만으로 기존 평가를 파일별 성공으로 표시하거나 미완성 파일별 평가를 시작하지 않는다. 기존 주기의 재개는 허용하되, 신규 정기 주기가 도래하면 새 경로가 준비될 때까지 안전하게 대기하고 완료로 표시하지 않는다.

## Implementation

- `managed_config.py`, 설정 예시 및 `loop_manager.py`의 interval 기본값을 20으로 통일한다. `maintenance_state.py`에는 nullable 첫 시작 MERGED 수와 마지막 성공 파일별 평가 MERGED 수를 호환 가능한 추가 schema로 보존하고, 기존 `last_success_merged_count`와 self 전수 평가 marker는 그대로 둔다.
- 등록 시 검증된 catalog의 count와 기존 프로젝트의 안전한 IDLE 경계에서 확정한 count를 구분해 idempotent하게 저장한다. 활성 기존 cycle 중에는 시작 기준을 미루고 완료·복구 후 확정한다. 첫/후속 파일별 평가의 due 계산을 독립 함수로 준비하고 기존 실행 호출부의 전환은 G-086에 맡기되, 구 평가를 새 정기 주기로 시작하는 경로는 막는다.
- source revision과 count의 확인 실패 또는 감소·불일치가 있으면 추측한 기준을 기록하지 않는다. G-086이 성공 기준을 확정할 때 사용할 명시적인 상태 API를 제공한다.

## Validation

- Worker는 `tests/test_maintenance_state.py`의 기준선·due targeted unit test와 `tests/test_managed_configuration.py`의 interval 설정 targeted unit test를 실행한다.
- 과거 성공 이력 유무, 기존 활성 cycle 복구, 새 등록과 기존 프로젝트 전환, 재시작, 첫 주기 19/20과 사용자 설정, self 일회성 marker, MERGED count 불일치에서 기준 보존을 검증한다.
- 전체 verify는 기존 유지보수 장벽·선택 순서와 설정 호환성을 확인한다.

## Progress

- [x] 기본 주기와 분리된 시작·성공 기준선 보존
- [x] 첫/후속 due 판단 및 전환 안전성 검증

## Decision Log

- R-042의 파일별 성공 시점은 기존 일반 유지보수 성공 시점과 구분한다. 실제 평가 및 성공 기준 확정은 G-086에서 연결한다.
