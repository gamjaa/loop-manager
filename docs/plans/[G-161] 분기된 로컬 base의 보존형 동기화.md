---
id: G-161
title: 분기된 로컬 base의 보존형 동기화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-010] Origin-Isolated Local Worktree Operation.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-056] Non-Blocking Local Base Synchronization.md
depends_on:
  - G-012
complexity: high
risk: critical
priority: high
---

## Purpose

clean diverged base의 확인된 로컬 전용 commit을 원격 base 위로 안전하게 재적용하고, 로컬 동기화와 검증한 원격 bootstrap·runtime 실행을 분리한다. source checkout을 갱신할 수 없어도 최신 원격 실행을 시작·재개할 수 있게 한다.

## Acceptance Criteria

- equal은 무변경, ahead는 로컬 작업 보존, behind는 기존 disjoint tracked·staged·ignored 파일을 보존하는 안전한 fast-forward, clean diverged는 확인된 로컬 commit 보존 rebase로 처리한다. 겹치는 dirty 변경·사용자 Git operation·소유권 불확실에서는 로컬 동기화만 보류하고 원격 입력의 유효한 자동 작업은 계속한다.
- bootstrap은 process lock 아래 등록된 self base branch의 최신 원격 SHA를 확정하고 그 revision의 bootstrap·runtime을 실행한다. source HEAD·branch의 일치, 로컬 동기화 성공이나 갱신 후 사용자 재실행을 실행 조건으로 요구하지 않는다. ahead·diverged·dirty·다른 checkout branch·동기화 skip 및 실패에서도 안전한 원격 실행을 계속하며 로컬 전용 코드·문서는 실행 입력에 포함하지 않는다.
- 원격 실행 전이와 자체 handoff에서 interpreter·인자·source 기준 절대 config 경로 및 process lock 소유권을 보존한다. 실행 revision·repository·runtime 소유권을 증명할 수 없으면 작업물과 재개 근거를 보존해 진단·재시도하며 로컬 HEAD나 오래된 코드로 대체 실행하지 않는다.
- R-010의 원격 격리를 유지한다. launcher는 현재 checkout의 Python bootstrap에 인자만 전달하며 실제 process lock 획득 뒤 fetch·runtime 준비를 수행한다. 자식 종료까지 부모 lock을 유지하고 자식은 중복 획득하지 않는다. runtime은 확정 SHA의 state-dir 내 clean detached worktree이며 clone·ignored 파일 복제·source 임시 updater를 추가하지 않는다. 로컬 계획·구현 입력과 runtime 기준 config 재계산은 허용하지 않는다.
- 충돌·일시 실패·중단에서 시작 전 ref·index·파일과 Manager 소유 intent를 보존한다. 소유권이 증명된 자동 작업만 abort·재개하며 사용자 operation을 해제하거나 dirty 작업을 임의 autostash하지 않는다.
- 동일 입력의 진전 없는 반복 Git 쓰기는 없고 새 관련 원격·로컬 입력에서 재검사한다. bootstrap·handoff 실패와 프로젝트 안전 차단을 혼동하지 않는다.

## Implementation

- git/source_sync.py와 runtime/bootstrap.py의 기존 동기화·journal·handoff 경계를 재사용한다. 로컬 분기 재적용의 준비·적용·확정·복구를 명시적 상태로 결합한다.
- 원격 실행 revision 선택·검증과 부수적인 source 동기화의 책임을 분리한다. 기존 runtime identity·process lock·원격 fetch·handoff helper를 재사용해 최신 원격 bootstrap까지 실행을 넘기고, source HEAD·branch 일치와 동기화 결과를 원격 실행 gate에서 제거한다.
- 로컬 base 상태를 원격 계획 입력의 gate로 다시 도입하지 않는다. 사용자가 만든 다른 branch·기본 checkout의 작업은 이 동기화 대상 밖에 둔다. source 보존 때문에 건너뛴 처리와 원격 실행 자체의 안전 오류를 구분해 복구한다.
- 문서·권한·게시와 검증의 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md, docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_source_sync.py와 tests/test_bootstrap_foundations.py의 fixture를 우선 재사용해 equal/ahead/behind/diverged·dirty·충돌·중단·소유 intent 판정 및 source HEAD 불일치·동기화 실패에도 원격 bootstrap·runtime을 선택하는 targeted unit case를 작성·실행한다. bootstrap entry·runtime 및 self handoff의 관련 기존 테스트 중 변경한 경계를 검증하는 최소 범위만 실행한다.
- 기존 bootstrap entry의 source 갱신 후 자식 미시작·재실행 요구와 skip 시 원격 실행 거부 기대값은 R-023·R-056의 최신 원격 실행 기준에 맞춰 갱신한다. source 보존과 실제 runtime SHA·lock·config 검사는 유지한다.
- Manager 전체 verify는 source의 commit·index·tracked·untracked·ignored 파일 보존, 갱신·skip·실패 뒤 사용자 재실행 없이 정확한 원격 코드 실행, config·인자·interpreter·lock 전이, 중단 후 동일 소유 작업 재개와 원격 revision·소유권 불확실 시 보존을 검증한다. 동기화 실패가 다른 유효한 프로젝트 작업의 차단으로 확대되지 않는지 확인한다.
- R-010의 기존 tests/test_origin_isolation.py·test_origin_registration.py·test_source_sync.py 회귀를 유지한다. 변경한 bootstrap unit 경계에는 clean behind 갱신 뒤 같은 실행의 runtime 시작과 ahead·diverged·dirty·다른 branch·동기화 오류에서 원격 SHA 선택, 부모 lock·자식 중복 획득 금지·source config 절대 경로를 확인하는 case를 포함한다. G-012 선행 Merge gate와 ignored 충돌 파일 보존·Git lock 비우회는 Manager 전체 verify에서도 유지한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-05 fef5741의 mutable READY snapshot과 원본 hash를 대조하고 R-023·R-056의 원격 bootstrap 실행 조건을 같은 계획에 명시함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-05 최신 planning 기준 32e496c와 제공된 mutable READY snapshot에서 재평가함. G-114의 probe 실패·파일 보존 회귀 보강은 bootstrap gate·diverged 동기화의 미충족 범위를 바꾸지 않으며 기존 개정 목표를 유지함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-06 a1db7e8의 최신 원격 코드·테스트로 R-010 전체 기준을 재평가함. source HEAD·branch 및 갱신 성공·재실행 gate의 미충족 목표를 기존 G-161에 연결함. 구현·테스트 실행은 하지 않음.
- [x] 구현 및 관련 targeted unit 검증: source sync·bootstrap foundation/entry·journal의 45개 unit case와 self handoff의 1개 case 통과. source journal 손상의 프로젝트 간 비차단 보강 후 journal class 3개 case 추가 재검증 통과.
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-06: `git/source_rebase.py`에 source 전용 intent·invocation·result와 복구 경계를 분리하고 기존 journal 저장·reflog 표식·commit 보존 helper를 재사용했다. clean 선형 commit만 rebase하며 dirty·merge commit·중간 replay 경로의 ignored 충돌은 보존한다. 실패 receipt와 실제 파일·index·operation이 일치하는 소유 conflict만 abort하고, receipt 없는 중단·후속 사용자 수정은 보존한다. 완료·abort 뒤 journal 확정 중단은 Git 쓰기를 반복하지 않고 확정한다. 같은 실패 입력의 rebase·fast-forward는 반복 보류하며 새 관련 입력에서 재검사한다.
- 2026-10-06: bootstrap의 source HEAD·branch·operation 일치 및 갱신 후 외부 재실행 gate를 제거했다. lock 아래 확정한 원격 SHA의 detached runtime 자식이 그 revision의 `main` bootstrap에서 repository·runtime·부모 lock을 검증한 뒤 요청 명령을 실행한다. sync skip·실패·예외는 진단하고 원격 SHA·runtime 소유권 오류는 보존·중단한다. 기존 config·interpreter·인자 전달과 부모 lock 유지 helper를 그대로 사용하며 별도 clone·updater·ignored 복제를 추가하지 않았다. 선택된 source journal은 원격 planning release의 gate에서 제외하고 PRODUCT에 지속 규칙을 반영했다.
- 2026-10-06: Linux 전체 검증 경로는 기존 `scripts/verify.sh`와 전체 unit manifest를 실행하는 verification runner를 재사용한다. Worker는 전체 verify·suite·lint·build·type check를 실행하지 않았으며 Manager 검증·Review·사용자 Merge는 남겨둔다.

- 2026-10-06: R-010의 남은 원격 runtime 실행 목표는 기존 G-161과 같아 source와 G-012 선행 gate를 연결한다. pinned runtime 이전 임시 updater는 이미 제거됐다. 로컬 dirty 상태의 일괄 skip·diverged 금지는 최신 R-023·R-056의 disjoint fast-forward·보존형 rebase 정책으로 대체된 조건이므로 되살리지 않고 원격 격리·사용자 상태 보존을 검증한다. 기존 별도 관리 worktree·등록·Worker Git 쓰기 금지 구현은 중복 계획하지 않는다.

- 여러 Git 상태와 복구 경계가 연결되어 complexity high다. 사용자 commit 손실을 막아야 하므로 risk critical이며 로컬 동기화의 실패를 전체 작업 차단으로 확대하지 않는다.
- 현재 prepare_bootstrap_runtime은 source 동기화 updated 뒤 실행을 넘기지 않고 반환하며 skip은 오류로 끝낸다. validate_bootstrap_revision도 source HEAD·branch의 원격 일치를 요구한다. R-023이 명시적으로 R-056의 비차단 기준을 채택하므로 이 gate 제거와 최신 원격 bootstrap 전이를 기존 G-161에서 함께 검증하고 별도 중복 계획을 만들지 않는다.
