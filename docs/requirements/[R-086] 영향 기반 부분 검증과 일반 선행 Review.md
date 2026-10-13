# [R-086] 영향 기반 부분 검증과 일반 선행 Review

## Context

Worker 교정마다 전체 verify를 반복하면 Reviewer가 거부할 후보에도 긴 검증 비용이 발생한다. Manager가 모델 없이 관련 회귀 테스트를 먼저 검사하고 일반 선행 Review를 수행해 이 비용을 줄인다. Worker의 R-003 targeted test 제한과 전체 검증·게시·복구 계약은 유지한다.

문서 병행 수정이나 영향 없는 정적 분석 표식만으로 부분 검증을 일괄 거부하면 일반 작업에서도 이 경로를 사용할 수 없다. 테스트 실행 입력과 검토 입력을 구분하고, 관계를 해소할 수 있는 불확실성은 대상을 넓혀 처리하며 범위를 확인할 수 없는 경우에는 기존 fallback을 유지한다.

## Requirement

- 첫 지원 범위는 Loop Manager 자체 저장소의 Python unittest adapter다. 부분 verify·일반 선행 Review 경로는 기본 사용하며 `[loop] verify_partial = false`로 끌 수 있다. 옵션 생략 시 true이며, 안전한 선정·검토 증거를 확인하지 못하면 기존 전체 verify → Reviewer로 fallback한다.
- Worker 완료 뒤 기존 commit·최신 base reconcile로 준비한 실제 후보와 base의 diff를 사용한다. 변경 전·후 전체 Python AST 그래프의 직접·역방향 간접 의존성으로 테스트 모듈을 고르고, 변경된 테스트를 포함한다. 삭제·rename·이동의 기존 소비자도 확인하고 실제 discovery manifest ID로 정규화한다. 파일명 유사도와 AI 선정은 사용하지 않는다.
- 코드와 함께 변경한 계획·Requirement·설계 등 문서는 검토 입력과 테스트 실행 입력의 역할을 구분한다. 현재 내용·소비 경계에서 검토 입력으로만 쓰이는 문서는 테스트 선정의 일괄 거부 사유로 삼지 않는다. 해당 문서와 전체 diff는 선행 Review·최종 승인 identity에 그대로 포함한다. 확장자·디렉터리나 verify 입력 목록 밖이라는 사실만으로 테스트 비영향을 인정하지 않으며, 테스트가 읽는 문서·fixture·template, 실제 지침·실행 설정·검증 설정 또는 역할을 확인하지 못한 비Python 입력은 필요한 관련 테스트를 증명하지 못하면 fallback한다. 문서-only 작업에서 테스트 대상이 없거나 R-082 별도 문서 처리가 진행 중이면 기존 경로를 유지한다.
- 불확실성은 참조 종류·확인한 대상 범위·선정에 미치는 영향으로 구분한다. 정적으로 확인한 모듈·심벌 이름이나 유한한 모듈 집합은 실제 관계로 반영하고 해당 소비자를 포함한다. 단순 reflection 함수·subprocess 이름의 존재만으로 저장소 전체를 거부하지 않되, 모듈 선택과 무관하거나 변경 대상 밖이라는 근거가 있어야 한다. 임의 문자열의 import·module registry 조회·실행 시 선택, 파싱 실패·미해결 내부 참조 등 숨은 소비자의 범위를 끝내 확인하지 못하면 전역 fallback을 유지한다. 현재 역방향 그래프에 관계가 없다는 사실만으로 영향이 없다고 판단하지 않는다.
- 공개 API·공통 테스트 지원 코드 변경은 변경 전후의 소비자와 fixture 범위를 확인할 수 있으면 해당 테스트를 넓혀 선정한다. 공통 진입점을 통해 재노출된 API는 단순 import 연결과 실제 사용 관계를 구분할 수 있는 근거가 있을 때만 과대 선정을 줄인다. import 초기화·부작용·동적 접근 등 남은 관계를 누락하지 않으며, 구분할 수 없으면 원래 전체 의존성 범위 또는 fallback을 유지한다. 실행 단위는 계속 manifest의 테스트 모듈이며 함수·라인별 테스트 선택을 추가하지 않는다.
- Git·SQLite·상태·권한·복구·게시의 핵심 공통 경계, 초기화·진입점과 수집·runner·interpreter·guard·종료·예약·검증 설정 변경은 범위를 확인하지 못하면 fallback한다. 단순 경로 단어로 일반 보조 모듈까지 일괄 거부하지 않고 실제 보호 경계와 판정 근거를 식별한다. 불충분한 삭제/이동 분석·대상 없음은 fallback이며, 위험 때문에 범위를 증명하지 못한 결정과 선택 비용이 큰 결정의 사유를 구분한다. 넓은 선택에 대한 기준은 실제 부분/전체 실행 비용과 선정 규모로 평가하며 부분 모드에서 전체 테스트를 실행한 뒤 전체 verify를 반복하지 않는다.
- 부분 verify는 Agent 밖에서 기존 project interpreter·guard·프로세스 종료 확인·serial/independent·자원 예약을 사용한다. 전체 discovery와 fixture·skip 의미를 유지하고 선택 모듈의 결과 누락·중복·identity 오류를 성공으로 인정하지 않는다. 미선택 모듈은 NOT_SELECTED로 구분한다. timeout·취소·종료 불명은 기존 안전 정책을 따른다.
- 부분 결과에는 candidate/base, manifest·선택 모듈/테스트 ID, 선정/fallback 이유·규모, 알고리즘/설정 identity, 시간·실패 진단을 남긴다. 추가한 소비자·해소한 불확실성·영향 밖으로 판단한 근거와 남은 불확실성을 구분해 설명할 수 있어야 한다. 분류·선정 규칙의 변경은 identity에 반영해 오래된 선택·선행 승인을 재사용하지 않는다. 부분 성공은 전체 verify token/receipt 또는 verified_head를 만들지 않는다.
- 부분 FAIL은 기존 verify 교정·반복 차단으로, 선행 Review FAIL은 기존 ISSUE_ID·실패 감사·Worker level 교정으로 연결한다. 선행 FAIL이면 전체 verify를 호출하지 않는다. 부분 PASS와 선행 PASS 뒤에는 실제 전체 verify가 필요하며 전체 FAIL은 기존 교정 정책을 따른다.
- 일반 선행 PASS는 독립된 후보 증거다. 동일 작업 주기·HEAD·base·전체 diff·ExecPlan·Requirement·PRODUCT·ARCHITECTURE·사용자 결정·실제 Reviewer instruction/provider/model/effort·필요 실행 환경/정책 identity와 유효한 현재 전체 verify receipt를 모두 확인한 경우에만 추가 모델 호출 없이 최종 승인한다. identity 부재·변경·중단·원격/rebase 경쟁·증거 불충분은 기존 최종 Reviewer 또는 안전한 전체 경로로 처리한다. 게시 직전 최신 ref·lease·ownership·dependency·사용자 결정 gate를 유지한다.
- R-082 문서 변경 선행 Review의 PASS/VERIFY_REQUIRED는 이 일반 선행 증거와 구분하며 최종 승인 근거로 재사용하지 않는다. G-183의 마지막 최종 승인 재사용과 G-241의 전체 verify 생략은 별도 작업이다. 일반 선행 Review는 전체 verify를 생략하지 않는다.
- 실제 실행 기록에서 부분/전체 시간·선택 규모·부분 PASS 이후 전체 FAIL·Review FAIL로 미실행한 전체 검증·Reviewer 호출/토큰·Worker 교정/총시간·fallback 사유를 비교한다. 미측정 시간 절감·토큰 절감을 합성하지 않고 동일 환경/범위를 확인한다.

## Acceptance Criteria

- 직접·간접 참조, 변경 테스트, 삭제/rename/이동의 기존 소비자 선정과 후보 변경 거부를 검증한다. API·공통 support의 소비자를 확인할 수 있는 경우는 범위를 넓혀 실행하고, 범위를 증명할 수 없는 경우는 실행 없는 fallback이다.
- 코드와 검토 전용 문서를 함께 수정하면 코드 기준으로 부분 대상을 선정하고 문서 bytes·전체 diff는 승인 입력에 보존한다. 동일 확장자의 테스트 fixture·지침·설정 입력은 구분하며, 역할 부재·문서-only 대상 없음·R-082 별도 처리는 기존 경로를 유지한다. 선행 승인 후 문서 변경은 승인 재사용을 거부한다.
- 정적으로 확인되는 import 별칭·literal 대상·유한한 module 집합과 모듈 선택에 무관함을 증명한 reflection/외부 명령은 일괄 거부하지 않는다. 숨은 동적 소비자가 그래프 밖에 있거나 대상 범위를 알 수 없는 경우, parse 실패·미해결 내부 참조·전체 범위만 확인되는 경우에는 좁은 PASS를 만들지 않는다. 해소·제외·fallback 근거를 진단에서 확인한다.
- 공통 진입점을 통한 재노출에서 확인한 실제 사용 관계만 정밀화하고 초기화·부작용·미해결 동적 관계는 보존한다. 직접 API 사용·별칭·재노출·지원 코드 경유·import 자체의 의존성을 비교해 필요한 회귀 모듈을 빠뜨리지 않는다. 실제 선정 테스트 모듈/ID는 기존 manifest와 일치한다.
- 핵심 공통 경계·검증 설정·대상 없음·넓은 선택의 fallback과 알고리즘/분류 identity 변경에 따른 과거 선택·승인 거부를 검증한다. 선정 개선과 비용 기준 조정의 효과를 따로 보고하며 임의의 절감률을 완료 기준으로 삼지 않는다.
- 실제 manifest identity와 선택 모듈의 필수 결과·fixture skip/실패·timeout/취소/종료 불명·자원 정책을 검증하며 부분 성공과 전체 receipt가 분리된다.
- 부분 FAIL/선행 FAIL은 Worker로 돌아가고 선행 FAIL 이후 전체 verify 호출 수는 0이다. 선행 PASS 후 실제 전체 성공과 동일 입력에서는 Reviewer 호출 1회로 게시 승인에 도달한다.
- HEAD/base/diff/명세/지침/profile/정책 변경·identity 부재·재시작/원격/rebase 경쟁에서 오래된 승인으로 게시하지 않는다. 전체 FAIL 뒤 교정, 문서 선행 Review·수동 제출/waiver·사용자 결정·게시 경계의 회귀를 확인한다.

## Product Impact

Manager의 부분 검증은 Review 전 회귀 검사이며 최종 보장은 별도 전체 verify와 유효한 Review 승인 및 게시 gate가 제공한다.

## Non-Goals

- Coverage·AI/이력 추천·함수/라인 선택·다른 언어/프레임워크, Worker 테스트 제한 완화·중복 제거, 전체 verify 생략, Review/verify 병렬 실행, G-250 병렬 최적화 중복 구현.

## Open Questions

- 없음.
