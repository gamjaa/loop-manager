---
id: G-328
title: 폐기된 Agent 승인과 Git 감시 검증 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-066] Agent CLI 지침의 독립성.md
  - docs/requirements/[R-080] Git Mock Defaults for Tests.md
depends_on: []
complexity: medium
risk: medium
priority: normal
---

## Purpose

현재 실행 경로에서 폐기된 AGENTS 사전 승인 parser·patch 적용기와 과거 Git 감시를 주장하는 테스트를 정리해, 과거 데이터 준비와 현재 권한·복구 검증의 책임 경계를 구분한다. 현재 snapshot 수용·원래 감사 보존·일반 작업 재개의 검증은 유지하며, 동일 assertion만 반복하는 handoff 조건 행렬도 실제 입력 하나로 통합한다.

revision `f5a9d4ea510c62cea769eacd32c604f32bf25425`의 저장된 발견 2·17은 `documents/guidance_approval.py`의 승인 전용 parser/identity/patch 적용 소비가 테스트·과거 행 준비에 남고, 실제 `guidance_service.resume_retired_guidance_block()`은 이를 호출하지 않음을 확인했다. 발견 15·23–26은 테스트명과 실제 허용 결과가 반대이거나 주입값이 실행 경로에 연결되지 않는 검증을 구분했다. 길이나 실행 비용만으로 삭제할 테스트는 정하지 않는다.

## Acceptance Criteria

- AGENTS 신규 approve/replace 형식·승인 patch 적용만 검증하는 코드와 테스트가 제거되고, 레거시 승인 행 준비는 필요한 고정 값으로 표현된다. 다른 곳이 실제 사용하는 타입·fixture·호환 export를 파일 단위로 일괄 제거하지 않는다.
- 사용자 동작과 공개 계약인 CLI/API·일반 역할 권한·sandbox·현재 결과/문서 검사·재개 조건을 보존한다. 과거 승인·사용자 결정·원문·session·차단/알림 감사와 DB 저장 형식도 그대로 읽고 보존한다.
- 현재 snapshot을 반환하고 과거 shared ref 차이를 위반으로 추정하지 않는 테스트는 그 결과를 명시적으로 assertion한다. 실제 sandbox 거부를 검사한 것으로 이름·주석을 남기지 않는다.
- 제거/통합한 테스트별로 폐기 이유와 남은 조건·assertion의 대응을 결과에 남긴다. 현재 지원하는 실패·보존·외부 연동 검증을 skip·빈 테스트로 바꾸지 않는다.

## Implementation

- [x] 확인한 `loop_manager_internal/documents/guidance_approval.py`와 `tests/test_guidance_approval.py`의 `GuidanceFormatTest`·`GuidanceInputContractTest`를 정리한다. 과거 행 생성에 필요한 값은 기존 `tests/support/guidance.py`의 fixture 계약 안에서 고정 입력으로 바꾸고, 런타임의 과거 대기 복구는 유지한다. `CommonPlansScopeTest`·`GuidanceGitAdapterTest`와 `tests/test_execution_guidance.py`의 stale 승인 보존·정상 Worker 진행·독립 차단 보호는 남긴다.
- [x] 확인한 `tests/test_execution_current_rebase_agent_scope.py`의 저장 결과 shared ref 변형과 `tests/test_published_checkpoint_resume.py`의 dirty snapshot 재개 사례는 현재 snapshot 수용·원래 감사 유지라는 이름과 assertion으로 고친다. 운영 scope·sandbox 거부 정책 자체는 수정하지 않는다.
- [x] 확인한 `tests/test_runtime_scope_diagnostics.py`의 호출되지 않는 외부 관측 실패 주입은 인접 소비자의 Git 조회 0회 검증으로 통합한다. 실제 관측 실패 처리는 `reconcile_external_base`를 검사하는 `test_live_forward_and_symbolic_head_accept_without_isolation_and_keep_original`의 실패 분기에 남긴다.
- [x] 확인한 `tests/test_scope_checkpoint_recovery.py`의 반환값 assertion 없이 과거 ref 위반 거부를 주장하는 사례는 `tests/test_scope_services.py:test_blocked_resume_reads_current_candidate_without_saved_scope_comparison`의 현재 snapshot/보존 검증에 통합한다. checkpoint cleanup·파일 보존·실제 차단 해제 회귀는 유지하며 참조가 없어진 전용 준비만 정리한다.
- [x] 확인한 `tests/test_self_merge_handoff.py:test_local_sync_status_never_gates_verified_self_merge_handoff`는 입력에 쓰이지 않는 `sync`/status 행렬을 제거하고 로컬 동기화 미호출·원격 ancestry 확인·handoff 발생 assertion을 한 사례에 보존한다. G-312의 handoff 구현은 변경하지 않는다.

## Validation

- Worker는 위 변경한 테스트의 최소 case와 `tests.test_execution_guidance`의 관련 보존 case를 targeted unit으로 실행한다. parser 삭제 뒤에는 남은 guidance scope/Git adapter case를 실행한다. 폐기된 parser의 통과를 완료 조건으로 다시 만들지 않는다.
- legacy 승인 row·stale 승인·일반 사용자 결정·다른 차단을 둔 실제 SQLite fixture에서 정상 재개와 감사 불변을 확인한다. 현재 snapshot·재개 반환값·과거 원문 보존을 계속 검사하고 실제 sandbox 거부 회귀는 기존 provider 테스트에 남긴다.
- 전체 verify는 남은 scope·checkpoint cleanup·reconcile_external_base 실패·handoff·guidance 호환 회귀를 모두 유지한다. 단지 테스트명에 있던 거부 의미를 새 정책으로 복원하거나 실제 Git 보존 검증을 삭제하면 실패다.
- 읽기 전용 Review에서 삭제한 조건/전용 helper와 계속 검증하는 테스트의 대응, 운영 실행 경로·공개 export·저장된 감사의 보존을 확인한다. 테스트 수 감소를 검증 범위 감소로 대신하지 않는다.

## Progress

- [x] 저장된 발견 7건과 R-066/R-080·현재 snapshot 수용 계약으로 정리 범위를 확정했다. 구현·테스트·verify는 실행하지 않았다.
- [x] 폐기한 parser 형식 검사는 현재 런타임에서 호출되지 않는 승인 parser/patch 적용기 전용 계약이었다. 알림의 과거 행은 고정 legacy 값으로 직접 준비하고 stale 승인 행 보존·과거 대기 재개·독립 차단 보존은 별도 회귀로 남겼다.
- [x] shared ref·dirty snapshot의 과거 저장 결과는 재개 gate가 아니며, 소비자 테스트가 현재 snapshot 반환과 원래 `agent_snapshot`·저장 결과 보존을 assertion한다. 빈 stale ref 검사 테스트는 해당 candidate assertion과 기존 실제 checkpoint cleanup/파일 보존 사례로 대체했다.
- [x] 외부 관측 실패 주입은 호출되지 않던 scope consumer 테스트에서 제거하고 0회 Git 조회를 assertion한다. 실제 `reconcile_external_base` 실패 분기는 기존 회귀로 남겼다. self-merge는 쓰이지 않던 sync/status 조합을 없애고 원격 ancestry·handoff·local sync 미호출을 한 사례에서 assertion한다.
- [x] 독립 cause 보존을 확인하는 `test_project_rebase_release_preserves_each_independent_plan_cause`를 실행해 guidance 승인과 일반 user decision 차단이 재개 뒤에도 유지됨을 확인했다.
- [x] targeted unit: guidance/scope/checkpoint/handoff 23개와 Worker 결과 계약 1개가 통과했다. 같은 19개 대상 명령의 첫 실행에서 saved-dirty snapshot과 mock `git_bytes` 기대 두 건이 실패했다. 두 번째 실행에서는 saved-dirty snapshot assertion이 두 current candidate 반환을 고려하지 않아 실패했고, 세 번째 동일 실행은 19개 모두 통과했다. 추가 legacy wait·fixture·cause 보존 4개와 corrected-result report contract 1개도 통과했다. 이전 최종 보고는 두 실패 결과를 `FAILED_COMMAND`와 `RERUN` 사이에 넣지 않아 판정에 실패했다. 첫 결과 2건, 중간 재실행 1건, 최종 동일 명령 PASS를 모두 적는 보고 형식을 기존 parser contract test와 `parse_worker_result`로 확인했다. 전체 verify는 Worker가 실행하지 않았다.

## Decision Log

- complexity medium: 승인 fixture의 레거시 호환과 여러 scope 소비자의 현재 검증을 구분한다. risk medium: 감사나 실제 거부 검증을 함께 없애면 회귀를 숨길 수 있어 제거 범위와 남은 assertion을 개별 대응시킨다. 최초 level은 3이다.
- AGENTS 승인 제거는 이미 활성 R-066의 계약이다. 새 권한·정책 선택을 만들지 않으며 새로운 사용자 결정을 요구하지 않는다. 일반 scope 복구 구현 교정은 기존 G-189에 남긴다.
- 이 계획 뒤 G-334가 보존된 guidance fixture의 연결 수명을 정리한다. 코드/fixture 정리와 수명 수정의 충돌을 그 dependency로 예방하며 새 유지보수 재평가를 요구하지 않는다.
