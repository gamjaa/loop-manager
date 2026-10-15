---
id: G-261
title: 삭제 반영 작업의 소유 브랜치와 worktree 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-055] Blocking Path Audit and Minimal Interruption.md
depends_on:
  - G-258
  - G-260
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

확인된 문서 삭제를 반영한 취소·완료 작업의 정확한 Manager 소유 로컬·원격 브랜치와 worktree를, 작업물 보존과 기대 OID 검증 뒤 정리한다. 원격 ref 삭제까지 확장하는 경계는 아래 User Decision의 선택지 A로 승인됐다. 승인된 대상·보존·소유권·PR head·기대 OID·중단 복구 조건의 구현과 검증은 남아 있다.

## Draft Decision

Question: R-055의 확인된 삭제를 반영한 취소·완료 작업에 한해, 아래 소유권·작업물 보존·정확한 ref/기대 OID·PR head 사용 확인과 중단 복구 조건으로 Manager의 로컬·원격 작업 브랜치 및 worktree 자동 정리 경계를 도입하도록 승인하는가?
Facts: R-055 D09/D10은 이 자동 정리를 요구하지만 ARCHITECTURE의 현행 Repository Safety and Security는 Merge된 로컬 관리 branch만 비강제 삭제하고 원격 branch는 자동 삭제하지 않는다. Architecture Change Policy는 “worktree, branch, commit, push와 PR 생성 경계 변경”에 사용자 승인을 요구한다. 현재 cleanup은 PR head에 포함된 clean worktree를 확인하며 local-only commit·dirty·미확인은 pending으로 보존한다. R-055는 원하는 변경의 근거이고 이 계획의 사용자 승인 기록을 대신 작성하지 않는다.
Options: (A) 아래 조건을 모두 지키는 R-055의 소유 로컬·원격 자원 정리 경계를 승인한다. (B) 현재 로컬 Merge 정리를 유지하고 이 확장은 defer한다. 다른 범위나 보존 조건을 원하는 경우 사용자 명세를 먼저 조정한다.
Impact: A는 기존 cleanup/Manager Git 효과를 확장하며 보호 실패·경쟁·PR head 사용 미확인은 해당 자원 pending으로 남긴다. B에서는 G-258–G-260의 확인된 문서·DB·취소 반영은 진행할 수 있고 이 자원 정리 목표는 미충족으로 남는다. 승인 전 아래 구현과 그 의존 실행을 시작하지 않는다.

## User Decision

Decision: approve
Revision: 5553ed3c62389b0e1ba07bcdcaf4a56efd067c17d9cf20bbb62ee073c2f8fff4
Answer: 선택지 A를 승인한다. 앞으로 작업 문서의 확인된 삭제를 반영한 취소·완료 작업에 대해 Manager가 관련 Manager 소유 원격·로컬 작업 브랜치와 worktree를 자동 정리하도록 한다. 이 계획의 작업물 보존, 소유권 확인, 정확한 ref·기대 OID 검증, PR head 사용 확인 및 중단 복구 조건을 모두 적용한다. base·사용자·다른 작업의 자원은 삭제하지 않으며, 보존 실패·사용 중·불확실한 자원은 pending으로 유지한다. 이 승인으로 Architecture Change Policy의 해당 경계 변경 판단을 해소하고 G-261의 draft를 해제한다.

## Acceptance Criteria

- G-258/G-260에서 삭제·현재 수명을 확인한 취소·완료 작업의 자원만 대상이다. repository·작업·정확한 소유 branch/worktree·writer 종료·다른 작업과 수동 소유자의 사용 여부를 정리 직전에 확인한다. base·사용자 branch·다른 작업 자원을 이름이나 문서 부재만으로 삭제하지 않는다.
- 미게시 commit·권위 있는 사용자 변경·원문·session·실행/실패 근거와 필요한 PR head를 복구 가능한 위치에 보존하고 보존 결과를 검증한 뒤 정리한다. 보존 실패·원문/소유권 불확실·미종료 writer·사용자 Git operation은 자원을 남기며 정리 완료를 만들지 않는다. 현재 dirty/local-only 보존 규칙을 단순히 제거하지 않는다.
- 원격의 실제 ref와 기대 OID·PR Open/Closed/Merged 상태·현재 head 사용을 재확인해 정확한 대상만 삭제한다. 새 commit·재생성·소유권 변경을 낡은 요청으로 제거하지 않는다. PR head 보존과 정리가 양립함을 확인하지 못하면 해당 정리만 보류하며 PR을 자동 종료·Merge하지 않는다.
- 문서 게시·DB 취소/완료·자원별 정리는 하나의 원자 작업으로 가정하지 않는다. 소유 intent·삭제 revision·보존 위치·정확한 ref/기대 OID 및 자원별 실제 결과를 유지하고 각 확정 지점부터 멱등 재개한다. 이미 사라진 동일 자원은 결과와 대조하고 같은 이름으로 재생성된 자원은 별도로 검증한다.
- 원격 ref 삭제 뒤 결과 미기록, local ref와 worktree 일부 정리, 권한·보호 정책·원격 조회 실패에서도 확인된 문서·DB 반영을 되돌리지 않는다. 미정리 대상·보존 위치·실제 이유를 pending으로 남기고 독립 작업을 계속한다. 같은 확인 결과로 삭제·게시·알림을 반복하지 않는다.
- 승인된 경계는 확인된 문서 삭제 처리에 한정한다. 기존 Merge cleanup·수동 reset/manual stop·base 보호·일반 force push 금지와 PR Merge의 사용자 소유를 유지하고 승인되지 않은 범위로 자원 정리를 확대하지 않는다.

## Implementation

아래 User Decision의 승인 범위에서 `git/cleanup_service.py`·`merge_cleanup_service.py`의 소유권·재검사·pending 처리, 기존 Manager Git journal·자원 claim·PR 관측·게시 lease를 재사용한다. 취소/완료의 현재 수명과 자원별 보존·삭제 intent를 연결하고 실제 ref 삭제 효과를 의미 판단·DB 취소와 분리한다. 복구 가능한 보존·정확한 기대 OID의 거부를 기존 관리 저장 경계 안에서 구현하며 새로운 외부 서비스·비호환 migration·독립 운영 상태 원본을 만들지 않는다.

G-238의 명시적 reset/manual stop·READY 복귀는 이 작업에 포함하지 않는다. 해당 경로와 공통 보존·Git journal 책임이 있으면 현재 Merge된 helper를 재사용하되 미완료 CLI 구현을 복제하거나 수동 소유 권한을 확대하지 않는다. PRODUCT·ARCHITECTURE에는 승인 후 실제 구현한 삭제 대상·보존·PR/lease·pending/재개 계약을 현행 정리 절에 통합한다.

## Validation

- `tests/test_worktree_cleanup.py`, `tests/test_cleanup_pending_state.py`, `tests/test_workspace_cleanup_ownership.py`의 최소 targeted unit으로 취소·완료 자격, base/사용자/다른 작업·수동 소유 거부, 미종료·보존 실패·dirty/local-only·PR head 사용 불확실·권한/보호 정책 실패를 실제 정책·SQLite·파일 보존으로 확인한다. Git·PR 관측은 mock하며 정리 판정을 성공값으로 대체하지 않는다.
- 기존 Git 통합 fixture의 임시 원격에서 정확한 소유 ref 삭제, 삭제 직전 새 commit과 같은 이름 재생성의 lease 거부, local-only commit·작업 파일의 복구 가능성을 실제로 확인한다. assertion은 ref/OID·복구된 bytes·사용자/base 불변 및 worktree 등록에 연결하며 실제 외부 repository에 게시하지 않는다.
- 기존 journal·pending fixture에 문서 게시 뒤 DB 미확정, 취소 확정 뒤 정리 전, 원격 삭제 뒤 결과 저장 전과 자원별 부분 정리의 재시작을 연결한다. 반복 삭제·중복 결과·취소 rollback·독립 실행 억제가 없어야 한다.
- Manager 전체 verify는 R-055 D09/D10·기존 Merge cleanup·lease 거부·수동 소유 보호 및 감사 보존을 유지한다. Reviewer는 소유권 밖 삭제, 보존 미확인 삭제, 낡은 OID 삭제와 PR 상태/정리 성공 합성을 blocking issue로 확인한다. 이 계획은 승인과 실제 검증·Merge 없이는 완료로 기록하지 않는다.

## Progress

- [x] 2026-10-10 delta·R-055 D09/D10·ARCHITECTURE의 현행 정리/변경 정책·cleanup 구현과 기존 회귀를 정적으로 대조했다. 구현·테스트·전체 verify를 실행하지 않았다.
- [x] 2026-10-11 사용자 Architecture Change Policy 판단: 선택지 A 승인 및 draft 해제 요청을 위 User Decision에 기록했다.
- [ ] 승인 범위의 자원 보존·정리·재개 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-11 사용자가 “앞으로 작업 문서가 삭제됐을 때 매니저가 관련 원격/로컬 브랜치도 삭제하도록 해줘. 유저 결정 남기고 draft 상태 풀어서 프리해줘”라고 명시적으로 요청했다. 해당 사용자 결정을 대신 기록하고 draft 표시를 제거했다. 기존 Draft Decision과 구현·검증 조건은 승인 범위와 판단 지문 보존을 위해 유지한다. 선행 작업·정상 실행 gate는 계속 적용한다.

- R-055는 새 자원 정리 목표를 명확히 지정하지만 현재 Manager 효과에는 소유 원격 ref 자동 삭제 연결이 없다. 정책 적용 대상의 구현을 임의 승인하지 않고 이 ID의 draft 판단으로 남긴다. 사용자만 draft 해제와 User Decision을 작성하며 Agent가 결정이나 승인을 합성하지 않는다.
- G-189의 전체 차단 감사, G-257의 참조 복구 게시, G-258의 관측·감사 구분, G-259의 의미 판단·문서 게시와 G-260의 현재 취소를 중복하지 않는다. 정리 보류가 확인된 취소나 독립 작업을 완료 대기로 되돌리지 않아야 한다. 이번 변경 검토는 프로젝트 최종 전문 확인이 아니다.
- complexity high: 비원자 원격 ref·로컬 branch·worktree·DB 결과를 보존 증명·기대 OID·PR head와 묶어 중단/재생성에도 안전하게 재개하는 설계 판단이 남아 있다. risk high: 잘못된 정리는 미게시 작업이나 사용 중인 원격 head를 잃게 할 수 있다. 검증된 보존본·정확한 ref/OID·Git/정리 intent로 수동 복원할 수 있어야 하고 외부 ref 삭제를 코드 rollback으로 복구한다고 가정하지 않는다. 보존 실패·경쟁·부분 정리·복구 가능성을 AC·Validation·Review에서 확인한다.
