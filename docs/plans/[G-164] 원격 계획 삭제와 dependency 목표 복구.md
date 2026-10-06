---
id: G-164
title: 원격 계획 삭제와 dependency 목표 복구
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on:
  - G-163
complexity: high
risk: high
priority: critical
---

## Purpose

원격에서 확인한 미착수 삭제·archive 이동·번호 공백을 구분하고 삭제된 선행 계획의 목표를 보존하는 자동 복구를 제공한다.

## Acceptance Criteria

- 번호 공백을 실패나 삭제의 근거로 쓰지 않는다. 실제 원격 삭제·archive 이동·삭제와 새 계획 추가를 구분하고 확인된 미착수 취소 감사, source·ID·실행/PR 이력을 보존한다. 삭제 ID를 재사용하지 않는다.
- 삭제 계획을 참조하는 관계는 최신 제품 목표에 맞는 적격 READY 개정·새 ID 대체·관계 정리를 먼저 시도한다. 아직 필요한 선행 목표를 dependency 제거로 우회하지 않는다.
- 유효한 최종 관계를 하나의 검증된 원격 게시와 catalog 반영으로 확정한다. 목표·소유권·영향 범위를 확정할 수 없으면 그 범위만 보류하며 독립 작업·PR 관측은 계속한다.
- 중단·재시작·경쟁에서 같은 취소·대체·게시를 중복하지 않는다. 실행된 계획·소유 작업 공간·기존 PR과 snapshot 밖 파일을 자동 개정하지 않는다.

## Implementation

- catalog/catalog_service.py·project_catalog_refresh.py의 원격 reconciliation과 취소 저장 경계를 재사용한다. 삭제 관측은 실제 tree·이력과 채택 revision에 결합한다.
- planning의 일반 mutable READY snapshot·결과 검증·게시 절차로 목표 복구를 요청한다. 의미가 불명확한 대체를 결정적 코드로 합성하지 않으며 복구 전후 의존 graph 전체를 검증한다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- catalog refresh·batch·cancellation 관련 기존 테스트에서 번호 공백, archive, 미착수 삭제, 실행 이력, 신규 추가 동시 관측, 선행 목표 보존의 최소 unit case를 실행한다.
- Manager 전체 verify는 삭제 복구의 게시/SQLite 중단·경쟁, 유효 dependency gate와 독립 계획 지속을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 원격 삭제 증명·취소 감사·삭제 목표 snapshot 및 검증된 게시 복구 구현과 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 원격 관계·목표 판단·원자적 채택이 연결되어 complexity high, 선행 목표 손실의 영향으로 risk high다. 취소를 Merge나 Requirement 충족으로 간주하지 않는다.
- `catalog/remote_deletions.py`가 이전 채택 revision의 일반 파일·hash·metadata와 최신 원격 ancestry/부재를 확인한다. 기존 무소유 READY 판정과 게시 CAS를 재사용하며, 실행/PR/별도 감사 또는 프로젝트 실행 소유권이 있으면 자동 취소하지 않는다. archive는 같은 ID 관측으로 구분하고 삭제·신규 추가를 ID 변경으로 추측하지 않는다.
- 취소 감사의 nullable `recovery_evidence` 추가는 기존 DB와 호환된다. 삭제 원문·source·최초 게시 및 채택 row를 보존하고, 확인된 원격 revision에서 취소와 독립 신규 등록을 같은 transaction으로 적용한다. 취소·복구 감사도 reconciliation CAS와 refresh cache identity에 포함한다.
- 삭제 목표의 영향은 dependency와 보존된 본문 참조로 식별한다. 목표 판단 없이 원격 dependency만 제거해도 해당 계획은 계속 보류한다. 유효한 영향 READY 선언은 보류 아래에서 동기화하고 일반 mutable snapshot으로 개정하며, 실행된 계획과 snapshot 밖 보류 파일은 보존한다.
- Planner는 영향 계획의 Decision Log에 `DEPENDENCY_RECOVERY` JSON으로 삭제 ID, 최신 source, 이유와 대체 dependency 또는 목표 불필요 판단을 기록한다. 코드가 목표 의미를 합성하지 않고 판단의 존재·source·유효한 전체 graph와 대체 gate를 검증한다. 불확실한 영향은 보류한 채 독립 batch를 게시할 수 있다. snapshot token·publication manifest에 목표 감사를 결합하고, 원격 게시 확인 뒤 개정·대체 등록·판단 감사·해당 hold 해제를 함께 확정한다.

### 삭제·복구 경계 대응표

| 경계 | 조건과 scope | 보호·복구 및 사용자 조치 |
|---|---|---|
| `catalog_service` / `remote_deletions.prove_deletions` | 원격 tree에서 ID 부재, 이전 채택 bytes·ancestry 확인 | 미착수·무소유만 취소한다. 번호 공백·archive 이동·동시 신규 추가는 삭제나 ID 변경의 추정 근거가 아니다. |
| `CatalogReconciliationService` / `DefinitionStateRepository.apply` | 삭제 이력 미확인·실행/PR 이력·삭제 목표의 영향 | ID·실행·PR·취소 감사와 dependency closure를 보존해 해당 범위만 hold한다. 독립 선언·PR 관측은 계속하며, 공유 실행 소유권이 서로 다를 때만 프로젝트 안전 진단을 유지한다. |
| `mutable_ready_snapshot` / `validate_planner_result` | 일반 snapshot에 속한 무소유 READY의 삭제 목표 복구 | 보존 원문과 최신 source를 Planner에게 전달한다. 명시적 목표 판단 없는 dependency 제거·대체 gate 누락·snapshot 밖 개정·미해결 graph는 거부한다. 불확실한 목표는 사용자 명세/계획 판단까지 보류한다. |
| 일반 planning publication intent / batch apply | snapshot·감사·Git 후보 및 원격 확인 일치 | 기존 단일 commit·manifest·SQLite transaction을 사용한다. 중단은 저장 intent로 복구하고 경쟁은 rollback/CAS로 거부하며 동일 취소·대체·게시를 중복하지 않는다. |

### 검증 근거

- `tests.test_incremental_planning.IncrementalPlanningCatalogTest`의 `remote_deletion` 선택 unit case: 취소·archive·번호 공백/신규 추가·실행/소유권 보존·본문 참조·목표 판단/gate·독립 batch·원격 dependency 제거·hash 미확인·감사 경쟁·중단/원격 경쟁·게시 후 intent 복구/멱등성을 검증한다.
- 기존 catalog 신규 등록/취소 ID 보호, mutable 실행 이력 제외, 혼합 batch 원자성, 게시 intent 복구와 nullable snapshot manifest의 개별 unit case도 통과했다. 최초 targeted 실패는 테스트의 nullable 최초 게시 SHA 기대값과 손상 감사 JSON 예외를 수정한 뒤 같은 명령으로 재검증했다.
- 기존 Linux `scripts/verify.sh`는 표준 verification runner를 실행하고 실패 종료 코드를 전달하므로 유지한다. Worker는 전체 verify를 실행하지 않았으며 Manager 검증·Review·사용자 Merge는 별도 완료 조건이다.
- Manager 검증에서 발견된 기존 선언 오류의 실행 보류·재개 회귀는 삭제 소유권 검사가 모든 hold에 적용된 것이 원인이었다. 추가 검사를 실제 `missing-plan` 원인으로 제한했다. 두 기존 보류·재개 unit case와 실제 삭제 시 충돌 소유권·원본 보존 case를 확인하며, 기존 공유 branch/worktree 및 Agent scope 검사는 유지한다.
