---
id: G-199
title: 격리 증거 없는 legacy 기본 checkout 비교 경계
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-010] Origin-Isolated Local Worktree Operation.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
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
priority: critical
---

## Purpose

확정된 A안에 따라 legacy 기본 checkout 비교 제외와 일반 verify·Agent preflight의 Git 오탐을 공통 검사 경계에서 제거한다. BLOCKED 인계 뒤 현재 사용자 파일·index·HEAD를 새 입력으로 수용하고 과거 snapshot 복원이 새 작업을 덮지 못하게 한다. 원격 통합·게시의 잔여 교정과 전체 경로 감사는 G-189에서 다룬다.

## Acceptance Criteria

- 격리 증거가 없거나 불완전한 과거 실행에도 기본 checkout의 파일·index·HEAD·로컬 base ref를 비교에서 제외한다. 실제 Agent 위반과 선택 자원의 소유권·origin·보안 설정 오류는 유지하며 과거 격리 성공을 합성하지 않는다.
- 일반 verify는 Agent invocation ledger 부재로 repository-wide scope를 적용하지 않는다. 실행 대상·실제 검증 입력이 불변이면 외부 fetch·무관한 ref/worktree 변화로 성공을 거부하지 않는다.
- Agent preflight는 전체 snapshot digest 완전 일치를 요구하지 않고 현재 호출의 workspace·자원·입력·정책을 확인한다. 신규 호출의 쓰기 격리·Reviewer 읽기 전용은 유지한다.
- BLOCKED 인계 뒤 파일 수정·staging·commit을 이전 writer 종료와 repository·계획·worktree·branch 연결 확인 후 수용한다. 과거 시작/결과와 현재 파일·index·HEAD의 일치가 재개 gate가 아니며 read-tree/reset/checkout 복원이 사용자 변경을 덮지 않는다.
- 바뀐 후보·검증 입력의 Worker 확인·targeted test·verify·Review·게시 token을 정확히 무효화하고 필요한 단계부터 이어간다. 과거 실패·session·snapshot·게시 감사와 독립 차단·승인은 보존한다.
- R-055의 T01–T06·T10–T11·T18·T22를 Agent 결과·저장 선검증·계획/프로젝트 복구·checkpoint의 실제 소비자에 연결한다. 실제 위반 혼합·관측 경쟁·중단·재시작을 함께 검증한다.

## Implementation

- agents/scope_validation.py·scope_observation.py·agent_scope_decision.py의 비교 투영, invocation_service.py·agent_write_isolation.py의 preflight와 execution/execution_verify_handler.py를 대조해 사용 자원 중심 판정을 재사용한다. 전체 snapshot 동등성 gate와 중복 검사를 제거한다.
- blocked_agent_local_baseline·scope_recovery.py·scope_baseline.py 및 recovery/scope_reconciliation.py·project_block_service.py에서 사용자 인계·현재 상태 수용을 과거 결과 비교·복원보다 먼저 처리한다. Manager 소유 미완료 Git 복구와 사용자 수동 변경을 구분한다.
- 구현 PR에서 PRODUCT·ARCHITECTURE의 해당 현행 절을 갱신한다. R-059의 기본 checkout 제외에도 같은 기준을 적용하며 캐시 구현·측정을 재계획하지 않는다. 새 복구 CLI·schema·범용 격리 계층은 추가하지 않는다.

## Validation

- 최소 unit은 tests/test_scope_block_revalidation.py·test_agent_write_isolation.py·test_worker_validation.py·test_scope_owned_publication.py에서 실제 변경 함수에 해당하는 case만 선택한다. Git 관측 경계를 대체하되 판단·SQLite·파일 보존은 실행한다.
- 기존 test_legacy_execution_without_isolation_cannot_attribute_external_source_change의 기본 checkout 전용 기대는 확정 A안에 맞추고 실제 Agent 위반·선택 worktree/branch·보안 설정 변경 거부는 별도 assertion으로 유지한다.
- 실제 shared ref와 여러 worktree의 동작이 필요한 기존 integration 회귀로 외부 fetch와 선택 후보 변경을 구분한다. 같은 정책 조합을 새 저장소·전체 Agent 실행으로 반복하지 않는다.
- Manager 전체 verify와 읽기 전용 Review는 파일·index 보존, 후보 성공 무효화, 재시작·원인별 해제 및 독립 차단·lease 보존을 확인한다.

## Progress

- [x] 2026-10-06 cfcd0796 기준 G-161 구현·legacy 엄격 비교 회귀를 대조한 기록 보존. 당시 확인한 targeted 53개·Manager verify 1268a5c5704f21f6691207ef8c5a4b68·최종 Reviewer PASS는 역사 근거이며 이번 새 실행 결과가 아님.
- [x] 2026-10-09 사용자 기준 문서의 A안·현재 입력 수용을 활성 Requirement에 반영
- [ ] 공통 비교·preflight·verify와 BLOCKED 인계/복원 경계 교정
- [ ] 최소 unit 및 필요한 실제 Git 회귀
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-09: 사용자 기준 문서의 6.1절에서 A안을 확정했다. 직접 문서 편집 위임에 따라 기존 정책 질문·draft 표시를 제거하고 같은 ID·경로·dependency와 기존 조사 근거를 보존한다. SQLite의 결정 revision·승인·실행 상태는 작성하지 않는다.
- 2026-10-06 cfcd0796 및 2026-10-09 b29bb81 기준 조사는 G-161의 원격 runtime·로컬 동기화 구현과 legacy 차단 회귀를 구분했다. R-059의 AC 7에도 같은 질문이 적용됐으며 캐시 성공으로 scope 완화를 증명하지 않았다. 당시 A/B 판단 대기는 이번 명세로 대체되며 구현 성공으로 취급하지 않는다.
- risk high: 잘못된 수용·복원은 사용자 작업 유실·실제 Agent 위반 은폐를 만들 수 있다. 원본 증거·독립 차단·현재 자원 검증을 유지한다. priority critical: 일반 verify와 수동 수정 후 재개를 막는 P0 오탐을 먼저 제거한다.
