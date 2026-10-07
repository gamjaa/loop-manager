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

사용자의 직접 요청으로 현재 작업 branch에서 프로젝트 본문 축약과 공통·역할별 지침 분리를 진행했다. 자동 실행 시 최신 Merge된 코드·본문·주입 근거를 먼저 대조하고 남은 정리만 수행한다. 이미 반영된 본문 제거를 반복하거나 직접 작업을 자동 Worker의 승인 기록으로 간주하지 않는다.

## Acceptance Criteria

- 새 runtime의 공통·역할별 원본 묶음이 고정되고 각 지원 역할/provider에 공통+해당 역할 instruction으로 실제 전달되는 근거를 먼저 확인한다. 합친 파일은 저장소 밖 실행용이며 커밋 대상이 아니다. 주입 기능·사전 승인 기능이 Merge되지 않았거나 증거가 부족하면 자동 Worker가 기존 본문을 추가 제거하지 않는다.
- 루트 AGENTS.md에 추가 변경이 필요한 경우에만 Worker가 제거/유지할 정확한 project diff를 제안하고 사용자 승인·거절·대체를 기존 절차로 기다린다. 이미 게시된 정확한 승인은 대상·기준·patch가 유효할 때만 재사용하며 직접 대화의 요청을 자동 승인 입력으로 이관하지 않는다. 최신 본문이 이미 기준을 만족하면 중복 patch·승인 요청을 만들지 않는다.
- 자동 Worker의 추가 정리는 이 저장소의 관리 worktree와 별도 PR에만 적용한다. 프로젝트 전용 규칙과 Requirement 우선 원칙을 보존하고 직접 구현 전 ExecPlan 선작성 의무를 새로 부과하지 않는다. 다른 등록 프로젝트·기본 checkout의 본문을 자동 제거하지 않는다.
- 최종 diff·주입 기준·정리 이유와 검증 결과를 확인할 수 있고 사용자 거절/미결정이면 기존 본문과 작업물을 보존한다.

## Implementation

- 기존 `agents/common_guidance.py`의 runtime 원본 묶음·역할별 합성·호출 snapshot과 guidance 승인·scope 경계를 재사용한다. 최신 루트 본문에 실제 남은 변경이 있을 때만 Worker가 정확한 제안과 빈 User Decision을 작성하고 승인 전에 중단한다. Planner·Reviewer는 루트 AGENTS patch를 제안하거나 적용하지 않는다.
- 유효한 사용자 결정 후 필요한 문서/설정 안내만 같은 프로젝트 규칙에 맞춘다. 기존 docs/AGENTS.common.md·역할별 파일과 호출 병합을 재사용하고 모든 역할 규칙을 common에 다시 모으거나 새로운 루트 파일·독립 지침 배포 경로를 만들지 않는다. 역할 지침 파일의 일반 문서 변경과 루트 AGENTS의 정확한 승인 대상을 구분한다.
- 자동 역할·권한·검증은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·설계·계획 작성은 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- Worker는 기존 호출 테스트를 재사용해 공통+해당 역할 합성·고정 원본 재사용·저장소 밖 실행용 파일과 남은 루트 변경의 승인 scope 관련 최소 targeted unit case만 실행한다. 이미 정리된 본문에는 중복 patch나 pending 결정을 만들지 않는지 확인한다. 문서 전용이면 실행 가능한 관련 unit test가 없는 이유를 결과에 남긴다.
- Manager 전체 verify는 기존 프로젝트 보존·등록, runtime 고정 원본 묶음과 역할별 instruction 계약을 검증하며 추가 루트 변경이 있으면 승인된 exact diff도 확인한다. 실제 provider 확인은 테스트 밖의 확인된 호출 근거로 남기고 테스트는 Agent를 mock한다. 직접 작업 branch의 변경이나 mock 통과를 자동 Review·사용자 Merge·실제 provider 전달의 완료로 기록하지 않는다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- R-064가 명시한 별도 후속 정리다. 현재 Planner는 승인안이나 User Decision을 작성하지 않는다. 난도는 한 프로젝트의 제한된 정리, 위험은 필수 지침 손실로 분류했다.
- 2026-10-07 사용자 직접 요청에 따른 로컬 지침 분리를 반영했다. 자동 후속은 최신 Merge 결과와 실제 전달 근거에서 남은 차이만 확인한다. 이 개정은 기존 ID·sources·dependency·실행 상태를 유지하며 직접 작업의 허락을 자동 Worker의 승인 token이나 완료 근거로 만들지 않는다.
