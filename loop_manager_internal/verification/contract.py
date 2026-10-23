"""Versioned, explicit verify connection; no shell parsing or tool guessing."""
from dataclasses import dataclass
import json
from pathlib import Path

VERSION = 1
ADAPTERS = {
    "perl-tap": {
        "version": 1, "tool": "Perl 5.38.2 Test::More 1.302194 / TAP::Parser 3.44",
        "collection": "recursive file discovery; file IDs own dynamically planned TAP assertions",
        "reporter": "TAP::Parser native results and plan/exit/wait checks",
        "execution": "file processes; Perl fork/process/network boundaries disabled",
        "references": ["https://perldoc.perl.org/5.38.2/TAP::Parser", "https://perldoc.perl.org/5.38.2/Test::More", "https://perldoc.perl.org/5.38.2/perlsub"],
        "validation": "Perl 5.38.2/Linux targeted native parser tests; CORE/XS bypass and threaded tests unsupported; OS termination requires Manager validation",
    },
    "unittest": {
        "version": 1, "tool": "Python stdlib unittest", "python": "3.11–3.14",
        "collection": "TestLoader discovery and load_tests",
        "reporter": "TimedResult with fixture skip ownership and subtest events",
        "execution": "module processes; internal parallelism disabled",
        "references": ["https://docs.python.org/3.12/library/unittest.html"],
        "validation": "Local targeted tests: Python 3.12/Linux; other Python versions and OS process isolation require Manager validation",
    },
}


class ContractError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate verify key: {key}")
        result[key] = value
    return result


def inside(root, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ContractError("verify path must be relative to worktree")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ContractError("verify path escapes worktree")
    return path


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ContractError("invalid verify declaration fields")


@dataclass(frozen=True)
class Connection:
    root: Path
    adapter: str
    python: Path | None
    stages: tuple[dict, ...]
    tool: Path | None = None


def load(root, project_python=None):
    root = Path(root).resolve()
    path = root / "scripts/verify.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        if not path.resolve().is_relative_to(root):
            raise ContractError("verify declaration escapes worktree")
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_object)
        fields(data, ("version", "adapter", "environment", "stages"))
        if type(data["version"]) is not int or data["version"] != VERSION:
            raise ContractError("unsupported verify version")
        if not isinstance(data["adapter"], str) or data["adapter"] not in ADAPTERS:
            raise ContractError("unsupported verify adapter")
        fields(data["environment"], ("python",) if data["adapter"] == "unittest" else ("perl",))
        tool = None
        if data["adapter"] == "perl-tap":
            declared_tool = data["environment"]["perl"]
            if not isinstance(declared_tool, str) or not Path(declared_tool).is_absolute() or not Path(declared_tool).is_file():
                raise ContractError("Perl executable must be an existing absolute path")
            tool = Path(declared_tool)
        declared = data["environment"].get("python", "launcher")
        if declared == "launcher":
            python = Path(project_python).absolute() if project_python else None
        else:
            if not isinstance(declared, str) or not declared or Path(declared).is_absolute() or ".." in Path(declared).parts:
                raise ContractError("project interpreter must be relative to worktree or launcher")
            # Keep the venv executable path: resolving its symlink loses pyvenv.cfg.
            python = (root / declared).absolute()
        if python is not None and not python.is_file():
            raise ContractError("project Python interpreter is missing")
        stages = data["stages"]
        if not isinstance(stages, list) or not stages:
            raise ContractError("verify stages are empty")
        names = set()
        for stage in stages:
            fields(stage, ("id", "kind", "cwd"), ("argv", "collection", "execution", "suite"))
            name = stage["id"]
            if not isinstance(name, str) or not name or name in names:
                raise ContractError("duplicate or missing verify stage identity")
            names.add(name)
            if not inside(root, stage["cwd"]).is_dir():
                raise ContractError("verify cwd is missing")
            if stage["kind"] == "tests":
                fields(stage, ("id", "kind", "cwd", "collection", "execution"))
                fields(stage["collection"], ("directory", "pattern"))
                if not inside(root, stage["collection"]["directory"]).is_dir():
                    raise ContractError("test directory is missing")
                pattern = stage["collection"]["pattern"]
                if not isinstance(pattern, str) or not pattern or "/" in pattern or "\\" in pattern:
                    raise ContractError("invalid discovery pattern")
                if stage["execution"] not in ("independent", "serial"):
                    raise ContractError("invalid test execution condition")
            elif stage["kind"] == "command":
                fields(stage, ("id", "kind", "cwd", "argv"))
                argv = stage["argv"]
                if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item and "\0" not in item for item in argv):
                    raise ContractError("verify command requires a nonempty argv array")
            elif stage["kind"] == "runner-integration":
                fields(stage, ("id", "kind", "cwd", "suite"))
                if (data["adapter"] != "unittest" or stage["cwd"] != "."
                        or stage["suite"] != "loop-manager-v1" or stage is not stages[-1]
                        or sum(item.get("kind") == "runner-integration" for item in stages) != 1):
                    raise ContractError("invalid fixed runner integration stage")
            else:
                raise ContractError("unsupported verify stage kind")
        if not any(stage["kind"] == "tests" for stage in stages):
            raise ContractError("verify requires a collected test stage")
        return Connection(root, data["adapter"], python, tuple(stages), tool)
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError(f"invalid scripts/verify.json: {error}") from error


def validate_manifest(units):
    names, identities = set(), set()
    if not units:
        raise ContractError("empty collected manifest")
    for unit in units:
        if not isinstance(unit.name, str) or not unit.name or unit.name in names or not unit.ids or not all(isinstance(identity, str) and identity for identity in unit.ids) or len(unit.ids) != len(set(unit.ids)) or identities.intersection(unit.ids):
            raise ContractError("duplicate or empty collected identity")
        names.add(unit.name)
        identities.update(unit.ids)
