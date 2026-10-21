---
id: G-299
title: 일괄 fetch 추적 ref의 심볼릭 대상 보호
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-092] 프로젝트 원격 브랜치 일괄 동기화.md
depends_on: []
complexity: high
risk: critical
priority: high
---

## Purpose

R-092의 일괄 수신 뒤 추적 ref 반영이 심볼릭 ref를 따라 사용자 branch나 다른 공유 ref를 변경하지 않게 한다. 이미 반영된 목록 조회·일괄 fetch·branch별 수신 검증·공유 잠금과 ref별 journal은 재사용하고, 대상 밖 쓰기를 막는 경계와 그 회귀만 보완한다.

현재 `_record_fetch_ref()`는 OID만 관측하고 일반 `update-ref`를 실행한다. 예를 들어 `refs/remotes/origin/topic`이 `refs/heads/user-work`를 가리키면, 예상 이전 OID 검사가 통과해도 실제 쓰기는 사용자 branch에 도달할 수 있다. ref 이름으로 잡은 잠금·journal과 실제 쓰기 대상이 달라지는 문제이며 실제 발생 사건이나 손실을 확인했다는 뜻은 아니다.

## Acceptance Criteria

- 정상 직접 추적 ref의 갱신·신규 생성은 branch별 검증과 기존 ref별 CAS/journal로 수행한다. 목록 조회 1회·필요한 일괄 수신 최대 1회와 변경 없는 실행의 수신 생략을 유지한다.
- 대상 추적 ref가 사용자 branch·다른 추적 ref·공유 ref를 가리키는 심볼릭 ref이거나 dangling symref이면 그 대상을 따라 쓰지 않는다. 소유권을 확정할 수 없는 심볼릭 형상과 기존 파생 `origin/HEAD`는 보존하고 해당 쓰기를 기존 안전한 보류·실패 경계로 돌린다. 사용자 alias를 정상 직접 ref로 조용히 치환하지 않는다.
- 관측 뒤 쓰기 전에 직접 ref가 같은 OID의 심볼릭 ref로 바뀌어도 대상 밖 ref를 변경하거나 그 변경을 Manager의 정상 전이로 채택하지 않는다. OID CAS와 사전 검사만으로 raw ref 종류의 경쟁을 해결했다고 간주하지 않는다.
- 잠금과 journal의 대상은 실제로 변경하는 정확한 ref와 일치한다. 원래 intent·소유자·실패 감사와 이미 완료한 다른 ref 전이를 보존하며 경쟁 값을 강제로 덮거나 과거 성공으로 새 형상을 승인하지 않는다.
- 이전 OID 전용 journal은 유효한 직접 ref와 소유 reflog 근거로 재개할 수 있다. 현재 심볼릭 ref의 referent OID가 같다는 사실만으로 미완료 쓰기의 완료·소유권을 확정하지 않는다. 기존 기록을 일괄 수정하거나 미실행 성공을 만들지 않는다.
- 같은 ref 반영 helper를 쓰는 단일 branch 경로의 대상·삭제 판정·완료 조건과 정상 복구를 유지한다. 보완한 실제 보존 경계는 PRODUCT·ARCHITECTURE의 관련 절에 통합한다.

## Implementation

- [ ] 확인한 `loop_manager_internal/git/base_fetch_service.py`의 `_recorded_project_fetch()`·`_record_fetch_ref()`에서 raw ref 종류·정확한 대상과 OID를 쓰기·완료 관측에 연결한다. `runtime/bootstrap_adapter.py:_tracking_ref_oid()`는 commit OID로 역참조하므로 이것만으로 직접 ref임을 증명하지 않는다. 다른 소비자가 쓰는 OID helper의 의미를 일괄 변경하지 않고 필요한 관측을 fetch 책임 안에 둔다.
- [ ] Git이 실제 쓰기 시점에도 대상 밖으로 역참조하지 않는 방법과 심볼릭 형상 보존을 함께 적용한다. 단순 사전 `symbolic-ref` 검사나 `--no-deref` 추가만으로 경쟁 중 사용자 alias 보존까지 보장한다고 가정하지 않는다. 기존 Git adapter·명령 경계를 재사용하고 실제 지원 Git 계약으로 구현 방법을 확인한다.
- [ ] 확인한 `loop_manager_internal/git/manager_git_journal.py`의 `execute()`·`recover_active()`·`verified_transition()`과 `runtime/resource_lock.py`의 ref별 lease를 재사용한다. fetch에 필요한 관측·marker 경계를 보완하되 다른 journal kind의 의미와 기존 version 1 기록을 바꾸지 않는다. 복구 시 심볼릭 형상·외부 전환을 만난 경우 기존 intent와 사용자 ref를 보존한다.
- [ ] 확인한 `tests/test_project_fetch_branch_deletion.py`의 fake Git 관측과 실제 journal, `tests/test_manager_git_journal.py`의 중단 fixture를 확장한다. 직접 ref·symbolic/dangling·동일 OID 전환 경쟁·기존 intent 재개 판단은 mock으로 검사하고 성공 판단 자체는 mock하지 않는다.
- [ ] 확인한 `tests/test_origin_isolation.py:OriginIsolationScopeTest`와 `tests/support/origin.py`·`tests/support/git.py`를 재사용해 실제 역참조 쓰기 차단의 최소 Git 회귀를 둔다. 기존 수신·설정 보존 회귀와 assertion을 중복하지 않고 새 실행이 증명하는 효과를 테스트에 설명한다.
- [ ] 확인한 PRODUCT의 `ExecPlan 생성과 자동 처리`·`원격 기준과 기본 worktree 보존`, ARCHITECTURE의 `역할 결과와 Manager Git 전이`에 실제로 보완한 정확한 ref 대상·경합 보존 조건만 반영한다. Requirement와 다른 ExecPlan은 수정하지 않는다.

## Validation

- Worker targeted 검사는 `tests.test_project_fetch_branch_deletion.ProjectFetchBranchDeletionTest`와 `tests.test_manager_git_journal.ManagerGitTransitionTests`의 변경한 최소 case를 실행한다. 직접 ref의 생성/갱신·동일 OID no-op과 과거 단일 fetch의 pending receipt 재개가 유지돼야 한다.
- symbolic alias가 사용자 branch 또는 다른 공유 ref를 가리키는 관측, dangling alias, 관측 뒤 동일 OID alias 전환을 주입한다. 대상 밖 쓰기 0회·alias 보존·기존 intent/실패 감사 보존을 확인한다. ref 잠금 소유자와 실제 쓰기 대상이 달라지는 실행은 허용하지 않는다.
- 최소 실제 Git 회귀에서 alias·referent·파생 `origin/HEAD`·사용자 HEAD/index/파일의 전후 값을 직접 비교한다. 정상 직접 ref가 함께 있으면 그 완료 결과를 보존하며, 이 검사를 같은 정책 조합마다 실제 저장소로 반복하지 않는다.
- 기존 `test_old_single_branch_pending_receipt_recovers_when_remote_is_equal`와 `test_project_fetch_recovers_owned_ref_update_after_result_write_crash`의 ref 쓰기 중복 방지 근거를 유지한다. 기존 journal이나 reflog가 부족한 경우 성공 기록을 합성하지 않아야 한다.

## Progress

- [x] revision `3e4b78eec608ae04c00caecaa864f1f6a5555839`의 R-092·관련 설계·ref 반영/journal·테스트를 정적으로 대조했다. 직접 구현된 일괄 fetch는 재사용하며 테스트·verify를 실행하지 않았다.
- [ ] 정확한 ref 쓰기·심볼릭 대상 보존과 legacy 복구 보완
- [ ] 최소 정책/실제 Git 회귀와 제품·설계 반영

## Decision Log

- [Git update-ref 공식 계약](https://git-scm.com/docs/git-update-ref#_description)은 기본 쓰기가 심볼릭 ref를 역참조하고 `--no-deref`는 ref 자체를 덮는다고 설명한다. 현재 코드와 이 계약의 대조로 대상 밖 쓰기 가능성을 식별했다. alias 보존과 쓰기 시점의 경쟁도 함께 검증하며 실제 손실 발생이나 수정 성공으로 기록하지 않는다.
- R-092의 정확한 ref·사용자 작업 보존을 구현하는 국소 교정이다. 새 원격 동기화 기능·사용자 ref 정리·branch 정책·비호환 journal migration을 도입하지 않는다. G-300은 남은 R-092 회귀/비용 근거를, G-189는 Merge 뒤 후속 차단·복구 소비자를 담당한다.
- complexity high: OID가 같아도 다른 raw ref 종류·쓰기 대상이 되는 경쟁을 기존 잠금·미완료 journal과 결합해 막아야 한다. risk critical: 잘못된 역참조는 소유권 밖 사용자 branch를 갱신해 미게시 commit의 연결을 잃게 하며 코드 rollback이 이를 복구하지 않는다. 쓰기 전 보존·정확한 대상 강제와 최소 실제 Git 회귀로 방지하고 불확실한 상태는 원문·intent를 남긴다. 최초 level은 6이며 Model·Effort는 Manager의 명시 설정을 따른다.
