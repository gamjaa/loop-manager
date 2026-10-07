---
id: G-203
title: ExecPlan archive 경로 identity 계약 정합화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-014] Automatic Completed ExecPlan Archiving.md
depends_on: []
complexity: medium
risk: high
priority: high
draft: true
---

## Purpose

재활성화된 R-014의 최초 활성 경로 보존과 현재 catalog의 선언 경로 갱신 사이 적용 계약을 확정한다. 최초 게시 감사·PR 불변 링크·Merge 증명을 보존하면서 archive와 신규 환경 복구가 같은 경로 의미를 사용하게 한다. 선택 전에는 구현하지 않는다.

## Draft Decision

Question: R-014의 SQLite `path`·archive 목적지 계약을 최초 게시 identity와 현재 선언의 분리로 명시할지, 원문대로 `exec_plans.path`와 archive 파일명을 최초 활성 경로에 고정할지 결정하고 R-014 및 관련 ARCHITECTURE를 정합하게 명시해 달라. 게시 뒤 동일 ID의 title/path 개정이 있을 때 사용할 archive 파일명도 포함한다.
Facts: R-014 Requirement 및 Acceptance Criteria 14는 SQLite `path`를 최초 활성 경로로 보존하고 최초 파일명으로 목적지를 계산한다. PRODUCT의 `### PRODUCT, Requirement와 ExecPlan`과 ARCHITECTURE의 `### 원격 catalog와 실행 이력의 분리`는 현재 path/title과 최초 게시 감사를 분리한다. `project_catalog_refresh.apply_archive`와 `execplan_catalog_state`는 archive 뒤 `path`를 실제 archive 경로로 갱신하며 `publication_path`·`publication_sha`를 보존한다. `test_planning_reuses_proven_archive_catalog_without_reread`는 그 갱신을 요구하고 archive repository는 현재 파일명으로 목적지를 계산한다. R-014 원문만 다시 활성화됐고 해당 코드·테스트는 비교 구간에서 바뀌지 않았다.
Options: A는 현재 선언 경로 및 최신 파일명을 archive에 사용하고 최초 경로·SHA는 publication 감사와 PR 링크에 보존하는 계약을 R-014에 명시한다. B는 R-014의 최초 경로·파일명 고정을 유지하며 실제 파일 관측·현재 title/path·재개용 위치를 별도 기존 경계에서 표현하도록 명세를 보강한다. 어느 선택도 Merge 증명·내용 보존·ID 및 의존성 보호를 완화하지 않는다.
Impact: A는 기존 구현이 이미 만족하는 부분을 유지하므로 명세 보강 뒤 남은 코드 작업을 다시 판단해야 한다. B는 archive·catalog cache·migration과 재개의 경로 해석 및 대응 테스트 변경이 필요하다. 결정과 명세 정합성이 확인될 때까지 이 계획과 의존 계획의 Worker·workspace·branch·PR을 시작하지 않으며 독립 작업은 계속한다.

## Acceptance Criteria

- 사용자 결정과 원격 명세가 최초 게시 identity, 현재 선언, archive 목적지의 의미를 모순 없이 정한다. 동일 ID의 active rename, Merge 뒤 archive 및 신규 환경 복구에서 선택한 의미가 관찰된다.
- 최초 게시 path/hash/SHA와 기존 PR 불변 링크, 실제 GitHub Merge 증명 및 의존성·ID 이력을 보존한다. archive 위치만으로 완료를 추정하지 않고 identity·hash 불일치와 목적지 충돌은 덮어쓰기 없이 후보별 진단으로 남긴다.
- push 전후 및 SQLite 확정 전 중단에서 같은 최종 파일과 저장 identity로 수렴한다. 정상 후보는 충돌 후보와 분리해 처리하고 실패가 MERGED·독립 작업·완료 cycle을 되돌리거나 Planner 재호출을 만들지 않는다.
- 명세 보강만으로 최신 코드와 검증 근거가 모두 충족되면 일반 Planner가 남은 구현 필요성을 다시 판단한다. 직접 문서 결정에 소급 구현·자동 검증 성공을 만들지 않는다.

## Implementation

- 미결정: A/B의 저장 필드·파일명·복구 해석을 선택하지 않는다. 사용자 소유 R-014·ARCHITECTURE는 Planner가 수정하지 않는다.
- 결정 뒤 기존 `catalog/project_catalog_refresh.py`, `catalog/execplan_archive.py`, `storage/execplan_catalog_state.py`, `storage/execplan_archive_state.py` 및 `storage/catalog_migration.py`의 관측·원자 적용·재개 책임을 재사용한다. 선택에 필요한 최소 변경만 수행하며 새 상태 원본이나 루트 파일을 만들지 않는다.
- 자동 역할·문서·검증 권한은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·계획 작성은 AGENTS.md·docs/PLANS.md를, 설계는 ARCHITECTURE의 원격 catalog·계획 게시·MERGED ExecPlan archive 절을 따른다. R-027 감사의 원문 복원 대기는 이 계획의 구현 범위에 포함하지 않는다.

## Validation

- Worker는 선택된 경로 계약에 직접 관련된 archive candidate·catalog cache unit case만 실행한다. 최초 게시와 개정된 title/path, archive 목적지, publication 감사·PR 링크 보존 및 과거 저장 근거 부족 사례를 구분한다.
- Manager 전체 verify는 기존 archive·catalog recovery fixture로 active rename→실제 Merge→archive→신규 DB 복구, push 후 SQLite 미확정 재개, 충돌한 목적지와 정상 후보 분리, archived dependency 및 archive-only Planner·완료 알림 억제를 확인한다.
- Reviewer는 결정된 계약과 실제 저장 필드·Git 파일·기존 링크를 대조하며 최초 게시 감사 손실, Merge 증명 생략 또는 다른 후보의 identity 채택을 blocking issue로 확인한다. 코드 rollback만으로 이미 게시된 경로와 운영 이력이 복원된다고 가정하지 않는다.

## Progress

- [x] 2026-10-07 기준 54b3484550d9e1579511cffdc73f4c3b9618e35a의 R-014 원문·변경 diff·catalog 구현과 archive·복구 테스트를 정적으로 대조했다. 구현·테스트 실행은 하지 않았다.
- [ ] 사용자 결정과 원격 명세 정합성 확인
- [ ] 필요한 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 기존 G-032/G-033의 Merge나 경로 이동만으로 R-014 전체 충족을 추정하지 않는다. AC 14의 실제 불일치와 최신 PRODUCT·ARCHITECTURE의 경로 분리 규칙 때문에 판단은 uncertain이며, 같은 질문을 이 ID에서 보존한다.
- complexity medium: 선택 뒤 기존 archive·catalog·복구 경계의 명확한 경로 계약을 연결한다. 사용자 정책 선택은 높은 추론 난도로 대신 결정하지 않는다. risk high: 잘못된 경로/identity 반영은 완료 계획의 조회·의존성 복구와 PR 링크에 광범위한 오류를 만들 수 있다. 원본 Git 내용·최초 게시 감사·archive operation을 보존해 수동 매핑 복원과 안전한 재개가 가능하도록 하며 해당 실패·복구 조건을 Acceptance Criteria와 Validation에 포함한다.
