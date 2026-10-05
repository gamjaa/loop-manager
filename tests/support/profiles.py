"""예제 설정과 독립된 Agent profile 테스트 설정."""
from pathlib import Path
import tempfile


def profile_config(test_case):
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    config = Path(directory.name) / "config.ini"
    config.write_text(
        "[codex.models]\n"
        "fast = test-fast\nbalanced = test-balanced\nstrong = test-strong\n"
        "[codex.roles]\nplanner = strong,xhigh\nreviewer = strong,high\n"
        "planner_check = fast,max\nmaintenance_planner = strong,xhigh\n"
        "[codex.worker]\n"
        "level1 = fast,medium\nlevel2 = fast,high\nlevel3 = fast,max\n"
        "level4 = balanced,high\nlevel5 = balanced,xhigh\nlevel6 = strong,high\n"
        "level7 = strong,xhigh\nlevel8 = strong,xhigh\n"
        "[claude.models]\n"
        "strong = claude-opus-test\nbalanced = claude-sonnet-test\nfast = claude-haiku-test\n"
        "[claude.roles]\nplanner = strong,high\nreviewer = strong,high\n"
        "planner_check = fast,medium\nmaintenance_planner = strong,high\n"
        "[claude.worker]\nlevel1 = fast,medium\n"
        "level2 = balanced,low\nlevel3 = balanced,medium\nlevel4 = balanced,high\n"
        "level5 = strong,medium\nlevel6 = strong,high\nlevel7 = strong,xhigh\n"
        "level8 = strong,max\n",
        encoding="utf-8",
    )
    return config
