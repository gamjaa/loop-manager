---
id: G-044
title: Durable Git Checkpoint Reconstruction and Publication Resume
sources:
  - docs/requirements/[R-019] Automatic Archive Eligibility Recovery Before Blocking.md
depends_on:
  - G-043
  - G-049
complexity: high
risk: critical
priority: critical
---

## Purpose

저장된 phase, `active_sha`, `validated_base_sha`와 publication marker가 실제 소유 branch·worktree·원격 branch·PR 상태보다 뒤처진 경우 신뢰 가능한 Git/GitHub 원본에서 안전한 다음 단계를 하나로 재구성한다. Git 명령의 일시 실패와 push·PR 생성 경계 중단은 상태를 보존해 자동 재개하고, 상충하는 해석이나 외부 변경을 덮어써야 하는 경우에만 최종 사용자 조치로 전환한다.

## Acceptance Criteria

- 실행 재개 전에 plan definition revision, base OID, local branch/HEAD, Git operation, `active_sha`, `validated_base_sha`, verify·Review revision, publication remote OID와 PR state/head를 하나의 구조화된 checkpoint snapshot으로 관측한다.
- branch, worktree와 workspace ownership이 현재 ExecPlan에 속하고 실제 HEAD/base ancestry가 안전한 다음 단계를 하나로 결정하면 저장된 phase와 SHA marker를 보정한다. marker 차이 자체를 즉시 BLOCKED로 만들지 않는다.
- 실제 rebase가 진행 중이면 보존된 충돌 correction 단계로, rebase가 안전하게 완료됐지만 저장 전 중단됐다면 최신 HEAD/base를 기록하고 필요한 전체 verify 단계로, validated base가 stale이면 rebase 또는 verify 단계로 결정적으로 복귀한다.
- `rebase --autostash`가 종료 코드 0과 완료 메시지를 반환해도 stash 재적용으로 unmerged index가 남을 수 있다. rebase 명령 직후와 실행 재개 시 index stage 1·2·3, Git operation marker, 소유 branch/HEAD와 target base를 관측하고, unmerged 경로가 있으면 rebase 완료·`active_sha` 갱신·일반 Worker 시작·verify·게시를 성공 처리하지 않는다. rebase metadata가 이미 사라진 경우도 보존된 rebase intent와 실제 index로 autostash 재적용 충돌을 식별한다.
- autostash 재적용 충돌의 복구 snapshot은 원래 구현 변경과 staged 상태, 충돌 stage blob OID, rebase 전후 HEAD, target base 및 확인 가능한 stash object identity를 보존한다. stash identity나 worktree 소유권을 확정할 수 없으면 임의로 stash를 다시 적용하거나 버리지 않고 안전한 재관측 또는 최종 사용자 조치로 보낸다.
- checkpoint를 보정한 뒤 archive 후보, 전체 verify와 Review가 현재 base/definition revision에 결합되지 않았으면 해당 결과만 무효화하고 다시 수행한다. 이미 증명된 worktree ownership, session, guidance 승인과 구현 변경은 유지한다.
- fetch, Git status/operation 조회, owned worktree 복구, stage, rebase continue와 원격/PR 조회의 timeout, Git lock 또는 일시 오류는 G-042의 `TRANSIENT_RETRY`로 처리한다. 현재 실행의 bounded retry 뒤에도 실패하면 상태를 보존한 pause로 종료하고 다음 실행에서 다시 관측한다.
- Git lock 파일을 제거하거나 reset, clean, stash, 강제 checkout으로 상태를 맞추지 않는다. 실패한 명령의 부작용을 추정하지 않고 실제 Git 상태를 다시 읽어 성공 여부와 다음 명령을 결정한다.
- local HEAD가 저장·관측한 소유 원격 branch OID와 같으면 push가 완료된 것으로 복구하고 중복 push하지 않는다. rebase로 local commit이 바뀐 PR 없는 동일 소유 branch만 기존 명시적 `--force-with-lease` 계약을 사용할 수 있다.
- push 뒤 PR 생성 전 중단되면 동일 head branch의 Open, Closed 또는 Merged PR과 head OID를 먼저 조회한다. 일치하는 기존 PR은 연결·복구하고 없을 때만 현재 검증 head로 PR 생성을 시도해 중복 PR을 만들지 않는다.
- PR 생성 응답 뒤 SQLite 저장 전 중단, 원격 fetch 뒤 checkpoint 저장 전 중단과 phase transaction 뒤 외부 명령 전 중단을 각각 실제 원격/로컬 상태로 멱등하게 복구한다.
- checkpoint 관측 중 base, remote head, PR 또는 SQLite row version이 바뀌면 낡은 보정 결과를 적용하지 않고 새 snapshot에서 다시 시작한다. 한 실행에서 안정 snapshot을 얻지 못하면 BLOCKED 알림 없는 pause로 종료한다.
- local/remote head가 둘 이상의 상충하는 phase를 허용하거나 관측하지 않은 remote commit을 덮어써야 하거나 branch/worktree 소유권을 증명할 수 없으면 자동 보정하지 않고 G-042의 최종 structured cause로 차단한다.
- external remote OID 불일치, 닫힌 PR과 Agent 범위 위반은 기존 사용자 조치 대상으로 유지한다. 안전한 owned state의 marker 차이와 단일 Git 명령 실패를 같은 범주로 분류하지 않는다.
- 재구성·명령 재시도·중단 복구 동안 기본 source checkout의 파일, index, HEAD와 local base ref를 변경하지 않는다.
- 기존 G-014 publication recovery와 G-026 owned PR conflict recovery의 push 권한을 넓히지 않으며, 모든 Manager Git 쓰기는 정확한 owned branch와 expected OID에 결합한다.

## Implementation

- 새 `execution_checkpoint.py`에 Git/GitHub observation snapshot과 가능한 next phase·artifact invalidation·pending operation 또는 ambiguity를 산출하는 pure assessor를 정의하고, 새 `execution_checkpoint_state.py` repository가 generation, observed OID, intent와 completion을 저장한다.
- `execution_runtime.py`에 preparation용 `ExecutionRecoveryPort`를 추가하고 `ExecutionPhaseCoordinator.prepare`가 phase dispatch 전에 assessor result를 적용하게 한다. `runtime_state.py`는 expected execution snapshot에 결합한 phase/SHA correction만 원자적으로 수행하고 Git 상태를 추론하지 않는다.
- Git status/operation, worktree recovery, stage/rebase와 remote/PR observation은 granular effect port로 분리한다. command 전 durable intent와 expected state, command 뒤 재관측 결과를 repository CAS로 연결하고 transient failure는 G-042 decision으로 반환한다.
- rebase effect의 성공 판정은 subprocess 종료 코드에 더해 unmerged index 부재와 기대 branch/base 상태를 요구한다. `ls-files -u -z`로 stage가 남은 경로를 확인하고, autostash 충돌은 rebase 디렉터리가 없어도 G-045 correction에 전달하며 `begin_agent_scope`의 baseline `write-tree` 이전에도 같은 preflight를 적용한다.
- G-014 remote publication record와 G-026 PR lookup을 새 `publication_resume.py` service가 공유하게 하고, G-049 PublicationEffects는 push/PR 전 이 service를 호출한다. 기존 PR 발견·push 완료·PR create interruption을 handler별 중복 분기로 유지하지 않는다.
- G-043 archive token과 G-037 stage token을 checkpoint snapshot에 포함해 correction 뒤 필요한 가장 이른 phase만 다시 수행한다. `loop_manager.py`에는 Git/GitHub concrete adapter와 compatibility helper wiring만 남긴다.

## Validation

- Worker는 새 `tests/test_execution_checkpoint.py`, `tests/test_execution_checkpoint_state.py`, `tests/test_publication_resume.py`와 coordinator adapter의 직접 관련 targeted case만 실행한다.
- stale `active_sha`, `validated_base_sha`와 phase 조합을 pure assessor table로 검증하고 owned state에서 필요한 artifact/phase만 보정하는지 runtime repository test로 확인한다.
- rebase/fetch/phase 저장과 stage/continue 전후 중단에서 durable intent와 actual observation이 중복 command 없이 완료·재개되는지 fake Git port로 검증한다.
- 실제 임시 Git 저장소에서 dirty 변경을 보유한 branch를 `rebase --autostash`로 최신 base에 반영해 종료 코드 0, unmerged index, rebase metadata 부재가 동시에 발생하는 경우를 재현한다. 이 상태와 프로세스 재시작에서 `active_sha` 성공 처리, 일반 Worker/baseline `write-tree`, verify 및 게시가 실행되지 않고 보존된 충돌 snapshot으로 이어지는지 검증한다.
- Git lock, timeout과 transient status/worktree failure가 bounded retry 뒤 pause되고 execution context·session·diff를 보존하며 final block/outbox를 만들지 않는지 검증한다.
- push·remote observation·PR create 전후 중단과 기존 Open/Closed/Merged PR에서 publication resume service가 중복 push·force push·PR을 만들지 않는지 검증한다.
- continuous base/remote/row-version race는 stale correction을 거부하고 안정 snapshot 뒤 같은 identity로 완료하는지 CAS test로 검증한다.
- external remote mutation, closed PR, ownership ambiguity와 여러 가능한 phase만 final structured cause로 전환하고 source checkout 불변 및 기존 lease를 유지하는지 검증한다.
- 전체 verify에서 latest-base archive/revision gate, PR conflict recovery와 publication ordering 회귀가 없어야 한다.

## Progress

- [ ] execution checkpoint snapshot·pure assessor 구현
- [ ] durable operation intent/generation repository와 runtime correction CAS 구현
- [ ] coordinator preparation recovery port 및 transient 재관측 통합
- [ ] shared publication resume service와 Git/GitHub adapter 이관
- [ ] marker·중단·race·ambiguity targeted 회귀 테스트 추가

## Decision Log

- SQLite marker를 무조건 Git에 맞추지 않고, G-031/G-034가 확정한 workspace ownership과 expected remote OID가 있는 경우에만 actual state를 복구 원본으로 사용한다.
- G-043 뒤에 실행해 archive eligibility와 catalog가 최신 snapshot을 사용하는 상태에서 일반 phase checkpoint를 보정한다. 보정으로 base가 달라지면 G-043/G-037 gate를 다시 거친다.
- 기존 publication interruption 복구는 유지하되 전체 phase·SHA 재구성 경계에 편입한다. push 권한이나 PR 생성 조건을 넓히는 계획이 아니다.
- Post-refactor revalidation: checkpoint reconstruction은 G-049 `prepare` 앞의 recovery port와 shared publication resume service로 구현한다. phase handlers가 저장 marker, Git command와 PR 조회를 각각 재구현하지 않게 한다.
