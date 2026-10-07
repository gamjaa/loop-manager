---
id: G-214
title: 공유 테스트 준비와 SQLite 연결 소유권 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-003] Targeted Worker Unit Tests.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

활성 Worker 준비의 세 중복 구현과 분산된 SQLite 연결 정리 책임을 공통 fixture 경계에 모은다. planning 입력 확정 여부와 반환 연결의 소유권 차이를 명시해 준비 계약이 서로 어긋나지 않게 한다.

## Acceptance Criteria

- `tests/support/worker.py`, `tests/support/recovery.py`, `tests/support/review_escalation.py`의 원격 생성→계획 게시→결정적 workspace→WORKING 투영 준비를 재사용한다. plan ID/title·일반 planning 입력 기록 여부·반환 계약의 의도된 차이를 명시적으로 유지한다.
- 소유 연결은 생성 직후 cleanup에 연결해 정상 종료·assertion/Git 예외·setup 실패에서도 닫는다. PublicationFixture와 workspace 충돌/예약의 직접 생성 연결도 이 규칙을 따른다. `_ready_execplan`의 기존 정리는 중복 등록하지 않는다.
- 실패 경계 테스트는 기존 AutomationCatalogFixture의 DB 준비를 재사용하되 WORKING 초기 상태, WAITING/TRANSIENT_PAUSE와 세 번째 실패의 알림 조건을 각 사례에 남긴다.
- 테스트별 실제 Git·SQLite·경로 격리, 복구 경계 identity/digest와 assertion을 보존한다. 사용자 동작·생산 공개 계약·schema/저장 형식·실제 Agent 없는 검증을 변경하지 않는다.

## Implementation

- 기존 `tests/support/lifecycle.py`, `tests/support/database.py`, ActiveWorkerFixture와 PublicationFixture를 우선 재사용한다. 활성 Worker 준비의 입력 확정과 자원 수명을 명시하고 기존 caller 반환값/호환 wrapper를 유지한다.
- `tests/test_execution_failure_boundary.py`, `tests/test_publication_base_validation.py`, `tests/test_workspace_collisions.py`, `tests/test_workspace_reservations.py`에서 직접 소유 연결과 공유 fixture 소유 연결을 구분한다. 생산 상태 전이/복구를 fixture로 복제하거나 새 전역 cache를 만들지 않는다.

## Validation

- Worker targeted unit: `tests.test_support_fixtures`, `tests.test_publication_fixtures`의 준비·정상/실패 cleanup·planning 입력 차이와 변경한 workspace/실패 경계의 최소 unit case. 연결 생성 후 실패에서도 close가 수행되고 빌린 연결을 먼저 닫지 않는지 확인한다.
- Manager 전체 verify: 실제 복구 history·Review profile 승격·게시 base 검증·workspace 중단 재개와 병렬 테스트 격리를 유지한다. 이전 결과를 현재 후보 성공으로 재사용하지 않고 기존 시나리오/assertion을 모두 수집한다.

## Progress

- [x] 저장된 평가 발견 16·20·29·30·32의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `tests/support/recovery.py:65`, `tests/test_execution_failure_boundary.py:12`, `tests/test_publication_base_validation.py:14`, `tests/test_workspace_collisions.py:234`, `tests/test_workspace_reservations.py:13`의 준비/수명 판단을 재사용했다.
- G-210의 세션 테스트 재배치와 G-215의 workspace 실행 준비 정리는 이 fixture 계약 확정 뒤 수행한다. 추가 기존 READY prerequisite는 없다.
- complexity medium은 명확한 준비 입력·소유권 차이의 연결, risk medium은 회귀 테스트의 준비/수명 오류로 잘못된 검증 결과를 만들 영향이며 격리된 fixture·실패 cleanup 검사와 전체 verify로 복구한다.
