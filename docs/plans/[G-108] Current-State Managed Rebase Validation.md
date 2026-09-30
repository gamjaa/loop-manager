---
id: G-108
title: Current-State Managed Rebase Validation
sources:
  - docs/requirements/[R-048] Evidence-Free Managed Rebase Recovery.md
depends_on: []
complexity: high
risk: critical
priority: critical
---

## Purpose

현재 `manager_git_journal.verified_rebase_result`는 현재 Git 소유권 검사와 과거 reflog 표식·Worker 교정·commit 재생 증거를 함께 완료 조건으로 사용한다. R-048이 승인한 현재 상태 판정을 독립적인 경계로 분리해 자동 해결과 사용자 수동 해결이 같은 기준을 사용할 수 있게 한다. 실행 경로 연결은 G-109, 저장된 차단의 재개는 G-110이 담당한다.

## Acceptance Criteria

- 판정 입력은 현재 계획·repository·canonical 관리 worktree·reservation·소유 branch, 기대 target base·원격 branch OID·PR identity/head와 현재 Git 관측이다. 필요한 identity나 기대값을 확정할 수 없으면 거부한다. Worker session/turn, 승인된 stage tree, 명령 receipt, reflog 실행 표식, 원본 commit 목록·순서·재생 대응 또는 journal의 완전성은 필수 입력이 아니다.
- 진행 중인 rebase는 동일 repository·plan·등록 worktree·소유 branch와 `head-name`·`orig-head`·`onto`, 기대 원격 OID의 관계를 검사한다. 관계가 일치하는 detached HEAD와 미해결 index는 해결 대기 결과로 반환하고 보존한다. 다른 Git operation이 섞이거나 대상·등록·ref·작업 범위가 어긋나면 거부한다. 진행 중인 상태를 완료 또는 게시 가능 상태로 반환하지 않는다.
- 완료 판정은 실제 HEAD·branch ref·worktree 등록과 대상 base ancestry, clean index·미해결 operation 부재, 허용된 파일·index 범위 및 현재 원격 branch/PR head의 기대 OID를 모두 검사한다. clean 상태나 ancestry 하나만으로 소유권을 인정하지 않는다. commit 내용·개수·순서의 변경과 과거 작업 기록의 누락 자체는 거부 이유가 아니다.
- 허용 변경 검사는 대상 base에 대한 최종 commit 변경 경로·mode와 남은 tracked·untracked·ignored 작업 파일, index stage·실제 보안 flags를 포함한다. 현재 역할·선택 ExecPlan·승인 guidance 및 archive 계약으로 설명할 수 없는 변경은 거부한다. 이 판정은 Agent의 실제 범위 위반을 면제하거나 다른 worktree·shared ref의 독립 변화를 허용하는 근거가 아니다.
- 수용 결과는 검사 정책 버전, 계획·repository·worktree·branch, target base, 관측 원격·PR 기대값, 최종 HEAD와 현재 상태·입력 지문에 결합된다. SQLite generation과 실행 소유권을 다시 대조해 같은 결과를 한 번만 반영하고, stale generation 또는 검사 뒤 상태 변화는 적용하지 않는다. 이는 현재 결과의 수용 기록이며 과거 Manager 명령이나 Worker 작업을 수행했다는 증거로 표시하지 않는다.
- 기존 journal·checkpoint·교정 기록과 실패 감사는 삭제·재작성하지 않는다. 저장 확장은 호환 가능한 필드·기록만 사용하고, 과거 작업 기록이 부족한 경우에도 현재 identity·기대값을 확정할 수 있으면 같은 판정을 수행한다. 이 계획은 기존 운영 경로의 완료 정책을 아직 교체하지 않는다.

## Implementation

- Git 읽기와 부작용 없는 판정을 분리한 작은 공통 경계를 추가한다. `execution_checkpoint.py`의 controlling operation·detached rebase 검사, 기존 workspace identity 및 파일 범위 검사를 재사용한다. 전용 모듈이 필요하면 내부 책임의 분리를 근거로 두고 G-060의 루트 inventory에 포함할 수 있게 한다. `execution_handlers.py`와 `loop_manager.py`에 별도 판정 규칙을 각각 추가하지 않는다.
- 관측 adapter가 Git/PR 조회 결과와 범위 검사 결과를 명시적으로 전달하게 한다. 누락된 관측·조회 실패·원격 경쟁·scope 불확실성을 정상 결과와 구분한다. 현재 파일 범위 확인은 commit별 replay나 과거 승인 tree 복원을 사용하지 않는다.
- `execution_checkpoint_state.py`와 `runtime_state.py::complete_rebase`에 현재 상태 수용 결과의 저장·CAS 계약을 추가한다. 기존 완료 기록을 계속 읽고 보존하면서 새 결과를 구분한다. 기존 journal 실행 증거를 인위적으로 보충하거나 Worker turn을 사후 생성하지 않는다.

## Validation

- Worker는 새 판정 경계의 직접 관련 unit case와 `tests/test_execution_checkpoint.py`, `tests/test_execution_checkpoint_state.py`, `tests/test_runtime_state.py`에서 변경한 저장·CAS case만 실행한다. 실제 Git lifecycle 또는 전체 verify는 직접 실행하지 않는다.
- 같은 현재 상태에서 과거 turn·stage 승인·receipt·replay·journal을 각각 제거해도 판정이 같음을 확인한다. 자동·수동 해결, commit 수·순서 변경, 기대값 없는 legacy 입력, detached 진행 상태와 완료 상태를 구분한다.
- repository·plan·worktree·branch·target·PR/head·원격 lease 불일치, 범위 밖 committed/dirty/staged 파일·mode·flags, 미해결 operation, 관측 누락과 stale generation은 거부한다. 원자 저장 실패는 checkpoint·실행 row·project 투영을 함께 롤백하고 재시작 뒤 같은 결과를 한 번만 적용한다.
- Manager 전체 verify는 기존 실행 경로와 다른 Manager Git 작업의 엄격한 증거 검사가 이 기반 변경으로 완화되지 않는지 확인한다.

## Progress

- [ ] 현재 Git 관측·범위 검사와 공통 판정 경계
- [ ] 호환 가능한 수용 결과 저장·generation 검사
- [ ] 정상 수동 결과와 실제 상태 불일치의 targeted 회귀 사례

## Decision Log

- R-048과 ARCHITECTURE의 R-048 절은 기존 R-045의 작업 수행 증거 정책을 명시적으로 대체한다. 추가 사용자 결정을 요구하거나 구 정책을 새 완료 조건으로 재도입하지 않는다.
- 실행 연결 없이 검증 가능한 기반을 먼저 Merge한다. G-109가 모든 기존 rebase 호출 경로를 연결하며, G-104·G-106·G-107의 게시·원격 복구 기능을 이 계획에서 중복 구현하지 않는다.
