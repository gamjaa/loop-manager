---
id: G-182
title: 동일 입력의 verify 성공 재사용과 rebase 후 선택
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-072] Reuse Successful Candidate Results with Proven Inputs.md
depends_on:
  - G-181
  - G-166
complexity: high
risk: high
priority: high
---

## Purpose

실제 verify 입력이 동일한 후보만 성공 결과를 재사용하고 충돌 없는 rebase/SHA 변경만으로 Worker를 재호출하지 않는다.

## Acceptance Criteria

- Manager가 먼저 안전한 rebase를 시도한다. 충돌 없이 완료된 SHA 변경만으로 완료한 targeted test·Worker를 반복하지 않는다. 별도 구현/verify/Review 교정 사유·불완전 rebase·충돌은 기존 Worker/session 경로를 따른다.
- 같은 프로젝트·ExecPlan·작업 주기의 실제 전체 verify 성공과 유효한 입력 fingerprint를 durable receipt로 저장한다. 새 후보의 파일 bytes/종류/mode·dirty/untracked·목록/명령/설정/환경·실제 Git/외부 입력이 증명된 경우에만 전체 verify를 생략한다.
- 목록 부재/오류/누락 의심·동적 입력 비교 불가·변경 입력·성공 기록 없음은 전체 verify를 실행한다. 실행/관측/재사용 판정 중 입력 변화와 새 실패는 과거 성공으로 덮지 않는다.
- 중단/재시작·receipt 저장 실패·후보 경쟁에서 낡은 성공을 현재 후보에 확정하지 않는다. reuse의 원래 run·입력·이유를 남기며 새 invocation이나 미측정 절감치를 만들지 않는다.

## Implementation

- execution의 rebase·revision 무효화와 verification handler·checkpoint 저장 경계를 연결한다. 입력 관측·receipt CAS·실제 검증 실행의 책임을 분리하고 G-181의 목록 계약을 사용한다.
- 현재 검증 timeout·종료·실패 피드백·교정 예산을 보존한다. SHA만 비교하는 cache 또는 이전 테스트 결과로 현재 실패를 덮는 fallback을 만들지 않는다.
- 공통 경계는 AGENTS.md, docs/ARCHITECTURE.md와 docs/PLANS.md를 참조한다. 기존 helper·관련 테스트를 재사용하고 지정 루트 경로를 보존한다.

## Validation

- tests/test_execution_revision.py, test_publication_base_validation.py와 verification handler 테스트에서 conflict-free rebase, Git 의존 검증, 목록/환경/dirty 변경·실행 중 변경·실패·재시작의 최소 unit case를 실행한다.
- Manager 전체 verify는 생략/필수 실행의 실제 호출 수, session·targeted 교정, receipt 경쟁과 최종 후보 gate를 검증한다.

## Progress

- [x] 2026-10-05 planning 기준 a049a76의 명세·원본·관련 테스트와 비종료 catalog에서 미충족 범위를 확인함. 구현·테스트 실행은 하지 않음.
- [ ] 구현 및 관련 targeted unit 검증
- [ ] Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 후보 채택·검증 input·성공 기록의 결합 때문에 complexity high, 거짓 검증 성공의 영향으로 risk high다. Review 재사용은 G-183에서 별도 판단한다.
