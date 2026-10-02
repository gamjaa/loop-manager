---
id: G-114
title: Verify Reachable Sandbox Mounts
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-127
complexity: medium
risk: high
priority: critical
---

## Purpose

Ubuntu의 Codex sandbox 초기화는 성공하지만 접근 불가능한 Docker mount와 덮여서 보이지 않는 기존 mount의 rw 표기를 전체 CLI gate가 실제 쓰기 권한으로 오인해 자동 실행을 중단하는 결함을 수정한다.

## Acceptance Criteria

- kernel fdinfo의 mount ID로 실제 경로에서 보이는 mount만 판정한다. 접근 권한이 없거나 덮인 경로가 존재하지 않아 열 수 없는 무관한 mount는 쓰기 가능한 mount로 오인하지 않는다.
- 보호 경로와 관련된 접근 불가 mount, 실제 보이는 허용 범위 밖 writable mount, 불확실한 mount ID는 계속 거부한다.
- native sandbox의 표준 character device mount만 허용한다. source, 다른 worktree, shared Git 및 .git pointer 읽기 전용 검증과 privilege·namespace·파일 쓰기 검증을 유지한다.
- worktree 하위 트리 조사, 시스템 정책 변경, sandbox 비활성화 및 승인 정책 변경은 하지 않는다.

## Implementation

- agent_sandbox_exec에 O_PATH와 fdinfo 기반의 mount visibility 판정을 추가한다. kernel mount 목록은 그대로 파싱하고 실제 보이는 mount를 기존 gate에 전달한다.
- /dev의 표준 character device에 대한 devtmpfs 예외만 명시한다.
- 관련 gate unit test와 운영 환경의 동일 sandbox 진단으로 정상 경로 및 거부 경로를 확인한다.

## Validation

- tests.test_agent_invocation.NativeWriteIsolationTests의 gate 관련 targeted unit test를 실행한다. 보이지 않는 mount, 접근 불가 무관·보호 mount, 알 수 없는 상태, 외부 writable mount 및 device 예외를 검증한다.
- 실제 Planner envelope에서 기존 gate가 성공하는지 모델 실행 없이 확인한다. 전체 verify는 Manager가 수행한다.

## Progress

- [x] gate 수정 및 직접 관련 targeted unit test 9개 통과
- [x] 실제 Planner 쓰기 envelope와 Reviewer 읽기 전용 envelope 모두 agent-write-boundary-v1 통과
- [ ] 사용자 요청 main 핫픽스 push
- [ ] Manager 전체 verify 및 Review

## Decision Log

- 2026-10-01: 사용자가 확인된 로직 오류를 핫픽스로 수정하고 main에 push하도록 요청했다. AppArmor profile 설치 후 native sandbox 초기화는 성공하며 보호 경로 100개와 Git pointer는 실제 읽기 전용이다. mountinfo에 남아 있는 접근 불가 Docker mount와 현재 proc에 덮인 autofs entry가 오탐의 원인이다. 격리 정책을 완화하지 않고 실제 mount identity와 접근 불가 상태를 구분한다.

- 실제 mount가 replacement /dev 아래에서 사라진 경우도 kernel 경로 open의 ENOENT로 구분한다. fdinfo 읽기 실패와 그 밖의 오류는 접근 불가로 오인하지 않고 실패를 유지한다. 표준 device 예외는 /dev의 지정된 character device에만 적용한다. 기존 verify 스크립트가 해당 모듈과 테스트 전체를 포함하므로 변경하지 않는다.
- 2026-10-02 사용자 결정: 리팩터링 우선 요청에 따라 미착수·무소유 READY인 이 계획은 G-127 Merge 이후 진행한다. 원래 mount 판정·보호 경계·검증 조건과 목표는 유지하고 실행 시 최신 Agent module·test 경로 및 현재 PRODUCT/R-050의 직접 Codex 실행 계약을 확인한다. 과거 운영·targeted test 결과를 새로운 revision의 검증 성공으로 사용하지 않는다.
