---
id: G-192
title: Runtime 등록부터 저장 단계 재개까지의 검증
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on:
  - G-161
complexity: high
risk: high
priority: high
---

## Purpose

R-020의 Manager runtime 귀속을 실제 bootstrap부터 runtime 자식의 선검증과 저장 단계 재개까지 검증한다. R-023의 정상 외부 fetch/push와 symbolic HEAD 전이를 같은 범위 판정·checkpoint 경계에 연결해 과거 Manager 증거와 현재 원격 입력을 혼동하는 차단도 교정한다. runtime 신뢰 범위와 Agent 권한은 유지한다.

## Acceptance Criteria

- 임시 self 저장소의 SHA A에서 Planner·Worker·Reviewer scope와 작업 상태를 저장하고 같은 state directory의 정상 bootstrap 진입으로 원격 SHA B를 선택한다. 실제 runtime 자식이 exact-SHA runtime 등록을 선검증한 뒤 저장된 단계의 처리를 이어간다. 테스트가 RuntimeMutation이나 허용 경로를 scope 함수에 직접 전달해 이 흐름을 대신하지 않는다.
- 등록 base가 `main` 또는 `develop`인 경우 모두 같은 등록 base·선택 SHA·runtime identity로 저장 scope를 재개한다. `origin/main`이 없거나 등록 base와 다른 SHA인 대체 base에서도 올바른 runtime을 거부하지 않는다. 원래 Agent snapshot·결과·checkpoint를 보존해 증명된 전이만 비교에 반영하고, R-023으로 설명되지 않는 사용 ref·symbolic HEAD target 변경과 실제 Agent 위반은 거부한다.
- runtime B 선택 뒤 외부 fetch/push로 base가 정상 전진한 C가 선검증·저장 결과·checkpoint에서 관측돼도, 선택 근거와 정확한 소유 runtime B를 계속 검증한다. tracking ref와 B의 단순 불일치로 runtime 기준선 오류를 만들지 않고 C는 관리 rebase·후보 재검증으로 처리한다. 임의 runtime 교체·새 handoff는 만들지 않는다.
- 유효한 invocation 격리와 원격·선택 작업의 현재 소유권이 확인된 정상 외부 base 전진은 Manager fetch 전이 사이에 끼어도 R-023대로 재개한다. Agent 결과·저장 선검증·PLAN_BLOCKED·PROJECT_BLOCKED 재평가·정상 및 진단 checkpoint가 같은 증거에 같은 Git metadata 귀속 판정을 내린다. 과거 receipt·reflog·원본 snapshot을 다시 쓰거나 실제 Agent 위반을 현재 정상 상태로 덮지 않는다.
- 같은 symbolic target의 파생 `origin/HEAD` OID 변화와 R-023이 증명한 최초 생성은 base 전진과 함께 처리한다. target 변조·삭제·잘못된 target/OID, 격리 증거 누락·불일치와 실제 범위 위반 혼합은 계속 거부한다.
- checkpoint 요청과 현재 cause/evidence/status가 바뀌면 R-055대로 같은 소유 작업의 최신 원인을 다시 검증해 취소·갱신한다. stale 요청 identity만으로 새 프로젝트 차단·Worker 교정 횟수를 만들지 않고, 실제 원격 lease·자원 소유권 경쟁과 별도 차단·승인을 보존한다.
- 정상 재개와 같은 SHA 재사용에서 session ID, 관리 worktree, Worker diff, 결과 snapshot, retry·failure history와 계획 상태가 보존된다. runtime 등록만으로 새 session·차단을 만들지 않고 같은 등록·기준선 변경을 중복 적용하지 않는다.
- 등록 직후 자식 선검증 전에 중단된 실행은 다음 bootstrap에서 durable intent와 실제 Git 상태를 대조해 재개한다. runtime만 다른 과거 false-positive 결과는 자동 복구하고, 실제 범위 위반이 섞인 결과는 차단과 원본 증거를 유지한다.
- dirty·attached·wrong-SHA·wrong-repository·소유 표식 불명 runtime과 잘못된 경로·과거 또는 미래 SHA 경로는 실제 Git 상태로 거부한다. reset·clean·강제 checkout·제거·재등록으로 수정하지 않으며 저장된 session과 작업 공간을 보존한다.
- runtime 귀속 판정의 수락·거부 양쪽에서 비교한 snapshot identity, exact path·SHA와 설명되지 않은 delta의 종류·관련 경로를 진단할 수 있다. config 값·자격 증명·파일 원문은 로그에 노출하지 않는다. 거부 전에 원본 snapshot을 재설정하지 않는다.
- 기존 엄격 비교 대상의 임의 worktree·branch·config·operation·ref 변경은 계속 거부한다. R-023과 ARCHITECTURE가 정한 유효한 실행별 격리 증거의 범위 판정도 보존하고, 다른 경로를 Manager runtime으로 인정하거나 실제 Agent의 공유 Git 쓰기를 허용하지 않는다.

## Implementation

- runtime/bootstrap·runtime_identity, agents/scope_recovery·agent_scope_decision, recovery/project_block_service의 기존 책임 경계와 tests/support의 Git·SQLite·중단 fixture를 재사용한다. G-161의 원격 bootstrap 전이를 재구현하지 않고 그 Merge 결과를 대상으로 검증한다.
- 등록 base를 사용하는 bootstrap과 persisted scope의 원격 ref 선택을 연결한다. 현재 `agent_scope_decision.decide_persisted_scope`와 `reconcile_runtime_snapshot` 및 저장 결과를 다시 검사하는 호출 경로는 `refs/remotes/origin/main`을 고정한다. 이미 검증한 self 등록·runtime 입력으로 정확한 base ref를 전달하고, 원본·결과·checkpoint 모두에 같은 제한된 조정을 적용한다. 다른 ref를 통째로 채택하거나 로컬 HEAD로 원격 기준을 대체하지 않는다.
- `git/base_fetch_service.py`·`manager_git_journal.py`와 `agents/scope_recovery.py`·`agent_scope_decision.py`의 기존 책임 경계에서 Manager의 과거 실행 증명과 R-023의 정상 외부 전진 확인을 분리한다. 현재 원격·정상 전진·invocation 격리·선택 작업 검사를 연결해 재개하고, 원본 Agent 결과와 설명되지 않은 변경은 별도로 검사한다. 유한한 재관측과 실제 ref 삭제·재작성·lease 경쟁의 거부를 유지하며 외부 변경을 Manager receipt로 합성하지 않는다.
- runtime 선택 identity의 SHA와 이후 작업 base OID를 분리해 전달한다. symbolic ref는 target과 파생 OID를 구분하고, `recovery/checkpoint_adapter.py`·`scope_reconciliation.py`에도 동일한 증거 판정을 연결한다. checkpoint cause 변경은 기존 요청·현재 원인·게시 intent의 CAS와 재관측으로 처리하며 다른 계획·승인을 해제하지 않는다.
- 현재 tests/test_origin_isolation.py, tests/test_bootstrap_runtime.py와 tests/test_scope_interrupted_agents.py의 함수별 사례를 연결하는 bootstrap 재개 회귀를 추가한다. 실제 Git·임시 state DB·부모와 자식의 identity 전달 및 선검증·단계 전이는 사용하고 외부 Agent·GitHub 호출만 테스트 경계에서 대체한다. 운영 DB나 실제 외부 서비스는 사용하지 않는다.
- 현재 scope_recovery의 snapshot hash 로그는 판정 성공 뒤에 있어 앞선 거부가 비교 identity를 남기지 못한다. 기존 로그·비밀값 제거 경계에서 거부 진단도 남기고, 확인된 연결 결함만 해당 책임 경계에서 교정한다. 전체 snapshot 채택이나 prefix 예외를 추가하지 않는다.
- 자동 역할·문서·검증 권한은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 따른다. 새 루트 항목과 범용 테스트 infrastructure는 만들지 않는다.

## Validation

- Worker는 변경한 scope·원격 전진·checkpoint·진단·fixture의 최소 unit case만 실행한다. 실제 bootstrap 통합 회귀는 작성하되 실행은 Manager 전체 verify에 맡긴다. 실행 가능한 unit 변경이 없다면 그 사유와 통합 검증 경계를 기록한다.
- scope decision의 최소 unit case에서 `main`·`develop`, main ref 부재·상이한 SHA, 정확한 base ref의 변경, symbolic HEAD의 검증된 OID 변화와 target 변조를 확인한다. 저장 원본·결과·checkpoint의 실제 위반이 runtime 조정으로 숨겨지지 않는 회귀를 유지한다.
- tests/test_origin_isolation.py·test_manager_git_journal.py·test_scope_block_revalidation.py·test_checkpoint_scope.py의 unit 경계에 Manager fetch → 정상 외부 전진 → Manager 재관측, 격리 증거 누락·불일치, 실제 위반 혼합과 저장된 시작/결과/Manager checkpoint의 수락·거부 일치 사례를 추가한다. `origin/HEAD` 최초 생성·파생 OID 전진·target 변조·삭제·OID 불일치를 구분한다.
- checkpoint cause 갱신·해제·재차단, 요청/게시/SQLite 확정 사이 중단과 재시작에서 stale 요청의 취소·현재 원인 갱신, 실제 소유권 경쟁·원래 감사 보존과 Worker 호출/교정 횟수 불변을 최소 unit case로 확인한다.
- Manager 전체 verify에서 세 역할의 A→B 실제 bootstrap·자식 선검증·저장 단계 재개, 같은 SHA 재사용, 등록 직후 중단, false-positive 복구, invalid runtime 전체 경우와 다른 shared Git 변경의 혼합을 확인한다. 직접 허용 경로를 주입하는 기존 단위 사례만으로 통합 성공을 보고하지 않는다.
- Manager 전체 verify는 두 worktree가 공유하는 실제 Git 저장소에서 runtime B 선택 후 정상 base C 전진과 HEAD 변화를 발생시켜 선검증·저장 결과·차단 재평가·checkpoint까지 확인한다. 외부 관측 일시 실패는 보존 후 재관측하고 ref 삭제·재작성·Agent 위반 혼합은 거부한다. 정상 전진만으로 `managed_worktree_scope_violation`·`shared_ref_mutation`이 생기거나 다른 계획의 진단 보존이 연쇄적으로 막히지 않는 근거를 확인한다.
- 기존 Agent 위반 차단, 격리 증거 유무에 따른 범위 판정, source·작업물 보존, process lock, runtime bootstrap 격리와 중단 재개 회귀를 유지한다. 각 R-020 기준에 해당하는 테스트와 Manager 검증 결과를 연결해 남긴다.

## Progress

- [x] 2026-10-05 기준 5ef0bb51840f9c2e93158d53c2b4b64d862f44e9에서 R-020 원문과 bootstrap·scope 구현 및 관련 테스트를 대조함. 기존 runtime 등록 테스트는 준비·귀속 함수를 직접 호출하고 실제 bootstrap→자식→저장 단계 재개를 끝까지 검증하지 않음. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 기준 cfcd0796ad122155321ea54b7ad36624192b139b와 mutable READY snapshot token 0b7faa6f85d56b048832ba5f5c92f9a80ca6927776a3c3b764e86df681f88f62의 원본 hash를 대조함. G-161의 등록 base 기반 bootstrap은 Merge됐지만 persisted runtime scope의 두 공통 helper와 저장 결과 재검사에는 main ref 고정이 남아 있어 기존 재개 검증 범위에 연결함. 구현·테스트 실행은 하지 않음.
- [x] 등록 base ref를 원본·결과·checkpoint와 false-positive 재검사에 전달하고, symbolic HEAD의 검증된 OID 전이만 조정함. 수락·거부 로그에 원본·현재·결과·checkpoint 지문과 변경 종류·경로를 기록하고 값·파일 원문은 제외함. 관련 targeted unit 검증 완료.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## 구현 및 검증 대응

| 기준 | 코드·회귀 근거 | 검증 경계 |
|---|---|---|
| 등록 base와 exact runtime SHA 연결 | `agent_scope_decision.decide_persisted_scope`, `reconcile_runtime_snapshot`; `ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_persists_all_operands_and_is_idempotent`; `PersistedScopeDecisionTests.test_registered_base_reconciles_results_and_checkpoint_without_adopting_other_refs`, `test_registered_base_rejects_wrong_oid_and_symbolic_head_target` | main·develop, main 부재·다른 SHA, 결과·checkpoint 위반 보존 unit 검증 |
| A→B bootstrap·실제 자식·저장 역할 처리 | `BootstrapSavedScopeResumeTests.test_remote_bootstrap_child_resumes_saved_scope_without_identity_injection`; `BootstrapSavedScopeFixtureUnitTests`, `ReviewerRebaseScopeHandoffUnitTests` | 임시 Git·state DB, 정상 CLI 부모·자식, Planner·Worker·Reviewer session·관리 worktree·후보 보존; 통합 회귀 작성, 실행은 Manager에 위임 |
| 등록 직후 중단과 같은 SHA 재사용 | 같은 bootstrap 회귀의 runtime_create 결과 기록 전 중단과 후속 두 bootstrap | durable intent 복구·자식 선검증·재생성/재사용의 재개; 통합 회귀 미실행 |
| runtime만 다른 과거 차단과 혼합 위반 | `RuntimeFalsePositiveBaseTests.test_false_positive_uses_registered_base_and_keeps_mixed_violation`; 기존 `ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation` | 등록 base 기반 복구, 실제 위반·원본 증거 보존 unit 검증 |
| 수락·거부 진단과 비밀값 제외 | `scope_recovery.runtime_scope_diagnostic`, `validate_persisted_agent_scope`; `RuntimeScopeDiagnosticTests` | 초기 거부를 포함한 snapshot identity·변경 키·경로 기록, config·ref·operation 값과 파일 원문 미노출 unit 검증 |
| 잘못된 runtime과 기존 격리·공유 Git 보호 | 기존 `BootstrapRuntimeTests`, `OriginIsolationScopeTest`, `TaskRelevantInvocationScopeTests`, `RuntimeRecreationEvidenceTests` | dirty·attached·경로·소유 표식 거부와 엄격 비교/격리 증거 보호를 변경하지 않음. 전체 회귀는 Manager verify 대상 |

- 실행한 targeted unit 명령: `python3 -m unittest tests.test_agent_scope_decision.PersistedScopeDecisionTests tests.test_runtime_scope_diagnostics.RuntimeScopeDiagnosticTests` (23개 통과), `python3 -m unittest tests.test_runtime_scope_diagnostics.RuntimeFalsePositiveBaseTests` (1개 통과), `python3 -m unittest tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation` (1개 통과). 진단 보완 뒤 `python3 -m unittest tests.test_runtime_scope_diagnostics.RuntimeScopeDiagnosticTests tests.test_runtime_scope_diagnostics.RuntimeFalsePositiveBaseTests tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation`로 5개를 재확인해 통과했다. 실행 중 실패는 없었다.
- 관리 rebase 수용 후 HEAD/base `42ac69b670e54d39d4539c1005d2e8d37be770ac`의 미커밋 최종 diff와 새 unit test 파일을 다시 대조했다. 이전 후보 결과를 재사용하지 않고 `python3 -m unittest tests.test_agent_scope_decision.PersistedScopeDecisionTests tests.test_runtime_scope_diagnostics.RuntimeScopeDiagnosticTests tests.test_runtime_scope_diagnostics.RuntimeFalsePositiveBaseTests tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation`를 현재 후보에서 실행해 25개 모두 통과했다. 통합 회귀·전체 verify·Review는 실행하지 않았다.
- Worker 보고 재검증 요청 후 같은 HEAD/base의 최종 diff를 다시 확인하고 위 25개 targeted unit 명령을 새로 실행해 `OK`를 확인했다. 이전 최종 보고의 부정 문구를 결과 검증기가 실패 토큰으로 해석하는 원인을 읽기 전용으로 확인했으며, 통과 결과만 명시한 새 보고가 `parse_worker_result`를 통과함을 확인했다. 결과 검증기 코드는 변경하지 않았다.
- 실제 bootstrap 회귀의 임시 저장소에는 외부 Agent·GitHub 경계 double과 등록 직후 중단 fixture를 넣는다. Reviewer 후보 재검증용 fixture 스크립트는 임시 프로젝트에만 존재하며 Manager 통합 검증 때 실행된다. Worker는 이 통합 회귀와 전체 verify를 실행하지 않았다.
- Manager 전체 verify·Review·Merge 결과와 Requirement 완료 판단은 기록하거나 대신 결정하지 않았다.

- Manager의 bootstrap 회귀 진단에서 임시 fixture의 raw snapshot에는 scope ID·시작 지문·격리 호출 ledger가 없어 source checkout의 A→B 전이가 과거 엄격 비교로 차단됨을 확인했다. fixture는 `begin_agent_scope`로 원본을 저장하고 `tests/support/runtime.py`의 `record_saved_scope_invocation`으로 역할·session·repository·worktree·시작 지문에 연결된 기존 ledger 증거를 저장하도록 수정했다. Reviewer는 후보 commit 뒤 읽기 전용 scope를 시작하고 Worker 결과는 같은 scope identity를 유지한다. 운영 격리 규칙과 증거 없는 과거 실행의 엄격 판정은 변경하지 않았다.
- 관리 rebase의 현재 HEAD `2ec69f8bcaf2d86f8d9227b2f288f54fe8d9d1b0`와 base `42ac69b670e54d39d4539c1005d2e8d37be770ac` 사이 최종 diff 및 이번 fixture 보완을 검토했다. `python3 -m unittest tests.test_bootstrap_runtime.BootstrapSavedScopeFixtureUnitTests tests.test_agent_scope_decision.PersistedScopeDecisionTests tests.test_runtime_scope_diagnostics.RuntimeScopeDiagnosticTests tests.test_runtime_scope_diagnostics.RuntimeFalsePositiveBaseTests tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation`를 현재 후보에서 실행해 26개 `OK`를 확인했다. 새 unit은 세 역할의 격리 증거 수락, 역할·session·repository·worktree·계획·시작 지문 불일치 거부, 실제 scope resource 선택과 session 불일치 시 엄격 판정 유지를 확인한다. bootstrap 통합 회귀의 재실행과 전체 verify·Review는 Manager에 남긴다.


- 후속 bootstrap 진단에서는 첫 runtime 선검증 수락 뒤 시작 snapshot만 영속화되어 과거 결과의 원격 ref·runtime 등록이 두 번째 선검증에서 위반으로 보이는 연결 결함을 확인했다. 모든 검증을 통과한 뒤 같은 exact runtime delta만 시작·결과·Manager checkpoint에 함께 저장하도록 수정했다. 이미 복구된 worktree 기준선과 checkpoint 허용 경로·Agent 변경·session·이력은 유지하며, 실제 위반의 거부 경로에서는 저장하지 않는다. Planner fixture에는 누락된 `active_sha`를 A로 저장했다.
- 현재 HEAD `8870205069da458aa9d8fb2848f8549911c994f0`의 base `42ac69b670e54d39d4539c1005d2e8d37be770ac` 대비 전체 diff와 이번 보완을 다시 확인했다. `python3 -m unittest tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_persists_all_operands_and_is_idempotent tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation tests.test_bootstrap_runtime.BootstrapSavedScopeFixtureUnitTests tests.test_agent_scope_decision.PersistedScopeDecisionTests tests.test_runtime_scope_diagnostics.RuntimeScopeDiagnosticTests tests.test_runtime_scope_diagnostics.RuntimeFalsePositiveBaseTests`를 실행했다. 새 unit의 DB schema 초기화 누락을 수정하고 동일 명령을 재실행해 최종 27개 `OK`를 확인했다. main·develop의 세 저장 operand 영속화와 중복 갱신 방지, 실제 ref 위반 거부 시 모든 원본 필드 보존을 확인했다. 통합 회귀·전체 verify·Review는 실행하지 않았다.

- 관리 rebase 수용 후 HEAD `139c272b081ad5b96a352f7dcf7b9632d3a98b14`와 base `418e8cec1b77f10bf017ea9d1b32b4459267f45c` 대비 최종 diff 및 현재 근거 입력을 다시 확인했다. 전달된 진단은 기존 로그이며 결과·checkpoint 동시 영속화와 Planner `active_sha` 보완이 현재 후보에 유지됨을 확인했다. 위 27개 targeted unit 명령을 현재 후보에서 새로 실행해 `Ran 27 tests`, `OK`와 종료 코드 0을 확인했다. 추가 코드 변경과 통합 회귀·전체 verify·Review 실행은 하지 않았다.


- 후속 진단에서 Worker scope 수락 뒤 모델 목록 조회가 외부 CLI 경계 double을 우회하는 fixture 결함을 확인해 임시 child의 `codex_model_efforts`도 대체했다. Planner의 scope는 원격 planning rebase·호출 결과 검사 뒤 정상 종료되므로 첫 재개에는 scope 수락을, 두 번째 실행에는 실제 runtime 검증과 같은 역할·session·worktree를 확인한다.
- Reviewer scope가 남은 상태에서 base rebase가 후보를 WORKING으로 돌리면 격리 증거를 단계 불일치로 버리고 source 갱신을 다시 위반으로 보는 연결 결함을 수정했다. scope·호출·session 격리 검증과 실제 소유 rebase의 COMPLETE 재확인이 모두 통과한 경우에만 기존 Reviewer 자원 판정을 사용한다. 새 Worker scope를 시작하기 전 저장 Reviewer 결과와 현재 Manager 결과를 검증하고 기존 checkpoint 비교 이력에 남긴다. 미완료·증거 없는 rebase와 실제 Reviewer 위반은 전환하지 않는다. Reviewer의 읽기 전용 규칙·Agent 쓰기 권한은 유지한다.
- 현재 HEAD `67146dfe6360f271df4901ee567f7e53d233c6fe`와 base `418e8cec1b77f10bf017ea9d1b32b4459267f45c`의 전체 diff·계획·근거를 확인했다. 기존 27개 targeted unit 명령에 `tests.test_bootstrap_runtime.ReviewerRebaseScopeHandoffUnitTests`를 추가해 28개를 실행했다. 새 fixture의 checkpoint 필수 인자 누락을 수정한 뒤 동일 명령을 재실행해 최종 28개 `OK`와 종료 코드 0을 확인했다. 통합 회귀·전체 verify·Review는 실행하지 않았다.

- 관리 rebase 수용 후 HEAD `3e1b6ab90c795395d3828ae5f32a23bbc34cc17b`와 base `c515a37a8cea2151d034444d37198bd67d8d58b2` 대비 전체 최종 diff, 현재 계획과 R-020·R-023·PRODUCT·ARCHITECTURE 근거를 다시 확인했다. `python3 -m unittest tests.test_bootstrap_runtime.ReviewerRebaseScopeHandoffUnitTests tests.test_bootstrap_runtime.BootstrapSavedScopeFixtureUnitTests tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_persists_all_operands_and_is_idempotent tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation tests.test_agent_scope_decision.PersistedScopeDecisionTests tests.test_runtime_scope_diagnostics.RuntimeScopeDiagnosticTests tests.test_runtime_scope_diagnostics.RuntimeFalsePositiveBaseTests`를 현재 후보에서 새로 실행해 `Ran 28 tests`, `OK`, 종료 코드 0을 확인했다. 추가 코드 변경과 통합 회귀·전체 verify·Review 실행은 하지 않았다.

- 관리 rebase 수용 후 HEAD `74e713a8518e0c4a7c11ba34138f0047910e16a9`와 base `8910885dd11ee7f933efd9143cac9888b89eb754` 대비 전체 최종 diff와 현재 계획·근거 입력을 다시 확인했다. 직전 기록의 28개 targeted unit 명령을 현재 후보에서 새로 실행해 `Ran 28 tests`, `OK`, 종료 코드 0을 확인했다. 추가 코드 변경과 통합 회귀·전체 verify·Review 실행은 하지 않았다.

- 현재 HEAD `3a5c6ef070169d1e16c4eb0700de260920e614f6`와 base `e274984973050b36c5195496fb8fa8588c65ed95`의 최종 diff 및 개정된 R-020·R-023·R-055를 대조했다. Manager 진단의 fixture import 위반은 공통 snapshot을 `tests/support/scope.py`로 옮겨 해결했다. Worker 자식 호출 뒤 checkpoint가 파생 `origin/HEAD`를 base fetch와 별도로 비교하던 경로에는 같은 증명된 symbolic OID 비교를 연결했고, 회귀의 실패 메시지에 자식 stdout을 포함했다. bootstrap 통합 회귀는 Worker가 실행하지 않았다.
- 개정 기준에 맞춰 원본 Agent 시작·결과·Manager snapshot은 runtime 조정으로 덮어쓰지 않고, 원본 지문에 결합된 `manager_metadata_agent_baseline` 비교·이력으로 저장한다. main·develop의 반복 선검증과 과거 차단 해제에서도 같은 비교를 사용한다. 격리·선택 작업 소유권이 확인된 경우에만 현재 원격 OID·ancestry·추적 ref 재관측으로 정상 외부 전진을 비교에 반영하며, target 변조·삭제·재작성·관측 실패는 엄격 비교로 남긴다. Manager receipt·reflog를 외부 전진의 receipt로 합성하지 않는다. Agent 결과·선검증·일반 차단 재평가·runtime false-positive·checkpoint에서 이 공통 경계를 사용한다.
- runtime B의 선택 fetch receipt·reflog와 소유 runtime은 이후 live base C와 분리해 검사한다. 선택 SHA·현재 base OID를 진단에 구분하고, 잘못된 runtime이나 원격 재작성은 거부한다. 두 Git worktree의 외부 C push/fetch를 실제 runtime 자식 선검증 전에 발생시키는 `BootstrapSavedScopeResumeTests.test_selected_runtime_b_survives_external_base_c_before_child_prevalidation`를 작성했다. 실행은 Manager에 남긴다.
- checkpoint dispatch는 같은 branch·worktree·PR의 현재 cause를 트랜잭션·request CAS로 다시 읽어 해소된 요청을 취소하거나 새 cause로 갱신하고, 원래 요청·게시 감사를 유지한다. safety 판정 중 cause 변경은 한 번만 재관측하며 게시 확정 전에도 다시 검사한다. 별도 프로젝트 차단·자원 변경·원격 lease와 Worker 교정 횟수는 유지한다.
- targeted unit 명령 `python3 -m unittest tests.test_bootstrap_runtime.ReviewerRebaseScopeHandoffUnitTests tests.test_bootstrap_runtime.BootstrapSavedScopeFixtureUnitTests tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_persists_all_operands_and_is_idempotent tests.test_origin_isolation.ManagerBaselineReconciliationUnitTests.test_runtime_prevalidation_reconciles_only_proven_delta_and_rejects_saved_mutation tests.test_agent_scope_decision.PersistedScopeDecisionTests tests.test_runtime_scope_diagnostics tests.test_checkpoint_scope.CheckpointScopeTests.test_stale_checkpoint_cause_refresh_cancel_and_competing_request tests.test_checkpoint_scope.CheckpointScopeTests.test_used_remote_and_shared_config_still_refused tests.test_support_fixtures.SupportFixtureTests.test_tests_and_support_do_not_import_test_fixtures`의 초기 실행에서 비교 기준 저장 누락과 HEAD 최초 부재 fixture의 기대 오류를 확인했다. 원본을 보존하는 비교 기준 저장 및 fixture 기대를 수정하고 동일 명령을 재실행해 최종 35개 `OK`, 종료 코드 0을 확인했다. `python3 -m unittest tests.test_checkpoint_scope.CheckpointScopeTests.test_checkpoint_accepts_manager_base_derived_head_and_rejects_target_change tests.test_checkpoint_scope.CheckpointScopeTests.test_dispatcher_rechecks_cause_before_safety_and_publication_completion`도 2개 `OK`, 종료 코드 0으로 확인했다. 전체 verify·Review는 실행하지 않았다.

- 비교 기준에 저장된 이전 runtime의 제거도 정확한 결정적 경로·원래 registration·완료된 `runtime_remove` intent/result/receipt와 실제 경로 부재가 모두 일치할 때만 반영한다. 다른 경로·등록 상태·남아 있는 파일·불완전 receipt는 허용하지 않는다. 해당 좁은 unit의 lock marker fixture를 운영 형식으로 수정한 뒤 위 동일 targeted 명령을 재실행해 최종 36개 `OK`, 종료 코드 0을 확인했다. checkpoint의 두 unit 명령도 현재 최종 코드에서 다시 실행해 2개 `OK`, 종료 코드 0을 확인했다. 원본 snapshot·과거 receipt는 유지하며 통합 회귀·전체 verify·Review는 실행하지 않았다.

## Decision Log

- 2026-10-07 사용자 요청으로 활성 R-020·R-023·R-055와 계획을 함께 보완했다. G-181의 base ref 재검사, G-171의 Manager 표식 불일치·stale checkpoint identity와 G-192의 runtime/HEAD 차단 메시지를 기존 코드 경계의 재현 시나리오로 연결한다. 관련 unit 11개와 최소 입력 검사는 기존 거부 경로의 근거이며 새 수용 동작의 구현 성공이 아니다. 운영 snapshot을 확보하지 않았으므로 개별 사건의 실제 원인을 확정하지 않는다. G-199의 legacy 기본 checkout 결정과 독립이며 이 개정으로 draft를 해제하지 않는다.
- R-020의 전체 충족은 기존 G-051·G-052 Merge만으로 확정할 수 없다. 실제 bootstrap 경로를 요구하는 Acceptance Criteria와 거부 진단을 보완하는 후속이다.
- source 갱신 뒤 자식을 시작하지 않는 현재 gate는 G-161이 이미 담당한다. 해당 Merge를 선행 조건으로 두어 원격 실행 구현을 중복하지 않는다. G-189의 최종 차단·재개 감사는 이 검증과 진단 보완 이후 진행한다.
- R-020의 과거 엄격 snapshot에 대한 위반 보존과 R-023의 격리 증거가 있는 실행에 대한 선택 범위 판정을 구분한다. 사용자 worktree 변화의 조건부 비교 제외는 갱신된 두 Requirement의 경계를 따르며 격리 증거 없는 실행의 쓰기를 외부 변화로 추정하지 않는다.
- 실제 부모·자식, 세 역할과 정상 외부 전진의 저장 단계·checkpoint 전이를 연결해야 하므로 complexity high다. ref 귀속을 잘못 완화하면 실제 위반·잘못된 게시를 수용할 수 있어 risk high로 올리고, 원본 증거·lease·소유권·혼합 위반 회귀를 유지한다. 보존된 작업의 반복 오탐을 막는 작업으로 priority high다.
- 대체 base의 runtime 귀속도 R-023이 정한 같은 등록 base 계약에 포함된다. G-161의 bootstrap·로컬 동기화 구현은 반복하지 않고, G-189가 이 계획의 원격 전진·runtime·checkpoint 교정 뒤 전체 경로를 감사한다. 실패 시 작업물·session·원본 감사와 독립 차단을 보존하며 포괄적인 기준선 교체나 운영 DB 수정을 복구 수단으로 사용하지 않는다.
