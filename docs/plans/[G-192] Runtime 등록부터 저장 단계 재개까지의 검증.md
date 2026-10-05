---
id: G-192
title: Runtime 등록부터 저장 단계 재개까지의 검증
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-161
complexity: high
risk: medium
priority: high
---

## Purpose

R-020의 Manager runtime 귀속을 실제 bootstrap부터 runtime 자식의 선검증과 저장 단계 재개까지 검증한다. 기존 함수별 검증 사이의 공백과 거부 경로의 진단 누락을 보완하며, runtime 신뢰 범위와 Agent 권한은 유지한다.

## Acceptance Criteria

- 임시 self 저장소의 SHA A에서 Planner·Worker·Reviewer scope와 작업 상태를 저장하고 같은 state directory의 정상 bootstrap 진입으로 원격 SHA B를 선택한다. 실제 runtime 자식이 exact-SHA runtime 등록을 선검증한 뒤 저장된 단계의 처리를 이어간다. 테스트가 RuntimeMutation이나 허용 경로를 scope 함수에 직접 전달해 이 흐름을 대신하지 않는다.
- 정상 재개와 같은 SHA 재사용에서 session ID, 관리 worktree, Worker diff, 결과 snapshot, retry·failure history와 계획 상태가 보존된다. runtime 등록만으로 새 session·차단을 만들지 않고 같은 등록·기준선 변경을 중복 적용하지 않는다.
- 등록 직후 자식 선검증 전에 중단된 실행은 다음 bootstrap에서 durable intent와 실제 Git 상태를 대조해 재개한다. runtime만 다른 과거 false-positive 결과는 자동 복구하고, 실제 범위 위반이 섞인 결과는 차단과 원본 증거를 유지한다.
- dirty·attached·wrong-SHA·wrong-repository·소유 표식 불명 runtime과 잘못된 경로·과거 또는 미래 SHA 경로는 실제 Git 상태로 거부한다. reset·clean·강제 checkout·제거·재등록으로 수정하지 않으며 저장된 session과 작업 공간을 보존한다.
- runtime 귀속 판정의 수락·거부 양쪽에서 비교한 snapshot identity, exact path·SHA와 설명되지 않은 delta의 종류·관련 경로를 진단할 수 있다. config 값·자격 증명·파일 원문은 로그에 노출하지 않는다. 거부 전에 원본 snapshot을 재설정하지 않는다.
- 기존 엄격 비교 대상의 임의 worktree·branch·config·operation·ref 변경은 계속 거부한다. R-023과 ARCHITECTURE가 정한 유효한 실행별 격리 증거의 범위 판정도 보존하고, 다른 경로를 Manager runtime으로 인정하거나 실제 Agent의 공유 Git 쓰기를 허용하지 않는다.

## Implementation

- runtime/bootstrap·runtime_identity, agents/scope_recovery·agent_scope_decision, recovery/project_block_service의 기존 책임 경계와 tests/support의 Git·SQLite·중단 fixture를 재사용한다. G-161의 원격 bootstrap 전이를 재구현하지 않고 그 Merge 결과를 대상으로 검증한다.
- 현재 tests/test_origin_isolation.py, tests/test_bootstrap_runtime.py와 tests/test_scope_interrupted_agents.py의 함수별 사례를 연결하는 bootstrap 재개 회귀를 추가한다. 실제 Git·임시 state DB·부모와 자식의 identity 전달 및 선검증·단계 전이는 사용하고 외부 Agent·GitHub 호출만 테스트 경계에서 대체한다. 운영 DB나 실제 외부 서비스는 사용하지 않는다.
- 현재 scope_recovery의 snapshot hash 로그는 판정 성공 뒤에 있어 앞선 거부가 비교 identity를 남기지 못한다. 기존 로그·비밀값 제거 경계에서 거부 진단도 남기고, 확인된 연결 결함만 해당 책임 경계에서 교정한다. 전체 snapshot 채택이나 prefix 예외를 추가하지 않는다.
- 역할·문서·검증 권한은 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 따른다. 새 루트 항목과 범용 테스트 infrastructure는 만들지 않는다.

## Validation

- Worker는 진단의 수락·거부·비밀값 제거와 변경한 fixture의 좁은 unit case만 실행한다. 실제 bootstrap 통합 회귀는 작성하되 실행은 Manager 전체 verify에 맡긴다. 실행 가능한 unit 변경이 없다면 그 사유와 통합 검증 경계를 기록한다.
- Manager 전체 verify에서 세 역할의 A→B 실제 bootstrap·자식 선검증·저장 단계 재개, 같은 SHA 재사용, 등록 직후 중단, false-positive 복구, invalid runtime 전체 경우와 다른 shared Git 변경의 혼합을 확인한다. 직접 허용 경로를 주입하는 기존 단위 사례만으로 통합 성공을 보고하지 않는다.
- 기존 Agent 위반 차단, 격리 증거 유무에 따른 범위 판정, source·작업물 보존, process lock, runtime bootstrap 격리와 중단 재개 회귀를 유지한다. 각 R-020 기준에 해당하는 테스트와 Manager 검증 결과를 연결해 남긴다.

## Progress

- [x] 2026-10-05 기준 5ef0bb51840f9c2e93158d53c2b4b64d862f44e9에서 R-020 원문과 bootstrap·scope 구현 및 관련 테스트를 대조함. 기존 runtime 등록 테스트는 준비·귀속 함수를 직접 호출하고 실제 bootstrap→자식→저장 단계 재개를 끝까지 검증하지 않음. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-020의 전체 충족은 기존 G-051·G-052 Merge만으로 확정할 수 없다. 실제 bootstrap 경로를 요구하는 Acceptance Criteria와 거부 진단을 보완하는 후속이다.
- source 갱신 뒤 자식을 시작하지 않는 현재 gate는 G-161이 이미 담당한다. 해당 Merge를 선행 조건으로 두어 원격 실행 구현을 중복하지 않는다. G-189의 최종 차단·재개 감사는 이 검증과 진단 보완 이후 진행한다.
- R-020의 과거 엄격 snapshot에 대한 위반 보존과 R-023의 격리 증거가 있는 실행에 대한 선택 범위 판정을 구분한다. R-020의 모든 사용자 worktree 추가를 차단한다는 문구와 현재 조건부 정책의 적용 관계는 최종 Requirement 판단에 명시하며, 이 계획에서 정책을 새로 선택하거나 권한을 확대하지 않는다.
- 실제 부모·자식과 세 역할의 저장 단계 전이를 검증해야 하므로 complexity high다. 기존 경계를 재사용하는 회귀·진단 보완으로 risk medium, 반복 오탐과 무검증 재개를 막는 작업으로 priority high다.
