---
id: G-089
title: Task-Relevant Agent Git Scope
sources:
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
depends_on:
  - G-088
  - G-097
complexity: high
risk: critical
priority: critical
---

## Purpose

현재 `validate_agent_scope`와 저장된 Agent snapshot 판정은 사용하지 않는 branch·tag·ref와 다른 worktree의 독립 변화도 repository 전체 변경으로 비교한다. 격리가 증명된 실행에서는 현재 작업의 안전에 필요한 자원만 감시하고, 자원을 실제 사용하기 직전에 다시 검증한다.

## Acceptance Criteria

- G-088의 유효한 실행별 격리 증거가 있을 때 선택 관리 worktree의 identity·등록·branch·HEAD·index·허용 파일, 현재 단계가 쓰는 소유 branch·원격 base ref·게시 ref·PR head, 영향을 주는 Git config와 operation만 Agent 범위에 포함한다. 기본 source checkout의 local HEAD·index·dirty와 무관한 다른 branch·tag·ref·worktree의 독립 변화만으로 `PLAN_BLOCKED`·`PROJECT_BLOCKED`를 만들지 않는다.
- 범위 밖 파일·index·HEAD, 소유 branch·게시 ref·PR head, 사용 중인 worktree 등록·config·operation의 예상 밖 변화와 영향 여부가 불확실한 변화는 계속 차단한다. 원격 base의 검증된 전진은 기존 fetch·rebase·재검증 절차를 따른다.
- 나중에 사용하게 되는 ref·worktree와 Git 작업 대상은 직전에 실제 값·소유권·예상 OID를 확인한다. snapshot에서 제외됐다는 이유로 오래된 값에 쓰거나 게시하지 않는다.
- 격리 증거가 없거나 불완전한 과거 snapshot은 기존 엄격 비교를 유지한다. Agent의 허용 파일, branch·worktree·push·PR 소유권과 Reviewer의 읽기 전용 계약은 그대로다.

## Implementation

- `agent_scope_decision.py`에 역할·단계별 사용 자원 선택을 명시하고 `loop_manager.py`의 현재/저장 snapshot 검증과 Agent 반환 후 범위 판정에 같은 선택 기준을 적용한다. 증거가 없는 경로는 기존 검사를 사용한다.
- `execution_handlers.py`, `publication_resume.py`와 관리 Git 명령 직전 경계에서 실제로 사용할 ref, PR head 및 worktree를 재조회·검증한다. 선택된 작업과 무관한 변화는 진단에 남기되 Agent 변경으로 귀속하지 않는다.
- G-088의 격리 증거를 검증한 경우에만 범위를 좁히며, 관측 OID나 시간적 동시성만으로 변경 주체를 추정하지 않는다.

## Validation

- Worker는 `tests/test_agent_scope_decision.py`, `tests/test_origin_isolation.py`, `tests/test_execution_handlers.py`에서 직접 변경한 범위·게시 전 확인 targeted unit test만 실행한다.
- 격리된 실행 중 무관한 local branch·tag·ref·다른 worktree와 기본 checkout 변화는 비차단, 사용 중인 자원 및 범위 밖 파일·index 변화는 차단, 격리 증거 부재·위조·세션 불일치 시 엄격 fallback을 검증한다.
- 사용 직전 원격 base·소유 branch·게시 ref·PR head 변경과 원격 경쟁은 최신 값으로 재검증하거나 안전 차단하고, 전체 verify에서 기존 workspace 및 PR 소유권 회귀가 없어야 한다.
- Worker targeted 결과: `TaskRelevantScopeTests`·`TaskRelevantInvocationScopeTests` 10개, `TaskRelevantPublicationBoundaryTests`·기존 PR 복구 2개·`PublicationResumeTests` 18개, immutable push lease 회귀 2개·기존 invocation profile 회귀 1개 모두 통과했다. 전체 suite·verify·Reviewer는 실행하지 않았다.
- 전체 verify 피드백의 stale-cache 복구 실패 2개를 개별 case로 재현했다. 복구 fixture의 live 원격 조회·소유 branch·PR 조회와 immutable OID·빈 lease 기대값을 실제 게시 경계에 맞춘 뒤 동일 명령으로 2개 모두 통과했다. 공용 fixture를 사용하는 원격 생성 경쟁 차단·기존 PR lease·중복 push 방지·기게시 legacy stale-cache 복구 4개도 개별 case로 통과했다. Worker는 전체 verify를 실행하지 않았다.
- Worker 결과 계약 재검증에서 두 피드백 case를 같은 명령으로 다시 실행해 모두 통과했다. 이전 보고는 실패 명령·재실행 명령 사이에 실패 결과 설명이 없어 거부됐다. 실패 결과와 최종 통과 결과를 순서대로 기록한 보고문을 기존 `parse_worker_result`로 확인했다.

## Progress

- [x] 역할·단계별 사용 Git 자원 판정
- [x] 격리 증거에 따른 엄격/최소 범위 분기
- [x] Git 사용 직전 소유권 재검증과 targeted 회귀 사례

## Decision Log

- G-088은 비차단에 필요한 쓰기 격리 증거를 제공한다. 이 계획은 Manager 소유 변경의 기준선 갱신을 추정으로 허용하지 않으며 해당 증명은 G-090/G-091이 담당한다.
- 기존 invocation ledger에 nullable `scope_id`를 추가하고 snapshot에 실행별 ID·시작 상태 지문을 보존한다. 동일 scope의 모든 호출이 성공·verified이고 시작 지문, 역할, provider, session과 repository·worktree identity가 일치할 때만 비교 자원을 좁힌다. 과거 snapshot과 누락·중단·실패·불일치 증거는 엄격 비교로 fallback한다. 유지보수 호출은 기존 provider의 Planner 정책과 해당 scope의 개별 session에 결합한다.
- 선택 worktree의 파일·index·HEAD·operation과 불확실한 config는 그대로 보호한다. config 제외는 미사용 branch의 알려진 설정만 허용하고 원본 snapshot 및 제외된 변화의 진단을 보존한다.
- 게시 경계는 실제 원격 ref·PR과 선택 worktree를 다시 조회한다. push는 확인한 immutable commit OID와 명시적 lease를 사용하며 최초 생성도 빈 lease로 원격 경쟁을 거부한다. PR 생성 전에 base 전진을 확인하면 기존 rebase·verify·Review 경로로 돌아간다.
- 기존 verify 스크립트가 변경 모듈과 unittest discovery를 이미 포함하므로 스크립트 변경은 필요하지 않다. Worker는 targeted unit test만 실행하며 전체 verify·Reviewer·Merge 완료 판정은 Loop Manager에 남긴다.
- 검증 피드백은 복구 테스트의 공용 workspace가 live 조회를 tracking cache mock으로 대체한 데서 발생했다. fixture에서 기존 live 조회 helper를 재사용하고 새 소유 branch·PR 재조회 및 push 인자를 검증한다. 제품 코드의 게시 전 검증과 lease 보호는 유지한다.
