---
id: G-174
title: 공통 지침 주입 후 프로젝트 AGENTS 본문 정리
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-064] 모델 호출을 통한 공통 Agent 지침 전달.md
  - docs/requirements/[R-066] AGENTS 변경 사전 승인.md
  - docs/requirements/[R-069] User Decision 경계와 입력 양식.md
depends_on:
  - G-147
  - G-173
complexity: medium
risk: high
priority: normal
---

## Purpose

G-147의 실제 Merge·runtime 주입 확인 뒤 이 저장소의 중복 공통 본문을 정확한 사용자 결정에 따라 별도 작업에서 정리한다.

## Acceptance Criteria

- 새 runtime의 공통 원본·고정 버전이 모든 지원 역할/provider에 실제 전달되는 근거를 먼저 확인한다. 주입 기능·사전 승인 기능이 Merge되지 않았거나 증거가 부족하면 기존 본문을 제거하지 않는다.
- 현재 AGENTS에서 제거/유지할 정확한 diff를 Worker가 제안하고 사용자 승인·거절·대체를 기존 절차로 기다린다. 이미 게시된 정확한 승인은 대상·기준·patch가 유효할 때만 재사용하며 포괄적 허락을 승인으로 간주하지 않는다.
- 승인된 정리는 이 저장소의 관리 worktree와 별도 PR에만 적용한다. 프로젝트 전용 규칙과 Requirement 우선 원칙을 보존하고 직접 구현 전 ExecPlan 선작성 의무를 새로 부과하지 않는다. 다른 등록 프로젝트·기본 checkout의 본문을 자동 제거하지 않는다.
- 최종 diff·주입 기준·정리 이유와 검증 결과를 확인할 수 있고 사용자 거절/미결정이면 기존 본문과 작업물을 보존한다.

## Implementation

- G-147의 runtime 원본·호출 snapshot과 기존 guidance 승인·scope 경계를 재사용한다. Worker만 정확한 제안과 빈 User Decision을 작성하고 승인 전에 중단한다. Planner·Reviewer는 AGENTS patch를 제안하거나 적용하지 않는다.
- 유효한 사용자 결정 후 필요한 문서/설정 안내만 같은 프로젝트 규칙에 맞춘다. 본문 이동을 명목으로 새로운 루트 파일이나 지침 배포 방식을 만들지 않는다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- Worker는 주입 확인·최소 AGENTS 생성·승인 scope 관련 변경 경계의 최소 targeted unit case만 실행한다. 문서 전용이면 실행 가능한 관련 unit test가 없는 이유를 결과에 남긴다.
- Manager 전체 verify는 승인된 exact diff, 기존 프로젝트 보존·등록, runtime 고정 지침과 Worker/Reviewer 계약을 검증한다. 실제 provider 확인은 테스트 밖의 확인된 호출 근거로 남기고 테스트는 Agent를 mock한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-064가 명시한 별도 후속 정리다. 현재 Planner는 승인안이나 User Decision을 작성하지 않는다. 난도는 한 프로젝트의 제한된 정리, 위험은 필수 지침 손실로 분류했다.
