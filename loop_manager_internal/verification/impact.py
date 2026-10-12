"""Deterministic, conservative module selection over both complete Git trees."""
from dataclasses import asdict, dataclass
import hashlib
import ast
from pathlib import PurePosixPath
import re
import subprocess
from collections import Counter, deque

from loop_manager_internal.maintenance import code_index
from loop_manager_internal.verification.contract import validate_manifest

VERSION = 1
MAX_FRACTION = 0.5
_RISK = re.compile(r"(?:^|[/_])(git|sqlite|storage|state|recovery|security|auth|permission|migration|schema|publication|checkpoint|execution|verification)(?:[/_.]|$)")
_OPAQUE = {"__import__", "import_module", "eval", "exec", "getattr", "setattr", "hasattr",
           "globals", "locals", "vars", "spec_from_file_location", "module_from_spec",
           "SourceFileLoader", "load_module", "run_module", "run_path"}


def digest(value):
    return hashlib.sha256(code_index.encode(value).encode()).hexdigest()


def source_facts(path, content):
    facts = code_index.parse_source(path, content)
    try:
        syntax = ast.parse(content)
        aliases = set(_OPAQUE)
        subprocess_bindings, system_bindings, os_bindings = {'subprocess'}, {'sys'}, {'os'}
        for node in ast.walk(syntax):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name == 'subprocess':
                        subprocess_bindings.add(item.asname or item.name)
                    if item.name == 'sys':
                        system_bindings.add(item.asname or item.name)
                    if item.name == 'os':
                        os_bindings.add(item.asname or item.name)
            if isinstance(node, ast.ImportFrom):
                aliases.update(item.asname or item.name for item in node.names if item.name in _OPAQUE)
                if node.module == 'subprocess' or node.module == 'sys' and any(item.name == 'modules' for item in node.names):
                    aliases.update(item.asname or item.name for item in node.names)
        if any(isinstance(node, ast.Name) and node.id in aliases
               or isinstance(node, ast.Attribute) and (node.attr in _OPAQUE
                    or isinstance(node.value, ast.Name) and (
                        node.value.id in system_bindings and node.attr == 'modules'
                        or node.value.id in subprocess_bindings
                        or node.value.id in os_bindings and (node.attr in {'system', 'popen'}
                            or node.attr.startswith(('exec', 'spawn')))))
               for node in ast.walk(syntax)):
            facts['uncertainties'].append('dynamic_import_or_reflection')
    except (SyntaxError, ValueError, UnicodeError, RecursionError):
        pass  # code_index already reports parse/depth uncertainty.
    return facts


@dataclass(frozen=True)
class Selection:
    head: str
    base: str
    modules: tuple[str, ...]
    ids: tuple[str, ...]
    reasons: dict
    fallback: tuple[str, ...]
    total_modules: int
    total_tests: int
    manifest_identity: str
    configuration_identity: str
    algorithm: str = f"python-ast-impact-v{VERSION}/index-v{code_index.VERSION}"

    def serialize(self):
        return asdict(self)


def tree(repository, revision, directory="tests"):
    """Read every tracked Python body at an immutable revision, in one batch."""
    entries = code_index.git(repository, "ls-tree", "-r", "-z", revision).split(b"\0")
    sources = []
    for entry in filter(None, entries):
        metadata, path = entry.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = path.decode("utf-8")
        if path.endswith(".py"):
            if mode not in {"100644", "100755"} or kind != "blob":
                raise ValueError("nonregular Python source: " + path)
            sources.append((path, oid))
    result = subprocess.run(["git", "-C", str(repository), "cat-file", "--batch"],
        input="".join(oid + "\n" for _, oid in sources).encode(), capture_output=True, check=True)
    position, facts = 0, {}
    for path, oid in sources:
        end = result.stdout.index(b"\n", position)
        header = result.stdout[position:end].decode().split()
        if len(header) != 3 or header[:2] != [oid, "blob"]:
            raise ValueError("Git tree source identity mismatch")
        size = int(header[2])
        content = result.stdout[end + 1:end + 1 + size]
        if len(content) != size:
            raise ValueError("incomplete Git source")
        facts[path] = source_facts(path, content)
        position = end + size + 2
    prefix = directory.rstrip("/") + "/"
    aliases = {code_index.module_name(path[len(prefix):]): path for path in facts if path.startswith(prefix)}
    return code_index.resolve(facts, facts, module_aliases=aliases)


def changes(repository, base, head):
    fields = code_index.git(repository, "diff", "--name-status", "-z", "--find-renames", base, head, "--").decode("utf-8").split("\0")
    result, index = [], 0
    while index < len(fields) and fields[index]:
        status, path = fields[index:index + 2]
        index += 2
        if status.startswith(("R", "C")):
            result.append((status, path, fields[index]))
            index += 1
        else:
            result.append((status, path if status != "A" else None, path if status != "D" else None))
    return result


def assert_candidate(repository, head, base):
    if (code_index.git(repository, "rev-parse", "HEAD").decode().strip() != head
            or code_index.git(repository, "rev-parse", base + "^{commit}").decode().strip() != base
            or code_index.git(repository, "status", "--porcelain").strip()):
        raise ValueError("partial verify candidate changed or is dirty")


def select_records(before, after, changed, modules, *, head, base, configuration,
                   directory="tests"):
    validate_manifest(modules)
    manifest_id = digest([asdict(module) for module in modules])
    selected, reasons, fallback = set(), {}, set()
    paths_to_modules = {}
    for module in modules:
        candidates = {module.name.replace(".", "/") + ".py",
                      directory.rstrip("/") + "/" + module.name.replace(".", "/") + ".py"}
        found = candidates.intersection(after)
        if len(found) != 1:
            fallback.add("manifest_source_unresolved:" + module.name)
        else:
            paths_to_modules[next(iter(found))] = module.name
    changed_paths = {path for _, old, new in changed for path in (old, new) if path}
    for path in changed_paths:
        name = PurePosixPath(path).name
        if not path.endswith(".py"):
            # Documents may define Review inputs, but cannot prove test impact.
            fallback.add("non_python_input:" + path)
        elif name == "__init__.py" or "/" not in path:
            fallback.add("package_or_entrypoint:" + path)
        elif _RISK.search(path.lower()):
            fallback.add("shared_high_risk_code:" + path)
        elif path.startswith(directory.rstrip("/") + "/") and path not in paths_to_modules:
            fallback.add("test_support_or_deleted_test:" + path)
    for _, old, new in changed:
        previous, current = before.get(old), after.get(new)
        implementation = not str(new or old).startswith(directory.rstrip('/') + '/')
        for facts in (previous, current):
            if facts and facts.get("boundary"):
                fallback.add("boundary_code:" + str(new or old))
            if facts and any(item["name"] == "load_tests" for item in facts["symbols"]):
                fallback.add("custom_collection:" + str(new or old))
            if facts and any(item["module"].split(".")[0] in {"sqlite3", "subprocess"}
                    or set(item["names"]).intersection({"sqlite3", "subprocess"}) for item in facts["imports"]):
                fallback.add("external_or_database_code:" + str(new or old))
        if implementation and (previous is None or current is None) and any(facts and facts["contracts"] for facts in (previous, current)):
            fallback.add("public_module_added_or_deleted:" + str(new or old))
        if implementation and previous is not None and current is not None and (
                previous["contracts"] != current["contracts"] or previous["exports"] != current["exports"]):
            fallback.add("public_api_changed:" + str(new))
        if old and old.endswith(".py") and old not in before:
            fallback.add("old_source_missing:" + old)
        if new and new.endswith(".py") and new not in after:
            fallback.add("new_source_missing:" + new)
    # Opaque consumers anywhere in either tree can hide incoming relationships.
    # Never claim requested-file-only incoming coverage is a complete graph.
    for label, graph in (("base", before), ("head", after)):
        names = Counter(code_index.module_name(path) for path in graph)
        if any(count != 1 for count in names.values()):
            fallback.add("ambiguous_module_paths:" + label)
        for path, facts in graph.items():
            if facts["uncertainties"]:
                fallback.add(f"uncertain_{label}:{path}:" + ",".join(facts["uncertainties"]))
        pending = deque((path, path, 0) for path in sorted(changed_paths.intersection(graph)))
        visited = set()
        while pending:
            path, origin, depth = pending.popleft()
            if (path, origin) in visited:
                continue
            visited.add((path, origin))
            module = paths_to_modules.get(path)
            if module:
                selected.add(module)
                relation = 'changed_test' if depth == 0 else 'direct_reference' if depth == 1 else 'indirect_reference'
                reasons.setdefault(module, set()).add(f"{label}:{relation}:{origin}->{path}")
            pending.extend((dependent, origin, depth + 1) for dependent in graph[path]["dependents"])
    if not selected:
        fallback.add("no_tests_selected")
    count = sum(len(module.ids) for module in modules if module.name in selected)
    total = sum(len(module.ids) for module in modules)
    if len(selected) >= len(modules) * MAX_FRACTION or count >= total * MAX_FRACTION:
        fallback.add("selection_too_broad")
    names = tuple(module.name for module in modules if module.name in selected)
    return Selection(head, base, names, tuple(identity for module in modules if module.name in selected for identity in module.ids),
        {key: sorted(value) for key, value in sorted(reasons.items())}, tuple(sorted(fallback)),
        len(modules), total, manifest_id, digest(configuration))


def select(repository, base, modules, configuration, directory="tests"):
    head = code_index.git(repository, "rev-parse", "HEAD").decode().strip()
    base = code_index.git(repository, "rev-parse", base + "^{commit}").decode().strip()
    assert_candidate(repository, head, base)
    try:
        before, after = tree(repository, base, directory), tree(repository, head, directory)
    except (ValueError, UnicodeError, subprocess.CalledProcessError) as error:
        selection = Selection(head, base, (), (), {}, ("source_graph_unavailable:" + str(error),),
            len(modules), sum(len(module.ids) for module in modules),
            digest([asdict(module) for module in modules]), digest(configuration))
    else:
        selection = select_records(before, after, changes(repository, base, head),
            modules, head=head, base=base, configuration=configuration, directory=directory)
    assert_candidate(repository, head, base)
    return selection


def selected_modules(selection, modules):
    """Require exact discovery identity, preserving original order and fixtures."""
    if digest([asdict(module) for module in modules]) != selection.manifest_identity:
        raise ValueError("partial manifest identity changed")
    chosen = [module for module in modules if module.name in selection.modules]
    if (tuple(module.name for module in chosen) != selection.modules
            or tuple(identity for module in chosen for identity in module.ids) != selection.ids
            or selection.fallback or not chosen):
        raise ValueError("partial selection identity mismatch or fallback")
    return chosen
