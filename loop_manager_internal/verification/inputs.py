"""Versioned verify input observations; uncertainty never authorizes reuse.

V1 uses case-sensitive POSIX segments: * and ? stay within a segment, ** is
an entire segment matching zero or more directories. Dot files are included.
Absolute paths, parent traversal, backslashes and character classes are rejected.
No glob match is an observed empty set; a missing literal is an error.
"""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Mapping

MANIFEST = "scripts/verify-inputs.json"
RULES = "verify-inputs-v1:posix-case-sensitive-segments-star-question-globstar-dotfiles;kinds-modes;no-symlink-follow;git-venv-bytecode-excluded;code-inputs-only;exclusions-bound-selected-inputs"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True).encode()).hexdigest()


def parse(raw: bytes) -> tuple[str, ...]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate manifest key")
            result[key] = value
        return result
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    if (not isinstance(value, dict) or set(value) != {"version", "paths"}
            or type(value["version"]) is not int or value["version"] != 1
            or not isinstance(value["paths"], list) or not value["paths"]):
        raise ValueError("unsupported verify inputs manifest")
    for path in value["paths"]:
        if (not isinstance(path, str) or not path or "\\" in path or ":" in path
                or "[" in path or "]" in path or "\x00" in path
                or any(segment in {"", ".", ".."} for segment in path.split("/"))
                or any("**" in segment and segment != "**" for segment in path.split("/"))):
            raise ValueError("unsupported verify input path")
        if path.split("/")[0] in {".git", ".venv"} or "__pycache__" in path.split("/"):
            raise ValueError("Git/runtime/cache inputs require separate identities")
    if len(set(value["paths"])) != len(value["paths"]):
        raise ValueError("duplicate verify input path")
    return tuple(value["paths"])


def matches(path: str, pattern: str) -> bool:
    parts, glob = path.split("/"), pattern.split("/")
    def match(i, j):
        if j == len(glob):
            return i == len(parts)
        if glob[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], glob[j]) and match(i + 1, j + 1)
    return match(0, 0)


@dataclass(frozen=True)
class InputObservation:
    fingerprint: str | None
    files: tuple[tuple[str, str, int, str], ...] = ()
    uncovered: tuple[tuple[str, str, int, str], ...] = ()
    completeness_token: str | None = None
    reason: str | None = None

    @property
    def proven(self) -> bool:
        return self.fingerprint is not None and self.reason is None


def observe(root: Path, *, command: tuple[str, ...], environment: Mapping[str, str],
            runtime: str | None, git_inputs: Mapping[str, str] | None = None,
            external_inputs: Mapping[str, str] | None = None,
            completeness: str | None = None) -> InputObservation:
    """Observe working bytes including untracked inputs, never a verify success.

    Environment/runtime/Git/external identities are accepted for API compatibility
    but are outside the repository-code reuse contract. Exclusion review can
    authorize only the exact completeness_token, bound to current uncovered
    path/content/kind/mode, selected code, command and interpretation rules.
    """
    try:
        manifest = root / MANIFEST
        if not stat.S_ISREG(manifest.lstat().st_mode) or manifest.parent.is_symlink():
            raise ValueError("manifest must be a regular file")
        raw = manifest.read_bytes()
        patterns = parse(raw)
        inventory = []
        def walk_error(error):
            raise error
        for directory, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            # Git internals/bytecode are generated; the installed venv is outside
            # the repository-code comparison contract.
            dirs[:] = sorted(d for d in dirs if d not in {".git", "__pycache__", ".venv"})
            for name in sorted(names + [d for d in dirs if (Path(directory) / d).is_symlink()]):
                if name == ".git":
                    continue
                target = Path(directory) / name
                path = target.relative_to(root).as_posix()
                before = target.lstat()
                mode = before.st_mode
                kind = "file" if stat.S_ISREG(mode) else "symlink" if stat.S_ISLNK(mode) else "special"
                content = target.read_bytes() if kind == "file" else os.fsencode(os.readlink(target)) if kind == "symlink" else b""
                after = target.lstat()
                # Reading may update atime; compare file identity and mutation
                # metadata without treating our own access as an input change.
                if any(getattr(after, key) != getattr(before, key) for key in (
                    'st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                    'st_size', 'st_mtime_ns', 'st_ctime_ns',
                )):
                    raise ValueError("verify input changed during observation: " + path)
                inventory.append((path, kind, stat.S_IMODE(mode), hashlib.sha256(content).hexdigest()))
        if manifest.read_bytes() != raw or not stat.S_ISREG(manifest.lstat().st_mode):
            raise ValueError("verify manifest changed during observation")
        selected = tuple(sorted(item for item in inventory if item[0] == MANIFEST or any(matches(item[0], p) for p in patterns)))
        for pattern in patterns:
            if not any(c in pattern for c in "*?") and not any(item[0] == pattern for item in selected):
                raise ValueError("missing literal verify input: " + pattern)
        # All unmatched files require a bounded exclusion proof. This includes
        # new code/config and moves, without guessing their language or purpose.
        uncovered = tuple(sorted(item for item in inventory if item not in selected))
        # Bind to the running observer/runner rules as well as project files.
        # These are repository implementation bytes, not installed tool identity.
        package = Path(__file__).resolve().parents[1]
        rules = sorted(path for path in (package / 'verification').iterdir()
                       if path.suffix in {'.py', '.pl', '.pm'})
        rules += [package / 'execution' / name for name in (
            'result_validation.py', 'verify_process.py', 'windows_job.py',
            'execution_verify_handler.py')]
        rules.extend((package / 'storage/verify_receipts.py',
                      package / 'maintenance/code_index.py'))
        context = {"rules": RULES,
                   "implementation": [(path.relative_to(package).as_posix(),
                       hashlib.sha256(path.read_bytes()).hexdigest()) for path in rules],
                   "manifest": hashlib.sha256(raw).hexdigest(), "command": command}
        token = digest((context, selected, uncovered))
        fingerprint = digest((context, selected))
        reason = None
        if not command:
            reason = "unconfirmed verify command"
        elif any(item[1] != "file" for item in selected):
            reason = "non-regular or external verify input"
        elif uncovered and completeness != token:
            reason = "unconfirmed inputs outside manifest"
        return InputObservation(fingerprint, selected, uncovered, token, reason)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        return InputObservation(None, reason=str(error))
