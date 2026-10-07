---
id: G-162
title: 검증된 runtime Python 캐시 재사용
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-059] Safe Python Bytecode Cache Use.md
depends_on: []
complexity: medium
risk: high
priority: normal
---

## Purpose

runtime bootstrap의 강제 -B와 캐시 부재를 요구하는 테스트를 R-059의 안전한 파생 캐시 정책으로 바꾸고, 관리 Python 실행 경계에서 사용자가 명시한 캐시 선택을 존중한다.

## Acceptance Criteria

- 기본 실행에서 무시된 Python bytecode 캐시를 생성·재사용한다. 명시적인 bytecode 비활성화는 존중하며 캐시가 없거나 깨졌거나 쓰기 불가여도 유효한 원본 실행이 가능하다.
- runtime revision·원본·Python interpreter가 다른 캐시를 유효한 코드로 채택하지 않는다. tracked 원본·사용자 ignored 파일·다른 worktree의 캐시를 정리하거나 Git clean 상태를 바꾸지 않는다.
- cold/warm 기록은 같은 revision·검증 범위·Python·환경과 생성/재사용 여부를 식별한다. 실제 비교 전 절감률이나 검증 성공을 합성하지 않는다.

## Implementation

- runtime/bootstrap.py의 기존 runtime 생성·선택·실행 경계를 사용하고 Python의 표준 bytecode 무효화 동작을 먼저 검토한다. 필요한 소유 캐시 식별·fallback만 추가한다.
- agents/agent_profiles.py의 환경 필터와 provider 최초·재개·verify 경계를 대조한다. 현재 필터가 버리는 명시적 무캐시 선택도 전달하되 필요한 비밀이 아닌 캐시 선택 값만 다루고 기존 환경·sandbox·범위 검사를 유지한다. 역할별 강제 bytecode 금지를 다시 만들지 않는다.
- 현재 verify·테스트 진단에 캐시 조건을 연결하고 캐시 관리만을 위한 서비스나 새 루트 파일을 만들지 않는다. 전체 비교 실행은 Manager에 맡긴다.
- 자동 문서·권한·게시·검증 경계는 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 따른다. 필요한 기존 helper와 관련 테스트를 우선 재사용하고 새 루트 항목을 만들지 않는다.

## Validation

- tests/test_bootstrap_foundations.py 및 runtime 테스트에서 기본 생성·재사용·원본/버전 변경·손상/권한 오류·명시적 -B와 사용자 ignored 자료 보존의 최소 unit case를 실행한다.
- provider 환경·최초/재개 unit case는 명시적 무캐시 선택 전달, 기본 ignored 캐시 허용과 권위 있는 변경 거부를 mock Agent 경계에서 확인한다.
- Manager 전체 verify와 비교 기록은 동일 범위 cold/warm 실행, runtime 선택·handoff 및 소유 자원 정리를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 표준 Python 기능을 사용하는 제한된 경계 변경으로 complexity medium, 잘못된 실행 원본 채택의 영향으로 risk high다.
- R-016의 과거 bytecode 금지는 R-022가 명시적으로 대체했다. 기존 ignored-only 판정은 재사용하며 이번에는 R-059의 runtime 잔여 금지와 명시적 환경 선택 누락만 교정한다.
