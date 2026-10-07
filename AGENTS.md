# Loop Manager 프로젝트 지침

이 파일은 Loop Manager 저장소의 개발 규칙이다. 자동 Agent의 역할·권한·승인·검증
절차는 runtime의 공통·역할별 `docs/AGENTS.*.md`를 합쳐 모델 호출에 별도로 전달한다.

## 작업 기준

문서 작성·검토 기준은 [Specification Skill](.agents/skills/specification-docs/SKILL.md)을 따른다.
`docs/PRODUCT.md` 또는 활성 Requirement 중 하나는 반드시 존재해야 한다.

## 코드와 검증

- Python 표준 라이브러리 기반 CLI다. 진입점은 `loop_manager.py`, 기능별 구현은
  `loop_manager_internal/`, 테스트는 `tests/`에 둔다.
- 기존 기능별 모듈과 공유 helper를 재사용하고, 진입점에 새로운 책임을 계속 누적하지 않는다.
- 변경 범위에 맞는 `unittest`를 실행한다. 특정 테스트 파일은
  `python3 -m unittest discover -s tests -p 'test_<module>.py'`로 선택할 수 있다.
  문서·설정만 변경했거나 실행 가능한 관련 unit test가 없으면 그 사유를 결과에 명시한다.
- 전체 검증 진입점은 Linux·macOS의 `bash scripts/verify.sh`와 Windows의
  `pwsh -File scripts/verify.ps1`이다. 자동 작업에서는 공통 지침의 역할별 검증 책임을 따른다.
