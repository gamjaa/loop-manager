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

- 2026-09-27 Worker verify 후속 수정: Planner import fixture에 새 `agent_write_isolation.py`를 포함하고, Planner 중단·재개 unit test의 외부 Codex 사전 검사만 test double로 고정해 시스템의 MCP·plugin·hook 설정이 프로세스 중단 검증을 가리지 않게 했다. 보고된 두 단일 targeted unit test를 각각 실패 명령 그대로 재실행해 통과했다. 전체 verify는 Worker가 실행하지 않았다.

- 2026-09-27 Worker review occurrence=5, revision=5 수정: 현재 `codex sandbox` 사전 검사는 기존 파일을 쓰기 모드로 여는 명령 경계만 확인한다. 새 파일 생성·삭제와 `codex exec` 내부 파일 도구의 동일한 쓰기 경계를 증명하지 못하므로 Linux Codex 호출도 `verified`로 기록하지 않고, 구체적 거부 증거를 남긴 뒤 process·기존 session/log 변경 전에 중단한다. 이전 Progress의 격리 완료 표시는 철회했다. provider 내부 경계를 독립적으로 검증할 수 있는 방법이 마련되기 전에는 모든 provider의 무인 호출을 안전하게 거부하며 엄격한 Git 판정을 유지한다. `AGENTS.md` 규칙 변경은 필요하지 않다.

- 2026-09-27 Worker targeted 검증: `tests/test_agent_invocation.py`의 `WriteIsolationTests` 17개를 실행해 명령 sandbox 검사만 통과한 Codex 호출도 거부되고, 실제 `run_agent` 경계에서 거부 사유가 SQLite에 남으며 CLI process가 시작되지 않고 기존 로그가 보존되는 것을 확인했다. 전체 verify는 실행하지 않았다.


- 2026-09-27 Worker review occurrence=6, revision=6 재조사: clean worktree에서 현재 구현과 Architecture의 R-023 설계를 다시 확인했다. `prepare_isolation`에는 성공 경로가 없으므로 이전 거부 증거와 unit test 통과는 G-088 완료 근거가 아니다. 첫 Progress 항목과 blocking issue는 미해결로 유지한다.
- 설치된 `codex-cli 0.157.1`의 `codex sandbox --help`는 임의 명령 전체를 내장 Linux sandbox에서 실행하는 경로를 제공한다. 그러나 기존 설정 그대로 `codex exec` 전체를 감싸면 `network_access=false`가 모델 API 연결에도 적용되고, 허용 worktree 밖의 `run_codex` 결과 경로와 provider session 저장도 제한된다. 명령 sandbox probe와 실제 CLI 전체의 실행 경계를 동일하게 만드는 것뿐 아니라 이 runtime 쓰기의 분리도 필요하다. 공식 설정 문서(https://learn.chatgpt.com/docs/config-file/config-reference)는 `sandbox_mode`를 명령 실행 정책으로 설명하며, 이것만으로 내부 파일 도구 전체의 격리를 확인했다고 기록할 수 없다.
- 검토 가능한 보완 설계: (1) provider 내장 workspace 정책·자동 승인 검토를 그대로 유지하면서 provider CLI 전체와 자손에 적용되는 OS 쓰기 경계를 추가한다. (2) Manager가 호출별 최소 runtime 디렉터리를 만들고 소유권을 확인하며, 결과·임시 파일은 그곳에만 쓴다. (3) provider session은 역할·repository identity에 결합한 Manager 소유 경로에서 보존·재개하고 기존 인증 자료는 복사·이동하거나 쓰기 허용하지 않는다. (4) API 연결은 provider runtime에 필요하지만 Agent 명령의 기존 network 정책과 승인 범위는 확대하지 않는다. (5) 실제 호출을 시작하는 동일한 경계 안에서 허용 파일의 생성·수정·삭제 및 보호 경로의 쓰기 거부를 검증하고, 검증 뒤에만 호출 identity에 결합한 증거를 저장한다. 보호 경로 검사는 source checkout의 로컬 상태를 변경하지 않아야 한다. 지원하지 못하는 OS/provider 또는 인증 갱신·session 저장과 양립하지 않는 실행은 구체적인 이유로 거부한다.
- 위 보완은 현재 Architecture 221–236행의 provider 내장 제한 설계 및 368행 이후 Architecture Change Policy와 대조할 사용자 결정이 필요하다. 추가 OS 경계와 provider runtime/session 경로의 설계 승인을 질문했으며 아직 승인 응답은 없다. 이 기록은 구현 완료나 사용자 승인으로 간주하지 않는다. AGENTS.md 규칙 변경은 필요하지 않아 guidance proposal을 만들지 않았다. SQLite 차단 전이는 Loop Manager 프로그램에 맡긴다.
- 이번 세션의 변경은 선택된 ExecPlan의 조사·보완 설계·승인 대기 기록뿐이다. 실행 코드·테스트·verify 스크립트를 변경하지 않아 targeted unit test는 실행하지 않았다. 이전 세션의 통과 결과를 이번 세션의 실행 결과로 보고하지 않는다. commit, push, PR 생성과 전체 verify는 실행하지 않았다.


- 2026-09-27 Worker review occurrence=7, revision=7 구현: 외부 격리 infrastructure를 추가하지 않고 설치된 provider의 `codex sandbox`로 `codex exec` 전체를 감싼다. 신규·재개·Planner·Worker·Reviewer·유지보수 역할의 Linux Codex 호출은 같은 native sandbox prefix와 동일한 검증 helper를 사전 검사와 실제 exec에 사용한다. 모든 호출을 무조건 거부하던 경로를 제거했고, 확인된 호출만 `verified` 증거를 저장한다. 실제 exec 직전 helper가 다시 실패하면 provider를 시작하지 않고 실패 호출의 증거를 무효화한다.
- 증명 경계: Linux mount namespace의 root와 보호 경로·하위 mount가 읽기 전용인지, 예상 밖 writable mount가 없는지, effective capability 제거·no_new_privs와 host process namespace 접근 차단을 확인한다. source checkout의 파일을 생성·수정·삭제하지 않고 readonly mount와 기존 Git 파일의 비파괴 쓰기-open 거부로 보호를 확인한다. 허용 경로에서만 임시 파일 생성·수정·rename·삭제를 실행한다. 기존 hardlink가 다른 읽기 전용 경로의 inode를 쓸 수 있는 경우도 시작 전에 거부한다. read-only mount 증거를 제공하지 못하는 Landlock-only/알 수 없는 native 구현도 성공으로 표시하지 않는다.
- runtime 경계: Manager 상태 디렉터리 아래 repository·worktree·role에 결합한 전용 Codex session 경로와 호출별 임시 경로만 추가 허용한다. 기존 인증·설정 파일은 원래 위치를 유지한 읽기 전용 symlink이며 복사·이동하지 않는다. 자식 환경만 전용 provider 경로를 사용한다. 바깥 sandbox는 CLI의 API 연결을 허용하지만 안쪽 Agent 명령은 기존 network 제한, granular 권한 확대 거부와 자동 승인 검토를 유지한다. 이전 session이 전용 저장소에 없거나 인증 갱신 등이 제한과 양립하지 않으면 기존 실패·새 세션 fallback 흐름을 사용하며 권한을 확대하지 않는다.
- 호출별 정책 지문은 native sandbox prefix·session identity·설정에 결합되고 재개마다 새 검사와 새 호출 임시 경로를 사용한다. Reviewer도 등록된 관리 linked worktree를 요구한다. 실제 gate 실패 시 이전 결과·로그를 보존하고 timeout·중단 시 추가 sandbox 프로세스 그룹을 종료한다. 기존 증거 없는 session의 엄격한 Git 판정은 유지한다. Claude와 native Windows의 확인 불가 거부는 유지하며 이번 변경으로 지원을 주장하지 않는다.
- targeted 검증: `python3 -m unittest discover -s tests -p test_agent_invocation.py -k WriteIsolationTests`로 격리 관련 두 unit test class의 30개 테스트가 통과했다. native mount 검증 로직, hardlink·경로 중첩 거부, 역할별 정상 adapter 연결, 신규 streaming session·재개, 실패 증거 무효화, 기존 로그 보존과 timeout 프로세스 그룹 종료를 검증한다. CLI·kernel 경계는 unit test double로 제어했으며 실제 provider 호출 또는 integration test를 실행했다고 주장하지 않는다. 새 모듈을 기존 verify의 compile 목록과 import fixture에 추가했고 전체 검증 범위는 줄이지 않았다. 전체 verify와 Reviewer 결과는 Loop Manager 실행을 기다린다. AGENTS.md, docs/PLANS.md와 Requirement는 변경하지 않았고 commit·push·PR 생성도 하지 않았다.

- 2026-09-27 Worker review `review:agent_write_isolation.py::inspect_worktree::document-draft-runtime-rejection` occurrence=1, revision=1 수정: 문서 수정본 `generate`는 실행 중인 runtime `ROOT`를 읽기 전용 Planner workspace로 사용하므로 `state_dir/worktrees`만 허용하던 검사가 기존 명령을 거부했다. 읽기 전용 호출에 한해 `state_dir/runtimes/<canonical source repository hash>/<HEAD SHA>`의 등록된 linked worktree를 추가 허용한다. canonical 경로, shared Git identity, exact SHA, detached HEAD와 clean 상태를 확인하며 runtime의 쓰기 호출, source checkout, alias·미등록·repository hash 불일치는 계속 거부한다. 허용된 runtime도 native sandbox의 protected 경로에 포함되고 writable roots에는 포함되지 않는다.
- 회귀 검증은 실제 `_document_drafts_command(generate)` → `run_agent` → Codex adapter → `inspect_worktree` 연결을 실행하되 Git·CLI·문서 저장소 외부 경계는 unit test double로 제어한다. runtime에서 Agent 호출과 호출별 SQLite 증거 기록까지 진행하는지, runtime 쓰기 및 identity·상태 불일치를 거부하는지 확인했다. `python3 -m unittest discover -s tests -p test_agent_invocation.py -k RuntimeReadOnlyIsolationTests` 3개와 `python3 -m unittest discover -s tests -p test_agent_invocation.py -k test_worktree_inspection_rejects_alias_and_unregistered_path` 1개가 통과했다. 기존 verify 스크립트는 변경하지 않았고 전체 verify·integration test·실제 provider 실행·commit·push·PR 생성은 수행하지 않았다.

- 2026-09-29 User Decision: 사용자가 G-088의 차단 알림에 포함된 하위 writable mount의 source/shared Git 별칭 사례를 제시하며, 하위 경로에 Git 경로가 있는 등의 사례를 고려하지 않아도 된다고 명시했다. 위 운영 환경 전제와 제외 범위를 승인된 계획 범위로 반영한다. `loop_manager.py::run_agent::missing-write-isolation`의 마지막 Review가 요구한 하위 bind mount 별칭 탐지·거부 및 회귀 테스트는 현재 Acceptance Criteria가 아니다. Worker와 Reviewer는 남아 있는 실제 격리 요구를 확인하되 이 제외 사례를 다시 교정 또는 blocking 근거로 사용하지 않는다. 기존 Review·실패 감사는 보존하고, 계획 입력 변경을 감지한 Manager가 정상 재개와 필요한 검증·Review를 수행한다.

- 2026-09-30 Worker review `review:agent_codex_sandbox.py::codexlaunch.run::runtime-handoff-bypass` occurrence=1, revision=1 수정: `CodexLaunch.run`은 process 생성 함수를 필수 입력으로 받고 실제 `run_codex` 호출은 기존 `_handoff_popen`을 전달한다. runtime handoff 확인·spawn을 기존 gate 잠금 안에서 수행하며 process 대기·종료 처리 중에는 잠금을 유지하지 않는다. 사전 검사 중 handoff가 발생하면 실제 sandbox 실행 경로에서 Reviewer·재개 Worker·신규 streaming Worker의 spawn을 거부하고 호출별 격리 증거를 무효화하며 기존 session·결과·로그를 보존한다. 정상 Reviewer·재개 Worker가 같은 잠금 경계에서 시작하고 대기 중 handoff를 허용하는 회귀 테스트도 추가했다.
- 현재 후보의 targeted 검증: `python3 -m unittest discover -s tests -p test_agent_invocation.py -k WriteIsolationTests` 32개, `python3 -m unittest discover -s tests -p test_agent_invocation.py -k RuntimeReadOnlyIsolationTests` 3개, `python3 -m unittest discover -s tests -p test_origin_isolation.py -k test_unverified_agent_snapshot_keeps_strict_shared_git_boundary` 1개가 모두 통과했다. 새 회귀 테스트는 `CodexLaunch.run`과 `_handoff_popen`을 실제로 실행하고 외부 CLI·kernel 및 process 생성만 unit test double로 제어한다. 전체 verify·Review는 Manager가 수행하며 Worker는 commit·push·PR 생성·전체 verify를 실행하지 않았다.
