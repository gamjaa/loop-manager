---
id: G-088
title: Verifiable Agent Git Write Isolation
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-051
  - G-091
complexity: high
risk: critical
priority: critical
---

## Purpose

현재 Codex의 `--approve-for-me`와 Claude의 `acceptEdits` 호출만으로는 Agent가 선택된 관리 worktree 밖의 파일과 공유 Git metadata를 쓸 수 없다는 증거가 없다. R-023의 무관한 Git 변화 비차단 판단에 앞서 실행별 쓰기 격리를 검증 가능한 상태로 만든다.

## Scope and Environment Assumptions

- 사용자는 2026-09-29에 하위 경로에 별도 Git 경로가 있거나 이와 유사한 특수 배치가 있는 경우를 고려하지 않아도 된다고 결정했다. 이 계획은 선택된 writable worktree와 실행별 임시 경로 안에 별도 하위 Git 저장소가 없고, 기본 source checkout·다른 worktree·shared Git metadata를 하위 경로로 노출하는 별도 bind mount 별칭이 없는 운영 환경을 전제로 한다.
- 하위 Git 저장소 및 위와 같은 mount 별칭의 전수 탐지, 별칭 원본의 device·inode 대조, 해당 배치를 통한 우회 방지와 그 회귀 테스트는 이 계획의 구현·격리 증명·Review 범위에서 제외한다. 그 전제를 프로그램이 별도로 증명하도록 요구하지 않으며, 해당 제외 사례만을 이유로 격리 확인을 거부하거나 blocking issue를 만들지 않는다.
- 선택된 등록 저장소의 표준 `.git` 디렉터리 또는 linked worktree의 `.git` 파일, 실제 per-worktree Git directory와 common/shared Git directory는 보호 대상이다. 기본 source checkout과 다른 등록 worktree의 canonical 경로, provider 내부 쓰기와 역할별 허용 파일·index 범위에 대한 기존 검증은 계속 적용한다.

## Acceptance Criteria

- 위 운영 환경 전제에서 Planner·Worker·Reviewer와 유지보수 Planner의 실제 호출 중 선택된 관리 worktree의 허용 파일·index 경계 밖, 기본 source checkout, 다른 worktree 및 shared Git metadata에 대한 Agent 쓰기가 차단됨을 실행 전에 증명한다. provider 내부 동작 때문에 이를 분리해 증명할 수 없는 환경은 격리 확인으로 표시하지 않는다.
- 격리 증거는 provider, 실행 환경, 역할, 선택된 canonical worktree, repository identity 및 Agent session/turn에 결합해 보존한다. 재개·provider 전환·설정 변경·중단 뒤에는 이전 증거를 무조건 재사용하지 않는다.
- 쓰기 격리를 검증할 수 없는 실행과 이를 증명할 수 없는 과거 기록은 기존의 엄격한 shared ref·다른 worktree 감시를 유지한다. 격리 확인 실패를 근거로 Agent의 Git·worktree·push·PR 권한을 확대하거나 무관한 변화를 외부 변경으로 추정하지 않는다.
- 지원하는 Linux·Windows 및 Codex·Claude 호출에서 격리 적용·거부 결과가 명확하며, 비밀값과 기본 worktree의 로컬 상태를 노출하거나 변경하지 않는다. 격리와 기존 허용 변경이 양립하지 않는 경우에는 무인 실행을 안전하게 멈추고 구체적 원인을 남긴다.

## Implementation

- `agent_invocation.py`와 `loop_manager.py`의 provider 호출 경계에 실행 가능한 격리 설정 및 실제 효력 확인을 넣는다. 단순 CLI 인자나 작업 디렉터리 지정만으로 확인했다고 기록하지 않는다.
- 검증된 실행만 식별할 수 있는 최소 durable 증거를 기존 Agent session·snapshot 상태와 연결한다. 선택된 worktree의 허용된 파일·index 처리와 provider 자체의 임시 ref 동작을 분리해, shared metadata 쓰기를 격리할 수 없다면 엄격 판정 경로를 유지한다.
- 호출 직전의 canonical path, worktree 등록, repository identity와 격리 설정을 대조하고 실패·중단·재시작 경로의 증거 수명을 명시한다. G-089는 이 증거가 유효한 경우에만 범위 축소를 사용한다.

## Validation

- Worker는 `tests/test_agent_invocation.py`와 직접 변경한 `tests/test_origin_isolation.py`의 격리·session targeted unit test만 실행한다.
- 선택 worktree 쓰기 허용, 기본 checkout·다른 worktree·shared metadata 쓰기 거부, provider/OS별 설정 불가, 재개·전환·중단 뒤 증거 무효화와 과거 세션의 엄격 fallback을 검증한다.
- 하위 Git 저장소 또는 보호 경로의 하위 bind mount 별칭이 존재하는 배치는 검증 대상에 포함하지 않는다. 등록된 저장소 자체의 `.git` 파일과 실제 shared Git metadata 보호는 검증 대상에 포함한다.
- 전체 verify에서 기존 Agent 결과 protocol, 승인된 guidance, process lock과 비밀값 격리가 유지돼야 한다.

## Progress

- [x] 실행별 쓰기 격리와 확인 증거 연결
- [x] 증거 없는 실행의 엄격 fallback 및 회귀 검증

## Decision Log

- R-023은 격리 증명 없는 실행에서 무관한 Git 변화의 비차단을 허용하지 않는다. 특정 CLI 옵션의 존재를 격리 증명으로 간주하지 않는다.

- 2026-09-26: 사용자 요청에 따라 반복되는 Manager rebase 소유권 오탐을 기존 G-091에서 먼저 수정한다. G-091의 구현 PR이 Merge된 뒤 이 계획의 기존 작업물과 게시 checkpoint를 보존해 재개한다.

- 2026-09-26 Worker 재확인: 현재 HEAD `94bb59f`에는 증거 없는 snapshot의 엄격한 shared Git 경계를 검증하는 회귀 테스트만 포함되어 있고 worktree에는 미커밋 변경이 없다. 호출 전 쓰기 격리, 효력 검증 및 실행·세션에 결합된 durable 증거는 아직 구현되지 않았다. 미해결 검토 `review:loop_manager.py::run_agent::missing-write-isolation` occurrence=2, revision=2는 해결되지 않았다.
- 승인 대기 범위: (1) `agent_invocation.py`와 `loop_manager.py::run_agent` 호출 경계에 선택 worktree 밖·source checkout·다른 worktree·shared Git metadata 쓰기를 차단하고 실행 전에 효력을 확인하는 격리 적용 및 확인 불가 시 안전 중단, (2) provider·환경·역할·canonical worktree·repository identity·session/turn에 결합된 실행별 증거 보존과 재개·provider/설정 변경·중단 뒤 재검증. 증거 없는 실행·과거 기록의 엄격한 Git 판정은 유지한다.
- 이전 자동 승인 검토가 위 두 변경을 아키텍처 승인 대상으로 거부했다는 전달 기록과 `ARCHITECTURE.md`의 Architecture Change Policy를 확인했다. 이번 재개 요청에는 해당 거부를 해소하는 별도 승인 결정이 없어 구현을 중단한다. `AGENTS.md` 규칙 자체의 변경은 필요하지 않으므로 guidance 변경 제안을 만들지 않는다. 사용자 승인 및 근거 문서 반영 후 재개가 필요하다. SQLite 상태 전이는 Architecture에 따라 Loop Manager 프로그램이 담당하며 Worker가 직접 수정하지 않았다.
- 이번 변경은 이 Decision Log 기록뿐이다. 실행 코드·테스트·verify 스크립트를 변경하지 않았으므로 targeted unit test를 실행하지 않았다. 이전 세션의 테스트 성공을 이번 세션의 검증 결과로 보고하지 않는다.

- 2026-09-26 검토 occurrence=3, revision=3 후속 확인: origin/main 대비 변경에는 위 Decision Log뿐 아니라 `tests/test_origin_isolation.py`의 회귀 테스트가 포함된다. 이번 세션에서 `python3 -m unittest discover -s tests -p test_origin_isolation.py -k test_unverified_agent_snapshot_keeps_strict_shared_git_boundary`를 실행하여 1개 테스트 통과(OK, 종료 코드 0)를 확인했다. 이는 증거 없는 snapshot의 엄격한 판정만 검증하며 호출 전 격리 또는 실행별 증거 보존의 구현·검증을 뜻하지 않는다. 해당 blocking issue와 승인 대기는 유지된다. 전체 verify는 실행하지 않았다.

- 2026-09-27 Worker: 사용자 재요청과 ARCHITECTURE의 R-023 사용자 결정에 따라 Linux Codex 호출에 등록 worktree·repository identity 확인, provider sandbox 사전 쓰기 검사, 호출 직전 설정·CLI·Git 지문 재확인 및 호출별 SQLite 증거를 추가했다. exec-policy 규칙이 있는 호출은 sandbox 밖 실행 가능성을 배제할 수 없어 중단한다. 실패·중단·과거 호출의 증거는 검증된 성공 호출로 조회할 수 없고 기존 엄격한 shared Git 판정을 유지한다. Linux Claude는 CLI 내부의 Bash·파일 도구와 session 설정을 동일한 사전 검사로 증명할 수 없고, native Windows는 현재 검사 경로가 없어 무인 호출을 시작하지 않고 구체적 격리 오류로 거부한다. 따라서 모든 지원 provider에서 격리 효력 검증을 마치는 첫 Progress 항목은 아직 완료하지 않았다. `tests/test_agent_invocation.py`의 `WriteIsolationTests` 12개, 기존 중단 session targeted case 1개, `tests/test_origin_isolation.py`의 엄격 fallback targeted case 1개가 통과했다. 전체 verify는 Loop Manager 실행을 기다린다.

- 2026-09-27 Worker 추가 검토: linked worktree의 `.git` pointer 쓰기 거부 검사와 Codex 확장·hook 설정의 fail-closed 판정을 더했다. 이전 권한 부여를 재현할 수 없는 재개 session은 새 증거로 승인하지 않고 호출 전에 거부한다. 지원되지 않거나 provider 파일 도구까지 격리를 증명할 수 없는 환경은 거부 증거를 보존하는 경계로 처리하므로 실행별 적용·거부 및 증거 연결 Progress를 완료로 갱신한다. 동일 targeted 명령의 `WriteIsolationTests` 15개와 엄격 fallback 단일 case가 통과했다. 전체 verify는 실행하지 않았다.

- 2026-09-27 Worker verify 피드백 수정: 기존 scope unit test가 실제 CLI 함수 `run_codex`를 mock할 때 adapter의 선행 preflight가 테스트용 설정에서 ledger identity 없이 실행돼 다수 오류가 발생했다. preflight를 `run_codex`·`run_claude`의 실제 process·log 생성 직전으로 옮겨 운영 호출의 격리 검사는 유지하면서 mock 경계를 복원했다. 보고된 실패 중 Reviewer 변경, 중단 Worker 재개 및 archive 게시 단일 case가 각각 통과했고, 갱신된 `WriteIsolationTests` 16개가 통과했다. Worker는 전체 verify를 실행하지 않았으며 Loop Manager 재검증을 기다린다.
