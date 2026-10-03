"""Source-bound static facts shared by maintenance and read-only Agent queries.

The cache contains facts, never review receipts. Resolution is rebuilt from the
current module set even when a file's parsed facts can be reused.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess

VERSION = 1
ARTIFACT_VERSION = 2
ARTIFACT_PATH = "docs/maintenance/code-map.jsonl"
_SCHEMA = """CREATE TABLE IF NOT EXISTS static_code_index (
    scope TEXT NOT NULL, path TEXT NOT NULL, content_hash TEXT NOT NULL,
    mode TEXT NOT NULL, version INTEGER NOT NULL, facts_json TEXT NOT NULL,
    PRIMARY KEY (scope, path))"""
_BOUNDARY = re.compile(r"(?:^|[/_])(storage|state|recovery|security|auth|permission|migration|schema|publication)(?:[/_.]|$)")
_DYNAMIC = {"eval", "exec", "__import__", "import_module"}


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def git(repository: Path, *arguments: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repository), *arguments], capture_output=True)
    if result.returncode:
        raise ValueError("코드맵 Git 원본 조회 실패: " + result.stderr.decode("utf-8", "replace")[:300])
    return result.stdout


def module_name(path: str) -> str:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def module_group(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return parent if parent != "." else "<root>"


def _dump(node) -> str:
    return ast.dump(node, include_attributes=False)


def parse_source(path: str, content: bytes) -> dict:
    """No model-generated descriptions are introduced into static facts."""
    facts = {"symbols": [], "imports": [], "exports": [], "attribute_uses": [], "contracts": [], "duplicates": [],
             "uncertainties": [], "semantic_hash": hashlib.sha256(content).hexdigest(),
             "boundary": bool(_BOUNDARY.search(path.lower()))}
    if not path.endswith((".py", ".pyi")):
        facts["uncertainties"].append("unsupported_source_format")
        return facts
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, UnicodeError, RecursionError):
        facts["uncertainties"].append("parse_failed")
        return facts
    try:
        semantic = _dump(tree)
    except RecursionError:
        facts["uncertainties"].append("analysis_depth_limit")
        return facts
    facts["semantic_hash"] = hashlib.sha256(semantic.encode()).hexdigest()
    exports = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            exports.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            exports.update(alias.asname or (alias.name.split(".", 1)[0] if isinstance(node, ast.Import) else alias.name)
                           for alias in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            exports.update(item.id for target in targets for item in ast.walk(target) if isinstance(item, ast.Name))
    facts["exports"] = sorted(exports)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            facts["symbols"].append({"name": node.name, "line": node.lineno,
                                     "end_line": node.end_lineno, "kind": type(node).__name__})
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            facts["imports"].append({"module": node.module if isinstance(node, ast.ImportFrom) else "",
                "level": node.level if isinstance(node, ast.ImportFrom) else 0,
                "names": [item.name for item in node.names], "aliases": [item.asname for item in node.names], "line": node.lineno})
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            use = {"binding": node.value.id, "attribute": node.attr, "line": node.lineno}
            if use not in facts["attribute_uses"]:
                facts["attribute_uses"].append(use)
        if isinstance(node, ast.Call):
            name = (node.func.id if isinstance(node.func, ast.Name) else
                    node.func.attr if isinstance(node.func, ast.Attribute) else "")
            if name in _DYNAMIC:
                facts["uncertainties"].append("dynamic_reference")
            if name in {"commit", "rollback", "execute", "transition", "check_permission", "authorize"}:
                facts["boundary"] = True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            statements = sum(isinstance(item, ast.stmt) for child in node.body for item in ast.walk(child))
            if statements >= 10:
                body = "\n".join(_dump(child) for child in node.body)
                facts["duplicates"].append({"hash": hashlib.sha256(body.encode()).hexdigest(),
                                             "symbol": node.name, "line": node.lineno})
    # Public module declarations and class methods form the contract projection.
    def contract(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                not node.name.startswith("_") or (prefix and node.name.startswith("__") and node.name.endswith("__"))):
                facts["contracts"].append([prefix + node.name, _dump(node.args),
                    _dump(node.returns) if node.returns else "", [_dump(item) for item in node.decorator_list]])
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                facts["contracts"].append([prefix + node.name, [_dump(base) for base in node.bases]])
                contract(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and (not target.id.startswith("_") or target.id == "__all__"):
                        facts["contracts"].append([prefix + target.id, _dump(node)])
    contract(tree.body)
    facts["symbols"].sort(key=lambda item: (item["line"], item["name"]))
    facts["uncertainties"] = sorted(set(facts["uncertainties"]))
    return facts


def resolve(facts: dict, paths) -> dict:
    """Re-resolve unchanged import declarations after moves/additions/deletions."""
    modules = {module_name(path): path for path in sorted(paths) if path.endswith((".py", ".pyi"))}
    roots = {name.split(".", 1)[0] for name in modules}
    result = {}
    for path, original in sorted(facts.items()):
        item = {**original, "references": [], "dependents": [], "related_tests": []}
        item["static_uncertainties"] = list(original.get("static_uncertainties", original["uncertainties"]))
        unknown = set(item["static_uncertainties"])
        module_bindings = {}
        for declaration in original["imports"]:
            module = declaration["module"] or ""
            level = declaration["level"]
            if level:
                package = list(PurePosixPath(path).parent.parts)
                if level > len(package):
                    unknown.add("unresolved_relative_import")
                    continue
                module = ".".join(package[:len(package) - level + 1] + ([module] if module else []))
            for name, alias in zip(declaration["names"], declaration["aliases"]):
                requested = name if not declaration["module"] and not level else ".".join(filter(None, (module, name)))
                target = modules.get(requested) or modules.get(module)
                if target:
                    if requested not in modules and name not in facts.get(target, {}).get("exports", []):
                        unknown.add("unverified_imported_symbol:" + requested)
                    reference = {"path": target, "symbol": name, "line": declaration["line"]}
                    if reference not in item["references"]:
                        item["references"].append(reference)
                    if requested in modules:
                        binding = alias or (name.split(".", 1)[0] if not declaration["module"] and not level else name)
                        if "." not in name or alias:
                            module_bindings[binding] = target
                elif level or requested.split(".", 1)[0] in roots:
                    unknown.add("unresolved_internal_import:" + requested)
        for use in original["attribute_uses"]:
            target = module_bindings.get(use["binding"])
            if target and use["attribute"] not in facts.get(target, {}).get("exports", []):
                unknown.add("unverified_module_attribute:" + target + ":" + use["attribute"])
        item["uncertainties"] = sorted(unknown)
        result[path] = item
    for path, item in result.items():
        for reference in item["references"]:
            if reference["path"] not in result:
                continue
            result[reference["path"]]["dependents"].append(path)
            if path.startswith(("tests/", "test/")) or PurePosixPath(path).name.startswith("test_"):
                result[reference["path"]]["related_tests"].append(path)
    for item in result.values():
        item["dependents"] = sorted(set(item["dependents"]))
        item["related_tests"] = sorted(set(item["related_tests"]))
    return result


@dataclass(frozen=True)
class CodeIndex:
    revision: str
    records: dict
    parsed: int
    reused: int

    @property
    def digest(self) -> str:
        return hashlib.sha256(encode(self.records).encode()).hexdigest()

    def serialize(self) -> bytes:
        header = {"version": ARTIFACT_VERSION, "kind": "static", "analysis_version": VERSION}
        return (encode(header) + "\n" + "".join(encode({"path": path, **record}) + "\n"
                for path, record in sorted(self.records.items()))).encode()


def build_index(connection: sqlite3.Connection, scope: str, inventory, loader) -> CodeIndex:
    connection.execute(_SCHEMA)
    old = {row[0]: row[1:] for row in connection.execute(
        "SELECT path, content_hash, mode, version, facts_json FROM static_code_index WHERE scope = ?", (scope,))}
    facts, identities, updates, parsed, reused = {}, {}, [], 0, 0
    for source in inventory.sources:
        previous = old.get(source.path)
        if previous and tuple(previous[:3]) == (source.content_hash, source.mode, VERSION):
            fact = json.loads(previous[3])
            reused += 1
        else:
            content = loader(source)
            if hashlib.sha256(content).hexdigest() != source.content_hash:
                raise ValueError("코드맵 원본 hash가 다릅니다: " + source.path)
            fact = parse_source(source.path, content)
            updates.append((scope, source.path, source.content_hash, source.mode, VERSION, encode(fact)))
            parsed += 1
        facts[source.path] = fact
        identities[source.path] = {"content_hash": source.content_hash, "mode": source.mode}
    resolved = resolve(facts, facts)
    with connection:
        connection.executemany("INSERT OR REPLACE INTO static_code_index VALUES (?, ?, ?, ?, ?, ?)", updates)
        connection.executemany("DELETE FROM static_code_index WHERE scope = ? AND path = ?",
                               ((scope, path) for path in old.keys() - facts.keys()))
    return CodeIndex(inventory.revision, {path: {**identities[path], "facts": resolved[path]}
                                         for path in sorted(facts)}, parsed, reused)


def read_worktree_source(repository: Path, path: str, tracked):
    """Read a stable regular file inside this checkout, never following symlinks."""
    root = repository.resolve()
    if (path not in tracked or path.startswith("/") or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("코드맵 조회 경로가 관리 worktree 범위 밖입니다: " + path)
    target = root / path
    if not target.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in [target, *target.parents] if parent != root):
        raise ValueError("코드맵 조회 경로에 symlink가 있습니다: " + path)
    before = target.stat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("코드맵 조회 대상이 일반 파일이 아닙니다.")
    content = target.read_bytes()
    after = target.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("코드맵 조회 중 원본이 변경됐습니다.")
    return content, "100755" if after.st_mode & 0o111 else "100644"


def query_worktree(repository: Path, paths: list[str]) -> dict:
    """Read only requested current files; no operational DB or artifact writes."""
    tracked = set(git(repository, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")) - {""}
    facts, identities = {}, {}
    root = repository.resolve()
    for path in paths:
        content, mode = read_worktree_source(root, path, tracked)
        identities[path] = {"content_hash": hashlib.sha256(content).hexdigest(),
                            "mode": mode}
        facts[path] = parse_source(path, content)
    # Resolve targets from the full tracked module set without reading their bodies.
    # This query makes no claim that incoming relations have been refreshed.
    resolved = resolve(facts, tracked)
    return {"worktree": str(root), "kind": "static", "analysis_version": VERSION,
            "incoming_coverage": "requested_files_only", "records": {
                path: {**identities[path], "facts": resolved[path]} for path in sorted(facts)}}


def main():
    parser = argparse.ArgumentParser(description="Read source-bound static context for current worktree files.")
    parser.add_argument("--worktree", type=Path, default=Path.cwd())
    parser.add_argument("--path", action="append", required=True)
    parser.add_argument("--index", type=Path, help="Manager가 생성한 정적 사실 전용 조회 snapshot")
    arguments = parser.parse_args()
    try:
        if arguments.index:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from loop_manager_internal.maintenance.code_context import query_snapshot
            result = query_snapshot(arguments.worktree, arguments.path, arguments.index)
        else:
            result = query_worktree(arguments.worktree, arguments.path)
        print(encode(result))
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
