import atexit
from functools import lru_cache
import shutil
import subprocess
import tempfile
from pathlib import Path
import loop_manager


def git_command(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return result.stdout.strip()

def _initialize_remote_repository(directory: str) -> Path:
    parent = Path(directory)
    remote = parent / "remote.git"
    repo = parent / "repo"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    repo.mkdir()
    git_command(repo, "init", "--initial-branch=main")
    git_command(repo, "config", "user.email", "loop-manager@example.com")
    git_command(repo, "config", "user.name", "Loop Manager Test")
    git_command(repo, "remote", "add", "origin", str(remote))

    files = {
        "AGENTS.md": "# Agents\n",
        "README.md": "# Readme\n",
        "config/settings.ini": "setting=true\n",
        "docs/ARCHITECTURE.md": "# Architecture\n",
        "docs/PLANS.md": "# Plans\n",
        "docs/PRODUCT.md": "# Product\n",
        "docs/notes.md": "# Notes\n",
        "docs/requirements/Active.md": "# Active\n",
        "docs/requirements/archive/Old.md": "# Old\n",
        "src/app.py": "VALUE = 1\n",
        "tests/test_app.py": "def test_app(): pass\n",
    }
    for relative_path, contents in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    git_command(repo, "add", "-A")
    git_command(repo, "commit", "-m", "Initial")
    git_command(repo, "push", "-u", "origin", "main")
    return repo

@lru_cache(maxsize=1)
def _remote_repository_seed():
    """프로세스 안에서 재사용할 초기 저장소를 필요할 때 준비한다."""
    directory = tempfile.TemporaryDirectory(prefix="loop-manager-test-seed-")
    try:
        _initialize_remote_repository(directory.name)
    except BaseException:
        directory.cleanup()
        raise
    atexit.register(directory.cleanup)
    return directory


def make_remote_repository(directory: str) -> Path:
    """초기 상태를 복사하고 각 테스트의 실제 저장소를 독립시킨다."""
    seed = Path(_remote_repository_seed().name)
    parent = Path(directory)
    remote = parent / "remote.git"
    repo = parent / "repo"
    shutil.copytree(seed / "remote.git", remote)
    shutil.copytree(seed / "repo", repo)
    git_command(repo, "init", "--initial-branch=main")
    git_command(repo, "remote", "set-url", "origin", str(remote))
    return repo


def commit_and_push(repo: Path, message: str) -> None:
    git_command(repo, "add", "-A")
    git_command(repo, "commit", "-m", message)
    git_command(repo, "push", "origin", "HEAD:main")

def advance_remote_file(directory: str, repo: Path, relative_path: str,
                        contents: str, message: str) -> str:
    upstream = Path(directory) / f"upstream-{message.replace(' ', '-')}"
    git_command(Path(directory), "clone", str(repo.parent / "remote.git"), str(upstream))
    git_command(upstream, "config", "user.email", "upstream@example.com")
    git_command(upstream, "config", "user.name", "Upstream Test")
    target = upstream / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    commit_and_push(upstream, message)
    return git_command(upstream, "rev-parse", "HEAD")


def copy_manager_import_fixture(repo):
    """Copy the importable Manager tree, omitting derived bytecode."""
    shutil.copy(loop_manager.ROOT / "loop_manager.py", repo / "loop_manager.py")
    shutil.copytree(
        loop_manager.ROOT / "loop_manager_internal", repo / "loop_manager_internal",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")


def prepare_manager_import_fixture(repo):
    copy_manager_import_fixture(repo)
    commit_and_push(repo, "Add planner import fixtures")
