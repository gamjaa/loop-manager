---
id: G-219
title: Worker Ponytail 원문 보존과 사용 안내
sources:
  - docs/PRODUCT.md
  - docs/requirements/[R-079] Worker Ponytail 지침 상시 주입.md
depends_on:
  - G-265
  - G-266
  - G-267
  - G-268
complexity: low
risk: low
priority: normal
---

## Purpose

상시 Ponytail 주입은 직접 Merge된 구현을 유지하고, 고정 upstream 원문 보존과 README의 실제 적용 안내에 남은 R-079 누락만 보완한다.

## Acceptance Criteria

- `docs/AGENTS.ponytail.md`가 기준 revision `552acd5efd0aeae2583a12efe39373d2f076f25e`의 upstream `AGENTS.md` 원문과 일치한다. 목록·문단·줄 배치를 임의 편집하지 않으며 MIT 라이선스·출처·기준 revision을 기존 소유 문서에서 확인할 수 있다.
- README 하단에서 저장된 원문으로 가는 유효한 `docs/AGENTS.ponytail.md` 상대 링크와 Worker에 항상 한 번 전달한다는 실제 정책을 확인할 수 있다. 설치 여부를 조회하거나 설치·활성 상태에 따라 생략한다고 설명하지 않는다.
- README는 현재 선택 runtime에서 매 Worker 호출에 적용한다는 설명을 간결하게 두고 출처·라이선스·감사 보존·실행 경계 상세를 ARCHITECTURE의 기존 Ponytail 절로 연결한다. 요구사항·안전·필수 검증·역할·게시 조건과 Plugin/App 비활성화·사용자 설정 보존을 유지한다.
- 원문 보완이 게시되고 새 runtime이 선택되면 진행 중인 작업의 다음 호출·재시도·재개·provider 전환에도 적용한다. 실행 중인 호출은 기존 안전 반환을 유지한다. 과거 version 1·version 2·Markdown snapshot, session·호출 해시·성공/실패 기록은 감사로 보존하며 실행 입력으로 재사용하거나 최신 값으로 소급 변경하지 않는다. 과거 snapshot의 누락·손상은 현재 호출을 막지 않으며 현재 원본·실행용 파일 오류는 모델 실행 전에 거부한다. 항상 주입·다른 역할 제외·Plugin 목록 조회 부재의 직접 구현은 유지한다.

## Implementation

- [ ] [고정 upstream 원문](https://github.com/DietrichGebert/ponytail/blob/552acd5efd0aeae2583a12efe39373d2f076f25e/AGENTS.md) 및 같은 revision의 LICENSE와 저장 파일을 대조해 원문 줄 배치를 보존한다. 최신 main으로 업데이트하거나 코딩 규칙을 새로 번역·확장하지 않는다.
- [ ] README의 기존 마지막 Ponytail 안내에 원문 상대 링크와 짧은 상시 적용 설명을 통합한다. 현재 ARCHITECTURE의 출처·라이선스·버전·실행 경계 설명을 재사용하고 본문 전체를 중복하지 않는다.
- [ ] 확인한 `agents/common_guidance.py`·`invocation_service.py`·`provider_guidance.py`의 현재 runtime 선택·역할별 전달, Worker 완료와 수동 후보의 전달 해시 반영을 보존한다. 원문 보완은 이 경로를 그대로 사용하며 Plugin 조회·캐시나 새로운 hash/snapshot 체계를 구현하지 않는다.

## Validation

- 원문·라이선스의 동일성을 고정 revision과 정적으로 비교하고 README 상대 링크·ARCHITECTURE anchor를 확인한다. 원문 instruction 데이터가 바뀌므로 Worker는 `tests/test_agent_invocation.py:CommonGuidanceInvocationTests`의 `test_ponytail_change_reaches_existing_and_new_tasks`·`test_provider_switch_reads_current_ponytail_and_records_each_delivery` 등 변경 관련 최소 case를 실행한다. 기존 작업도 현재 원문을 받고 과거 전달 해시·session은 보존돼야 한다. 문서 링크나 직접 구현된 지침 선택을 위해 새 테스트를 복제하지 않는다.
- 역할별 한 번 전달·다른 역할/사용자 prompt 제외·실제 전달 hash와 후보 token, 과거 snapshot 감사 보존/실행 미사용과 현재 원본·실행용 파일 오류 거부를 기존 invocation/state 회귀로 유지한다. Codex 신규/재개/fallback의 Plugin/App 비활성화·sandbox·자동 검토도 유지한다. Reviewer는 고정 revision 원문과 최종 파일 및 간결한 사용 안내를 대조한다.

## Progress

- [x] 2026-10-08 a3ce7f88cef4cd46b34e0ead46ec5472fb5bd6f7의 R-079·PRODUCT·ARCHITECTURE·원본·README와 실제 전달/해시 회귀를 정적으로 대조했다. 고정 upstream 원문·LICENSE를 읽었으며 구현·테스트·verify는 실행하지 않았다.
- [ ] 원문 보존 및 README 안내 보완
- [ ] 관련 targeted unit test·Manager 전체 verify·읽기 전용 Review·사용자 Merge

## Decision Log

- 현재 R-079/R-064는 원문 적용을 새 작업에 한정하지 않는다. revision `e5bddd3ac59afa446cce932f57d287d912b6c68b`의 호출별 지침 선택·과거 snapshot 실행 미사용은 직접 구현으로 소비하고, 이 작업에는 고정 upstream 원문·README 안내의 누락만 남긴다. 과거 Progress·감사는 보존하며 구현·테스트·verify 성공을 새로 기록하지 않는다.
- 고정 upstream 원문과 저장 파일의 지침 내용은 같지만 저장본에 목록 앞·뒤 빈 줄과 마지막 두 문장의 문단 결합이 있다. R-079가 요구한 원문 보존에 맞추며 다른 upstream revision을 채택하지 않는다. 현재 라이선스의 MIT 내용과 출처는 보존한다.
- 2feff70의 상시 주입·Plugin inventory 제거와 현재 tests/test_agent_invocation.py·test_agent_invocation_state.py·test_codex_native_invocation.py의 회귀가 실제 구현 근거다. 해당 직접 구현에 소급 ExecPlan이나 자동 실행 성공을 만들지 않는다.
- README는 561859f의 문서 정리 뒤 ARCHITECTURE 링크만 남아 원문 상대 링크와 상시 적용 설명이 빠졌다. 기존 소유 문서 링크를 유지하면서 R-079의 명시 조건만 복구한다.
- complexity low: 고정 원문과 기존 안내를 명확한 기준에 맞추는 변경이다. risk low: 새 규칙·provider·저장 형식을 추가하지 않고 기존 감사를 보존하며 현재 runtime 선택과 기존 hash 계약을 따른다. 저장 파일 diff로 복구할 수 있다.
