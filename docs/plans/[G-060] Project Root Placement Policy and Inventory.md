---
id: G-060
title: Project Root Placement Policy and Inventory
sources:
  - docs/requirements/[R-028] Project Root File Placement and Safe Reorganization.md
depends_on:
  - G-051
  - G-052
  - G-053
  - G-054
  - G-055
  - G-056
  - G-057
  - G-058
  - G-059
  - G-061
  - G-062
  - G-064
  - G-065
  - G-066
  - G-067
  - G-069
  - G-071
  - G-072
  - G-075
  - G-077
  - G-080
complexity: medium
risk: medium
priority: normal
---

## Purpose

현재 루트에는 필수 launcher·문서 외에 `agent_invocation.py`, `execution_handlers.py`, `maintenance_state.py` 등 내부 모듈이 직접 놓여 있다. `loop_manager.py`, `run.sh`·`run.ps1`과 테스트 import가 이 배치에 결합돼 있으므로 단순 파일 이동은 실행을 깨뜨린다. 실제 파일 이동에 앞서 모든 루트 항목의 배치 근거와 공통 품질 기준을 고정한다.

## Acceptance Criteria

- 루트의 모든 현재 항목을 사용자 진입, 실행·빌드·배포, 도구 필수 탐색 또는 내부 구현으로 분류하고 근거와 참조를 기록한다. `.agents/`, `docs/`, `scripts/`와 고정 파일, launcher·manifest·필수 설정은 지정 경로를 유지한다.
- 내부 모듈의 import graph, 테스트 참조, 실행·도구 탐색 근거와 안전한 하위 패키지 목적지를 기록한다. 이동할 수 없는 항목에는 구체적인 이유를 남긴다.
- 이후 신규 루트 파일은 구체적 루트 필요 근거를 확인한다. 이 계획은 기존 파일의 path, CLI, 사용자 동작, 공개 계약, SQLite 저장 형식과 verify 경로를 변경하지 않는다.
- 등록된 모든 프로젝트의 Planner·Worker·Reviewer 공통 지침에도 필수 루트 경로 보존과 파일별 이동 근거 확인을 적용하되 기존 프로젝트의 실제 파일은 이 계획에서 이동하지 않는다.
- 기존·신규 등록 프로젝트의 첫 구조 전수 평가에서 루트 항목의 실제 참조, 사용자 이용 경로, 소유권과 안전한 목적지를 조사한다. 이미 수행한 전수 평가와 별도의 중복 전체 스캔을 강제하지 않고, 근거가 확인된 이동만 R-027의 동작 보존 계획으로 연결한다.

## Implementation

- G-059의 전수 inventory를 최신 루트 파일 집합과 대조해 파일별 참조, 소유권과 이동 목적지를 기록한다. G-051, G-064~G-072, G-077과 G-080에서 추가·변경된 루트 모듈이 있으면 import·검증 참조에 포함하고, `ROOT` 기반 resource lookup과 launcher가 요구하는 고정 파일을 식별한다.
- 필수 폴더·파일과 신규 루트 항목 판정 기준을 공통 provider-neutral prompt에 반영한다. 실제 내부 모듈 이동은 G-063에서 수행한다.
- 기존 등록 프로젝트에도 적용되는 provider-neutral prompt에 필수 루트 경로와 안전한 파일 배치 기준을 반영한다.
- 유지보수 Planner의 첫 구조 평가 입력과 결과에 루트 inventory를 포함하고, 이미 평가된 프로젝트는 다음 해당 평가에서 확인된 기존 결과를 재사용한다. 소유권·참조를 증명하지 못한 항목은 이동 계획에서 제외하고 판단 근거를 기록한다.

## Validation

- Worker는 `tests/test_loop_manager.py`의 역할별 prompt와 `tests/test_managed_configuration.py`의 공통 지침 배포 관련 targeted unit test만 실행한다.
- 현재 루트 inventory가 필수 launcher·manifest·문서와 내부 모듈을 근거로 구분하고 기존 등록 프로젝트의 prompt가 고정 경로를 포함하는지 검증한다.
- 기존·신규 프로젝트의 첫 구조 평가에서 루트 항목을 빠짐없이 조사하고 이전 전수 평가를 중복 실행하지 않으며, 불확실한 항목을 자동 이동 대상으로 만들지 않는지 검증한다.
- 전체 verify에서 기존 실행 진입점, 문서·verify 경로와 등록 프로젝트 공통 지침이 유지돼야 한다.

## Progress

- [ ] 루트 inventory 및 안전한 이동 경계 확정
- [ ] 공통 파일 배치 지침과 targeted test 추가

## Decision Log

- `loop_manager.py`는 현재 launcher가 직접 참조하므로 루트에 둔다. 이동 후보는 파일명이나 길이가 아니라 실행·도구 탐색 필요 여부로 판정한다.
- G-051의 분리 모듈도 루트 inventory에 포함하되, 실제 이동 여부는 G-060의 동일한 참조·소유권 기준으로 판정한다.
- G-077과 G-080의 실행·계획 catalog 변경이 Merge된 뒤의 루트 구조를 조사한다. 두 작업에서 바뀐 import·테스트 참조를 G-063의 이동 근거에 포함한다.
