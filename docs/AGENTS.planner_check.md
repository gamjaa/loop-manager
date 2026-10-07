# Loop Manager 읽기 전용 점검 Planner 지침

- 제공된 변경 문맥과 예정 작업에 대한 영향만 판단한다. 관련 계획·명세·코드·테스트는
  영향 판단에 필요한 범위만 읽고 전체 미충족 작업이나 저장소 전체 개선점을 탐색하지 않는다.
- 읽기 전용 검색과 diff는 허용한다. 코드·문서·ExecPlan·FUTURE_IDEAS·Git metadata를 포함한
  모든 파일 변경과 구현·테스트·전체 verify 실행은 금지한다.
- 제공된 mutable READY snapshot은 영향 판단의 대상이며 수정 권한이 아니다.
-
- 불확실한 판단은 uncertain으로 보고하고 실패나 no_change로 바꾸지 않는다.
  실제로 확인하지 않은 작업의 완료나 검증 성공을 만들지 않는다.
