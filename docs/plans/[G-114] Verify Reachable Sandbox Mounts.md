---
id: G-114
title: Verify Reachable Sandbox Mounts
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-050] Direct Codex Exec with Native Sandbox.md
depends_on:
  - G-127
  - G-129
complexity: medium
risk: high
priority: critical
---

## Purpose

Codex 내장 도구 sandbox의 경계 확인에서 무관하거나 접근 불가능한 Docker mount 때문에 정상 호출을 오탐 차단하지 않고, 선언된 보호 경로의 쓰기 거부와 허용 경로의 쓰기 가능성을 확실히 확인한다. 현재 PRODUCT/R-050을 기준으로 기존 전체 mount 증명 목표를 선언된 경로의 경계 확인 목표로 조정한다. 과거 mount ID gate는 구현 대상으로 되살리지 않는다.

## Acceptance Criteria

- 경계 확인은 선언된 보호 경로·보호 파일·허용 쓰기 경로에 한정한다. 선언되지 않은 Docker mount, 가려진 mount, /dev 항목과 worktree 하위 트리를 조사하거나 해당 상태 때문에 정상 호출을 거부하지 않는다.
- 보호 경로가 없거나 읽기 전용임을 확인할 수 없으면 실패한다. source checkout, 다른 worktree, shared Git directory와 .git pointer의 쓰기 거부 조건은 유지한다. 보호 경로·파일의 오류를 무관한 mount로 간주해 생략하지 않는다.
- 보호 파일 probe는 기존 내용을 truncate·수정하지 않는다. 쓰기 open의 EACCES·EPERM·EROFS만 거부 증거로 인정하고, 성공 또는 불확실한 오류는 실패한다. 허용 경로의 생성·수정·rename·삭제는 실행 소유 scratch 안에서만 확인하고 기존 사용자 파일을 보존한다.
- 경계 확인 실패 또는 알 수 없는 성공 표식은 격리 성공으로 기록하지 않으며 provider 실행 전에 중단한다. session·작업물·retry 이력과 과거 증거를 보존한다.
- Codex는 직접 `codex exec`로 실행한다. 내장 도구 정책을 확인하는 독립 probe는 provider를 실행하지 않는다. Codex 프로세스 전체·모든 자손의 mount ID, 외부 writable mount 목록, privilege·namespace 및 중첩 sandbox 성공을 별도 증명 조건으로 요구하지 않는다.
- 시스템 정책 변경, sandbox 비활성화, 쓰기 범위 확대 및 승인 정책 완화는 하지 않는다. Reviewer의 파일 쓰기 거부와 테스트 재실행 금지는 각각 내장 정책과 역할 지침으로 유지한다.

## Implementation

- `loop_manager_internal/agents/agent_sandbox_exec.py`의 `check_boundary` 및 `main`, `loop_manager_internal/agents/agent_codex_sandbox.py`의 `CodexLaunch.probe`를 현재 기준으로 사용한다. 선언된 경로 검사와 provider를 실행하지 않는 probe는 이미 구현돼 있으므로 전체 mount parser·O_PATH·fdinfo visibility gate 및 표준 device 예외를 추가하지 않는다.
- `tests/test_codex_native_invocation.py`의 기존 보호 파일·읽기 전용 실패·scratch·알 수 없는 probe 출력·실제 gate 실패 사례와 Acceptance Criteria를 대조하고 빠진 실패·오탐 회귀만 보완한다. 특히 선언되지 않은 mount 상태를 읽지 않는 경계와 선언된 보호 경로의 누락·조회 오류를 계속 거부하는 경계를 확인한다. 현재 구현을 재작성하거나 동일 검증을 새 모듈에 중복하지 않는다.
- 확인된 결함이 있을 때만 기존 probe 책임 경계 안에서 수정한다. 정책 설정·repository config 수집과 Planner 투영은 G-113의 책임이며 이 계획에 추가하지 않는다. 실제 모델 실행 또는 운영 환경의 시스템 설정 변경을 검증 조건으로 요구하지 않는다.

## Validation

- Worker는 `tests/test_codex_native_invocation.py`의 `NativeWriteIsolationTests` 중 수정한 probe·실패 전달 경계와 새로 보완한 회귀의 최소 targeted unit test만 실행한다. unrelated mount 조사 없음, 보호 경로 누락·읽기 전용 조회 실패, 보호 파일 쓰기 성공·불확실 오류 거부, scratch 보존 및 실패 시 evidence 무효화를 검증한다.
- 기존 구현·회귀로 모든 조건이 충족되고 코드·테스트 변경이 없으면 그 근거와 targeted test 미실행 사유를 기록한다. 과거 agent-write-boundary-v1 운영 결과는 현재 agent-tool-boundary-v2 검증 성공으로 사용하지 않는다.
- 전체 verify는 Manager가 실행하고 직접 호출·신규·재개·fallback의 정책 및 session 보존 회귀를 확인한다. Reviewer는 전달된 결과와 diff만 읽기 전용으로 검토한다.

## Progress

- [x] 과거 작업 기록: 이전 mount gate 수정·targeted unit test 9개 및 agent-write-boundary-v1 envelope 확인 (현재 정책·revision의 검증 결과가 아님)
- [ ] 현재 선언된 경로 probe와 Acceptance Criteria 대조 및 빠진 오탐·거부 회귀만 보완
- [ ] 변경한 경계의 targeted 검증 또는 변경 없음 근거 기록
- [ ] Manager 전체 verify, 독립 Review 및 사용자 Merge

## Decision Log

- 2026-10-01: 사용자가 확인된 로직 오류를 핫픽스로 수정하고 main에 push하도록 요청했다. AppArmor profile 설치 후 native sandbox 초기화는 성공하며 보호 경로 100개와 Git pointer는 실제 읽기 전용이다. mountinfo에 남아 있는 접근 불가 Docker mount와 현재 proc에 덮인 autofs entry가 오탐의 원인이다. 격리 정책을 완화하지 않고 실제 mount identity와 접근 불가 상태를 구분한다.

- 실제 mount가 replacement /dev 아래에서 사라진 경우도 kernel 경로 open의 ENOENT로 구분한다. fdinfo 읽기 실패와 그 밖의 오류는 접근 불가로 오인하지 않고 실패를 유지한다. 표준 device 예외는 /dev의 지정된 character device에만 적용한다. 기존 verify 스크립트가 해당 모듈과 테스트 전체를 포함하므로 변경하지 않는다.
- 2026-10-02 사용자 결정: 리팩터링 우선 요청에 따라 미착수·무소유 READY인 이 계획은 G-127 Merge 이후 진행한다. 원래 mount 판정·보호 경계·검증 조건과 목표는 유지하고 실행 시 최신 Agent module·test 경로 및 현재 PRODUCT/R-050의 직접 Codex 실행 계약을 확인한다. 과거 운영·targeted test 결과를 새로운 revision의 검증 성공으로 사용하지 않는다.

- 2026-10-03 사용자 요청에 따른 정책 정합성 수정: 대기 중인 모든 계획을 현재 정책에 맞춰 수정하라는 지시에 따라, PRODUCT/R-050이 제외한 전체 mount·privilege·namespace 증명 조건을 현재 Acceptance Criteria·Implementation·Validation에서 제거하고 원래 오탐 방지 문제를 선언된 경로 검사로 한정했다. 이는 유지보수 Planner가 독자적으로 목표를 축소한 결과가 아니라 사용자 요청에 따른 기존 계획 편집이다. ID·경로·title·dependency는 유지하며 sources에 R-050을 추가했다. 위 2026-10-01 mount gate 기록과 2026-10-02 목표 보존 결정 중 현재 정책과 충돌하는 부분은 이 결정으로 대체하며 과거 기록은 보존한다. SQLite와 실행 상태는 수정하지 않는다.
