"""Bounded pre-Review regression checks; a subsequent full verify is mandatory."""
from dataclasses import asdict, dataclass, field
import hashlib
from pathlib import PurePosixPath
import subprocess
from collections import Counter, deque

from loop_manager_internal.maintenance import code_index
from loop_manager_internal.verification.contract import validate_manifest
from loop_manager_internal.verification import impact_analysis

VERSION = 4
MAX_MODULES = 24
_SMOKE = ('test_preliminary_review', 'test_managed_configuration', 'test_notification_events')
_VERIFY_INPUTS = ('loop_manager_internal/verification/', 'scripts/verify',
                 'scripts/unittest', 'sitecustomize.py', 'pyproject.toml',
                 'requirements', 'config/config.ini')
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


def file_inventory(repository, base, head):
    result = {}
    for label, revision in (('base', base), ('head', head)):
        for entry in filter(None, code_index.git(repository, 'ls-tree', '-r', '-z', revision).split(b'\0')):
            metadata, path = entry.split(b'\t', 1)
            mode, kind, _ = metadata.decode().split()
            result.setdefault(path.decode(), {})[label] = mode if kind == 'blob' else kind
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
                   directory="tests", documents=None, files=None):
    validate_manifest(modules)
    manifest_id = digest([asdict(module) for module in modules])
    selected, required, reasons, fallback, depths = set(), set(), {}, set(), {}
    analysis = {'documents': [], 'expansions': [], 'resolved': {}, 'remaining_uncertainties': {},
                'scope_limits': [], 'coverage': 'selected_modules_only',
                'size_policy': {'max_related_modules': MAX_MODULES, 'changed_tests_required': True}}
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
    for path in sorted(changed_paths):
        name = PurePosixPath(path).name
        if path.startswith(_VERIFY_INPUTS):
            fallback.add('verification_contract_changed:' + path)
        if not path.endswith(".py"):
            proof, reason = impact_analysis.review_document(path, (before, after), configuration, documents, paths_to_modules, files)
            if reason:
                analysis['scope_limits'].append(reason)
                analysis['documents'].append({'path': path, 'kind': 'unproven_input',
                    'source_identity': {label: hashlib.sha256(mode.encode() + b'\0' + content).hexdigest()
                        for label, (mode, content) in (documents or {}).get(path, {}).items()}})
            else:
                analysis['documents'].append(proof)
        elif name == "__init__.py" or "/" not in path:
            analysis['expansions'].append("package_or_entrypoint:" + path)
        elif path.startswith(_PROTECTED):
            analysis['expansions'].append("shared_high_risk_code:" + path)
        elif path.startswith(directory.rstrip("/") + "/") and path not in paths_to_modules:
            if path not in after:
                analysis['scope_limits'].append("deleted_test_or_support:" + path)
            else:
                analysis['expansions'].append('test_support_consumers:' + path)
    for _, old, new in changed:
        previous, current = before.get(old), after.get(new)
        implementation = not str(new or old).startswith(directory.rstrip('/') + '/')
        for facts in (previous, current):
            if facts and facts.get('impact', {}).get('protected_effect', facts.get('boundary') and not facts.get('impact')):
                analysis['expansions'].append("boundary_code:" + str(new or old))
            if facts and any(item["name"] == "load_tests" for item in facts["symbols"]):
                fallback.add("custom_collection:" + str(new or old))
            if facts and any(item["module"].split(".")[0] == 'sqlite3'
                    or 'sqlite3' in item["names"] for item in facts["imports"]):
                analysis['expansions'].append("database_code:" + str(new or old))
        if implementation and (previous is None or current is None) and any(facts and facts["contracts"] for facts in (previous, current)):
            analysis['expansions'].append("public_module_added_or_deleted:" + str(new or old))
        if implementation and previous is not None and current is not None and (
                previous["contracts"] != current["contracts"] or previous["exports"] != current["exports"]):
            analysis['expansions'].append("public_api_changed:" + str(new))
        if old and old.endswith(".py") and old not in before:
            analysis['scope_limits'].append("old_source_missing:" + old)
        if new and new.endswith(".py") and new not in after:
            analysis['scope_limits'].append("new_source_missing:" + new)
    # An opaque collection inventory cannot prove the manifest is unchanged.
    # Other opaque consumers remain limitations of this bounded precheck.
    for label, graph in (("base", before), ("head", after)):
        collection_inputs = {path for path, facts in graph.items()
                             if any(item['name'] == 'load_tests' for item in facts['symbols'])}
        dependencies = {path: set() for path in graph}
        for path, facts in graph.items():
            for consumer in facts['dependents']:
                dependencies[consumer].add(path)
        pending = deque(collection_inputs)
        while pending:
            for dependency in dependencies[pending.popleft()] - collection_inputs:
                collection_inputs.add(dependency)
                pending.append(dependency)
        inventory_changed = any(status.startswith(('A', 'D', 'R', 'C'))
                                and any(path and path.endswith('.py') for path in (old, new))
                                for status, old, new in changed)
        if inventory_changed or changed_paths.intersection(collection_inputs):
            for path in sorted(collection_inputs):
                if graph[path].get('impact', {}).get('unbounded_file_read'):
                    fallback.add('uncertain_collection_input:' + path)
        names = Counter(code_index.module_name(path) for path in graph)
        if any(count != 1 for count in names.values()):
            analysis['scope_limits'].append("ambiguous_module_paths:" + label)
        for path, facts in graph.items():
            unknown = facts.get('selection_uncertainties', facts['uncertainties'])
            if unknown:
                analysis['remaining_uncertainties'][label + ':' + path] = unknown
            if facts.get('impact', {}).get('resolved'):
                analysis['resolved'][label + ':' + path] = facts['impact']['resolved']
        pending = deque((path, path, 0) for path in sorted(changed_paths.intersection(graph)))
        pending.extend((consumer, path, 1) for path in sorted(changed_paths.intersection(graph))
                       for consumer in sorted(set(graph[path].get('reexport_consumers', []))))
        for origin in sorted(path for path in changed_paths if not path.endswith('.py')):
            for consumer, facts in sorted(graph.items()):
                reads = facts.get('impact', {}).get('file_reads', [])
                if any(origin == read or origin.startswith(read.rstrip('/') + '/')
                       or PurePosixPath(origin).name == read for read in reads):
                    pending.append((consumer, origin, 1))
        visited = set()
        while pending:
            path, origin, depth = pending.popleft()
            if (path, origin) in visited:
                continue
            visited.add((path, origin))
            module = paths_to_modules.get(path)
            if module:
                selected.add(module)
                depths[module] = min(depth, depths.get(module, depth))
                if depth == 0:
                    required.add(module)
                relation = 'changed_test' if depth == 0 else 'direct_reference' if depth == 1 else 'indirect_reference'
                reasons.setdefault(module, set()).add(f"{label}:{relation}:{origin}->{path}")
            pending.extend((dependent, origin, depth + 1) for dependent in sorted(set(
                graph[path]["dependents"] + graph[path].get('usage_dependents', []))))
    related = set(selected)
    if analysis['remaining_uncertainties'] or analysis['scope_limits'] or analysis['expansions'] or not selected:
        for module in modules:
            if module.name.rsplit('.', 1)[-1] in _SMOKE:
                selected.add(module.name)
                depths[module.name] = -1
                reasons.setdefault(module.name, set()).add('common_regression_precheck')
    # Required changed modules may exceed the soft budget. Never split a module
    # or run the entire manifest as a partial check before repeating full verify.
    budget = min(MAX_MODULES, max(0, len(modules) - 1))
    selected = required | set(sorted(selected - required,
        key=lambda name: (depths[name], name))[:max(0, budget - len(required))])
    if not selected:
        fallback.add('no_tests_selected')
    if selected and len(selected) == len(modules):
        fallback.add('entire_manifest_required')
    analysis['related_modules'] = sorted(related)
    analysis['not_selected_related_modules'] = sorted(related - selected)
    analysis['scope_limits'] = sorted(set(analysis['scope_limits']))
    analysis['size_policy']['effective_budget'] = max(budget, len(required))
    total = sum(len(module.ids) for module in modules)
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
            documents=document_sources(repository, base, head, changed), files=file_inventory(repository, base, head))
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
