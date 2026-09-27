---
id: G-099
title: Phase-Bound Managed Rebase Evidence
sources:
  - docs/requirements/[R-045] Phase-Bound Managed Rebase Validation and Recovery.md
depends_on: []
complexity: high
risk: critical
priority: critical
---

## Purpose

현재 `execution_handlers.py`의 완료 검사는 승인된 충돌 해결 tree를 재생 commit과 비교하면서도, 당시 파일 signature를 후속 commit까지 재생한 최종 worktree에 다시 요구한다. 뒤의 정상 commit이 같은 파일을 변경하면 소유 rebase가 거부된다. 충돌 해결, 후속 재생과 최종 소유권을 각 시점의 증거로 검증한다.

## Acceptance Criteria

- rebase 시작 전에 repository·plan·branch·canonical worktree, 실행/checkpoint generation, 시작 HEAD, target base, 원격 branch·PR head 기대 OID와 재생할 원본 commit 순서를 고정한다. 충돌마다 원본 commit·위치·parent HEAD, Worker invocation/session/결과, 허용 경로·내용·mode와 stage 전후 index를 결합하고 정확한 staged tree를 Git 계속 호출 전에 durable하게 기록한다.
- 각 충돌 해결은 해당 원본 commit에 대응하는 실제 재생 commit의 tree·parent로 확인한다. 중간 commit 충돌, 한 `rebase --continue` 호출의 여러 후속 commit, 여러 차례 충돌을 구별하며 승인된 교정의 파일 signature를 최종 worktree에 다시 요구하지 않는다.
- 나머지 commit은 원본과 재생의 순서·parent·patch 보존 또는 각자의 승인된 해결 증거로 대조한다. 기존 정책이 허용한 무변경·skip은 원본 commit별 근거가 확인될 때만 인정한다. commit 개수, clean 상태, 전체 `range-diff` 또는 과거 reflog 문구만으로 완료를 승인하지 않는다.
- 범위 밖 stage, 승인 뒤 blob·mode·index flag 변경, 해결 commit tree 변조, 재생 commit의 누락·추가·재정렬·변조, 다른 교정 turn의 증거를 거부한다. 최종 HEAD·소유 branch ref·worktree 등록·target base 관계, 최신 원격 OID/PR head, index·작업 파일·Git operation 및 해당 호출의 최신 실행 표식을 별도로 검증한다.
- 충돌 없는 rebase, 변화 없는 소유 rebase, 중간·다중 충돌과 허용 skip에 동일한 단계 기준을 적용한다. PR 없는 checkpoint branch와 기존 PR 충돌 복구 모두 새 판정기를 사용한다. rebase하지 않은 일반 재개에는 rebase 증거를 요구하지 않고 Agent 격리·Git 쓰기 권한·게시 lease는 유지한다.

## Implementation

- 부작용 없는 단계별 재생 판정 경계를 `manager_git_journal.py`의 저장·Git 실행 책임과 분리한다. `checkpoint_ownership.py`의 전구간 patch 비교는 충돌 없는 재생에만 사용하고, 충돌 해결 commit과 나머지 원본의 대응은 별도 검증한다.
- `execution_handlers.py`의 Worker 교정·stage·`_begin_rebase_continuation`·`_finish_rebase_journal` 및 `loop_manager.py`의 직접 rebase 경계에서 같은 버전의 실행·원본 순서·stage·재생 증거를 수집한다. `rebase_correction_state.py`의 교정 turn identity와 `execution_checkpoint_state.py`의 generation을 연결한다. 기존 `<state-dir>` journal/SQLite에는 호환 필드만 추가한다.
- `manager_git_journal.verified_managed_transitions`도 저장된 결과를 조회할 때 확인된 단계 증거를 다시 대조한다. 실패 진단은 단계, intent/교정 identity, 원본·재생 commit과 기대·관측 지문을 구분하며 비밀값이나 파일 본문을 기록하지 않는다. R-045의 장기 규칙은 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 판정과 stage 수집의 `tests/test_managed_mutation_evidence.py`, `tests/test_manager_git_journal.py`, `tests/test_rebase_correction.py`, `tests/test_execution_handlers.py` 중 관련 targeted unit test만 실행한다.
- 승인 tree가 해결 commit에는 일치하고 후속 commit이 같은 파일 내용 또는 mode를 바꿔도 완료되는 경우, 중간·다중 충돌, 원본 순서·parent/patch 불일치, 변조된 stage·다른 turn의 증거, 원격 OID와 최종 소유권 불일치를 검증한다.
- 전체 verify는 실제 Git rebase의 무충돌·다중 commit·충돌·허용 skip과 PR/PR 없는 경로를 확인한다. 새 판정 모듈을 검증 대상에 포함하고 기존 범위를 약화하지 않는다.

## Progress

- [ ] 버전이 있는 실행·교정·stage 증거와 순수 단계 판정 경계
- [ ] 두 Manager rebase 경로에 동일한 재생·최종 소유권 검증 연결
- [ ] 정상 재생과 변조·범위 위반 회귀 사례

- 재개 Worker 보완: CONTINUE와 SKIP이 같은 stage 전후 index 판정 경계를 사용하도록 분리하고, SKIP의 승인 후 index flag·범위 밖 변경 및 불완전한 stage 증거를 호출 전과 journal 재검증에서 거부한다.
- targeted unit 검증: skip 호출 전 검사, continue 회귀 검사, stage 판정 2개, skip receipt 재검증 등 5개 통과.
- `tests/test_managed_rebase_lifecycle.py`에 실제 Git 다중 충돌·skip·후속 동일 파일 내용/mode 변경, PR/게시 checkpoint 분기, SQLite 재시작 전후 Worker·verify·Review 사례를 추가했다. 통합 테스트와 전체 verify는 Worker 실행 금지 범위이므로 실행하지 않았으며 Loop Manager 검증이 남아 있다.

- mode 검증 보완: 승인 파일 signature에 결합한 Git mode를 stage 전에 저장하고 CONTINUE·SKIP과 journal 재검증에서 index mode를 직접 비교한다. `core.fileMode=false`의 clean diff로 index 실행 비트 변경이 숨겨지는 회귀를 unit test와 실제 Git fixture에 추가했다. targeted unit test 8개가 통과했으며 실제 Git 사례는 Loop Manager 전체 verify 대상이다.

- SKIP 내용 검증 보완: index가 clean인 승인 경로도 호출 직전 직접 signature를 읽어 승인값과 비교하고, 동일 snapshot의 dirty 관측 및 안정성을 확인한다. 이 관측을 journal에 저장해 재검증에서도 누락·내용 불일치를 거부한다. 호출 전 변경·삭제·관측 중 변경과 저장 receipt 변조를 확인하는 targeted unit test 2개가 통과했다. 전체 verify는 Loop Manager 검증 대상이다.

## Decision Log

- G-091의 소유 Git journal을 확장한다. R-023의 Agent 기준선 조정과 범용 차단 해제는 G-092에 남기며, 이 계획은 현재 rebase 실행의 증거와 완료 판정만 다룬다.
