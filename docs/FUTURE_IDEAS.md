# 후속 아이디어

## 관측 개선

- 빈 `core.excludesFile` 값이 현재 snapshot에서 worktree 루트의 내용 hash로 해석되는 경로를 검토한다. R-070의 Windows 복원 unit 진단에서 확인했다. Git의 빈 설정 의미와 기존 snapshot 호환성을 함께 검증한 뒤 ignore-source 관측을 정리하며, 현재 PR에서 감시 범위를 임의로 줄이지 않는다.

## Claude 실환경 후속 점검

일반 호출의 공식 문서·mock 기반 개발을 마친 뒤 실제 환경을 사용할 기회가 생기면 확인한다.
아래 미확인은 현재 Claude 지원 구현의 선행·완료 조건이나 독립 Codex 작업의 차단 사유가 아니다.

- 실계정 인증·갱신·credential store, 자동 실행 공통 홈과 개인 설정 source 분리, 신규·재개·fallback session의 실제 호환성을 확인한다. R-085의 후속 확인과 함께 다룬다.
- Planner·Worker·Reviewer의 실제 허용 쓰기와 source·다른 worktree·shared Git 보호, 파일 도구·Bash/하위 프로세스의 제한 및 자동 승인 검토·권한 확대 거부를 확인한다.
- 이용 가능한 OS·CLI·모델/계정 조합의 지원 범위와 오류·중단·재개를 확인하고 문서/mock 결과와 구분해 기록한다.
- 실행 중 조직 managed 정책 변경의 적용 순서·도구 실행 사이 제한 유지·감지/중단 가능성을 후속 조사한다. 현재는 감시·적용 전 차단·지속 제한 보장을 구현하지 않으며 별도 hook·polling·외부 격리 도입을 요구하지 않는다.
