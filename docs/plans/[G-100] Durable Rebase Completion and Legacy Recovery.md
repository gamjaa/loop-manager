---
id: G-100
title: Durable Rebase Completion and Legacy Recovery
sources:
  - docs/requirements/[R-045] Phase-Bound Managed Rebase Validation and Recovery.md
depends_on:
  - G-099
complexity: high
risk: critical
priority: critical
---

## Purpose

Git rebase가 이미 끝난 뒤 journal이나 SQLite 완료 반영 전에 중단되면 같은 Git 작업을 다시 실행할 수 없다. G-085처럼 구 형식 intent에 승인 tree와 실행 표식이 남은 경우도 완료 근거를 재구성하지 못하면 작업물이 계속 차단된다. G-099의 단계별 판정으로 동일 작업의 완료를 복구하고 확정한다.

## Acceptance Criteria

- Worker 결과 저장·검증·stage 전후, 해결 commit 생성 뒤 후속 재생 전후, Git 완료 뒤 journal 확정 전, journal 확정 뒤 SQLite 반영 전의 중단에서 저장 증거와 현재 Git 상태를 다시 대조한다. 확인된 stage·`--continue`·Worker 교정은 중복 수행하지 않고 다음 미완료 단계만 진행한다.
- journal 완료 결과는 해당 intent·교정·실행 표식 및 현재 소유 Git 상태가 일치할 때만 확정한다. 저장 결과 재조회도 G-099의 동일 판정 기준을 적용한다. 불완전한 intent, 다른 session/실행, 변조된 재생 또는 예상 밖 파일·index 변화는 작업물과 intent를 보존하고 안전 차단한다. target/원격 OID/PR head 변화는 이전 완료의 소유권과 새 기준의 적용 필요를 분리해 기존 경쟁·lease 규칙으로 재관측하거나 차단한다.
- 구 형식 intent는 승인된 staged tree를 실제 해결 commit에 유일하게 대응시키고 나머지 원본 재생과 현재 소유권까지 증명할 수 있을 때만 호환 완료로 확정한다. G-085의 동일 파일 후속 변경을 복구하되 clean HEAD·base ancestry·commit 수만으로 누락 증거를 추정하거나 과거 감사를 새 형식으로 위조하지 않는다.
- journal 결과 identity·checkpoint generation을 확인한 뒤 rebase 하위 단계를 정리하고 ExecPlan 실행 row·프로젝트 투영을 일관되게 반영한다. journal 확정 후 SQLite 반영 전 재시작은 같은 결과만 멱등하게 적용한다. 원격 base 전진은 과거 완료 사실과 별도 새 rebase 필요성을 구분한다.
- 기존 Worker session·피드백·승인·게시 OID·retry 감사는 보존한다. 후보 revision이 달라지면 필요한 Worker 확인·targeted test·Manager 전체 verify·Review를 다시 거친 뒤에만 게시한다. PR 없는 checkpoint branch와 기존 PR 충돌 복구에 같은 저장·재개 기준을 적용한다.

## Implementation

- `manager_git_journal.py`에 intent·단계 결과·확정 결과를 재대조하는 복구 경계를 두고 `execution_checkpoint_state.py`와 `runtime_state.py`에는 호환 가능한 완료 identity/generation 연결만 추가한다. 파일 journal과 SQLite가 원자적이라는 가정 없이 journal 확정 후 실행 상태 반영을 transaction으로 처리한다.
- `execution_handlers.py`의 `_post_rebase_observation`, `_finish_rebase_journal`, `_complete_rebase_intent`와 `loop_manager.py`의 직접 rebase 복구가 같은 G-099 판정과 저장된 진행 위치를 사용하도록 한다. 원격 경쟁과 소유권 실패는 기존 진단·차단 경계로 보낸다.
- 구 형식 intent 전용 판독은 증거가 있는 경우만 좁게 허용하고 판정 근거를 새 감사 기록으로 남긴다. 새 실행의 증거 요건을 legacy 허용 수준으로 낮추지 않는다. 완료·중단 복구의 장기 규칙은 구현 PR에서 `docs/PRODUCT.md`에 반영한다.

## Validation

- Worker는 직접 변경한 journal·checkpoint·state 전이의 `tests/test_manager_git_journal.py`, `tests/test_managed_mutation_evidence.py`, `tests/test_execution_checkpoint_state.py`, `tests/test_runtime_state.py`, `tests/test_execution_handlers.py` 중 관련 targeted unit test만 실행한다.
- 저장 경계별 중단·재시작에서 교정/session 보존, 동일 결과의 단일 적용, 원격 경쟁·다른 invocation·불완전 legacy 거부를 확인한다. G-085 형식의 승인 tree와 실행 표식이 실제 해결 commit 및 후속 commit에 유일하게 대응하는 경우만 완료된다.
- 전체 verify는 실제 Git 이력에서 후속 동일 파일 변경과 중단 복구 후 Worker 확인·verify·Review·기존 게시 gate까지 이어지는지 확인한다. 판정기 단독 통과나 journal 결과 존재만으로 게시 가능 상태가 되지 않아야 한다.

## Progress

- [ ] journal·SQLite 저장 경계별 멱등 완료 복구
- [ ] 증거에 한정한 구 형식 intent 복구
- [ ] 후보 revision 재검증과 PR/비PR 경로 회귀

## Decision Log

- G-099의 단계 증명 없이 이전 결과를 채택하지 않는다. Agent Git 기준선과 범용 차단 복구는 G-092의 범위이며, 이 계획은 R-045의 완료 사실과 실행 상태 반영을 다룬다.
