---
id: G-202
title: 수동 제출 Planner의 공통 입력과 판정 기준
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-052] Worker Classification and Initial Level Calibration.md
  - docs/requirements/[R-077] Manual Plan Work Start and Completion.md
depends_on:
  - G-208
  - G-209
  - G-210
  - G-211
  - G-212
  - G-213
  - G-215
  - G-216
  - G-217
complexity: low
risk: medium
priority: normal
---

## Purpose

수동 submit의 G 생성 Planner에도 기존 공통 작성 기준과 필요한 비종료 catalog만 전달한다. R-077의 직접 구현 추적용 계획은 유지하되, 새 호출 경로가 R-052의 complexity·risk 근거와 PRODUCT의 Planner 입력 경계를 빠뜨리지 않도록 한다.

## Acceptance Criteria

- 실제 수동 G 생성 호출의 공통+Planner 역할 instruction에서 남은 설계 난도와 실패 영향·복구 가능성을 분리하는 작성 기준을 확인할 수 있다. 파일 수·핵심 경로·Git/SQLite 자체로 high를 강제하지 않으며, 생성 계획의 본문에는 complexity·risk의 구체적인 근거가 있다. high·critical이면 관련 실패·복구가 Acceptance Criteria·Validation·Review에 연결된다. 정적 작성·품질 규칙을 사용자 prompt에 중복하지 않는다.
- 제출 commit의 R·구현 diff와 기존 제품·설계·PLANS·프로젝트 지침을 유지한다. 수동 추적용 G는 원래 R 전체의 완료 조건을 축소하거나 이미 Merge·검증됐다고 쓰지 않는다. 수동 제출은 일반 미충족 작업 계획과 구분하고 read-only 생성 계약을 유지한다.
- Planner의 기본 catalog 입력은 코드로 만든 비종료 compact 정보다. MERGED·archive 계획의 본문/metadata와 모든 기존 계획 전문을 일괄 전달하지 않는다. 충돌 방지를 위한 전체 ID·예약·취소·완료 이력 처리는 Manager에 유지하고, 필요한 비종료 계획 문맥만 근거 있는 범위로 전달한다.
- 기존 JSON 반환·고유 ID 예약·전체 R 연결·묶음 dependency 검증·무소유 branch 준비와 실패 보존을 유지한다. 빠진 판단·근거를 Manager가 성공 문구로 채우거나 자동 구현·Worker 호출로 대신하지 않는다.

## Implementation

- `planning/manual_plan_generation.py`의 기존 planner 호출이 `agents/invocation_service.py`·`agents/common_guidance.py`를 통해 고정된 `docs/AGENTS.common.md`+`docs/AGENTS.planner.md`를 instruction으로 받는 경계를 재사용한다. 작성 rubric·품질 안내는 이 원본을 사용하고 제거된 `EXECPLAN_AUTHORING_GUIDANCE`·`quality_guidance.py`를 다시 import하거나 사용자 prompt에 복사하지 않는다. 수동 JSON/read-only/제출 revision·계획 본문 근거 요청은 작업별 문맥으로 유지한다. 이미 연결된 지침 주입을 중복 구현하지 않는다.
- catalog의 기존 compact 직렬화 및 수동 제출 관계를 사용해 모델 입력을 제한한다. `application/manual_work.py`의 프로그램 ID 계산·등록 검증과 모델의 의미 판단을 분리하고 필요한 본문만 선택한다. 일반 Planner·Worker profile 산식과 R-052의 이미 Merge된 승격 구현은 변경하지 않는다.
- 자동 역할·권한은 docs/AGENTS.common.md와 해당 역할의 docs/AGENTS.*.md를, 프로젝트 규칙·계획 작성·Agent 실행 설계는 AGENTS.md·docs/ARCHITECTURE.md·docs/PLANS.md를 참조한다. 새 루트 파일, 판정 Agent, 설정 기본값 또는 범용 추상화를 추가하지 않는다.

## Validation

- Worker는 수동 generation 호출의 최소 unit case를 추가·실행해 실제 request의 공통+Planner instruction과 고정 버전 재사용, 사용자 prompt의 명세/diff revision·본문 근거 요청·MERGED·archive 전문 제외·관련 비종료 문맥·정적 규칙 중복 부재 및 read-only를 확인한다. 기존 호출·provider fixture를 재사용하고 외부 Agent는 test double로 대체한다.
- Manager 전체 verify는 생성 JSON과 여러 독립 G의 등록·internal dependency 거부·기존 작업 중복 거부·재시작의 동일 ID 보존 및 일반 Planner 입력·명시 profile·12개 최초 level 조합의 회귀를 확인한다. Reviewer는 근거 없는 분류·위험 시나리오 누락 및 과거 구현의 성공 합성이 없는지 확인한다.

## Progress

- [x] 2026-10-07 f2ae0811a0230045852743a5fae84c6a91579bb0에서 일반 작성 helper와 수동 generation prompt·본문 검증·기존 테스트를 대조했다. 구현·테스트 실행은 하지 않았다.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 2026-10-07 기준 `f2ae0811a0230045852743a5fae84c6a91579bb0`의 관찰: 일반 Planner는 `EXECPLAN_AUTHORING_GUIDANCE`를 사용하지만 수동 generation은 사용하지 않았다. 당시 수동 prompt와 PLANS 본문만으로는 R-052의 판정 근거·high 위험 시나리오를 요구하지 않으며 생성 검증은 절 제목 존재만 확인했다. 이 누락을 R-052 전체 fulfilled로 판단하지 않은 근거로 남긴다.
- 수동 generation의 `existing`는 base의 docs/plans 하위 모든 Markdown 전문을 읽고 `reserved`는 MERGED를 포함한 모든 row를 모델에 전달한다. ID 이력 검사는 코드에 남기고 최신 PRODUCT의 비종료 입력 경계를 같은 generation 책임에서 적용한다. 과거 R-001의 교체된 READY-first 정책을 되살리는 작업은 아니다.
- complexity low: 기존 prompt/catalog helper를 국소 호출 경계에 연결하며 새로운 상태 전이·모델 선택을 설계하지 않는다. risk medium: 잘못된 입력·판정은 추적 계획의 누락·분류 오류를 만들지만 기존 생성·dependency·게시 검증과 보존된 요청에서 교정할 수 있다. 직접 R-077 구현에 소급 ExecPlan이나 자동 검증 성공을 만들지 않는다.
- 2026-10-07 지침 분리 정합화: 현재 작업 branch에서는 작성 rubric을 AGENTS.planner.md로 옮겼고 수동 생성도 공통 호출 경계의 Planner instruction을 받는다. 남은 작업은 실제 전달·본문 근거의 일치와 비종료 catalog 제한을 확인·보완하는 것이다. 현재 branch의 지침 변경만으로 이 계획이나 R-052 전체를 완료 처리하지 않는다.
