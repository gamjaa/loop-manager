---
id: G-063
title: Safe Internal Module Package Migration
sources:
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-051
  - G-060
complexity: high
risk: high
priority: critical
---

## Purpose

G-060의 루트 inventory에서 내부 구현으로 확인한 모듈은 루트 고정 탐색 대상이 아니다. 현재는 flat import와 `loop_manager.py`의 `ROOT`, detached runtime 실행 및 테스트 monkeypatch가 경로에 결합돼 있으므로 import graph에 따라 안전하게 하위 패키지로 옮긴다.

## Acceptance Criteria

- G-060의 근거 있는 inventory에서 이동 대상으로 판정한 내부 모듈만 하위 패키지로 옮긴다. 필수 `.agents/`, `docs/`, `scripts/`, `AGENTS.md`, `loop_manager.py`, launcher, manifest, config와 verify 지정 경로를 유지한다.
- 사용자 CLI, 외부에 노출된 `loop_manager.py` 호출 계약, config·state 위치, 원격 runtime 선택, Git/PR 동작과 SQLite 저장 형식은 보존한다. 내부 import 경로와 그 경로를 직접 참조하는 테스트는 함께 갱신한다.
- tracked 파일 이동은 Git에서 추적 가능하고 사용자 파일·생성물의 삭제나 자동 이동이 없다. Windows·Linux launcher와 전체 verify는 새 경로에서 정상 동작한다.
- 이동 뒤에도 루트에 남는 항목은 G-060 inventory의 구체적인 탐색 또는 사용자 진입 근거가 있다.
- 하위 패키지는 runtime·저장·Agent·Git/workspace·planning·execution·maintenance·notification 책임을 구분한다. 이동으로 진입점 `loop_manager.py`의 역방향 import, 전체 모듈 주입이나 순환 의존성을 새로 만들지 않는다. 현재 그런 경계를 사용하는 adapter는 후속 G-120~G-125에서 제거할 대상으로 대응표에 기록한다.
- G-119~G-127에서 추가로 분리할 구현·테스트의 목적지를 inventory와 연결하고, 기존 모듈 이동과 후속 책임 분리의 범위를 중복하지 않는다. `full_assessment.py`는 후속 정리까지 실행에 필요한 소비 경로를 유지하며 최종 삭제를 피하기 위한 루트 wrapper를 새로 만들지 않는다.

## Implementation

- G-060 inventory의 import graph에서 leaf부터 내부 모듈을 package namespace로 옮기고, `loop_manager.py`의 import와 필요한 공개 re-export만 보존한다. G-051의 `agent_scope_decision.py`는 `loop_manager.py` 자료형을 실행 시 import하지 않는 경계와 직접 테스트의 import 경로를 함께 점검한다. 모든 내부 모듈에 root wrapper를 남겨 루트 정리 목적을 무효화하지 않는다.
- detached runtime child, 직접 실행 launcher, resource lookup, 테스트 patch target 및 Linux·Windows verify의 모듈 목록을 같은 변경에서 갱신한다. 현재 두 verify 스크립트의 모듈 목록 차이도 이동 대상의 검증 누락 없이 정리한다.
- 파일 이동과 함께 관련 없는 책임 재설계나 storage migration을 수행하지 않는다.
- 루트 진입점과 이름이 충돌하지 않는 내부 namespace(예: `loop_manager_internal/`)를 G-060의 참조 조사로 확정하고 이후 계획에서 같은 경계를 사용한다. 패키지 `__init__.py`는 명시적 export만 담당하며 여러 service를 초기화하거나 전체 구현을 모으지 않는다. `ROOT`와 resource 위치는 저장소 루트 identity로 전달하고 이동한 모듈의 `__file__` 부모를 새 루트로 오인하지 않는다.
- 기존 공개 helper는 확인된 호출 계약에 필요한 명시적 re-export로 보존한다. repository 내부 테스트는 실제 소비하는 모듈의 patch target을 갱신하며 테스트용 전역 patch forwarding·`sys.modules` 교체·임의 attribute proxy를 추가하지 않는다. 새 domain service와 fixture 분리는 후속 계획에서 수행한다.

## Validation

- Worker는 이동한 모듈의 import·CLI·resource lookup targeted unit test와 `tests/test_agent_scope_decision.py`, `tests/test_origin_isolation.py`, `tests/test_managed_configuration.py`의 직접 관련 targeted unit test만 실행한다.
- 동일 SHA runtime 자식의 import, launcher 경로, 공통 파일 복사, root resource lookup, 공개 helper와 테스트 patch target을 검증한다.
- 전체 verify에서 모든 기존 테스트, Python module compile 및 Linux·Windows verify 스크립트의 새 모듈 목록이 통과해야 한다.

## Progress

- [ ] G-060 inventory의 내부 모듈을 package로 이동
- [ ] runtime·launcher·검증 경로 targeted test 갱신
- [ ] 후속 책임 분리 목적지·공개 export·순환 의존성 대응표 확정

## Decision Log

- 내부 모듈을 옮기는 순서는 의존성 graph를 따른다. 공개 launcher와 고정 문서 경로는 이동 대상이 아니다.
- G-051은 `agent_scope_decision.py`를 새 루트 모듈로 추가했다. `loop_manager.py`와의 단방향 import 경계 및 테스트 fixture를 이동 영향 범위에 포함한다.
- 2026-10-02: 사용자 요청으로 미착수 계획을 보완한다. 패키지 이동은 G-063, 큰 테스트 분리는 G-119, 기능별 구현 분리는 G-120~G-126, 임시 평가 코드 정리와 남은 발견 확인은 G-127이 담당한다. G-063은 경로 이동과 그에 필요한 import·resource·검증 수정을 끝낸 뒤 Merge하며 모든 책임 분리를 한 PR에 합치지 않는다.
- 2026-10-02 사용자 결정: 기존 다른 미착수 작업보다 리팩터링을 우선하므로 priority를 `critical`로 높인다. G-060의 선행 inventory와 후속 리팩터링 순서는 유지한다.
