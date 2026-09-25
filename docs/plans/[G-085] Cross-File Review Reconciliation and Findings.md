---
id: G-085
title: Cross-File Review Reconciliation and Findings
sources:
  - docs/requirements/[R-042] Periodic Repository-Wide Code Assessment for Token-Efficient Refactoring.md
depends_on:
  - G-084
complexity: high
risk: high
priority: normal
---

## Purpose

파일을 읽은 순서에 따라 중복 구현이나 역참조를 놓치지 않도록 모든 선정 파일의 잠정 결과를 종합한다. 실제 근거가 있는 구조 문제만 기존 비종료 계획 또는 새 동작 보존 리팩터링 계획으로 연결할 수 있는 검증된 발견으로 만든다.

## Acceptance Criteria

- 모든 선정 파일을 실제로 검토한 뒤 새 코드맵과 유효한 기존 코드맵의 참조·역참조·유사 책임 후보를 종합한다. 오래된 코드맵의 경로·지문·mode를 현재 원격에서 확인하고, 없거나 참조가 불명확하면 현재 원본의 필요한 부분을 읽는다.
- 뒤에 읽은 파일이 앞서 읽은 파일과의 관계를 드러내면 해당 관계와 필요한 원본 부분을 재확인한다. 파일 읽기 순서가 달라도 같은 source와 대상 집합에서는 확인해야 할 관계와 완료 조건이 같다. 전체 선정 파일을 매번 다시 읽는 별도 전수 단계는 두지 않는다.
- 각 관계는 확인 결과 또는 제외 근거를 기록한다. 필요한 관계가 미확인, 상충 또는 오래된 source에 기반하면 완료로 판정하지 않는다. 종합 단계도 중단·재개 가능한 단위로 진행한다.
- 발견은 대상 경로·원본 지문, 구체적 코드 근거, 처리 상태, 책임 경계와 중복·관련 없는 후속 문맥 감소 이유를 포함한다. 분리의 추가 탐색·연동 비용도 검토한다. 문제가 없으면 근거 있는 없음 결과를 허용한다.
- 같은 문제를 해결하는 비종료 ExecPlan이 있으면 그 ID에 연결하고 새 계획을 중복 생성하지 않는다. 새 리팩터링 계획은 사용자 동작·공개 계약·저장 형식을 보존하고 독립 검증·Merge 가능해야 하며 targeted unit test와 전체 verify 회귀 조건을 갖춘다. 파일 수·길이·토큰 수만으로 계획을 요구하지 않는다.

## Implementation

- G-084의 잠정 기록을 소비하는 관계 graph와 종합 결과 검증기를 별도 유지보수 모듈에 둔다. 코드맵 후보를 현재 Git 원본과 대조하고 참조 미확인 목록 및 확인 근거를 durable하게 추적한다.
- 유지보수 Planner의 최종 결과에 검증된 관계·발견·기존 plan 연결 또는 새 plan 의도를 포함하고, 기존 catalog의 비종료 plan 범위와 대조한다. 새 계획 파일의 실제 게시 및 성공 확정은 G-086에서 수행한다.
- 코드 변경이 필요하지 않은 경우 리팩터링 계획을 형식적으로 요구하지 않는다. 문서·AGENTS.md 개선은 self 전용 G-059/G-078/G-079 경로로 남긴다.

## Validation

- Worker는 관계 종합·발견 검증기의 targeted unit test와 직접 변경한 `tests/test_maintenance_planning.py`의 비종료 plan 연결 targeted test를 실행한다.
- 앞뒤 읽기 순서, 새·기존 코드맵 결합, 역참조, map 부재·무효화, 원본 참조 검증, 미확인 관계 차단, 중단·재개, 중복 plan 방지와 근거 있는 no-issue 결과를 검증한다.
- 전체 verify는 기존 유지보수 DAG 검증과 사용자 동작 보존 리팩터링 계획 계약을 확인한다.

## Progress

- [ ] 파일 간 관계 종합과 미확인 관계 추적
- [ ] 검증된 발견·기존 plan 연결 계약

## Decision Log

- G-085의 발견은 잠정 평가 결과다. 원격 계획 게시와 파일별 성공 기록의 동시 확정은 G-086이 맡는다.
