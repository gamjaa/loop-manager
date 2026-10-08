---
id: G-220
title: 원격 runtime과 로컬 동기화 설계 정합화
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-020] Manager-Owned Runtime Worktree Scope Reconciliation.md
  - docs/requirements/[R-023] Self-Hosted Runtime Refresh Handoff.md
  - docs/requirements/[R-056] Non-Blocking Local Base Synchronization.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

ARCHITECTURE의 bootstrap·기본 worktree 동기화와 안전 절을 현재 Merge된 원격 revision 선택·비차단 동기화 구현에 맞춘다. `origin/main` 고정과 기본 checkout rebase 일괄 금지라는 과거 설명을 현재 PRODUCT·코드·회귀와 정합하게 유지한다.

## Acceptance Criteria

- `## 실행 환경과 시작`·`### 자체 runtime 선택과 handoff`의 실행 revision은 등록 self base를 해석한 `origin/<base-branch>`이며 self 등록이 없을 때의 기존 main 기본값과 구분된다. 최신 원격 bootstrap·runtime을 같은 실행에서 시작하고 source HEAD·branch·동기화 성공을 실행 조건으로 요구하지 않는다.
- bootstrap 자체의 소유권이 확인된 부수 source 동기화와 runtime 자식의 일반 프로젝트 동기화를 구분한다. 기본 checkout이 equal·ahead·behind·diverged인 경우를 현재 PRODUCT와 source_sync/source_rebase 구현대로 설명한다.
- 확인된 clean 선형 로컬 commit의 diverged rebase는 durable intent·현재 자원·사용자 작업 보존 조건 아래 수행하고 자동 push하지 않는다. dirty·겹침·사용자 operation·동시 변경·소유권 불명확은 보존·skip하며 실패/중단 복구는 Manager 소유 상태가 증명된 범위로 제한한다. 로컬 결과는 자동 작업 입력으로 채택하지 않는다.
- `### 기본 worktree 동기화`·`## Repository Safety and Security`의 rebase 금지·bootstrap 무변경 설명을 승인된 좁은 예외와 일치시킨다. 원격 기준 자체의 확인 실패·삭제/재작성은 로컬 동기화 실패와 구분하며 오래된 runtime·로컬 코드를 대체 실행하지 않는다.
- 선택 runtime B와 이후 정상 원격 base C 전진, source config/interpreter 전달·부모 lock·self PR Merge handoff 및 중단 상태 보존을 유지한다. G-199의 격리 증거 없는 legacy 판정과 G-191의 미완료 Claude 검증, G-204·G-205의 작업 선택/유지보수 결정 대기를 해결된 것으로 설명하지 않는다.

## Implementation

- `runtime/bootstrap.py`의 등록 base 조회·원격 선택·부수 sync·같은 revision 자식 실행, `git/source_sync.py`·`source_rebase.py`의 관계별 처리·실패·중단 복구와 관련 테스트를 기준으로 위 설계 절을 재작성한다.
- 정보 원본·부모/자식 책임·원격 선택과 로컬 동기화의 분리만 기술하고 PRODUCT의 제품 정책과 README 설치 안내를 장문 반복하지 않는다. R-056에서 사용자가 이미 승인한 제한된 예외를 현재 구현대로 반영한다.
- 실행 코드·저장 schema·실행 권한·Requirement·사용자 결정·다른 계획의 목표는 변경하지 않는다. G-207의 Worker level/profile 설명과 G-205의 유지보수 정책 정합화는 해당 계획에 남긴다.

## Validation

- 문서 변경이므로 Worker는 unit test를 실행하거나 새 테스트를 작성하지 않고 그 사유를 보고한다. `tests/test_bootstrap_entry.py`의 develop base·모든 source 상태·sync 실패/경쟁·원격 revision 거부와 `tests/test_source_sync.py`의 disjoint dirty fast-forward·clean diverged·local commit 보존·충돌·중단/복구 사례를 읽어 문구와 대조한다.
- Manager 전체 verify는 원격 전용 입력·등록 base·exact runtime·lock/config/interpreter·로컬 작업 보존과 제한된 rebase 복구 회귀를 유지한다. Reviewer는 새 설명이 사용자 Git operation의 자동 abort·로컬 commit push·sandbox 완화나 legacy 자기 승인을 허용하지 않는지 확인한다.

## Progress

- [x] 2026-10-08 a3ce7f88cef4cd46b34e0ead46ec5472fb5bd6f7의 활성 명세 전문, bootstrap/source 동기화 원본·관련 테스트 및 G-199·G-205·G-207 catalog 범위를 대조했다. 구현·테스트·verify는 실행하지 않았다.
- [ ] 관련 설계 절 정합화
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- bootstrap_base_branch·prepare_bootstrap_runtime은 등록 self base와 원격 revision을 사용하며 source 동기화를 부수 처리로 수행한다. source_sync는 앞선 로컬 commit을 보존하고 clean diverged만 source_rebase의 소유 intent·복구 경계로 보낸다. ARCHITECTURE의 origin/main 고정·bootstrap source 무변경·diverged 일괄 skip·기본 checkout rebase 금지는 이 구현 및 현재 PRODUCT와 다르다.
- 기존 비종료 G-199는 격리 증거 없는 legacy 비교의 사용자 정책, G-205는 유지보수 종료 정책, G-207은 Worker profile 설계만 다룬다. 이 계획은 그 질문을 선택하거나 해당 구현을 중복하지 않는다.
- complexity low: 구현·테스트로 확정된 책임과 예외를 기존 설계 절에 반영하며 새 전이·복구 판단을 설계하지 않는다. risk low: 실행·외부 Git 상태를 바꾸지 않는 문서 변경이고 diff로 복구 가능하다.
