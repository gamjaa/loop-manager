# [R-086] 영향 기반 부분 검증과 일반 선행 Review

## Context

Worker 교정마다 전체 verify를 반복하면 Reviewer가 거부할 후보에도 긴 검증 비용이 발생한다. Manager가 모델 없이 관련 회귀 테스트를 먼저 검사하고 일반 선행 Review를 수행해 이 비용을 줄인다. Worker의 R-003 targeted test 제한과 전체 검증·게시·복구 계약은 유지한다.

## Requirement

- 첫 지원 범위는 Loop Manager 자체 저장소의 Python unittest adapter다. 실행 사이클은 명시적 opt-in이며 기본값은 기존 전체 verify → Reviewer다.
- Worker 완료 뒤 기존 commit·최신 base reconcile로 준비한 실제 후보와 base의 diff를 사용한다. 변경 전·후 전체 Python AST 그래프의 직접·역방향 간접 의존성으로 테스트 모듈을 고르고, 변경된 테스트를 포함한다. 삭제·rename·이동의 기존 소비자도 확인하고 실제 discovery manifest ID로 정규화한다. 파일명 유사도와 AI 선정은 사용하지 않는다.
- 동적 참조·미해결 관계, 공유 fixture/support, 초기화·진입점·공개 API, Git·SQLite·상태·권한·복구 공통 코드, 수집·runner·검증 설정 변경, 불충분한 삭제/이동 분석, 대상 없음·넓은 선택은 결정적인 규칙으로 보수적으로 fallback한다. 부분 모드에서 전체 테스트를 실행한 뒤 전체 verify를 반복하지 않는다.
- 부분 verify는 Agent 밖에서 기존 project interpreter·guard·프로세스 종료 확인·serial/independent·자원 예약을 사용한다. 전체 discovery와 fixture·skip 의미를 유지하고 선택 모듈의 결과 누락·중복·identity 오류를 성공으로 인정하지 않는다. 미선택 모듈은 NOT_SELECTED로 구분한다. timeout·취소·종료 불명은 기존 안전 정책을 따른다.
- 부분 결과에는 candidate/base, manifest·선택 모듈/테스트 ID, 선정/fallback 이유·규모, 알고리즘/설정 identity, 시간·실패 진단을 남긴다. 부분 성공은 전체 verify token/receipt 또는 verified_head를 만들지 않는다.
- 부분 FAIL은 기존 verify 교정·반복 차단으로, 선행 Review FAIL은 기존 ISSUE_ID·실패 감사·Worker level 교정으로 연결한다. 선행 FAIL이면 전체 verify를 호출하지 않는다. 부분 PASS와 선행 PASS 뒤에는 실제 전체 verify가 필요하며 전체 FAIL은 기존 교정 정책을 따른다.
- 일반 선행 PASS는 독립된 후보 증거다. 동일 작업 주기·HEAD·base·전체 diff·ExecPlan·Requirement·PRODUCT·ARCHITECTURE·사용자 결정·실제 Reviewer instruction/provider/model/effort·필요 실행 환경/정책 identity와 유효한 현재 전체 verify receipt를 모두 확인한 경우에만 추가 모델 호출 없이 최종 승인한다. identity 부재·변경·중단·원격/rebase 경쟁·증거 불충분은 기존 최종 Reviewer 또는 안전한 전체 경로로 처리한다. 게시 직전 최신 ref·lease·ownership·dependency·사용자 결정 gate를 유지한다.
- R-082 문서 변경 선행 Review의 PASS/VERIFY_REQUIRED는 이 일반 선행 증거와 구분하며 최종 승인 근거로 재사용하지 않는다. G-183의 마지막 최종 승인 재사용과 G-241의 전체 verify 생략은 별도 작업이다. 일반 선행 Review는 전체 verify를 생략하지 않는다.
- 실제 실행 기록에서 부분/전체 시간·선택 규모·부분 PASS 이후 전체 FAIL·Review FAIL로 미실행한 전체 검증·Reviewer 호출/토큰·Worker 교정/총시간·fallback 사유를 비교한다. 미측정 시간 절감·토큰 절감을 합성하지 않고 동일 환경/범위를 확인한다.

## Acceptance Criteria

- 직접·간접 참조, 변경 테스트, 삭제/rename/이동의 기존 소비자 선정과 후보 변경 거부를 검증한다. 불확실성·fixture·고위험/설정·대상 없음·넓은 선택은 실행 없는 fallback이다.
- 실제 manifest identity와 선택 모듈의 필수 결과·fixture skip/실패·timeout/취소/종료 불명·자원 정책을 검증하며 부분 성공과 전체 receipt가 분리된다.
- 부분 FAIL/선행 FAIL은 Worker로 돌아가고 선행 FAIL 이후 전체 verify 호출 수는 0이다. 선행 PASS 후 실제 전체 성공과 동일 입력에서는 Reviewer 호출 1회로 게시 승인에 도달한다.
- HEAD/base/diff/명세/지침/profile/정책 변경·identity 부재·재시작/원격/rebase 경쟁에서 오래된 승인으로 게시하지 않는다. 전체 FAIL 뒤 교정, 문서 선행 Review·수동 제출/waiver·사용자 결정·게시 경계의 회귀를 확인한다.

## Product Impact

Manager의 부분 검증은 Review 전 회귀 검사이며 최종 보장은 별도 전체 verify와 유효한 Review 승인 및 게시 gate가 제공한다.

## Non-Goals

- Coverage·AI/이력 추천·함수/라인 선택·다른 언어/프레임워크, Worker 테스트 제한 완화·중복 제거, 전체 verify 생략, Review/verify 병렬 실행, G-250 병렬 최적화 중복 구현.

## Open Questions

- 없음.
