---
id: G-044
title: Durable Git Checkpoint Reconstruction and Publication Resume
sources:
  - docs/requirements/[R-019] Automatic Archive Eligibility Recovery Before Blocking.md
depends_on:
  - G-043
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

- orchestration의 저장 phase를 Git/GitHub observation과 직접 비교하는 checkpoint assessor를 만들고, 가능한 다음 단계·무효화할 산출물·필요한 명령 또는 ambiguity reason을 typed result로 반환한다.
- ExecPlan row에 checkpoint generation, 관측 base/local/remote/PR OID, 적용 phase와 pending operation identity를 additive하게 저장한다. 각 외부 명령 전 의도와 expected state를 기록하고 명령 뒤 실제 상태 확인으로 완료한다.
- Git command wrapper가 operation 종류와 결과를 G-042 reason code로 변환하되 출력 문자열만으로 소유권/안전 위반을 추론하지 않게 한다. transient 결과는 재관측 loop로, 확정된 safety 결과는 final block gate로 보낸다.
- G-014의 remote publication 기록과 G-026의 PR lookup을 공통 resume service에 연결해 push/PR create 전후 어느 지점에서도 remote branch와 기존 PR을 먼저 확인하도록 한다.
- G-043 eligibility snapshot 및 G-037 stage revision token을 checkpoint 결과와 결합해 base/definition 보정 뒤 archive·verify·Review를 필요한 가장 이른 안전 단계부터 다시 수행한다.

## Validation

- Worker는 checkpoint assessor, Git operation retry와 publication resume의 직접 관련 test class 또는 새 전용 test module만 targeted unit test로 실행한다.
- `active_sha`, `validated_base_sha`와 phase가 각각 stale인 owned worktree를 actual HEAD/base ancestry로 보정하고 필요한 verify·Review만 다시 실행하는지 검증한다.
- rebase 시작·완료, fetch, phase 저장과 stage/continue 경계의 프로세스 중단을 재현해 다음 실행이 실제 state를 읽고 중복 명령 없이 재개하는지 검증한다.
- Git lock, timeout과 일시적인 fetch/status/worktree recovery 실패가 bounded retry 뒤 pause되며 상태·session·diff를 보존하고 BLOCKED 알림을 만들지 않는지 검증한다.
- push 전·후, remote 확인 전·후와 PR create 응답 전·후 중단에서 중복 push, force push와 중복 PR이 없는지 검증한다.
- continuous base/remote 경쟁에서 stale checkpoint를 적용하지 않고 다음 실행으로 미루며, 안정된 뒤 같은 recovery identity로 완료하는지 검증한다.
- 외부 remote head 변경, 닫힌 PR, ownership 불명과 둘 이상의 가능한 phase는 원격을 덮어쓰지 않고 정확한 최종 사용자 조치로 전환되는지 검증한다.
- dirty·ahead·diverged 기본 checkout에서도 모든 성공·pause·block 경로가 source checkout을 보존하고 G-014/G-026 lease 규칙을 유지하는지 검증한다.

## Progress

- [ ] Git/GitHub checkpoint snapshot·assessor 및 durable generation 구현
- [ ] phase/SHA 보정과 stage 산출물 무효화·재개 연결
- [ ] transient Git 재관측 및 다음 실행 pause 정책 구현
- [ ] remote push·PR create 경계의 멱등 resume service 통합
- [ ] marker 불일치, 명령 실패, 중단, 경쟁과 ambiguity 테스트 추가

## Decision Log

- SQLite marker를 무조건 Git에 맞추지 않고, G-031/G-034가 확정한 workspace ownership과 expected remote OID가 있는 경우에만 actual state를 복구 원본으로 사용한다.
- G-043 뒤에 실행해 archive eligibility와 catalog가 최신 snapshot을 사용하는 상태에서 일반 phase checkpoint를 보정한다. 보정으로 base가 달라지면 G-043/G-037 gate를 다시 거친다.
- 기존 publication interruption 복구는 유지하되 전체 phase·SHA 재구성 경계에 편입한다. push 권한이나 PR 생성 조건을 넓히는 계획이 아니다.
