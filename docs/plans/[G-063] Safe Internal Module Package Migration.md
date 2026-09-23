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
priority: normal
---

## Purpose

G-060의 루트 inventory에서 내부 구현으로 확인한 모듈은 루트 고정 탐색 대상이 아니다. 현재는 flat import와 `loop_manager.py`의 `ROOT`, detached runtime 실행 및 테스트 monkeypatch가 경로에 결합돼 있으므로 import graph에 따라 안전하게 하위 패키지로 옮긴다.

## Acceptance Criteria

- G-060의 근거 있는 inventory에서 이동 대상으로 판정한 내부 모듈만 하위 패키지로 옮긴다. 필수 `.agents/`, `docs/`, `scripts/`, `AGENTS.md`, `loop_manager.py`, launcher, manifest, config와 verify 지정 경로를 유지한다.
- 사용자 CLI, 외부에 노출된 `loop_manager.py` 호출 계약, config·state 위치, 원격 runtime 선택, Git/PR 동작과 SQLite 저장 형식은 보존한다. 내부 import 경로와 그 경로를 직접 참조하는 테스트는 함께 갱신한다.
- tracked 파일 이동은 Git에서 추적 가능하고 사용자 파일·생성물의 삭제나 자동 이동이 없다. Windows·Linux launcher와 전체 verify는 새 경로에서 정상 동작한다.
- 이동 뒤에도 루트에 남는 항목은 G-060 inventory의 구체적인 탐색 또는 사용자 진입 근거가 있다.

## Implementation

- G-060 inventory의 import graph에서 leaf부터 내부 모듈을 package namespace로 옮기고, `loop_manager.py`의 import와 필요한 공개 re-export만 보존한다. G-051의 `agent_scope_decision.py`는 `loop_manager.py` 자료형을 실행 시 import하지 않는 경계와 직접 테스트의 import 경로를 함께 점검한다. 모든 내부 모듈에 root wrapper를 남겨 루트 정리 목적을 무효화하지 않는다.
- detached runtime child, 직접 실행 launcher, resource lookup, 테스트 patch target 및 Linux·Windows verify의 모듈 목록을 같은 변경에서 갱신한다. 현재 두 verify 스크립트의 모듈 목록 차이도 이동 대상의 검증 누락 없이 정리한다.
- 파일 이동과 함께 관련 없는 책임 재설계나 storage migration을 수행하지 않는다.

## Validation

- Worker는 이동한 모듈의 import·CLI·resource lookup targeted unit test와 `tests/test_agent_scope_decision.py`, `tests/test_origin_isolation.py`, `tests/test_managed_configuration.py`의 직접 관련 targeted unit test만 실행한다.
- 동일 SHA runtime 자식의 import, launcher 경로, 공통 파일 복사, root resource lookup, 공개 helper와 테스트 patch target을 검증한다.
- 전체 verify에서 모든 기존 테스트, Python module compile 및 Linux·Windows verify 스크립트의 새 모듈 목록이 통과해야 한다.

## Progress

- [ ] G-060 inventory의 내부 모듈을 package로 이동
- [ ] runtime·launcher·검증 경로 targeted test 갱신

## Decision Log

- 내부 모듈을 옮기는 순서는 의존성 graph를 따른다. 공개 launcher와 고정 문서 경로는 이동 대상이 아니다.
- G-051은 `agent_scope_decision.py`를 새 루트 모듈로 추가했다. `loop_manager.py`와의 단방향 import 경계 및 테스트 fixture를 이동 영향 범위에 포함한다.
