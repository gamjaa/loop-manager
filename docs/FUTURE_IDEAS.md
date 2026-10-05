# 후속 관측 개선

- 빈 `core.excludesFile` 값이 현재 snapshot에서 worktree 루트의 내용 hash로 해석되는 경로를 검토한다. R-070의 Windows 복원 unit 진단에서 확인했다. Git의 빈 설정 의미와 기존 snapshot 호환성을 함께 검증한 뒤 ignore-source 관측을 정리하며, 현재 PR에서 감시 범위를 임의로 줄이지 않는다.
