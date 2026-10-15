import atexit
from functools import wraps
from functools import lru_cache
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import loop_manager
from unittest import mock
from loop_manager_internal.verification.timing import phase
from loop_manager_internal.git.git_adapter import github_repository as _parse_github_origin


def git_test_environment():
    """사용자 설정과 template을 제외한 테스트 subprocess 입력."""
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith('GIT_CONFIG_') and key.upper() != 'GIT_CONFIG'
                   and key.upper() != 'GIT_TEMPLATE_DIR'}
    # 통제 template에는 git/ignore·attributes가 없어 XDG 기본 입력도 차단한다.
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_NOSYSTEM='1',
                       GIT_ATTR_NOSYSTEM='1',
                       XDG_CONFIG_HOME=str(Path(__file__).with_name('git-template')),
                       GIT_TEMPLATE_DIR=str(Path(__file__).with_name('git-template')))
    return environment


def isolated_git_test(cls):
    """setUp부터 cleanup까지 운영 adapter와 Python 자식도 같은 입력을 상속한다."""
    setup = cls.setUp

    @wraps(setup)
    def isolated_setup(self):
        patch = mock.patch.dict(os.environ, git_test_environment(), clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        setup(self)

    cls.setUp = isolated_setup
    return cls


def git_command(repo: Path, *arguments: str, env=None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        text=True,
        encoding="utf-8",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=git_test_environment() if env is None else env,
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
        env=git_test_environment(),
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
        "docs/requirements/Active.md": "# [R-001] Active\n",
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
        with phase("git_fixture:seed_initialization"):
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
    with phase("git_fixture:independent_copy"):
        shutil.copytree(seed / "remote.git", remote)
        shutil.copytree(seed / "repo", repo)
        git_command(repo, "init", "--initial-branch=main")
        git_command(repo, "remote", "set-url", "origin", str(remote))
    return repo


def commit_and_push(repo: Path, message: str) -> None:
    git_command(repo, "add", "-A")
    git_command(repo, "commit", "-m", message)
    git_command(repo, "push", "origin", "HEAD:main")


def local_github_origin(test, repo, *, read_git=git_command):
    """로컬 bare 원격만 등록 GitHub identity로 정규화한다."""
    origin = read_git(repo, 'remote', 'get-url', 'origin')
    previous = getattr(loop_manager.github_repository, 'fixture_origins', {})
    origins = {**(previous if isinstance(previous, dict) else {}), origin: 'fixture/project'}
    def github_repository(url):
        return origins[str(url)] if str(url) in origins else _parse_github_origin(url)
    github_repository.fixture_origins = origins
    def adapter_origin(url):
        return loop_manager.github_repository(url) if str(url) in origins else _parse_github_origin(url)
    for target, replacement in (('loop_manager.github_repository', github_repository),
                                ('loop_manager_internal.git.git_adapter.github_repository', adapter_origin)):
        patch = mock.patch(target, new=replacement)
        patch.start()
        test.addCleanup(patch.stop)
    return 'fixture/project'


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
