---
id: G-199
title: 격리 증거 없는 legacy 기본 checkout 비교 경계
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-010] Origin-Isolated Local Worktree Operation.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-056] Non-Blocking Local Base Synchronization.md
  - docs/requirements/[R-059] Safe Python Bytecode Cache Use.md
depends_on:
  - G-012
  - G-161
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
complexity: medium
risk: high
priority: high
draft: true
---

## Purpose

격리 증거 없는 저장 Agent 실행에서 기본 checkout 변화만 남은 경우의 비교·복구 정책을 확정한다. G-161의 원격 실행과 로컬 동기화 구현은 재계획하지 않고, R-010·R-056의 기본 checkout 비귀속과 R-023의 legacy 엄격 비교 사이에 남은 경계만 다룬다. 사용자 결정 전 비교 완화와 차단 해제의 구현 방향은 미결정이다.

## Acceptance Criteria

- 사용자가 R-056의 legacy 기본 checkout 복원 불요와 R-023의 격리 증거 없는 실행의 엄격 감시가 함께 적용될 범위를 활성 Requirement에 명시하고, 해당 근거에 결합된 이 draft의 질문에 답한다. 자동 Agent가 결정이나 draft 해제를 작성하지 않는다.
- 같은 결정이 R-059의 일곱 번째 기준에서 참조하는 로컬 base·기본 checkout 비교 제외에도 적용되는지 관련 활성 Requirement에 명시한다. cache 허용이나 계측 결과를 legacy 범위 완화의 근거로 사용하지 않으며 캐시 구현·측정은 이 계획에 추가하지 않는다.
- 확정 정책은 현재 Agent 결과와 저장된 PLAN_BLOCKED·PROJECT_BLOCKED 재평가에 일관되게 적용된다. 기본 checkout의 파일·index·HEAD·로컬 base ref, 다른 worktree·shared ref와 선택 작업의 소유 자원을 구분하며 실제 Agent 쓰기를 외부 사용자 변화로 추정해 승인하지 않는다.
- 재개가 허용되면 현재 repository·선택 worktree·소유 branch·원격 기대값·범위와 실행 자리를 검증하고 해당 원인만 처리한다. 허용되지 않거나 증거가 부족하면 작업물·원본 snapshot·결과·session·실패 및 승인 감사를 보존하고 필요한 증거와 조치를 구체적으로 알린다. 과거 실행에 격리·검증 성공을 합성하지 않는다.
- 로컬 상태 복원·reset·clean·stash·사용자 lock 제거와 포괄적인 ref/worktree 기준선 교체를 하지 않는다. 후보가 바뀐 재개는 기존 targeted test·Manager verify·Review와 게시 lease를 통과하며 별도 차단·dependency·사용자 결정을 유지한다.

## Implementation

- 결정 전에는 구현하지 않는다. 현재 `agents/scope_validation.py`의 invocation 증거 gate, `agent_scope_decision.scoped_metadata`의 비교 투영, `recovery/scope_reconciliation.py`의 원인별 재검사를 재사용할 수 있는지 확정 정책으로 다시 확인한다.
- 결정된 예외만 해당 공통 경계에 반영하고, PRODUCT의 장기 규칙과 필요한 역할 문맥을 같은 구현 PR에서 정합하게 유지한다. R-023의 다른 worktree·shared metadata 보호와 R-055의 독립 작업 진행을 유지한다. 프로젝트 AGENTS 변경은 이 계획에 포함하지 않는다.
- 자동 역할·검증·문서 변경 경계는 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 따른다. 새 루트 파일·범용 격리 체계를 추가하지 않는다.

## Validation

- Worker는 확정 정책에 맞춘 scope 비교·저장 차단 재평가의 최소 unit case만 실행한다. 유효·누락·불일치 격리 증거, 기본 checkout만의 변경과 사용 ref·선택 worktree 위반 혼합, 다른 원인 보존을 구분한다.
- Manager 전체 verify는 세 역할의 실제 Git 기본 checkout 변화, legacy snapshot·재시작, 원인별 재개 또는 보존, session·감사·독립 계획 보존과 후보 재검증을 확인한다. 기존 `test_legacy_execution_without_isolation_cannot_attribute_external_source_change`의 기대 변경은 사용자 결정과 source에 연결하며 테스트 삭제로 완료를 만들지 않는다.
- Reviewer는 증거 없는 Agent 쓰기가 허용되거나 실제 위반을 비교 예외로 숨기지 않는지, snapshot·승인·lease·최종 검증이 보존되는지 읽기 전용으로 확인한다.

## Progress

- [x] 2026-10-06 cfcd0796ad122155321ea54b7ad36624192b139b의 활성 명세 전문, scope 비교·복구 코드와 관련 테스트를 대조함. G-161의 53개 targeted PASS, Manager verify 1268a5c5704f21f6691207ef8c5a4b68의 PASS와 최종 Reviewer PASS를 읽었으며 새 검증을 실행하지 않음.
- [ ] 사용자 정책 결정과 활성 Requirement 정합성 확인
- [ ] 확정된 범위의 구현·targeted test
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-09 delta 기준 b29bb81c9c3c76cc196b30fd04eed1f52a02d6fa, mutable READY token d249095f1d5c09428e872445c00ba5d442b3732eb5ea3eef1ecbf3c2a5833dac와 원본 hash cd18da799d1082dac4b5fc31e757f275ab6e36f26482d556284c8455cdaba71e를 확인했다. R-059 전체 기준 검토에서 AC 7의 R-056 참조에도 기존 legacy 비교 질문이 적용됨을 확인해 같은 ID에 source·정합성 확인만 연결한다. 기존 질문·Facts·Options·Impact·draft 대기·dependency·complexity medium·risk high·priority high를 보존하며 사용자 선택·승인·해제를 작성하지 않는다. source 변경에 결합된 최신 판단 revision은 Manager가 생성한다. 캐시 측정은 독립 계획이며 정책 선택 전 legacy 구현은 계속 보류한다. 구현·테스트·검증 성공을 만들지 않았다.

- G-161의 Merge로 원격 runtime 선택·보존형 로컬 동기화는 구현됐으나 legacy source 변화의 귀속은 바뀌지 않았다. `tests/test_scope_block_revalidation.py`는 격리 증거 없는 같은 source 변화 차단을 유지하도록 명시적으로 검증한다. 구현 누락으로 단정해 검사를 제거하거나 충족으로 감추지 않는다.
- complexity medium: 결정 후 비교·복구 경계는 기존 모듈과 원인별 판정으로 한정된다. 정책 모호함은 높은 effort로 해결하지 않는다. risk high: 잘못된 완화가 증거 없는 범위 밖 쓰기나 저장 차단을 승인할 수 있어 수동 감사·차단 재설정·보존 worktree 복구가 필요하다. 원본 증거 보존과 실제 위반 혼합 거부를 Acceptance Criteria·Validation·Review에 포함하며 외부 쓰기를 코드 rollback으로 되돌린다고 가정하지 않는다.
- G-192의 등록 base별 runtime 귀속 검증과 이 질문은 독립이다. 해당 검증과 다른 READY 작업은 계속할 수 있으며 R-055의 전체 경로 감사만 이 정책 확정을 기다린다.

## Draft Decision

Question: 격리 증거 없는 과거 Agent 실행에서 기본 checkout의 파일·index·HEAD·로컬 base ref만 변한 경우, R-056의 비교 제외를 적용할지, R-023의 엄격 비교를 유지하면서 현재 격리 증거를 확보한 정상 복구 뒤에만 제외할지 활성 Requirement에 명시해 달라.
Facts: R-056은 legacy 기본 checkout 복원을 요구하지 않도록 하지만 R-023·PRODUCT·ARCHITECTURE는 격리 증거 없는 과거 실행에 엄격 비교를 유지한다. 현재 scope_validation의 verified_scope gate가 실패하면 전체 metadata 비교가 남고 test_legacy_execution_without_isolation_cannot_attribute_external_source_change는 같은 기본 checkout 차단을 해제하지 않는다. G-161은 이 경계를 변경하지 않았다.
Options: A는 legacy에도 기본 checkout 비교만 제외하고 다른 shared metadata·선택 자원 검사를 유지한다. B는 증거 없는 과거 실행의 엄격 비교를 유지하되 기본 checkout을 복원하지 않는 증거 확보·세션 복구 조건을 명시한다. 두 경우 모두 실제 Agent 위반·사용자 작업 보존과 자동 자기 승인 금지를 유지해야 한다.
Impact: A는 기본 checkout 변화로 남은 차단을 줄이지만 증거 없는 쓰기의 귀속을 다룰 보호 조건이 필요하다. B는 엄격 비교를 유지하므로 legacy 재개에 필요한 증거·지원 복구·사용자 조치를 구체화해야 한다. 선택과 활성 Requirement 정합성이 확인될 때까지 이 계획과 의존 감사의 Worker·workspace·branch·PR은 시작하지 않는다.
