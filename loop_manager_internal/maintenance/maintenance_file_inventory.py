"""Deterministic source inventory from one verified remote Git tree.

Classification records a reason for every tracked path. Ambiguous paths stop an
assessment instead of disappearing from its coverage silently.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import sqlite3
from pathlib import Path, PurePosixPath
import re
import subprocess


class AssessmentError(ValueError):
    """The stored self assessment cannot be bound to this source tree."""


class SourceInventoryError(ValueError):
    """A remote tree or tracked file cannot be classified safely."""


@dataclass(frozen=True)
class TrackedSource:
    path: str
    content_hash: str
    mode: str
    revision: str
    reason: str


@dataclass(frozen=True)
class ClassifiedPath:
    path: str
    disposition: str
    reason: str


@dataclass(frozen=True)
class RootPlacement:
    """One revision-bound top-level placement decision for file assessment."""

    path: str
    category: str
    reason: str


@dataclass(frozen=True)
class SourceInventory:
    revision: str
    sources: tuple[TrackedSource, ...]
    classifications: tuple[ClassifiedPath, ...]


_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SOURCE_DIRS = {
    "src", "lib", "app", "cmd", "internal", "pkg", "modules", "components",
    "server", "client", "web", "frontend", "backend", "bin", "tools",
    "scripts", "test", "tests", "spec", "specs",
}
_EXCLUDED_DIRS = {
    "docs", "doc", "documentation", "archive", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", "build", "dist", "target",
    "coverage", ".next", ".gradle", "out", "generated", "node_modules",
    "vendor", "third_party", "external", ".venv", "venv", "site-packages",
}
_DEPENDENCY_DIRS = {"node_modules", "vendor", "third_party", "external", ".venv", "venv", "site-packages"}
_DOC_SUFFIXES = {".md", ".markdown", ".rst", ".adoc"}
_ASSET_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".pdf", ".zip", ".gz", ".woff", ".woff2", ".ttf", ".mp3",
    ".mp4", ".csv", ".tsv",
}
_CONFIG_SUFFIXES = {".ini", ".cfg", ".toml", ".yaml", ".yml", ".json", ".lock"}
_CODE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".cs", ".fs",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".swift",
    ".rb", ".php", ".ex", ".exs", ".erl", ".hrl", ".hs", ".lua",
    ".pl", ".pm", ".r", ".jl", ".dart", ".elm", ".clj", ".cljs",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".sql", ".html", ".css", ".scss", ".sass", ".vue", ".svelte",
    ".zig", ".nim", ".v", ".f90", ".f95", ".f", ".asm", ".s",
}
_BUILD_FILES = {
    "makefile", "gnumakefile", "justfile", "dockerfile", "cmakelists.txt",
    "build", "build.bazel", "workspace", "workspace.bazel", "package.json",
}
_DOCUMENT_NAMES = {"license", "license.txt", "notice", "notice.txt", "authors", "contributors"}
_ROOT_TOOL_PATHS = {".agents", "docs", "scripts", "AGENTS.md"}
_ROOT_EXECUTION_PATHS = {"config", "tests", ".github", ".gitignore"}
_ROOT_ENTRY_NAMES = {"readme", "license", "notice", "authors", "contributors"}


def _git(repo: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=False,
        )
    except OSError as error:
        raise SourceInventoryError("Git 명령을 시작할 수 없습니다.") from error
    if result.returncode:
        raise SourceInventoryError(
            f"Git 원본을 읽을 수 없습니다 ({args[0]}): "
            + result.stderr.decode("utf-8", "replace")[:300]
        )
    return result.stdout


def verify_remote_revision(repo: Path, base_branch: str, revision: str) -> None:
    """Require the caller's source to equal the currently observed origin ref."""
    if not _OID.fullmatch(revision) or not base_branch or base_branch.startswith("-"):
        raise SourceInventoryError("원격 source revision 또는 base branch가 유효하지 않습니다.")
    ref = f"refs/remotes/origin/{base_branch}"
    _git(repo, "check-ref-format", ref)
    actual = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    if actual.decode("ascii") != revision:
        raise SourceInventoryError("origin/base revision이 평가 source와 다릅니다.")


def _blob_fingerprint(repo: Path, oid: str) -> tuple[str, bytes, bool]:
    """Hash exact blob bytes while retaining only a small classification sample."""
    try:
        process = subprocess.Popen(
            ["git", "-C", str(repo), "cat-file", "blob", oid],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise SourceInventoryError("Git blob을 읽을 수 없습니다.") from error
    assert process.stdout is not None
    digest = hashlib.sha256()
    sample = bytearray()
    has_nul = False
    while chunk := process.stdout.read(65536):
        digest.update(chunk)
        has_nul = has_nul or b"\0" in chunk
        if len(sample) < 8192:
            sample.extend(chunk[:8192 - len(sample)])
    process.stdout.close()
    error = process.stderr.read(300) if process.stderr is not None else b""
    if process.stderr is not None:
        process.stderr.close()
    if process.wait():
        raise SourceInventoryError("Git blob 읽기에 실패했습니다: " + error.decode("utf-8", "replace"))
    return digest.hexdigest(), bytes(sample), has_nul


def _classify(path: str, mode: str, sample: bytes, has_nul: bool) -> tuple[str, str]:
    parts = PurePosixPath(path).parts
    lowered = tuple(part.lower() for part in parts)
    name = lowered[-1]
    suffix = PurePosixPath(name).suffix
    if mode == "160000":
        return "excluded", "Git submodule is an external dependency"
    if any(part in _DEPENDENCY_DIRS for part in lowered[:-1]):
        return "excluded", "external dependency directory"
    if any(part in _EXCLUDED_DIRS for part in lowered[:-1]):
        return "excluded", "documentation, archive, generated, or cache directory"
    document_stem = PurePosixPath(name).stem
    if ((document_stem in {"readme", "changelog"} and suffix in {"", ".txt", *_DOC_SUFFIXES})
        or name in _DOCUMENT_NAMES or suffix in _DOC_SUFFIXES):
        return "excluded", "documentation file"
    if suffix in _ASSET_SUFFIXES:
        return "excluded", "non-source asset or data format"
    if len(parts) == 1 and re.fullmatch(r"requirements(?:[-_][a-z0-9_-]+)?\.txt", name):
        return "excluded", "root dependency manifest"
    if mode not in {"100644", "100755"}:
        return "uncertain", f"unsupported Git file mode {mode}"
    if has_nul:
        return "uncertain", "binary blob outside a known asset or dependency location"
    if lowered[0] in {".agents", ".codex"}:
        return "excluded", "agent metadata directory"
    if name in _BUILD_FILES or lowered[:2] == (".github", "workflows"):
        return "source", "project build or execution contract"
    if mode == "100755" or sample.startswith(b"#!"):
        return "source", "Git executable mode or interpreter shebang"
    if lowered[0] in _SOURCE_DIRS:
        return "source", "source, test, or script directory"
    if suffix in _CODE_SUFFIXES:
        return "source", "recognized source or script format"
    if name.startswith(".") or suffix in _CONFIG_SUFFIXES or lowered[0] in {"config", ".github"}:
        return "excluded", "project configuration or metadata"
    return "uncertain", "tracked file has no conclusive source, document, asset, or configuration role"


def tracked_source_inventory(repo: Path, base_branch: str, revision: str, *, include_uncertain: bool = False) -> SourceInventory:
    """Classify every tracked path and bind targets to exact remote blob bytes."""
    verify_remote_revision(repo, base_branch, revision)
    tree = _git(repo, "ls-tree", "-r", "-z", "--full-tree", revision)
    sources: list[TrackedSource] = []
    classifications: list[ClassifiedPath] = []
    seen: set[str] = set()
    for entry in tree.split(b"\0"):
        if not entry:
            continue
        try:
            metadata, raw_path = entry.split(b"\t", 1)
            mode, kind, raw_oid = metadata.split(b" ")
            path = raw_path.decode("utf-8")
            oid = raw_oid.decode("ascii")
            git_mode = mode.decode("ascii")
        except (ValueError, UnicodeError) as error:
            raise SourceInventoryError("Git tree 항목을 안전하게 해석할 수 없습니다.") from error
        if (path in seen or not path or path.startswith("/") or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or not _OID.fullmatch(oid) or kind not in {b"blob", b"commit"}):
            raise SourceInventoryError("Git tree 경로 또는 object identity가 유효하지 않습니다.")
        if (kind == b"commit") != (git_mode == "160000"):
            raise SourceInventoryError("Git tree object type과 file mode가 다릅니다.")
        seen.add(path)
        if kind == b"commit":
            disposition, reason = _classify(path, git_mode, b"", False)
        else:
            disposition, reason = _classify(path, git_mode, b"", False)
            if disposition != "excluded":
                content_hash, sample, has_nul = _blob_fingerprint(repo, oid)
                disposition, reason = _classify(path, git_mode, sample, has_nul)
                if disposition == "source" or (include_uncertain and disposition == "uncertain" and git_mode in {"100644", "100755"}):
                    sources.append(TrackedSource(path, content_hash, git_mode, revision, reason))
        classifications.append(ClassifiedPath(path, disposition, reason))
    verify_remote_revision(repo, base_branch, revision)
    classifications.sort(key=lambda item: item.path)
    covered = {source.path for source in sources}
    uncertain = [item for item in classifications if item.disposition == "uncertain" and (not include_uncertain or item.path not in covered)]
    if uncertain:
        details = "; ".join(f"{item.path}: {item.reason}" for item in uncertain[:10])
        raise SourceInventoryError("추적 파일 분류를 확정할 수 없습니다: " + details)
    return SourceInventory(revision, tuple(sorted(sources, key=lambda item: item.path)), tuple(classifications))


def root_placement_inventory(inventory: SourceInventory) -> tuple[RootPlacement, ...]:
    """Classify every tracked root item without automatically scheduling a move."""

    source_paths = {source.path for source in inventory.sources}
    roots: dict[str, list[ClassifiedPath]] = {}
    for item in inventory.classifications:
        roots.setdefault(item.path.split("/", 1)[0], []).append(item)
    placements = []
    for path, items in sorted(roots.items()):
        lowered = path.lower()
        if path in _ROOT_TOOL_PATHS:
            category, reason = "tool_required", "Loop Manager fixed root path"
        elif path in _ROOT_EXECUTION_PATHS:
            category, reason = "execution_build_deploy", "project execution, validation, or configuration root"
        elif (PurePosixPath(lowered).stem in _ROOT_ENTRY_NAMES
              or any(item.path == path and item.reason == "documentation file" for item in items)):
            category, reason = "user_entry", "root user-facing project document"
        elif any(item.path in source_paths for item in items):
            category = "internal_implementation"
            reason = ("root source candidate; retain unless assessment confirms its "
                      "entry path, references, owner, and safe destination")
        else:
            category, reason = "uncertain", "root placement requires investigation of actual use and ownership"
        placements.append(RootPlacement(path, category, reason))
    return tuple(placements)


def cached_source_inventory(connection: sqlite3.Connection, repository: Path,
                            base: str, revision: str, *, include_uncertain: bool = False) -> SourceInventory:
    """Reuse classification of an immutable tree, checking the live ref every time."""
    verify_remote_revision(repository, base, revision)
    connection.execute("""CREATE TABLE IF NOT EXISTS maintenance_inventory_cache (
        repository TEXT NOT NULL, base TEXT NOT NULL, revision TEXT NOT NULL,
        inventory_json TEXT NOT NULL, PRIMARY KEY (repository, base))""")
    key = (str(repository.resolve()), base + (":uncertain" if include_uncertain else ""))
    row = connection.execute(
        "SELECT revision, inventory_json FROM maintenance_inventory_cache WHERE repository = ? AND base = ?", key,
    ).fetchone()
    if row and row[0] == revision:
        value = json.loads(row[1])
        return SourceInventory(revision, tuple(TrackedSource(**item) for item in value["sources"]),
                               tuple(ClassifiedPath(**item) for item in value["classifications"]))
    inventory = tracked_source_inventory(repository, base, revision, **({"include_uncertain": True} if include_uncertain else {}))
    with connection:
        connection.execute("INSERT OR REPLACE INTO maintenance_inventory_cache VALUES (?, ?, ?, ?)",
                           (*key, revision, json.dumps(asdict(inventory), ensure_ascii=False)))
    return inventory


# The self repository keeps its original source-hash report format across restarts.
_ASSESSMENT_EXCLUDED_PARTS = {"archive", "__pycache__", ".venv", "venv", "node_modules", "dist", "build", ".pytest_cache", ".mypy_cache", "coverage"}
_ASSESSMENT_PRODUCT_DOCS = {"docs/PRODUCT.md", "docs/ARCHITECTURE.md"}


def source_inventory(repository: Path, revision: str) -> dict[str, str]:
    """Hash tracked source bytes at one immutable Git revision."""
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", revision) is None:
        raise AssessmentError("전수 평가 source revision이 유효하지 않습니다.")
    tree = _git(repository, "ls-tree", "-rz", "--name-only", revision)
    paths = sorted(path.decode("utf-8") for path in tree.split(b"\0") if path)
    inventory: dict[str, str] = {}
    for path in paths:
        parts = path.split("/")
        if any(part in _ASSESSMENT_EXCLUDED_PARTS for part in parts):
            continue
        is_doc = path in _ASSESSMENT_PRODUCT_DOCS or (
            len(parts) == 3 and parts[:2] == ["docs", "requirements"]
            and path.endswith(".md")
        )
        is_code = path.endswith((".py", ".sh", ".ps1")) and (
            parts[0] not in {"docs", ".agents"}
        )
        if not (is_doc or is_code):
            continue
        content = _git(repository, "show", f"{revision}:{path}")
        inventory[path] = hashlib.sha256(content).hexdigest()
    if not _ASSESSMENT_PRODUCT_DOCS <= inventory.keys():
        raise AssessmentError("전수 평가 필수 제품 문서가 source revision에 없습니다.")
    return inventory
