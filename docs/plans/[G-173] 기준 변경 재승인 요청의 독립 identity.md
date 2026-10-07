---
id: G-173
title: 기준 변경 재승인 요청의 독립 identity
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-069] User Decision 경계와 입력 양식.md
  - docs/requirements/[R-066] AGENTS 변경 사전 승인.md
depends_on:
  - G-172
complexity: high
risk: critical
priority: high
---

## Purpose

기준 파일 변경 시 과거 승인 문구를 다시 적용하지 않고 최신 기준에 결합한 새 요청을 보존·재개한다.

## Acceptance Criteria

- 대상 내용/mode·확정 patch·Baseline이 의미 있게 바뀌면 이전 결정을 무효화하고 새 request identity와 pending 결정을 만든다. 이전 요청·승인·거절·감사 이력을 보존한다.
- 새 요청은 최신 정확한 제안·필요한 결정·수정 위치를 제공한다. 같은 기준의 재관측·재시작은 요청이나 알림을 중복하지 않고 서로 다른 요청을 과거 Revision 편집으로 혼동하지 않는다.
- 완전한 새 사용자 결정만 같은 Worker session과 작업물에서 재개한다. 부분 배치·옛 승인·Agent 작성 결정·scope 위반은 적용하지 않으며 최종 base/patch 재확인·검증·Review·PR gate를 유지한다.

## Implementation

- guidance preapproval·실행 guidance·최종 scope 검사와 기존 승인 저장소에 request 생성/소비 lifecycle을 연결한다. 최신 기준 fingerprint와 기존 확정 결정을 별도로 보존한다.
- R-069 parser의 정규화 identity를 사용하되 기존 결정의 의미를 추정해 새 승인으로 이식하지 않는다. 변경된 파일과 pending 자료를 보존한 채 사용자 대기로 전환한다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- guidance·scope 재승인 테스트에서 기준/patch/mode 변경, line ending만 변경, 반복 요청, 배치 일부 승인, 중단·재시작·최종 rebase의 최소 unit case를 실행한다.
- Manager 전체 verify는 정확한 새 patch 적용, 과거 승인 비적용·이력 보존, 거절·대체·독립 차단과 같은 session 재개를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 기준·요청·최종 적용의 durable 전이로 complexity high, 승인 없는 프로젝트 지침 적용 영향으로 risk critical다.
