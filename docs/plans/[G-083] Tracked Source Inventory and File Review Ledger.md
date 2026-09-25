---
id: G-083
title: Tracked Source Inventory and File Review Ledger
sources:
  - docs/requirements/[R-042] Periodic Repository-Wide Code Assessment for Token-Efficient Refactoring.md
depends_on: []
complexity: high
risk: high
priority: normal
---

## Purpose

현재 주기 평가에는 실제로 검토한 파일의 목록과 원본 identity가 없다. 한 원격 revision의 추적 파일을 언어에 관계없이 분류하고, 마지막 성공 검토 기록과 비교할 수 있는 독립 경계를 만든다.

## Acceptance Criteria

- 검증된 최신 `origin/<base-branch>` revision의 Git 추적 파일에서 코드·테스트·실행·검증 스크립트를 결정적 순서로 inventory한다. 확장자 고정 목록으로 대상을 누락하지 않는다. 문서·ExecPlan·archive·생성물·캐시·외부 의존성을 제외하고 경계가 불분명한 추적 파일은 분류 근거를 남기거나 평가를 실패 처리한다.
- 각 대상의 경로, 원본 내용 지문과 Git 실행 모드를 해당 revision에 결합한다. 경로가 새롭거나 마지막 성공 검토의 경로·내용·mode가 다를 때만 선정한다. commit 범위와 작업 공간 mtime은 선정에 사용하지 않는다.
- 삭제 파일은 현재 대상에서 제외하고 이전 기록을 안전하게 정리할 수 있다. rename은 새 경로에서 미검토로 취급한다. 다른 파일·문서만 바뀐 경우 동일한 대상 내용은 재선정하지 않는다.
- 프로젝트별 마지막 성공 검토 시각·source revision·경로·지문·mode를 호환 가능한 SQLite 저장소 경계로 제공한다. 검토하지 않은 파일의 성공 기록을 전진시키는 API를 허용하지 않는다.
- 첫 정상 평가에 기록이 없는 기존 파일은 모두 선정되며, 등록·배포 때 inventory를 이유로 전수 평가나 Agent 호출을 시작하지 않는다. self 전용 G-059의 코드·문서 inventory와 완료 marker는 독립으로 유지한다.

## Implementation

- 별도 유지보수 inventory/ledger 모듈에 Git tree 조회, 파일 분류, mode·내용 지문 검증과 선정 함수를 둔다. 추적된 실제 파일의 역할은 경로·Git metadata·프로젝트 실행 계약에 근거해 분류하고, 불확실한 경우 조용히 제외하지 않는다.
- 프로젝트별 성공 검토 ledger를 additive schema로 만들고 source identity를 비교하는 읽기 API 및 검증된 평가 결과만 원자 transaction에서 기록할 수 있는 쓰기 API를 제공한다. 호출부의 전체 assessment orchestration 연결은 G-086에서 수행한다.
- G-059의 self 전용 inventory를 주기 평가의 출처로 재사용하지 않는다. 기존 유지보수의 bounded Git 변경 문맥은 평가 대상 선정 근거로 사용하지 않는다.

## Validation

- Worker는 새 inventory/ledger 모듈의 targeted unit test와 직접 변경한 `tests/test_maintenance_state.py`의 ledger 관련 targeted test만 실행한다.
- Python 외의 언어와 확장자 없는 실행 파일, 테스트·verify 스크립트, 문서·archive·의존성 제외, 불확실한 파일 분류, 내용 동일·mode 변경·rename·삭제·새 파일, 원격 revision drift 및 잘못된 지문을 검증한다.
- 전체 verify는 기존 self 전수 평가와 등록·기존 프로젝트의 유지보수 흐름이 유지되는지 확인한다.

## Progress

- [ ] 결정적 원격 파일 inventory와 분류 근거
- [ ] 파일별 성공 검토 ledger와 선정 API

## Decision Log

- inventory와 ledger는 후속 평가 단계가 사용할 검증 가능한 자료 경계다. 이 계획 자체는 성공 평가나 계획 게시를 기록하지 않는다.
