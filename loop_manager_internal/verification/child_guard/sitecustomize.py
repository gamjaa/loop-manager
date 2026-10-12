"""테스트가 시작한 Python 자식에도 같은 Agent/API 차단을 적용한다."""
import os
if os.environ.get("LOOP_MANAGER_TEST_NO_AGENTS") == "1":
    try:
        from pathlib import Path
        import runpy
        # 다른 worktree를 import하는 자식의 package 선택을 선점하지 않는다.
        runpy.run_path(str(Path(__file__).resolve().parent.parent / "guard.py"))["install_guard"]()
    except BaseException:
        # Python otherwise reports sitecustomize errors and continues without protection.
        os.write(2, b"verify Python guard installation failed\n")
        os._exit(1)
