---
name: specification-docs
description: Create, review, and refine PRODUCT.md, ARCHITECTURE.md, requirements/*.md, and FUTURE_IDEAS.md through conversation with the user. Use when defining product behavior, documenting a requested change, recording technical design or deferred ideas, finding missing scenarios or edge cases, or deciding whether a specification is ready for implementation.
---

# Specification Docs

사용자와 대화하며 제품 문서를 작성하거나 검토한다. 명시적인 작업 모드를 요구하지 말고 요청과 기존 문서 상태에 필요한 절차만 수행한다.

## Templates

새 파일은 Skill 디렉터리를 기준으로 다음 템플릿을 읽는다. Requirement는 Summary·Context·Requirement·Acceptance Criteria를 유지하고, PRODUCT·ARCHITECTURE는 프로젝트에 필요한 절을 선택·통합·재구성할 수 있다.

- `../../../docs/PRODUCT.template.md`
- `../../../docs/ARCHITECTURE.template.md`
- `../../../docs/REQUIREMENT.template.md`

대상 프로젝트에 같은 템플릿이 있으면 대상 프로젝트의 파일을 우선한다. 기존 문서가 있으면 템플릿으로 덮어쓰지 않고 필요한 부분만 수정한다.

## Reserved Paths

이 Skill이 사용하는 `.agents/skills/specification-docs/`와 `SKILL.md`, `docs/PRODUCT.md`,
`docs/ARCHITECTURE.md`, `docs/FUTURE_IDEAS.md`, `docs/requirements/`와 그 아래 `archive/`,
세 문서의 `docs/*.template.md` 경로는 예약 경로다. Skill·템플릿·문서를 다른 위치로 옮기거나
이름을 바꾸어 조회 경로와 상대 참조를 깨뜨리지 않는다. 예약은 위치 고정이며 모든 파일의
생성 의무가 아니다. 문서 내용은 이 Skill의 작성 범위에서 갱신하고 Requirement의 archive
취급은 아래 Lifecycle을 따른다.

## Document Boundaries

| 문서 | 중심 질문 | 작성 방향 |
|---|---|---|
| PRODUCT | 제품이 무엇을 보장하는가? | 현재 보장해야 하는 동작·정책·제품 경계·상태·예외·불변식·완료 조건. 구현 세부와 작업 이력은 넣지 않는다. |
| ARCHITECTURE | 그 보장을 어떤 구조로 지키는가? | 현재 채택한 구조·기술 선택의 이유·책임 경계·정보 원본·데이터 형식·호환·외부 연동 계약·상태 전이·보존·격리·검증·복구. 제품 정책과 설치 안내는 반복하지 않는다. |
| Requirement | 이 기준에서 무엇을 왜 바꾸며 어떻게 확인하는가? | 한 변경의 목적·원하는 동작·검증 가능한 Acceptance Criteria. 불필요한 내부 설계·구현 계획·실행 상태는 넣지 않는다. |
| ExecPlan | 그 변경을 어떻게 구현하고 검증하는가? | 후속 실행 도구가 상위 명세를 구현·검증할 작업이며 이 Skill의 작성 대상이 아니다. |
| FUTURE_IDEAS | 지금 구현하지 않을 아이디어는 무엇인가? | 범위 밖 아이디어만 간결히 보존한다. 상세 구현 계획으로 확장하거나 현재 계획의 근거로 승격하지 않는다. |

사용자 요청·대화에서 확인한 의도와 기존 문서·코드를 바탕으로 다음 기준을 먼저 적용한다. 코드나 문서 파일의 유무만으로 초기 설계 여부나 설계 확정을 추정하지 않는다.

| 상황 | 작성 대상과 반영 시점 |
|---|---|
| 사용자와 최초 제품·기술 기준을 정립하는 중 | PRODUCT·ARCHITECTURE에 최초 기준을 작성한다. |
| 코드는 아직 없음 | 사용자가 설계를 계속 다듬으려는지, 변경 목표를 별도 Requirement로 관리하려는지에 따라 작성 대상을 정한다. 설계 기준을 확정했다는 사실만으로 Requirement 작성을 강제하지 않는다. |
| 기존 코드는 있지만 설계 문서가 없음 | 현행 동작·설계의 문서화와 새 변경 요구를 분리하고, 새 변경은 Requirement로 기록한다. |
| 구현된 제품에 기능·정책 추가 또는 변경 | Requirement만 작성·갱신하고 PRODUCT·ARCHITECTURE는 읽기 전용으로 참고한다. 설계 문서 갱신은 실제 구현 변경을 반영하는 단계에서 수행한다. |

기존 구현의 명확한 기능·정책 변경은 Requirement로 작성한다. 위 기준으로도 설계 반영과 Requirement 작성 중 어느 쪽인지 불명확하고 활성·archive 어디에도 Requirement가 없을 때만 설계 문서 반영을 기본 선택지로 제시해 사용자에게 확인한다. 이때 PRODUCT·ARCHITECTURE는 제품·설계 기준을 정의·유지하고, Requirement는 원하는 변경·Acceptance Criteria를 기록해 ExecPlan의 근거가 된다는 차이를 요청에 맞춰 짧게 설명해 사용자가 기록 목적을 판단할 수 있게 한다. 이 기본값은 작성 대상 선택에만 적용하며 미결정 제품 정책의 승인으로 취급하지 않는다. 사용자가 이미 작성 대상을 명시한 경우에는 같은 확인을 반복하지 않는다.

Requirement로 기록할 변경은 관련 활성 Requirement를 먼저 찾아 갱신하며 별도 문서 작성 요청을 기다리지 않는다. 신규 작성 전 활성 Requirement 전체를 대상으로 제목·키워드뿐 아니라 변경 목적·제품 규칙·동작 시나리오·영향 범위를 비교한다. 여러 활성 Requirement가 관련되면 필요한 기존 Requirement를 함께 갱신한다. 관련 문서가 없거나 독립 목표를 결합하기 부적절할 때만 새 문서를 만든다. 조회 실패·내용 확인 불가는 관련 계획이 없다는 근거가 아니다. PRODUCT·ARCHITECTURE에 변경된 장기 규칙·설계·계약을 반영하는 일은 Requirement 작성 단계와 분리해 실제 구현 변경과 함께 수행한다. 구현 세부·실행 계획이나 상태를 Requirement에 넣지 않는다.

제품 정책과 설계는 사용자와 확정한 내용을 반영하고, 미결정 사항은 임의로 확정하지 않는다. 현재 기준은 사용자가 채택한 보장·설계이며 구현 오류를 정상 규칙으로 승격하지 않는다. 구현과 기준이 다르면 그 차이를 명시하고 미구현 목표를 현재 동작처럼 기록하지 않는다.

## Quality Guidance

기존 문서·템플릿을 먼저 확인한다. 갱신은 설명 추가보다 관련 절의 재작성·통합을 우선한다. 새 요구사항과 기존 조건을 하나의 설명으로 구성하고 반복 정책·같은 의미의 문장·분산된 예외·대체된 설계·문서 목적 밖 구현 세부·작업 이력을 정리한다.

첫 문단과 각 절은 해당 질문의 답부터 제시한다. 정책의 조건·예외·결과는 소유 문서 한 절에 모으고 다른 곳은 요약·링크로 연결한다. 순서가 의미를 바꾸는 흐름만 단계로, 조건별 허용·거부·보존 결과는 표나 짧은 항목으로 쓴다. 현재 구조와 미구현 목표를 구분한다.

PRODUCT·ARCHITECTURE는 Planner와 사용자가 함께 읽는 기준이다. 제품이 무엇을 보장하고 어떤 조건에서 어떻게 동작하는지, 주요 구조를 왜 채택했는지 문서에서 파악해 사용자 의도에 맞는 방향인지 점검할 수 있게 쓴다.

ARCHITECTURE에는 현재 형상과의 호환에 필요한 데이터 형식·필수 필드·타입·허용 값·식별자·경로 규칙·버전 호환·오류 처리 결과를 해당 계약에 기록한다. 내부 구현 상세라는 이유로 필수 계약을 생략하지 않으며 함수·클래스·DB 컬럼 전체를 기계적으로 나열하지 않는다. 별도 상세 명세를 참조할 때도 핵심 의미와 정확한 절·위치 참조를 남긴다.

Context는 문제·목적, Requirement는 원하는 동작·정책·제약, Acceptance Criteria는 객관적인 충족 결과를 기록하며 세 절을 유지한다. Requirement와 Acceptance Criteria를 합치지 않는다. Product Impact는 지속할 제품 규칙을 별도로 식별할 때, Non-Goals는 범위 오해를 막을 때만 둔다. Open Questions는 작성 중 사용하고 확정 뒤 빈 절·해결된 질문을 제거한다. 비어 있거나 중복인 절과 참고 자료의 긴 복사를 강제하지 않으며 필요한 근거만 링크·경로로 연결한다.

Requirement를 새로 작성하거나 본문을 변경할 때는 제목 다음에 `## Summary`를 짧은 한 문단으로 작성·갱신한다. 변경 목적·원하는 변화와 영향을 받는 기능·데이터·권한 등 관련성 판단에 필요한 범위·핵심 조건·예외를 요약하며 본문에 없는 목표·설계·실행 상태를 추가하지 않는다. Summary는 관련 문서를 찾는 안내이고 정책·Acceptance Criteria의 원본은 본문이다. 관련성이 불명확하거나 요약이 없어 제목만으로 판단하기 어려우면 본문을 확인한다. 기존 Requirement를 요약 추가만을 위해 일괄 수정하지 않는다.

구현자가 제품 정책을 임의로 정하지 않아도 될 수준까지 명세하되 안전하게 결정할 기술 선택은 고정하지 않는다. 요청 밖 기능·확장 시나리오·설정·관리 도구를 요구사항에 추가하지 않고 필요한 사용자 결정 또는 FUTURE_IDEAS로 분리한다.

편집 전후의 적용 조건·판단 주체·제품 규칙/불변식·정상/실패/예외 흐름·권한/거부·상태 전이·데이터 보존/재개·Acceptance Criteria·설계 근거를 대조해 의미와 완전성을 보존한다. 길이·항목 수·토큰 수에 기계적인 제한을 두지 않는다.

## Write Scope

프로젝트의 다른 파일은 문맥 확인을 위해 읽을 수 있지만, 일반적인 Skill 작업에서 생성·수정·삭제할 수 있는 파일은 다음으로 제한한다.

- `docs/PRODUCT.md`
- `docs/ARCHITECTURE.md`
- `docs/requirements/` 바로 아래의 활성 Requirement
- `docs/FUTURE_IDEAS.md`

Requirement 작성·갱신 작업에서는 PRODUCT·ARCHITECTURE를 현재 상태를 확인하는 읽기 전용 근거로만 사용하며 함께 수정하지 않는다. 원하는 변경과 일치시키기 위해 두 문서를 선반영하지 않는다.

기본 판단표에 따른 설계 작성·보완, 사용자가 명시적으로 요청한 현행 문서의 작성·교정·정리 또는 Document Boundaries의 사용자 응답·기본값에 따라 설계 반영을 선택한 경우에는 별도 설계 작업으로 해당 범위의 설계 문서를 편집할 수 있다. 초기 설계를 제외한 설계 문서는 현재 제품 상태와 채택된 보장·설계를 나타내며 미구현 변경을 현재 동작으로 기록하지 않는다.

코드, 테스트, 설정, 템플릿, 다른 Skill 파일을 포함한 나머지 파일은 수정하지 않는다. 필요한 변경을 발견하면 이유와 대상만 사용자에게 알린다.

## Workflow

1. 대상 프로젝트의 `PRODUCT.md`·`ARCHITECTURE.md`와 `docs/requirements/` 바로 아래의 활성 Requirement를 기본 조회 대상으로 삼고 관련 템플릿을 확인한다. archive 본문 조회는 Requirement Lifecycle의 예외에 한한다.
2. 사용자 의도·정상/실패 시나리오·참고 자료와 기존 제품 규칙을 비교한다. 기존 문서·코드로 확인할 사실은 먼저 조사하고 확정된 사실·가정·사용자 결정을 구분한다.
3. Document Boundaries의 기본 판단표를 먼저 적용하고, 필요한 확인 뒤 Write Scope에 맞는 문서만 작성·갱신한다.
4. 주요 정상 흐름, 실패 흐름, 상태 전이, 권한, Product Invariant, Non-Goal과 edge case의 누락을 찾는다.
5. 제품 정책·동작 의미를 임의로 정하지 않는다. 구현을 막을 질문에는 배경·선택지·영향·합리적인 권장안을 간결하게 제공한다. 내부 기술 선택은 과도하게 질문하지 않는다. 확정 전 질문은 대화나 검토용 초안에서 관리하고 답변은 해당 본문에 통합한다. 중요한 미결정 사항을 해소하기 전에는 활성 Requirement로 저장·게시하지 않는다.
6. 편집 후 Quality Guidance에 따라 문서별 역할·링크·정합성과 의미 보존을 확인한다.
7. 구현 가능한 수준인지 Ready Check를 수행하고 남은 결정 사항을 짧게 보고한다.

사용자가 아이디어만 설명하면 초안을 작성한다. 기존 문서를 제시하면 검토와 보완을 우선한다. Open Question에 답하면 관련 부분만 갱신한다. 사용자가 누락 검토를 요청하면 파일을 불필요하게 다시 쓰지 않고 발견 사항을 먼저 설명한다.

## Requirement Lifecycle

새 Requirement를 만들기 직전에 프로젝트 루트에서 다음 명령을 실행하고 출력된 ID를 파일명과 첫 제목에 사용한다.
```sh
python .agents/skills/specification-docs/scripts/next_requirement_id.py
```
이 명령은 활성·archive Requirement의 원본 ID 중 최댓값 다음 ID를 한 줄로 출력한다. archive 디렉터리가 없으면 빈 집합으로 취급한다. 결과는 로컬 작성용 후보이며 예약이 아니므로 문서를 만들기 직전에 조회한다. Skill은 파일을 직접 나열해 번호를 계산하지 않는다. 조회가 실패하면 번호를 계산하거나 기존 문서에서 추정하지 말고 오류를 해결한다.

Requirement는 별도 상태 없이 제품에 원하는 변화와 Acceptance Criteria를 기록하는 순수 문서다. ID와 제목은 첫 `# [R-ID] 제목` 제목을 사용한다. 사용자 결정이 필요한 항목을 해소하고 Ready Check를 통과한 뒤 `docs/requirements/` 바로 아래에 저장한다. 파일 경로는 안정적인 식별자로 유지한다.

`docs/requirements/archive/`의 Requirement 본문은 다음 경우에만 필요한 문서·절을 제한적으로 읽는다.

- 사용자가 과거 결정의 이유나 요구사항의 변경 이력을 확인하려는 경우
- 과거 기능의 재도입·변경을 검토하며 당시의 조건·예외·제약을 확인해야 하는 경우
- 기존 문서나 ExecPlan이 참조하는 보관 문서의 의미를 확인해야 하는 경우

archive는 과거 근거이며 현재 정책이나 변경 목표로 자동 적용하지 않는다. 기본 탐색에 archive의 요약·본문을 일괄 포함하지 않으며, 문서 존재 여부 확인과 스크립트의 ID 조회는 본문을 판단 근거로 읽는 것과 구분한다. 현재 변경의 Requirement로 재사용하거나 수정·삭제하지 않고 활성 위치로 되돌리지 않는다. 보관 문서와 관련된 새 변경은 사용자 요청을 기준으로 새로운 고유 ID·경로의 Requirement를 작성한다. 보관 문서의 ID·경로를 다시 쓰거나 원문을 편집용 복사본으로 만들지 않는다. 이 Skill의 기존 Requirement 재사용·갱신은 활성 문서에만 적용한다.

명세 작성 중 활성 Requirement를 완료로 간주해 보관 위치로 옮기지 않는다.

## Ready Check

다음은 작성 완료 전 품질 검사다. 모든 항목을 문서의 별도 절로 추가하는 양식이 아니며 실제 변경에 필요한 조건만 확인한다.

- 주요 정상 흐름과 실패 흐름이 정의돼 있다.
- 중요한 상태와 허용되는 상태 전이가 정의돼 있다.
- 권한과 거부 조건이 필요한 경우 정의돼 있다.
- 핵심 Product Invariant와 Non-Goal이 분명하다.
- PRODUCT를 읽으면 정상·실패 상황에서 사용자에게 제공할 결과와 적용 조건·제품 경계를 판단할 수 있다.
- ARCHITECTURE를 읽으면 그 결과를 보장하는 책임 경계·정보 원본·호환 계약·복구 방식과 주요 설계 이유를 파악할 수 있다.
- 현재 구현과 채택된 기준이 다른 경우 그 차이가 드러나며 미구현 목표가 현재 동작으로 표현되지 않는다.
- 현재 형상과의 호환에 필요한 데이터·연동 계약의 필수 형식과 실패 처리 결과를 본문 또는 정확한 상세 명세 참조로 확인할 수 있다.
- Requirement에는 검증 가능한 Acceptance Criteria가 있다.
- 새로 작성하거나 변경한 Requirement의 Summary가 본문과 일치하고 관련성 판단에 필요한 범위를 담는다.
- 구현자가 임의로 제품 정책을 정해야 하는 미결정 사항이 없다.
- Requirement가 바꾸는 기존 규칙과 유지할 계약을 구분할 수 있고, 의도한 변경으로 설명되지 않는 명세 충돌이 없다. 변경 전후의 차이를 없애려고 PRODUCT·ARCHITECTURE를 선반영하지 않는다.
- 대상 프로젝트의 문서와 제공된 작업 지침에서 필요한 규칙과 판단 조건을 확인할 수 있다.

명시적인 문서 작성 요청은 해당 사용자 명세를 함께 작성하는 위임으로 취급한다. 기본 판단표에 따라 Requirement로 기록할 변경 요청은 관련 Requirement 작성·갱신의 위임으로 취급한다. 이 Skill 단계에서는 명세를 작성하며 코드 구현을 함께 수행하지 않는다.
