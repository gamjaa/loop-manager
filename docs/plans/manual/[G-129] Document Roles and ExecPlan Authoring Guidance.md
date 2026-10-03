---
id: G-129
title: Document Roles and ExecPlan Authoring Guidance
sources:
  - docs/PRODUCT.md
depends_on: []
complexity: low
risk: low
priority: normal
---

## Purpose

사용자가 승인한 문서 역할 정리와 ExecPlan 작성 지침 보완을 직접 수행한다. README는 사용 안내, PRODUCT는 현재 제품 규칙, ARCHITECTURE는 기술적 보장 방식, Specification Skill은 문서 작성 기준을 담당하도록 정리한다. 현재 구현 계약과 실행 권한을 유지하며, 후속 요청의 복구 변경은 Requirement로 정리해 Loop Manager가 구현하도록 남긴다.

## Acceptance Criteria

- README·PRODUCT·ARCHITECTURE의 중복과 역할 혼합을 줄이고, 기존 조건·예외·실패·검증 의미를 보존한다.
- 현재 PRODUCT와 코드에 의해 대체된 설명을 현행 동작에 맞게 정리하고 미구현 설계와 구분한다.
- Specification Skill에 문서별 질문·작성 방향·의미 보존 검토를 명시한다. AGENTS.md와 다른 작업의 ExecPlan은 변경하지 않는다.
- 일반·유지보수 Planner에 동일한 ExecPlan 작성 기준을 전달한다. mode별 기존 변경 권한·목표 보존·protocol과 Worker·Reviewer prompt는 유지한다.
- README는 Loop Manager 자체의 사용자 이해·사용 안내로 한정한다. 다른 프로젝트로 배포하는 AGENTS·PLANS·Skill·템플릿은 README 없이 필요한 작업 규칙과 문서 작성 기준을 제공한다.
- 공통 문서는 대상 프로젝트의 파일과 제공된 작업 문맥으로 판단할 수 있어야 한다. 후속 복구 요청의 계획별 차단·원격 자동 복구·로컬 재검사 명령은 요구사항으로만 확정하고 실행 코드·명령을 구현하지 않는다.
- 후속 diff 의견의 원격 입력·진행 기록 의미, 계획 삭제 복구, 유지보수 우선, branch 관여 범위, 기존 PR 충돌 복구, planning 오류의 독립 실행과 연관 문서 수정 역할을 명확히 한다. 현행 코드 설명과 구현 예정 요구사항을 구분한다.

## Implementation

- 현재 문서의 중복 정책은 소유 절로 모으고 기술적 적용 조건을 ARCHITECTURE로 이동한다.
- 기존 Skill의 문서 경계·품질 기준을 좁게 보완한다.
- agent_prompts.py의 일반·유지보수 Planner에 공통 작성 지침을 한 번씩 연결한다.
- 공통 Skill에서 README와 Loop Manager 자체에 관한 설명을 제거하고 필요한 규칙은 대상 프로젝트의 문서와 제공된 지침에 유지한다. 템플릿의 README 참조·Requirement archive 안내와 PLANS의 일반 Planner snapshot 예외를 기존 AGENTS 규칙에 맞춘다.
- R-055의 개별 오류 격리와 R-060의 다음 정상 실행 자동 복구 조건을 보완한다. 로컬 환경 수정 후 제한된 재검사 명령은 R-062에 작성한다.
- ARCHITECTURE의 모호한 문구와 PLANS의 연관 문서 역할을 보완한다. 사용자 계획 삭제·planning 오류의 독립 진행은 R-055, 사용자 branch 처리 범위는 R-060, Worker 문서 연동 수정과 Planner ExecPlan 개정은 R-061에 반영하고 R-054의 오류 조건을 맞춘다.

## Validation

- 기존 prompt·유지보수 mode·Skill 배포 관련 targeted unit test만 실행한다.
- 이전 prompt와 새 prompt를 대조해 작성 지침 추가 외에 기존 권한·입력·protocol이 바뀌지 않았는지 확인한다.
- 문서 이동 원문·역할·상대 링크·Markdown 구조와 git diff --check를 확인한다.
- 전체 verify·독립 Review·사용자 Merge는 수행 결과로 주장하지 않는다.

확인 결과: 관련 기존 unit test 3개 통과, Planner prompt 10개 조합에서 작성 지침을 제외한 이전 문자열 일치, Worker·Reviewer prompt 일치, 이동한 상세 조건 52개와 문서 링크 검증 통과. Skill 배포 테스트는 Windows의 WSL launcher 대신 Git Bash 절대 경로를 선택한 임시 실행기로 확인했으며 테스트 파일은 변경하지 않았다. Skill 전용 검증기는 PyYAML 부재로 실행하지 못했고 frontmatter와 템플릿 참조는 표준 라이브러리로 직접 검증했다. 실제 새 ExecPlan 작성 품질은 배포 후 생성 결과로 확인해야 한다.

README 경계 추가 검증: 공통 파일 배포·등록·미작성 템플릿 거부 관련 기존 unit test 3개 통과. README 없는 PowerShell 임시 배포에서 AGENTS·PLANS·Skill·템플릿의 원본 일치와 Skill 참조의 유효성을 확인했다. AGENTS는 이전과 바이트 단위로 동일하다. 기본 `core.autocrlf=true` 테스트 환경에서는 Git blob과 checkout의 LF/CRLF 차이로 등록 해시 비교가 실패해, 임시 검증 프로세스에만 `core.autocrlf=false`를 적용했다. 사용자 Git 설정·등록 코드·테스트 파일은 변경하지 않았으며 기본 설정에서의 등록 성공을 주장하지 않는다.

복구 요구사항 후속 검증: 공통 배포 문서 6개의 README 참조 부재, 제공된 문맥 기준과 Skill의 기존 품질 지침·템플릿 경로를 확인했다. 활성·archive Requirement 62개의 ID 고유성, R-062의 템플릿 절 구조와 변경 문서의 상대 링크·공백 검사를 통과했다. AGENTS는 이전과 바이트 단위로 동일하다. 이 단계에서는 문서만 수정했으며 복구 코드·명령의 구현이나 실행 결과를 주장하지 않는다.

diff 의견 후속 검증: catalog의 missing-plan 판정, planning 오류 gate, 기존 PR의 Merge 가능 여부·충돌 복구와 Agent의 독립 Git ref 제외 코드 경로를 확인했다. 의견 8개의 조건, 변경 Requirement의 절 구조, 상대 링크·UTF-8·공백과 Requirement ID 고유성 검사를 통과했다. 이 후속 작업에서 AGENTS·PRODUCT·Planner 코드는 수정 전과 바이트 단위로 동일하다. 코드 실행·운영 복구는 수행하지 않았다.

## Progress

- [x] 문서와 현재 prompt·검증 경계 조사
- [x] 문서 정리와 작성 지침 반영
- [x] targeted 검증과 의미 보존 대조
- [x] README 경계와 공통 배포 문서 독립성 확인
- [x] 공통 문서의 외부 문맥 의존 설명 제거와 복구 요구사항 정리
- [x] ARCHITECTURE·PLANS diff 의견 8개 반영과 연관 요구사항 정합성 보완

## Decision Log

- 2026-10-03: 사용자는 문서 역할에 맞는 기존 문서 정리와 향후 작성 방향의 명시를 요청했다. 추가 답변으로 Planner 프롬프트 작성 지침 보완도 승인했다. 이 파일은 직접 작업의 기록이며 자동 실행 catalog 밖의 manual 경로에 보존한다. 새로운 제품 기능·정책이나 Requirement를 추가하지 않는다.
- 2026-10-03: 사용자는 README를 Loop Manager 자체의 사용자 이해 문서로 한정하고, 다른 프로젝트에서 사용하는 공통 파일의 필요한 내용이 README 정리를 이유로 빠지지 않아야 한다고 명확히 했다. AGENTS의 기존 규칙은 유지하고 공통 Skill·PLANS·템플릿의 경계와 일치를 보완한다.
- 2026-10-03: 사용자는 README의 메타 설명 제거, 공통 문서의 대상 프로젝트 밖 문맥 의존 제거, 계획별 차단·원격 자동 복구·로컬 환경 복구 명령을 요청했다. 범위 질문에 요구사항·문서 정리 후 Loop Manager가 구현하도록 답했다. 기존 R-055·R-060에서 빠진 조건만 보완하고 명령은 R-062로 작성하며, 현재 README·PRODUCT·ARCHITECTURE에 구현된 기능으로 기재하지 않는다.
- 2026-10-03: 사용자는 ARCHITECTURE·PLANS의 diff 의견 8개를 제시했다. 연관 문서 수정은 Worker가 담당하고 Planner는 영향받는 ExecPlan을 개정한다는 답변을 받았다. 구현이 필요한 정책은 기존 Requirement에 보완하고 ARCHITECTURE에는 구현 예정임을 명시한다. PLANS는 제공된 권한과 snapshot 안에서 문서 정합성을 유지하는 역할을 설명하며 AGENTS·코드·다른 작업의 계획은 변경하지 않는다.
