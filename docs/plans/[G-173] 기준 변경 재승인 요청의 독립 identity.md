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
risk: high
priority: high
---

## Purpose

R-066·R-069에 따라 새 파일 재승인 요청을 생성·적용하는 기존 목표를 철회한다. 이 작업의 남은 범위는 과거 파일 승인 대기를 일반 작업·복구 흐름으로 전환하고, 기존 결정·감사·작업물·session을 보존하는 것이다. 이미 구현된 복구와 문서 정리를 먼저 대조해 필요한 교정만 수행한다.

## Acceptance Criteria

- 대상 내용·mode·patch·Baseline의 변경을 이유로 파일 승인 request identity나 pending 결정을 새로 만들지 않는다. Manager의 모델 호출·계획 검증·Worker 결과·verify·rebase·게시에서 별도 지침 읽기·변경 제안·승인 patch 대조·승인 대기를 요구하지 않는다.
- 과거 파일 승인 양식의 존재·형식 오류만으로 작업을 막지 않으며, 저장된 요청·결정·revision·patch·Comment·메모·감사와 사용자가 편집한 문서를 삭제·변환·덮어쓰지 않는다. 과거 승인을 현재 권한이나 적용 patch로 사용하지 않는다.
- 기존 파일 승인 대기는 대상 지침 내용을 재검사하거나 문서·대상 파일을 자동 수정하지 않고 일반 작업·복구 흐름으로 전환한다. 같은 계획의 branch·worktree·Worker session·작업물·실패 이력을 보존하며, 일반 사용자 판단 대기와 다른 차단은 사용자 대신 해제하지 않는다.
- 선택 자원의 identity·소유권·실제 범위 위반과 진행 중 Git operation을 확인하고 필요한 복구·검증·Review·PR gate를 유지한다. 과거 Agent snapshot이나 격리 성공을 합성해 복구 근거로 삼지 않는다.
- `docs/PLANS.md`와 호출 안내에 폐지된 파일 승인·재승인 기능의 새 양식·예시·필수 입력을 추가하지 않는다. 이미 최신 계약을 따르는 문서·코드는 재구현하지 않고 일반 User Decision과 고정된 공통·역할별 instruction 계약을 유지한다.

## Implementation

- `loop_manager_internal/documents/guidance_service.py`의 `resume_retired_guidance_block`과 기존 일반 복구·scope·checkpoint 경계를 재사용한다. 이미 작성된 재승인 구현이 있다면 최신 명세와 충돌하는 부분만 정리하고, 새 승인 저장소·request lifecycle·patch 적용 경로를 만들지 않는다.
- 일반 사용자 판단과 폐지된 파일 승인 대기를 구분해 필요한 복구만 연결한다. 과거 형식은 읽기 호환·감사 용도로 보존하고 G-172의 파서를 새 승인 소비 경로로 되살리지 않는다. 소유권·범위·독립 차단 확인이 실패하면 해당 작업과 진단을 보존한다.
- `docs/PLANS.md`와 실제 호출·검증·게시 안내를 최신 R-066·R-069와 대조하고 잔존 특별 계약만 제거한다. 기존 User Decision과 과거 기록을 일괄 수정하지 않으며, 이미 충족된 부분은 기존 구현·검증 근거를 확인하고 중복 변경하지 않는다.
- 자동 역할·권한·검증은 고정된 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md instruction을 사용하고, 설계·계획 작성은 docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 프로젝트 지침 해석은 Agent CLI에 맡기며 현재 실행의 고정 instruction과 일반 파일 권한을 유지한다.

## Validation

- Worker는 `tests/test_execution_guidance.py`와 `tests/test_execution_evidence_free_blocked_scope.py`의 기존 fixture를 재사용해 과거 승인 비적용·형식 오류의 비차단·기록 보존·일반 대기 전환의 관련 최소 unit case를 실행한다. 변경 범위에서 필요한 경우에만 회귀 case를 보완한다.
- 중단·재시작과 관리 rebase 전후에 같은 작업물·session·결정·감사가 보존되고, 대상 지침 읽기·새 승인 요청·patch 적용 없이 복구되는지 확인한다. 실제 소유권·선택 자원 위반과 일반 사용자 결정·독립 차단은 계속 적용돼야 한다.
- Manager 전체 verify와 읽기 전용 Reviewer는 일반 복구·최종 검증·Review·게시 gate를 확인한다. 과거 승인으로 권한을 합성하거나 기록을 삭제·변환하는 변경은 blocking issue로 판단하며, 문서 개정이나 기존 코드 존재만으로 실행·검증·Merge 성공을 기록하지 않는다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [x] 2026-10-07 ad5450f의 G-172 Merge diff와 R-069, guidance 무효화·재개 코드 및 테스트를 정적으로 대조함. 새 요청 lifecycle과 docs/PLANS.md 정합성은 남아 있으며, 이전 planning 결과의 PRODUCT/G-172 기준 변경을 최신 원격 입력으로 재검토함.
- [ ] 현행 범위 대조·필요한 교정 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-09 사용자 요청에 따른 목표 조정: 최신 R-066·R-069의 별도 승인 제거를 유지한다. 아래 과거 재승인 lifecycle·파서·양식 결정은 당시 감사 기록이며 현재 구현 지시를 대체하지 않는다. ID·경로·기존 title·dependency·실행 이력을 보존하고, 현재 목표를 과거 대기의 일반 복구와 기록 보존으로 한정했다. 이 개정은 일반 User Decision 작성·해제, SQLite 상태 변경이나 구현·검증·Merge 완료를 의미하지 않는다.

- 2026-10-07 기준 94e9e0b0825d3bd2cbee5e75aac06f9f2a335e0f 및 새 mutable READY token 5b96f9d2d8d50ffcdf25f4829238a01aa03f6a52846d0435d8b315ce6ad09aa3의 원본 hash f348adbe662fdaf4c2a4447e6f662795a932a4c5fba7e0dcd60eacf965beea74를 재확인했다. 추가 PRODUCT/R-077 변경은 수동 자원 준비와 후속 순서이며 승인·요청 목표를 대체하지 않는다. 앞선 허용 개정을 보존하고 PRODUCT·G-172 및 User Decision은 수정하지 않았다.

- complexity high: 과거 승인 identity를 유지하면서 새 요청 생성·사용자 편집·중단 복구·최종 base 재확인을 연결하는 durable 전이 판단이 남아 있다. risk high: 옛 승인 재사용이나 편집본 손실은 승인 계약과 후속 실행을 잘못 진행시킬 수 있고 원본 Git 내용·승인 감사에 따른 수동 복원이 필요하다. 이 범위에서 비가역 손실이나 소유권 밖 쓰기를 새로 허용하지 않으므로 critical로 분류하지 않는다.
- mutable READY token 9843ff4e2fce9d360eb7cf3a46aeeeeae6307ef8303d801a65e8197db34e0a6c와 원본 hash를 대조해 같은 ID·sources·dependency·목표를 보존했다. R-069가 명시적으로 대체한 Revision/자기 승인 규칙과 미반영 docs/PLANS.md 안내를 기존 재승인 책임에서 함께 마무리하며 기존 `_published_plan_allows_common_plans`의 직접 Acceptance Criterion 경계를 사용한다. User Decision은 작성하거나 변경하지 않았다.
