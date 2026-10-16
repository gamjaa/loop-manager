---
id: G-284
title: Worker 범위 밖 문서의 보존 복원
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-082] 단계별 원격 문서 반영과 PR 전 ExecPlan 갱신.md
depends_on: []
complexity: medium
risk: critical
priority: high
---

## Purpose

R-082의 후속 복원 목표와 확인된 사용자 결정에 따라, 종료한 Worker가 직접 변경했다고 확인한 다른 ExecPlan과 활성·archive Requirement의 범위 밖 변경을 원문 보존 뒤 해당 파일·index entry에서만 제거한다. 이미 구현된 선택 frontmatter 복원, rebase 문서의 보존·경로별 적용, checkpoint와 Worker 피드백을 재사용해 중단 재개와 동일 Worker 문맥의 최종 diff·보고 갱신을 연결한다. 코드·테스트·선택 ExecPlan 본문과 나머지 파일/index·HEAD·branch·session·실행 및 실패 이력은 유지하며 전체 baseline을 일괄 복원하지 않는다. 사전 수정·동시 편집·변경 주체 또는 소유권이 불확실한 경로는 남긴다. draft 정리·대기 해제와 게시 확정은 Manager가 검증한다.

## Acceptance Criteria

- 선택된 소유 구현 worktree에서만 수행한다. Worker와 관련 writer의 종료, 현재 repository·작업·branch·worktree 및 미완료 Git operation·checkpoint 소유권을 확인한다. 진행 중 rebase나 writer 종료 불명·대상 연결 불명확은 덮어쓰지 않고 현재 근거와 작업물을 보존한다.
- 원본 브랜치에서 rebase로 유입된 문서와 실제 Worker 변경을 구분한다. 현재 `validate_worker_workspace_scope()`의 관측 origin OID·후보 공통 조상과 호출 전후 형상을 함께 사용하며, 경로 차이나 과거 snapshot 차이만으로 변경 주체를 확정하지 않는다. 호출 전부터 있던 수정, 동시 외부 수정, 원문·mode·index 또는 변경 주체를 확정할 수 없는 경로는 복원하지 않는다. 원본 부재·조회 실패는 변경 없음이나 복원 권한으로 바꾸지 않는다.
- 실제 Worker 변경으로 확인한 다른 ExecPlan·활성 및 archive Requirement의 수정·삭제·추가·rename·이동은 대상 양쪽 경로와 부재 여부까지 검증한다. 복원 전에 대상 경로·작업/호출 identity·고정 기준 revision·변경 이유와 확보한 변경 원문·mode·index entry를 소유 checkpoint에 보존한다. 보존 실패나 경로 별칭·symlink·다른 소유 파일과의 충돌에서는 쓰지 않는다. 새 파일 제거도 호출 전 부재와 Worker 생성이 증명된 경로에 한정하며 원문을 먼저 보존한다.
- 복원은 확인한 파일과 index entry만 대상으로 한다. 선택된 ExecPlan 본문·구현·테스트·나머지 파일과 index·HEAD·branch·session·실행/실패 이력은 유지한다. 전체 reset·clean·checkout·stash나 과거 Git baseline 일괄 복원을 도입하지 않는다. 기존 Manager의 rebase 문서 채택·Requirement archive와 선택 frontmatter 부분 복원을 이 복원으로 대체하지 않는다.
- 준비·파일 적용·index 적용·결과 확인 중 중단을 기존 checkpoint generation과 소유 intent로 재개한다. 이미 복원한 경로는 동일 결과를 확인하고 남은 적용만 수행한다. 새 사용자 변경·checkpoint 경쟁·원문 손상·target 변화는 덮어쓰지 않으며 이전 보존 자료와 미확정 이유를 남긴다. 후속 checkpoint 교체도 미완료 복원 근거와 확정 감사를 보존한다.
- 복원 완료 후 같은 Worker session 또는 기존 fallback 문맥에 복원 경로·이유와 최종 diff·문서 보고 갱신을 전달한다. 복원으로 해소된 범위 오류만 정리하고 독립 실패·사용자 판단·검증 이력은 유지한다. 정상 복원 자체를 최종 BLOCKED 사유·검증 성공·Worker level 초기화로 처리하지 않는다. 최신 최종 후보의 targeted test·Manager 전체 verify·읽기 전용 Review·사용자 Merge gate를 유지한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/agents/scope_adapter.py:validate_worker_workspace_scope()`·`scope_lifecycle.py:begin_agent_scope()`와 `scope_validation.py:validate_agent_scope()`에서 현재 호출 전후 근거·원본 유입 제외·범위 오류 경로를 재사용한다. 순수 경로/원문 판정과 Manager의 파일·index 효과를 분리하고 실제 변경 주체를 증명할 수 있는 대상만 준비한다. 모든 Requirement 거부나 source 조회·정확한 선언 검사를 완화하지 않는다.
- [ ] 확인한 `git/rebase_file_evidence.py:capture_originals()`의 literal path·원문/mode·index·부재·symlink 부모 검사와 `git/rebase_documents.py`의 경로별 보존·현재 형상 재확인·index 적용/결과 확인 패턴을 재사용한다. rebase 전용 `resolve()`나 폐기된 `agents/scope_baseline.py`의 전체 baseline 복원 경로를 정상 Worker 결과 복원으로 호출하지 않는다. Worker 시작 원문·현재 채택 원본과 실제 기준을 먼저 확정하고 후보 전체를 과거 tree로 되돌리지 않는다.
- [ ] 확인한 `storage/execution_checkpoint_state.py:ExecutionCheckpointRepository`에 이 작업의 보존·미완료 적용·확정 결과를 현재 snapshot/CAS 경계로 연결한다. 새 DB 원본·비호환 schema·범용 journal을 추가하지 않는다. 원문은 소유 checkpoint의 기존 저장 방식으로 보존하고 prompt·운영 요약·invocation ledger에 문서 전문을 추가하지 않는다.
- [ ] 확인한 `execution/execution_worker_handler.py:WorkerHandler.run()`의 frontmatter 복원 피드백·기존 `_retry()`와 session/feedback 저장을 재사용해 경로별 복원 결과를 전달한다. 원문 보존과 적용 완료 확인이 끝나기 전에 정상 진행을 확정하지 않는다. 선택 frontmatter 부분 복원은 재구현하지 않고 후속 교체에서도 기존 `worker_frontmatter_restores`·문서 반영 기록과 독립 감사를 유지한다.
- [ ] 필요한 effect를 `agents/bindings.py`·`execution/execution_ports.py`의 명시적 계약에만 연결한다. PRODUCT·ARCHITECTURE의 현재 결과 검증·문서 보존 절에 승인된 경계와 실제 구현만 반영한다. Worker는 이 작업에서도 모든 Requirement를 수정하지 않으며 R-082 자체를 교정하거나 archive하지 않는다.

## Validation

- Worker는 `tests/test_scope_services.py:ScopeValidationTests`, `tests/test_execution_worker_phase.py:ExecutionWorkerPhaseTests`와 `tests/test_requirement_sources.py:WorkerFrontmatterRestoreTests`에서 변경한 최소 unit case만 실행한다. 실제 SQLite/checkpoint·파일 bytes·순수 판정과 handler를 검사하고 Git·provider 효과만 double로 제공한다. 기존 선택 frontmatter 복원·범위 거부 회귀와 fixture를 재사용한다.
- 호출 전 clean 보호 문서에 대한 Worker 수정·삭제·추가·rename/이동, 원본 rebase 유입과 혼합 변경을 구성한다. 경로별 원문/mode/index 복원과 보존 감사, 선택 본문·구현·테스트·다른 staged/unstaged/untracked 파일·HEAD/branch·session/실패 불변을 확인한다. 원본 유입 문서에는 복원 효과가 호출되지 않아야 한다.
- 사전 수정·동시 편집·writer 생존/종료 불명·작업 소유권 변경·rebase 진행·원본 조회 실패·부모 symlink·경로 별칭·보존 실패·CAS 경쟁에서는 대상 파일을 쓰지 않는다. 파일 적용 전/후·index 적용 전/후·완료 기록 전 중단을 각각 현재 형상에서 재개해 남은 적용만 수행하며 복원 후 생긴 사용자 편집을 보존한다. 성공 결과를 미리 둔 fixture만으로 중단 복구를 통과시키지 않는다.
- mock만으로 확인할 수 없는 실제 파일/index 선택 효과는 `tests/test_scope_services.py`의 기존 격리 Git fixture를 사용한 최소 통합 사례 하나로 검사한다. Worker는 이 통합을 실행하지 않고 Manager의 기존 전체 verify에서 실행한다. 특정 두 보호 경로만 복원한 뒤 다른 staged/unstaged 파일과 index entry·HEAD가 유지되는 assertion에 실제 Git의 목적을 한정하며 정책 조합마다 전체 Git lifecycle을 반복하지 않는다.
- 복원 완료의 Worker 피드백·동일 session 재개·fallback 문맥·현재 문서 보고·독립 실패 보존을 검사한다. 부분 복원이나 피드백 전 중단에는 verify/Review/게시 token을 만들지 않는다. `scripts/verify.json`의 unittest/protocol 1, Python 3.11–3.14 수집·fixture·Agent 실행 차단·필수 compile을 유지하며 adapter·검증 체계를 개편하지 않는다.

## Progress

- [x] 2026-10-16 사용자가 선택지 A를 승인하고 기존 복원 기능 재사용·코드와 선택된 ExecPlan 본문 보존을 명시했다. 아래 User Decision에 기록했으며 원격 게시·출처 확인과 draft 대기 해제는 기존 Manager 전환을 따른다.
- [ ] Worker 변경 판정·원문 보존·선택 적용과 중단 재개 연결
- [ ] 최소 targeted unit·기존 Git 효과 회귀·Worker 보고 갱신 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 질문 해소 검토: 제공된 verified 사용자 변경 `d7a199007b8f7959611d5f7e7fbe7a070f24f5f1`의 새 Answer는 종료한 Worker의 직접 변경·원문 및 복원 근거 보존·경로별 파일/index 복원·동일 Worker 보고 갱신을 승인한다. 기존 복원/checkpoint/피드백 재사용과 코드·테스트·선택 본문·나머지 상태 보존, 일괄 baseline 복원 금지 및 불확실한 경로 보존을 명시해 기존 질문 전체를 해소한다. 이를 Purpose·Acceptance Criteria에 통합했으며 구현·검증 성공이나 Worker의 Requirement 수정 권한을 만들지 않는다. 최신 Architecture Change Policy와도 범위가 일치한다. Draft Decision·User Decision·draft 표시는 Manager 검증·게시 전환을 위해 원문 그대로 보존한다.
- 최신 delta 기준은 efc71a72f4e2ce152c9c4355e6d08bdc4ee242ac이며 보존된 58814a9afa875bf5c440d610afe4e58725826921 이후 변경을 추가 확인했다. R-082의 신규 복원 절과 AC, scope adapter·Worker handler·frontmatter helper·checkpoint·직접 회귀를 대조했다. 현재 callback은 선택 frontmatter만 복원하고 다른 Requirement/ExecPlan은 `validate_agent_scope()`에서 거부한다. 기존 catalog의 지침 정합화·commit 기록 복구·ignored 통합 보호·차단 전수 감사는 이 정상 경로의 선택 파일/index 복원을 구현하지 않는다.
- complexity medium: 경로·원문/index 보존·checkpoint CAS·Worker 피드백의 기존 경계와 helper를 재사용한다. 다만 변경 주체 판정·원본 유입·경로별 적용·중단 재개를 연결해야 하므로 단일 문구 교정으로 완료할 수 없다. risk critical: 오판한 파일 제거·index 복원이 Git에 없는 사용자 원문을 잃게 할 수 있어 적용 전 보존·경쟁 거부·실제 선택 효과의 회귀가 필요하다. 최초 Worker level은 4이며 Model/Effort는 Manager 설정을 따른다. priority high는 반복적인 범위 오류·사용자 복구를 줄이면서 사용자 작업 보존을 지키는 목표다.
- 2026-10-16 사용자가 선택지 A를 승인하고 기존 복원 기능 재사용·코드와 선택된 ExecPlan 본문 보존을 명시했다. 위 User Decision은 이 요청을 기록한 것이며, draft 표시와 저장된 대기는 사용자 출처·질문 해소를 검증하는 기존 Manager 전환에서 정리한다. 구현·테스트·verify 성공이나 다른 범위의 승인을 만들지 않으며 G-206의 보존 후보·실행 기록과 Requirement archive·최종 제품 전문 확인 결과를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (22a2e7f0393683dd2f1212e5a35c4d3c2bcc775ab4060257761d7427878c40b4): 기존 결정의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 현재 R-082 및 계획 본문과 일치한다.
- 기존 사용자 결정 유효성 재검토 (1e73b4e0992200514de48c99d2c889419508caf0070840fff02bd81c7315f7dc): 최신 R-082와 계획 본문의 Worker 변경 확인·원문 보존·파일/index별 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치하며, 새 검증 launcher는 복원 권한을 바꾸지 않는다.
- 기존 사용자 결정 유효성 재검토 (a6919162280e1d29a5f7a9be6367545c9e3fca78801898ee75f52bbf28d5f610): 현재 R-082와 계획의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치하며 이번 delta는 복원 권한을 바꾸지 않는다.
- 기존 사용자 결정 유효성 재검토 (7362289a9e635e586d703d94104a2a2bac97ce9da788ed87cacb8e730ef7ad16): ARCHITECTURE의 catalog 교정은 기존 R-082의 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건과 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (5bedc66ca6e87c9e674ee2c7b207558ef5c16a17f9f7a1cb8652be8eab6818b3): 현재 R-082와 계획의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치하며 이번 delta는 복원 권한을 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (b1a9eaccaaccb0f779e03be676555765f913af4881c90e15d82c9b2504be59e4): 현재 R-082와 계획의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치하며 이번 transaction 교정은 복원 권한을 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (005491ae452b1fce21420bc8e673b41ec0b91ef0cad91006ba29f3cd79e173e4): 현재 R-082와 계획의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치한다. 이번 변경은 유효성 기록 추가로 복원 권한을 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (14fcf98f45f6a82abeaf586642c8c2885bc753ee0e9101e61202f2c1950e7903): 현재 R-082와 계획의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치한다. 이번 기록 추가는 복원 권한을 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (a442d09814311ead09699dbc81d0f7abe35a7892d4a4d7c80597f8926726690a): 현재 R-082와 계획의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치하며 이번 교정은 복원 권한을 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (00ecdd2aa608d02c368295375012d52c9a432cab7c54a464a3da30a51d8ae2d8): 현재 R-082와 계획의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치한다. 이번 삭제 source 증명 및 PR 경합 변경은 복원 권한을 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (ed17c8e626e003626c04f5b14c7dd66a078527234c0bdc7109aaf38cba36bd34): 현재 R-082와 계획의 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 기존 결정과 일치한다. 선택된 ExecPlan의 문서 보고 제외는 복원 승인 범위를 변경하지 않는다.
- 기존 사용자 결정 유효성 재검토 (bfe3391e53a0efca86b851250c77844c2a35a8855192a99c787fe13be1be485d): 종료한 Worker의 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 유지되며 추가 source 조회·인계·복구 교정은 복원 권한을 확대하지 않는다.
- 기존 사용자 결정 유효성 재검토 (9f7ed584ae87462b1890bcaae4fb6b261c3bb2b093f14a1a6829afc5790da2de): 현재 R-082와 계획의 종료한 Worker 직접 변경 확인·원문 보존·파일/index별 제한 복원·불확실한 경로 보존·동일 Worker 보고 갱신 조건이 유지된다. 이번 rebase 입력 보존·재개 변경은 복원 승인 범위를 변경하지 않는다.
