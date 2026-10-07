---
id: G-217
title: verify 자식 환경 정제의 실제 전달 경계 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-067] 프로젝트 공통 verify 실행 규약.md
depends_on: []
complexity: medium
risk: high
priority: high
---

## Purpose

정제된 환경 복사본과 실제 subprocess 환경이 갈라진 verify guard 경계를 정리한다. 현재 audit이 marker/PYTHONPATH만 반영해 자식에 남기는 조율 인증값을 기존 자식 실행 정책대로 제외한다.

## Acceptance Criteria

- 실제 자식 생성에 사용되는 환경에서 LOOP_MANAGER_VERIFY_ENDPOINT·SECRET·SESSION이 제외된다. 명시적 env와 상속 env(None) 모두 실제 전달 경계에서 확인하며 정제 helper를 다시 호출한 결과만 검사하지 않는다.
- 부모의 조율용 os.environ·호출자 설정은 유지하고 자식에는 정제된 복사본을 전달한다. 기존 marker/PYTHONPATH·중첩 자식의 Agent 호출 차단·필요한 일반 환경은 유지한다. 인증값을 argv·로그·결과로 옮기지 않는다.
- 정제/시작 실패·timeout·중단에서 기존 자식 종료 확인과 해당 예약 보존/회수를 유지한다. 다른 verify의 부모 인증/프로세스/예약을 변경하지 않는다.
- 사용자 동작(verify 실행)·공개 계약(명령/환경)·저장 형식(진단/상태)·인증 저장 방식과 검증 성공 판정을 보존한다. 새로운 sandbox·서비스·권한·환경 설정을 도입하지 않는다.

## Implementation

- `loop_manager_internal/verification/guard.py`의 audit과 기존 자식 launch 경계에서 하나의 effective child environment 준비 책임을 재사용한다. env=None에서도 부모 환경을 지우지 않고 실제 생성 경계에 정제 값을 전달한다. 명시적 env의 기존 방어도 유지한다.
- `tests/test_verify_runtime.py`가 실행 대역에 실제로 전달된 환경을 검사하도록 정리한다. 실제 Agent는 호출하지 않으며 무관한 인증/저장 정책을 수정하지 않는다.

## Validation

- Worker targeted unit: `tests.test_verify_runtime`의 명시적/상속 환경·부모 보존·실제 자식 환경·중첩 Agent 거부 사례. 시작/정제 실패·timeout/중단, 다른 verify의 인증/예약 보존과 비밀값 출력 부재를 확인한다.
- Manager 전체 verify: 자체 직렬/병렬 runner·조율 통신·자식 종료/예약·Agent 없는 검증의 실제 연결을 확인한다. Reviewer는 정제된 복사본과 실제 subprocess env의 identity를 대조하고 부모 인증 삭제/자식 인증 잔류를 blocking issue로 확인한다.

## Progress

- [x] 저장된 평가 발견 9의 revision·원본 hash·인용을 확인했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현, targeted unit, Manager 전체 verify, 읽기 전용 Review 및 사용자 Merge

## Decision Log

- 근거: manifest `696fbe1661814304e27fbaed394fba5f83afe82be298b5892f9c55a32923c5a2`, source revision `2eb8cfc83b290dbfe3e705e3b2891ca20da78145`; `loop_manager_internal/verification/guard.py:53`의 실제 환경 부분 갱신과 저장된 test double 누락 판단을 재사용했다.
- G-148은 공통 core/adapter·선택적 도입을 제공한다. 이 계획은 기존 자체 verify의 이미 선언된 자식 인증 제외 계약을 실제 전달에 연결하는 국소 책임 정리이며 새 runner 기능을 구현하지 않는다. G-148이 이후 이 경계를 재사용하므로 추가 기존 READY prerequisite는 없다.
- complexity medium은 상속/명시 환경과 부모 조율을 구분하는 명확한 전달 경계 설계다. risk high와 priority high는 실제 자식 환경에 현재 조율 인증값이 남아 인증 경계를 넘을 수 있는 보안 위험에 따른다. 잘못된 부모 정제도 여러 verify의 통신/예약 처리를 끊을 수 있다. 영향은 해당 실행의 단기 조율 인증으로 한정하며 기존 자식 종료 확인·예약 보존과 해당 실행 종료/새 인증으로 복구한다. 이미 전달된 값은 코드 rollback으로 회수된다고 가정하지 않는다. 인증 저장 방식·장기 비밀값은 변경하지 않는다.
