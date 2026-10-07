---
id: G-181
title: verify 입력 목록과 Manager 생성 요청 계약
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
depends_on: []
complexity: high
risk: high
priority: high
---

## Purpose

프로젝트별 scripts/verify-inputs.json의 입력 의미와 Manager의 실제 누락 요청을 구현하고 이 저장소의 실제 전체 verify 입력 목록을 제공한다.

## Acceptance Criteria

- version 1 상대 경로/glob의 해석을 명시하고 목록 자체·실제 입력 파일/종류/mode·명령/환경/Git 또는 외부 입력을 확인할 수 있다. 문서/.agents는 실제 verify 입력일 때 포함하며 확인 불가한 입력은 재사용 불가다.
- Worker가 스크립트와 목록을 함께 유지하고 Reviewer가 누락·재사용 약화를 검토한다. 목록 밖 새 코드/설정 또는 이동은 path/content/검증 설정에 결합한 bounded 확인으로 처리한다. 성공한 verify를 목록 완전성의 증명으로 쓰지 않는다.
- 후보의 목록 부재/오류는 전체 verify로 진행하고 즉시 Worker/Planner 생성 호출을 하지 않는다. 다음 일반 planning에서 최신 원격 base의 실제 누락과 기존 관련 계획/draft/PR을 확인해 한 번만 생성 작업을 요청한다.
- manager:verify-inputs는 등록된 실제 요청·목적·규칙 버전에 결합한 허용 source이며 파일 조회·Requirement retirement에서 제외된다. 미등록 예약어·빈 source·임의 제품 목표 우회는 거부한다. 기존 문서 source·archive fallback을 유지한다.

## Implementation

- verification의 기존 Manager adapter와 catalog source 검증·planning 요청 경계를 확장한다. 이 저장소의 runner·scripts·tests·의존성·runtime 입력을 전체 구조에서 확인해 목록을 작성한다.
- 목록 파싱/입력 관측과 생성 요청을 분리하고 공통 runner 도입을 전제하지 않는다. 저장소 밖/동적 입력을 표준 목록으로 확인할 수 없으면 보수적으로 전체 실행을 선택한다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- source/catalog·planning selection·verification 입력 테스트에서 glob·파일 kind/mode·누락/이동·등록 source·remote base vs 오래된 branch·중복 요청의 최소 unit case를 실행한다.
- Manager 전체 verify는 실제 입력 목록 누락 방지, 기존 source/retirement·draft·PR 관계와 목록 부재 시 기존 전체 검증을 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Implementation Notes

- version 1 해석·파일 관측·bounded completeness token은 `verification/inputs.py`, 실제 명령/환경 전달은 `manager_adapter.observe_inputs`에 분리했다. 성공 저장·재사용 실행 gate는 G-182/G-183 범위로 유지한다.
- 일반 planning prompt에서 고정된 최신 원격 base의 목록과 기존 관련 계획/draft/PR을 확인하며, additive 요청 registry와 catalog/Planner 검증으로 예약어의 실제 요청·목적·규칙 버전을 결합한다.
- 이 저장소의 입력 목록은 runner의 compile/discovery, 테스트 support가 읽는 원본·문서·Skill·설정·실행 스크립트를 포함한다. 외부 Python/Git/OS·환경 입력은 별도 관측 identity가 없으면 재사용할 수 없다.

## Decision Log

- 프로젝트 입력·source·planning 요청의 결합으로 complexity high, 누락된 검증 입력 영향으로 risk high다. 예약어는 이번 계획의 source로 자체 생성하지 않고 R-072를 구현 근거로 사용한다.

