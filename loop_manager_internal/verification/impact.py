"""Deterministic, conservative module selection over both complete Git trees."""
from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import PurePosixPath
import subprocess
from collections import Counter, deque

from loop_manager_internal.maintenance import code_index
from loop_manager_internal.verification.contract import validate_manifest
from loop_manager_internal.verification import impact_analysis

VERSION = 2
MAX_FRACTION = 0.5
_PROTECTED = ('loop_manager_internal/git/', 'loop_manager_internal/storage/',
              'loop_manager_internal/recovery/', 'loop_manager_internal/verification/',
              'loop_manager_internal/security/', 'loop_manager_internal/agents/',
              'loop_manager_internal/runtime/', 'loop_manager_internal/config/',
              'loop_manager_internal/execution/execution_publication',
              'loop_manager_internal/execution/execution_checkpoint',
              'loop_manager_internal/execution/execution_verify',
              'loop_manager_internal/execution/execution_review',
              'loop_manager_internal/execution/execution_revision',
              'loop_manager_internal/execution/execution_worker',
              'loop_manager_internal/execution/execution_ports.py',
              'loop_manager_internal/execution/execution_assembly.py',
              'loop_manager_internal/execution/execution_state.py',
              'loop_manager_internal/execution/preliminary_review.py',
              'loop_manager_internal/execution/result_validation.py',
              'loop_manager_internal/execution/stage_documents.py')


def digest(value):
    return hashlib.sha256(code_index.encode(value).encode()).hexdigest()


def source_facts(path, content):
    return impact_analysis.analyze(path, content)


def resolve_records(facts, directory='tests'):
    prefix = directory.rstrip('/') + '/'
    aliases = {code_index.module_name(path[len(prefix):]): path for path in facts if path.startswith(prefix)}
    return impact_analysis.resolve(facts, aliases)


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
    analysis: dict = field(default_factory=dict)

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
    return resolve_records(facts, directory)


def document_sources(repository, base, head, changed):
    result = {}
    for _, old, new in changed:
        for label, revision, path in (('base', base, old), ('head', head, new)):
            if not path or path.endswith('.py'):
                continue
            entry = code_index.git(repository, 'ls-tree', '-z', revision, '--', path).rstrip(b'\0')
            if not entry:
                continue
            metadata, actual = entry.split(b'\t', 1)
            mode, kind, oid = metadata.decode().split()
            if kind != 'blob' or actual.decode() != path:
                continue
            result.setdefault(path, {})[label] = (mode, code_index.git(repository, 'cat-file', 'blob', oid))
    return result


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
                   directory="tests", documents=None):
    validate_manifest(modules)
    manifest_id = digest([asdict(module) for module in modules])
    selected, reasons, fallback = set(), {}, set()
    analysis = {'documents': [], 'expansions': [], 'resolved': {}, 'remaining_uncertainties': {},
                'size_policy': {'module_fraction': MAX_FRACTION, 'test_fraction': MAX_FRACTION}}
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
            proof, reason = impact_analysis.review_document(path, (before, after), configuration, documents, paths_to_modules)
            if reason:
                fallback.add(reason)
            else:
                analysis['documents'].append(proof)
        elif name == "__init__.py" or "/" not in path:
            fallback.add("package_or_entrypoint:" + path)
        elif path.startswith(_PROTECTED):
            fallback.add("shared_high_risk_code:" + path)
        elif path.startswith(directory.rstrip("/") + "/") and path not in paths_to_modules:
            if path not in after:
                fallback.add("deleted_test_or_support:" + path)
            else:
                analysis['expansions'].append('test_support_consumers:' + path)
    for _, old, new in changed:
        previous, current = before.get(old), after.get(new)
        implementation = not str(new or old).startswith(directory.rstrip('/') + '/')
        for facts in (previous, current):
            if facts and facts.get('impact', {}).get('protected_effect', facts.get('boundary') and not facts.get('impact')):
                fallback.add("boundary_code:" + str(new or old))
            if facts and any(item["name"] == "load_tests" for item in facts["symbols"]):
                fallback.add("custom_collection:" + str(new or old))
            if facts and any(item["module"].split(".")[0] == 'sqlite3'
                    or 'sqlite3' in item["names"] for item in facts["imports"]):
                fallback.add("database_code:" + str(new or old))
        if implementation and (previous is None or current is None) and any(facts and facts["contracts"] for facts in (previous, current)):
            analysis['expansions'].append("public_module_added_or_deleted:" + str(new or old))
        if implementation and previous is not None and current is not None and (
                previous["contracts"] != current["contracts"] or previous["exports"] != current["exports"]):
            analysis['expansions'].append("public_api_changed:" + str(new))
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
            unknown = facts.get('selection_uncertainties', facts['uncertainties'])
            if unknown:
                fallback.add(f"uncertain_{label}:{path}:" + ",".join(unknown))
                analysis['remaining_uncertainties'][label + ':' + path] = unknown
            if facts.get('impact', {}).get('resolved'):
                analysis['resolved'][label + ':' + path] = facts['impact']['resolved']
        pending = deque((path, path, 0) for path in sorted(changed_paths.intersection(graph)))
        pending.extend((consumer, path, 1) for path in sorted(changed_paths.intersection(graph))
                       for consumer in sorted(set(graph[path].get('reexport_consumers', []))))
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
    analysis['expansions'].sort()
    analysis['documents'].sort(key=lambda proof: proof['path'])
    return Selection(head, base, names, tuple(identity for module in modules if module.name in selected for identity in module.ids),
        {key: sorted(value) for key, value in sorted(reasons.items())}, tuple(sorted(fallback)),
        len(modules), total, manifest_id, digest(configuration), analysis=analysis)


def select(repository, base, modules, configuration, directory="tests"):
    head = code_index.git(repository, "rev-parse", "HEAD").decode().strip()
    base = code_index.git(repository, "rev-parse", base + "^{commit}").decode().strip()
    assert_candidate(repository, head, base)
    try:
        before, after = tree(repository, base, directory), tree(repository, head, directory)
    except (ValueError, UnicodeError, OSError, RecursionError, subprocess.CalledProcessError) as error:
        selection = Selection(head, base, (), (), {}, ("source_graph_unavailable:" + str(error),),
            len(modules), sum(len(module.ids) for module in modules),
            digest([asdict(module) for module in modules]), digest(configuration))
    else:
        changed = changes(repository, base, head)
        selection = select_records(before, after, changed,
            modules, head=head, base=base, configuration=configuration, directory=directory,
            documents=document_sources(repository, base, head, changed))
    assert_candidate(repository, head, base)
    return selection


def selected_modules(selection, modules):
    """Require exact discovery identity, preserving original order and fixtures."""
    if selection.algorithm != f'python-ast-impact-v{VERSION}/index-v{code_index.VERSION}':
        raise ValueError('partial selection algorithm changed')
    if digest([asdict(module) for module in modules]) != selection.manifest_identity:
        raise ValueError("partial manifest identity changed")
    chosen = [module for module in modules if module.name in selection.modules]
    if (tuple(module.name for module in chosen) != selection.modules
            or tuple(identity for module in chosen for identity in module.ids) != selection.ids
            or selection.fallback or not chosen):
        raise ValueError("partial selection identity mismatch or fallback")
    return chosen
