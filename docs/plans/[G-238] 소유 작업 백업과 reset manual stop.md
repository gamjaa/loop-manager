---
id: G-238
title: 소유 작업 백업과 reset manual stop
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-076] Read-Only Operational State CLI.md
  - docs/requirements/[R-077] Manual Plan Work Start and Completion.md
depends_on:
  - G-236
  - G-237
  - G-243
  - G-265
  - G-266
  - G-267
  - G-268
complexity: high
risk: high
priority: high
---

## Purpose

자동 reset과 manual stop에서 소유 작업의 종료·복구 가능한 백업을 확인한 뒤 branch와 현재 실행 근거를 최신 원격 base로 초기화한다. 수동 reset은 G-237의 취소만 수행하며 초기화는 manual stop이 맡는다.

## Acceptance Criteria

- 등록된 비종료 작업·전체 제출 묶음·정확한 Manager 소유 branch/worktree만 대상으로 한다. 실행 종료와 자원 해제를 확인하며 PR 생성/연결·MERGED/archive·사용자 결정·독립 안전 차단·사용자 Git operation·소유권 불명은 거부한다.
- 미커밋·미게시 파일/commit과 현재 상태를 복구 가능하게 백업하고 위치를 출력한다. 백업 실패에서는 초기화하지 않는다. 아직 base에 없는 수동 R/G 정의·사용자 결정·원본 snapshot·감사·묶음 관계도 보존한다.
- 관리 rebase만 안전하게 취소하고 사용자 source checkout/branch는 변경하지 않는다. 현재 소유 작업 branch를 확인한 원격 base로 초기화하며 원격에는 정확한 관측 OID lease를 사용한다. 예상 밖 후속 commit·PR·소유자 변화는 덮어쓰지 않는다.
- 현재 후보에 사용할 수 없는 session·checkpoint·검증/Review 근거만 초기화한다. 묶음 전체 성공과 유효 source 확인 뒤 READY를 원자 확정하며 일부 G만 전환하지 않는다. READY는 dependency 충족·독립 차단 해제가 아니다.
- manual stop 성공은 현재 수동 대상·제출 유효성만 해제하고 과거 membership·제출 감사는 보존한다. 해당 READY는 자동 작업으로 선택·갱신할 수 있으나 과거 수동 기록을 현재 소유권으로 오인하지 않으며, 실패·중간 단계에서는 해제 완료를 확정하지 않는다.
- 중단·Git/원격 변경·상태 확정 사이의 실패는 백업·intent·기존 상태·실제 결과를 보존해 재개한다. 부분 성공을 완료로 보고하지 않고 branch 반복 초기화·중복 게시를 만들지 않는다. 정상 완료/무변경 0, 거부/미확정 1, 인자 오류 2와 남은 조치를 제공한다.

## Implementation

기존 수동 자원 예약·Manager Git journal·현재 rebase 판정·게시 lease와 checkpoint generation을 재사용한다. 명시적 요청의 검증된 범위만 초기화하며 CLI 이름을 소유권 없는 reset/clean 권한으로 확대하지 않는다. 필요한 백업·호환 intent는 기존 관리 경계 안에 두고 PRODUCT·ARCHITECTURE에 보존/복구 계약을 반영한다.

## Validation

- targeted unit test로 미종료·백업 실패·PR 출현·lease/소유자 경쟁·미게시 R/G·묶음 일부 거부·각 저장 경계 중단을 실제 정책/SQLite/파일 보존으로 확인한다. 판정 함수를 성공값으로 대체하지 않는다.
- 전체 verify는 복구 가능한 백업과 source 불변, 정확한 lease의 거부, 전체 READY transaction·원문/감사 보존·중단 재개를 확인한다. 실제 rebase 취소·ref/lease 효과가 필요한 최소 통합 회귀에는 이유와 assertion을 남긴다.

## Progress

- [x] eb797d3a에서 자동 reset/manual stop 및 해당 백업·초기화 intent가 없음을 확인했다. 구현·테스트 실행 없음.
- [ ] 백업·초기화·복구 구현과 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- complexity high: 종료·백업·비원자 Git 게시·묶음 상태와 재개 intent의 불변식을 함께 설계해야 한다. risk high: 잘못된 초기화는 로컬/원격 작업을 잃게 할 수 있으나 사전 검증된 복구 백업과 원문을 사용해 수동 복원할 수 있다. 백업 실패 무변경·정확한 lease·중단 보존이 필수 검증이다.
- R-076의 명시적 보존/거부 조건을 적용하고 데이터 삭제나 비가역 migration을 추가하지 않는다. 별도 backup 명령·자동 PR 취소/대체는 만들지 않는다.
