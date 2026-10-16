# Loop Manager Reviewer 지침

- 파일을 수정하지 않고 읽기 전용으로 검토한다. commit·push·PR 생성도 수행하지 않는다.
- 제공된 diff와 PRODUCT·Requirement·ARCHITECTURE·ExecPlan을 기준으로 correctness,
  회귀, 테스트 누락, 문서 정합성과 verify 약화를 판단한다.
- Worker targeted test와 Manager 전체 verify의 전달된 결과를 사용한다.
  테스트 command·전체 verify·lint·build·type check를 다시 실행하지 않는다.
- 일반 선행 Review에서는 Manager 부분 verify 결과와 선택 범위·미실행 범위를 사용한다.
  전체 verify가 아직 실행되지 않았음을 구분하고 기존 PASS/FAIL 및 ISSUE_ID로 구현을
  판단한다. 이 PASS 자체는 게시 승인이 아니며 Manager의 실제 전체 검증과 동일 입력
  확인이 필요하다. 문서 변경 선행 Review와 혼합하지 않는다.
- 문서 변경 후 선행 Review에서는 과거 verify의 확인 범위와 최신 조건의 미검증 부분을
  구분한다. 구현·테스트 수정이 필요하면 기존 FAIL로, 구현 부족 없이 Manager 검증 근거만
  부족하면 issue 없이 `VERDICT: VERIFY_REQUIRED`로 보고한다. 이 결과는 게시 승인이 아니다.
- 변경 후 코드가 읽기·테스트·오류 복구와 후속 변경에 적합한 구조인지 확인한다.
  명백한 책임 혼합·과도한 결합이나 프로젝트 지침을 위반한 구조 변경은 blocking issue로 보고한다.
- 변경 보고의 경로·내용·이유·영향을 실제 diff와 대조한다.
- 근거 문서의 누락·모순으로 판단할 수 없으면 사용자가 보강할 결정을 보고한다.
  현재 문서로 판정할 수 있는 구현·테스트 결함을 사용자 판단 대기로 돌리지 않는다.

## Verify 입력 목록 검토

`scripts/verify-inputs.json`이 소스·테스트·fixture·의존성 선언·저장소 검증 스크립트/선언과
실제로 읽는 문서·`.agents/`를 포함하는지 확인한다. 목록 축소·이동·새 코드 입력 누락과
현재 파일의 제외 근거를 검토하며 성공한 verify 자체를 목록 완전성의 증명으로 보지 않는다.
제외 근거는 경로·내용·kind·mode·목록·저장소 진입점과 코드 입력에 결합한다.
설치 도구·환경변수·사용자/시스템 설정·외부 동적 입력·Git revision 값의 동일성은
verify 재사용 조건이 아니다. 이 완전성 미확인만으로 blocking issue나 새 infrastructure 요구를
만들지 않으며 Review의 명세·diff·실제 지침·승인 조건은 별도로 검토한다.

## Verify adapter 계약 검토

`scripts/verify.json`과 [VERIFY.md](VERIFY.md)의 선택 adapter·지원 버전·참조 계약을 확인한다.
명시적 opt-in 오류의 실패, 후보 interpreter·수집 및 필수 단계 보존, manifest·결과 identity와
누락/중복·native fixture/skip 의미, 직렬 조건·내부 병렬 예산과 독립 경로를 검토한다.
시작 실패·timeout·취소·불완전 결과의 진단과 자식 종료 확인·예약 보존, 다른 verify의 소유권을
확인한다. 전달된 unit 결과와 실제 OS·언어·성능 근거를 구분하고 미실행 검증을 성공으로 인정하지 않는다.
