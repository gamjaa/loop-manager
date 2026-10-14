---
id: G-280
title: 원격 작업 통합의 ignored 경로 보호
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-022] Ignored-Only Managed Worktree Tolerance.md
  - docs/requirements/[R-055] 차단 경로 전수 조사와 불필요한 작업 중단 축소.md
depends_on: []
complexity: medium
risk: critical
priority: high
---

## Purpose

정상 원격 작업 입력을 통합할 때 활성 ignored 파일과 실제 Git 쓰기 경로가 겹치면 파일을 보존하고 해당 쓰기를 보류한다. 현재 `recovery/work_branch_integration.py:synchronize()`는 HEAD/원격 tip의 최종 diff만 대소문자를 구분해 비교한다. 이 검사는 로컬 commit 재생 중에만 나타나는 경로와 대소문자 별칭을 놓치며, 원격 후속 입력 처리의 소유 rebase abort가 이 검사보다 먼저 실행되는 경로도 있다. 이미 구현된 통합·재시작을 유지하면서 기존 공통 경로 검사와 실행 직전 재검사를 이 소비자에 연결한다.

## Acceptance Criteria

- incoming tracked 경로와 현재 index-free ignored 경로의 동일 경로·부모/자식 관계·대소문자 별칭을 기존 공통 규칙으로 판정한다. 최종 tip diff뿐 아니라 통합에서 실제 재생할 commit의 중간 경로도 포함하며, 별도의 확장자 allowlist나 원격 통합 전용 경로 비교 규칙을 만들지 않는다.
- 보존 commit 적용의 HEAD/index 변경, 실제 rebase 시작/재개와 원격 후속 입력을 위한 소유 rebase abort 중 ignored 파일을 덮어쓸 수 있는 효과 직전에 현재 경로를 다시 확인한다. 처음 검사 뒤 또는 중단 사이 생긴 충돌 파일도 보존한다. 충돌하면 HEAD·index·작업 파일·현재 Git operation을 바꾸지 않고 해당 쓰기만 보류한다. 현재 자원·후보·session·보존 intent와 실패 감사를 유지하며 새 성공·완료를 기록하지 않는다.
- 충돌하지 않는 ignored 파일의 생성·수정·삭제는 원격 입력 수용·동일 입력 복구를 막지 않는다. 자료 수집이나 경로 관계를 확인할 수 없으면 이를 빈 충돌 목록으로 바꾸지 않으며, 실제 쓰기의 미확인 범위와 기존 보존/실패 경계를 사용한다. ignored 파일을 삭제·복사·stage·stash·reset·clean해서 통합을 강행하지 않는다.
- 현재 작업의 origin·branch·PR identity·소유 operation target과 exact 원격 관측/lease 검증은 유지한다. 이미 완료한 소유 operation의 결과 기록은 파일을 다시 쓰는 단계와 구분한다. 정상 OID 전진·과거 snapshot 차이·무관한 ignored 내용 변화에 대한 Git 접근 감시나 새 프로젝트 전체 차단을 도입하지 않는다.
- 새 호출·보존 중 중단·재생 전 중단·충돌 중 원격 후속 입력·abort 직전 경쟁에서 같은 파일 보존 계약을 지킨다. 이미 완료한 Git 효과를 반복하지 않고 필요 단계에서만 재검사한다. 회귀는 실제 판단·SQLite checkpoint를 사용하고 관측/외부 효과만 mock한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/recovery/work_branch_integration.py`의 `synchronize()`·`integrate()`·`restart_base_rebase()`·`_abort_preserved_rebase()`에서 실제 파일 쓰기와 완료 결과 수용을 구분한다. 현재 최종 diff의 문자열 비교를 공통 검사 소비로 교체하고, HEAD/index 적용·rebase와 abort의 안전한 실행 직전에 현재 경로를 확인한다. 새 baseline·원격 통합 엔진·일괄 복구 계층을 만들지 않는다.
- [ ] 확인한 `loop_manager_internal/git/git_adapter.py`의 `_ignored_paths_overlapping_git_update()`·`_paths_overlap()`와 rebase action의 남은 commit/abort 검사, `git/direct_rebase_service.py`의 실제 시작 경계를 재사용한다. 복원/재생 target과 현재 operation에 맞는 helper를 선택하고 불명확한 경로 관측을 안전하게 전달한다. `recovery/recovery_adapter.py`·`recovery/bindings.py`의 기존 effect 계약에 필요한 callback만 연결한다. 다른 소비자의 동작이나 Git journal의 target/lease 계약을 바꾸지 않는다.
- [ ] 확인한 `tests/test_work_branch_integration.py`의 `WorkBranchIntegrationUnitTests`와 `tests/test_task_remote_inputs.py`의 기존 원격 통합 fixture를 확장한다. 초기/재개/후속 입력의 실제 handler·SQLite를 검사하며 common helper의 관측 입력으로 대소문자·경로 계층·중간 replay 경로를 구성한다. 충돌한 파일의 bytes 및 HEAD/index/operation 불변을 검증하는 최소 실제 Git 사례만 추가하고 정책 조합마다 fixture를 복제하지 않는다.
- [ ] 구현이 바꾸는 보존 책임·실행 직전 재검사만 PRODUCT의 Git 쓰기 보호와 ARCHITECTURE의 작업 branch 통합 절에 반영한다. R-022의 기존 보존 규칙과 R-055 T19를 충족하는 교정이며 원격 입력·삭제 브랜치 재시작·파일별 복원 정책을 확대하지 않는다.

## Validation

- Worker는 `python -m unittest tests.test_work_branch_integration.WorkBranchIntegrationUnitTests` 중 추가/변경한 unit의 최소 범위로 실제 통합 handler·공통 판정·SQLite를 확인한다. 신규 case는 동일/부모/자식/대소문자 경로, tip diff에서는 사라진 replay 경로, 충돌 없는 ignored 변화와 관측 실패를 포함한다. helper의 성공 반환값으로 실제 경로 판정을 대체하지 않는다.
- 초기 확인 뒤 보존 commit 적용 전, rebase 실행 직전, 중단 후 재개 및 원격 후속 입력의 abort 직전에 ignored 충돌이 생기는 관측을 제공한다. 파일을 바꾸는 Git 명령이 호출되지 않고 현재 checkpoint·후보·session·실패 이력이 보존되는지 검사한다. 완료한 rebase의 기록 수용과 충돌 없는 진행에서 불필요한 쓰기 반복이나 재차단이 없어야 한다.
- 실제 Git 효과는 기존 격리된 로컬 원격 fixture의 최소 사례로 확인한다. 중간 commit이 경로를 추가했다가 삭제해 최종 diff에는 없지만 ignored 원문이 남은 입력에서 통합을 시도하고, 해당 bytes·HEAD·index·나머지 작업물·operation 보존을 assert한다. 대소문자 별칭의 모의 경로 판정은 native 파일시스템 동작의 성공으로 기록하지 않는다. 관련 native 통합 회귀는 Manager의 기존 검증 경계에서 실행한다.

## Progress

- [x] delta 원본에서 단순 최종 diff 비교, 그 앞의 소유 rebase abort와 공통 helper의 대소문자/replay 검사를 정적으로 대조했다. 코드·테스트 실행은 하지 않았다.
- [ ] 공통 경로 보호의 원격 통합 연결과 실행 직전 검사
- [ ] 최소 targeted unit 및 기존 통합 회귀의 보존 assertion
- [ ] 관련 제품·설계 문서 반영과 Manager verify·Review·사용자 Merge

## Decision Log

- 정상 원격 통합·삭제 재시작·파일별 복원은 이미 직접 구현됐다. 새 계획은 현재 확인한 ignored 파일 보존 누락만 다루며, 전체 차단 경로 감사는 이 교정의 결과를 소비한다. 기존 계획의 snapshot 권한을 넓히거나 archive를 새 작업 근거로 읽지 않는다.
- complexity medium: 경로 판정·재생/abort 관측 helper와 소유 integration intent가 이미 존재한다. 다만 시작·보존 commit 적용·중단 재개·후속 입력 abort의 실제 효과 경계에 일관되게 연결해야 하므로 단일 문자열 수정으로 완료할 수 없다. 새 저장 형식·주요 infrastructure는 필요하지 않다.
- risk critical: index-free ignored 원문은 Git의 baseline/commit에 들어가지 않아 잘못된 checkout/rebase/abort가 덮어쓰면 코드 rollback이나 기존 Git 이력으로 복구할 수 없다. 쓰기 직전 충돌 거부·원문 보존과 실제 effect assertion이 필요하다. medium complexity와 critical risk의 최초 Worker level은 4이며 Model/Effort는 Manager의 명시 설정을 따른다.
- priority high: 기존 자동 통합 경로의 파일 보존과 안전한 무인 실행을 위한 교정이다. 충돌하지 않는 ignored artifact의 정상 생성·변경·삭제를 제한하거나 사후 Git 변화로 권한 위반을 판정하지 않는다.
- 선택 adapter는 `scripts/verify.json`의 unittest이며 `docs/VERIFY.md`의 Python 3.11–3.14/protocol 1 계약을 확인했다. 이 교정에는 adapter 도입이나 검증 체계 변경이 필요하지 않으므로 기존 수집·fixture·필수 compile·실제 Agent 차단과 부분/전체 검증 계약을 유지한다.
