"""예제 설정과 독립된 Agent profile 테스트 설정."""
from pathlib import Path
from contextlib import contextmanager
import os
import tempfile
from unittest import mock


def use_empty_provider_home(test_case):
    """인증 외의 테스트가 개인 인증 파일·링크 권한에 의존하지 않게 한다."""
    directory = tempfile.TemporaryDirectory()
    test_case.addCleanup(directory.cleanup)
    test_case.enterContext(mock.patch.dict(os.environ, {
        'HOME': directory.name, 'USERPROFILE': directory.name,
    }))


@contextmanager
def fake_provider_home():
    """명령·정책 테스트의 인증 입력과 외부 ACL 관측을 고정한다."""
    with tempfile.TemporaryDirectory() as directory, \
         mock.patch.dict(os.environ, {'HOME': directory, 'USERPROFILE': directory}), \
         mock.patch('loop_manager_internal.agents.windows_runtime.assert_private_directory'):
        yield


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
