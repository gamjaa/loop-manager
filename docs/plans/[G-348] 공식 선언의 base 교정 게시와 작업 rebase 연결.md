---
id: G-348
title: 공식 선언의 base 교정 게시와 작업 rebase 연결
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
  - docs/requirements/[R-061] Agent 역할·문서 권한과 draft ExecPlan 사용자 판단.md
  - docs/requirements/[R-072] 입력 동일성이 증명된 구현 후보 성공 결과 재사용.md
  - docs/requirements/[R-091] 프로젝트 공정 배정과 우선순위 기반 병렬 작업.md
depends_on:
  - G-236
  - G-343
complexity: high
risk: high
priority: high
---

## Purpose

공식 ExecPlan 선언의 교정이 필요하면 최신 원격 base에서 게시·catalog/DB 반영을 먼저 확정하고, 보존된 작업 브랜치에는 그 base의 rebase로 전달한다. 유효한 base와 오래된 작업 선언의 차이, 실제 공식 선언 오류, 의미를 확정할 수 없는 관계를 구분해 필요한 처리만 연결한다.

현재 `execution/execution_revision_handler.py:_metadata_correction()`은 오류를 저장하고 복구/차단으로 전달한다. `catalog/requirement_correction_publication.py`는 source rename의 base 교정 게시를 제공하며 일반 planning도 선언 게시·catalog 반영을 수행한다. 새 rebase frontmatter 복원 intent 생성 제거·역사적 원격 작업 선언 수용과 G-343의 원인 입력/명시 요청 재검사는 현재 원격 구현으로 재사용한다. 이 작업에는 선언 오류 분류부터 기존 게시 결과의 반영·작업 재개까지 남은 연결과 중단 복구만 둔다.

## Acceptance Criteria

- R-082: 최신 원격 base의 공식 선언이 유효하고 작업 브랜치만 오래됐으면 base 수정·교정 commit 없이 해당 base를 반영한다. 과거 작업의 ID·draft, 공식 revision·catalog 관계와 원격 작업 소유권은 확인한다. 이미 공식 revision을 포함한 후보의 실제 불일치, 조회 실패·다른 ID·필수 사용자 결정은 오래된 선언으로 간주하지 않는다.
- 교정은 현재 명세·참조·의존 관계와 기존의 확정 가능한 교정 규칙 또는 검증된 허용 planning 결과로 결정되는 범위에 한정한다. 필요한 dependency를 삭제하거나 SQLite 값을 정답으로 삼아 사용자 선언을 덮지 않는다. 의미·소유권·권한이 불명확하면 해당 작업과 원문·이유를 기존 판단/복구 경로에 보존하고 독립 작업은 계속한다. ExecPlan·코드 변경만으로 새 Planner를 호출하지 않는다.
- 교정이 필요하면 최신 원격 base에서 별도로 준비·게시하고, 같은 공식 선언의 catalog/DB 반영을 확인한 뒤 작업 브랜치에 rebase한다. 게시 또는 DB 반영이 미확정이면 다음 실행을 시작하지 않는다. 작업 브랜치에 별도 선언 편집·복원 commit·cherry-pick을 만들지 않는다.
- 교정 준비·commit·push·catalog/DB 적용·작업 rebase 사이의 중단과 원격 base 전진에서 확인된 결과부터 재개한다. 이전 원문·게시 intent·현재 원격 결과를 재확인해 중복 교정/게시를 막고 새 base에서는 교정 필요성과 유효성을 다시 판단한다. 기존 본문·mode·코드·파일/index·session·checkpoint·실행/실패 감사와 다른 계획의 상태를 보존한다.
- R-055/R-061: rebase 시작·Worker 종료·Git 완료의 검사 시점을 유지한다. 실제 Worker가 만든 금지 변경만 되돌리는 부분 복원은 G-284의 별도 경계이며 공식 선언 교정의 대체 수단이 아니다. Git 완료 후 공식 선언·전체 최종 diff·문서/archive와 작업물 보존을 검사하고 남은 불일치는 채택하지 않는다.
- 이미 저장된 과거 `rebase_frontmatter` intent만 현재 bytes·mode·index·소유권·정확한 tree/commit 관계를 검증해 이어간다. 새 intent는 생성하지 않는다. 손상·경쟁·다른 파일 변경에서는 자료를 보존하며 과거 intent 복구를 구현·targeted test·verify·Review 성공 근거로 사용하지 않는다.
- G-236의 pause·현재 G-343의 원인 관련 입력/명시 요청 검사를 소비한다. 자동 재검사는 비교 가능한 worktree/원격 ExecPlan·source 지문 변화 또는 같은 block/generation에 결합된 진행 중 Git 관측에서 안전한 START로 돌아온 근거를 요구한다. 명시 요청은 접수 당시 대상·원인·checkpoint generation을 확인하고 소비·해제·checkpoint 전이를 원자 확정한다. 교정 게시·rebase로 입력이나 generation이 달라지면 낡은 요청을 재사용해 해제하지 않으며, 코드/검사 정책 전진이나 과거 해제 표시만으로 자동 재개하지 않는다. 교정 게시나 rebase를 완료·검증 성공으로 만들지 않고 R-072의 단계별 실제 입력과 유효 근거로 필요한 검증만 선택한다. dependency의 실제 Merge·사용자 결정·원격 lease·writer 종료와 필수 검증 gate를 유지한다.

## Implementation

- [ ] 확인한 `execution/execution_revision_handler.py:_metadata_correction()`, `recovery/blocked_plan_reconciliation.py`, `recovery/project_block_service.py`의 오류 전달·현재 선언 조회·재개 경계에서 공식 base 오류와 오래된 작업 선언을 구분한다. 분류와 외부 Git 관측·게시·DB 적용의 책임을 분리한다. G-343의 `metadata_block_git`·원격/작업 지문과 `local_recovery_state.py`의 요청 snapshot·원자 소비는 재사용하며, 교정 게시 결과를 이 입력과 현재 generation에 연결한다. 재검사 계기나 요청 소비를 별도로 구현하지 않는다.
- [ ] 확인한 `catalog/requirement_correction_publication.py:RequirementCorrectionPublisher`·`catalog/requirement_sources.py`의 source 정합화, `planning/planner_publication.py`·`planning/publication_service.py`·`storage/execplan_catalog_state.py`의 검증된 선언 반영을 재사용한다. 기존 source 교정·허용 planning 게시를 다시 만들지 않고 선언 교정의 현재 입력/결과 identity와 미완료 반영을 연결한다. 기존 게시 intent/CAS·Manager Git journal·자원 잠금 안에서 필요한 호환 확장만 하며 범용 교정 엔진·별도 상태 원본·새 CLI를 추가하지 않는다.
- [ ] 확인한 `recovery/work_branch_integration.py:synchronize()`, `git/managed_rebase_runtime.py`, `git/rebase_documents.py`의 고정 target·문서 채택·최종 검증을 소비한다. `c9f89f11`의 역사적 선언 수용과 `8869fe08`의 새 복원 intent 생성 제거는 재구현하지 않는다. base 게시·catalog/DB 확정 뒤 기존 실행을 보존해 rebase/후속 검증으로 이어지게 하고 저장된 과거 intent의 bytes/tree 검증을 유지한다.
- [ ] 확인한 `storage/execution_checkpoint_state.py`·`storage/runtime_state.py`의 generation·실행 원본·planning 재개 대상과 기존 교정 게시 저장으로 준비/게시/적용/통합의 확정 지점을 구분한다. 외부 관측 중 DB 쓰기 요청을 열어 두지 않고 적용 직전에 대상·원인·원격·소유권·generation을 재확인한다. 경쟁/손상에서 작업을 초기화하거나 완료한 Git 효과를 반복하지 않는다.
- [ ] 아래 기존 테스트의 관측 double·실제 SQLite·임시 파일을 재사용해 남은 연결만 검사한다. PRODUCT·ARCHITECTURE의 선언 교정/복구 소유 절에 실제 구현 결과를 통합한다. Worker가 Requirement·다른 ExecPlan·선택 frontmatter를 수정하게 하지 않는다.

## Validation

- Worker는 변경한 분류·게시 결과 소비·재개 연결의 최소 unit만 실행한다. `tests/test_work_branch_integration.py:RemoteIntegrationAnchorTests`의 역사적 선언 case를 재사용하고 공식 base가 이미 포함된 불일치·공식 원문 무효·ID/draft 오류·조상 조회 오류는 실제 파서/판단과 저장 상태로 구분한다. 유효한 base/오래된 작업에는 교정 쓰기 0회, 미확정 입력에는 통합·새 호출 0회여야 한다.
- `tests/test_requirement_correction_publication.py`·`tests/test_planning_publication.py`·`tests/test_execplan_catalog_state.py`의 기존 게시/CAS fixture에 공식 교정 → 원격 확인 → catalog/DB 일치 → 작업 rebase 순서를 연결한다. 확인 가능한 source 교정과 검증된 선언 개정만 소비하고 존재하지 않는/의미 불명 dependency·draft/PR/수동 소유 경합에는 임의 제거·덮어쓰기 0회를 확인한다. 기존 Requirement 식별 정리와 source rename 행렬은 복제하지 않는다.
- 교정 준비 뒤 base 전진, push 확인 뒤 DB 적용 전 중단, DB 적용 뒤 rebase 전/중 중단과 재시작을 기존 관측/SQLite fixture로 검사한다. 완료 commit/push/적용의 반복 없음, 본문·mode·코드·session·실패·checkpoint 보존과 정확한 target/lease 확인이 필요하다. G-236의 pause fixture와 현재 `tests/test_scope_block_revalidation.py:EvidenceFreeRebaseBlockTests`·`tests/test_local_environment_recovery.py`는 소비 지점만 연결한다. 교정 뒤 실제 관련 문서 지문 변화의 재검사와 SHA/정책만 전진한 무변경의 보존, 접수 뒤 원인/generation 변경 시 미소비 보존·낡은 해제 거부를 확인한다. 기존 요청·원자 rollback 전체 행렬을 복제하지 않으며 pause resume 자체의 게시/rebase/Worker 효과는 0회다.
- `tests/test_rebase_definition_boundary.py`의 새 복원 commit 금지·최종 선언 거부 회귀를 유지한다. 제거된 새 intent 생성/직접 복원 성공 기대는 되살리지 않는다. 계속 지원하는 저장된 legacy intent의 write·stage·commit 이후 재개는 현재 `restore_rebase_frontmatter()`를 실제 bytes/index/tree 관측과 실행해 보존·멱등성·손상/경쟁 거부를 검사한다. `tests/test_execution_rebase_correction_phase.py`의 완료 표식 double만으로 실제 legacy 복구를 검증했다고 간주하지 않는다.
- 실제 base 게시와 작업 rebase의 효과는 기존 격리 Git fixture의 최소 연결 사례로 유지한다. 확인할 추가 효과는 base에만 교정 commit 존재, 작업 이력에 별도 복원/cherry-pick 없음, 작업 본문/구현 보존과 최종 공식 선언 일치다. 상태 조합은 mock 관측으로 분리하고 Worker는 실제 Git lifecycle을 실행하지 않는다. 기존 unittest version 1·launcher Python 3.11–3.14·수집/fixture·필수 compile·신뢰된 runner 통합 범위를 보존한다.

## Progress

- [x] revision `8fe2686a4fb360883602faf3951b6ac82759fc99`의 G-343 archive 이동과 metadata 입력 비교·명시 요청/generation·원자 소비 구현 및 회귀를 정적으로 대조했다. 기존 선행 ID는 유지하고 재구현 없이 남은 base 교정의 소비 조건에 연결했다. 구현·테스트·verify는 실행하지 않았다.
- [x] revision `c9f89f1114f57b8774c0b564189a30be9ce63abd`와 비교 기준 `9a70123d9b19845567536b4a06eb6b1c2e81ca3f`의 명세·코드·테스트 delta, 관련 catalog와 교정 게시/metadata 복구 경계를 정적으로 대조했다. 직접 구현된 두 국소 교정과 source 게시 경로를 확인했으며 구현·테스트·전체 verify는 실행하지 않았다.
- [ ] 공식 선언 분류·base 교정 게시/반영·보존 작업 rebase 연결
- [ ] 중단/원격 경쟁·legacy intent 보존의 최소 회귀와 소유 문서 정합성

## Decision Log

- G-343의 metadata 재평가 입력·명시 요청·원자 소비는 이번 원격 구현으로 소비하고 같은 ID dependency를 유지한다. archive 이동만으로 실행 성공을 합성하지 않으며 실제 Merge 근거는 Manager가 확인한다. G-284는 종료한 Worker의 직접 금지 변경만 복원하고 이 작업은 공식 선언의 base 게시/반영과 작업 통합을 맡는다. G-236의 pause 경계를 먼저 소비하며 선행의 실제 Merge 전에는 실행하지 않는다. G-295의 Windows 예산, G-337/G-338의 planning 잔여 정리/상호 대기와 G-325의 무진전 이력은 별도 목표다. snapshot token `7b52c07e28a762a9132691c8f24dc300fe40dadae8afc732ae607d67de6b7387`의 ID·경로·metadata·dependency와 기존 기록을 보존했다.
- R-082의 새 base 교정 절과 AC가 근거다. `_metadata_correction()`의 오류 보존이나 새 복원 commit 금지만으로 전체 흐름이 충족됐다고 판단하지 않는다. 이미 base가 유효한 경우의 역사적 선언 수용·source rename 게시·일반 planning 등록은 소급 계획하지 않는다. 의미 불명 dependency는 임의로 확정하지 않고 기존 판단 대기를 보존한다.
- complexity high·최초 level 5: 독립된 base 게시·catalog/DB 반영·기존 작업의 고정 rebase를 중단/경쟁에서도 같은 선언에 연결하고 과거 intent 수명을 보존해야 한다. risk high: 순서/소유권 오판은 잘못된 선언으로 실행하거나 원격 작업·기존 기록을 훼손해 수동 복구가 필요할 수 있다. 최신 원문/lease 재확인·게시 후 CAS·작업물 보존과 단계별 중단 회귀로 제한한다. priority high는 공식 선언 오류의 안전한 자동 복구와 게시 계약을 지키기 위한 것이다. Model·Effort는 Manager의 명시 설정을 따른다.
