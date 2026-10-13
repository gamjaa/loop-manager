"""Bounded AST proofs for partial selection; never execute project code.

The shared index's raw uncertainty remains intact. These proofs only refine the
partial graph and identify references which the ordinary import graph misses.
Unknown bindings, namespace escape and dynamic execution remain conservative.
"""
import ast
from copy import deepcopy
import hashlib
from collections import Counter
from pathlib import PurePosixPath

from loop_manager_internal.maintenance import code_index


UNKNOWN = object()
_LOADERS = {'__import__', 'builtins.__import__', 'importlib.import_module', 'runpy.run_module'}
_REFLECTION = {'getattr', 'hasattr', 'setattr', 'builtins.getattr', 'builtins.hasattr', 'builtins.setattr'}
_EXECUTION = {'eval', 'exec', 'builtins.eval', 'builtins.exec', 'runpy.run_path',
              'importlib.util.spec_from_file_location', 'importlib.util.module_from_spec',
              'importlib.machinery.SourceFileLoader'}
_PROCESSES = {'subprocess.run', 'subprocess.Popen', 'subprocess.call', 'subprocess.check_call',
              'subprocess.check_output', 'os.system', 'os.popen'}
_OPAQUE_NAMES = {'__import__', 'import_module', 'eval', 'exec', 'load_module',
                 'run_module', 'run_path', 'spec_from_file_location', 'module_from_spec', 'SourceFileLoader'}
_FILE_METHODS = {'read_text', 'read_bytes', 'open', 'read', 'readline', 'readlines',
                 'glob', 'rglob', 'iterdir', 'exists', 'is_file', 'is_dir', 'stat', 'lstat'}
_FILE_FUNCTIONS = {'open', 'builtins.open', 'io.open', 'glob.glob', 'glob.iglob',
                   'os.listdir', 'os.scandir', 'os.walk', 'os.stat', 'os.lstat', 'os.access',
                   'os.path.exists', 'os.path.isfile', 'os.path.isdir',
                   'shutil.copy', 'shutil.copy2', 'shutil.copyfile', 'shutil.copytree',
                   'pkgutil.get_data', 'importlib.resources.read_text', 'importlib.resources.read_binary',
                   'xml.etree.ElementTree.parse', 'xml.dom.minidom.parse'}
_FILE_ALIASES = {'symlink', 'symlink_to', 'link', 'link_to', 'hardlink_to', 'rename', 'replace'}
_NAMESPACES = {'__dict__', '__globals__', '__builtins__', 'f_globals', 'f_locals',
               '__getattribute__', '__getattr__'}


class Source:
    def __init__(self, path, content):
        self.path = path
        self.syntax = ast.parse(content)
        self.parents = {child: node for node in ast.walk(self.syntax) for child in ast.iter_child_nodes(node)}
        self.writes = Counter(node.id for node in ast.walk(self.syntax)
                              if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store))
        self.writes.update(node.arg for node in ast.walk(self.syntax) if isinstance(node, ast.arg))
        self.writes.update(node.name for node in ast.walk(self.syntax)
                           if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        self.bindings = {}
        choices = {}
        for node in ast.walk(self.syntax):
            if isinstance(node, ast.Import):
                for item in node.names:
                    self.bindings[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
                    choices.setdefault(item.asname or item.name.split('.')[0], set()).add(self.bindings[item.asname or item.name.split('.')[0]])
            elif isinstance(node, ast.ImportFrom) and not node.level:
                for item in node.names:
                    self.bindings[item.asname or item.name] = '.'.join(filter(None, (node.module, item.name)))
                    choices.setdefault(item.asname or item.name, set()).add(self.bindings[item.asname or item.name])
        self.dynamic_bindings = {name for name, values in choices.items() if values.intersection(_LOADERS | _REFLECTION | _EXECUTION | _PROCESSES)}
        self.bindings = {name: value for name, value in self.bindings.items() if not self.writes[name] and len(choices[name]) == 1}

    def qualified(self, node):
        if isinstance(node, ast.Name):
            if self.writes[node.id]:
                return None
            return self.bindings.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            owner = self.qualified(node.value)
            return owner + '.' + node.attr if owner else None
        return None

    def value(self, node, seen=()):
        if node is None:
            return UNKNOWN
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return [self.value(item, seen) for item in node.elts]
        if isinstance(node, ast.Name) and self.writes[node.id] == 1 and node.id not in seen:
            parent = self.parents.get(node)
            while parent:
                if isinstance(parent, ast.For) and isinstance(parent.target, ast.Name) and parent.target.id == node.id:
                    return self.value(parent.iter, (*seen, node.id))
                parent = self.parents.get(parent)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left, seen), self.value(node.right, seen)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
        return UNKNOWN

    def strings(self, node):
        value = self.value(node)
        if isinstance(value, str):
            return [value]
        if isinstance(value, list) and value and len(value) <= 64 and all(isinstance(item, str) for item in value):
            return value
        return None

    def file_path(self, node):
        value = self.value(node)
        if isinstance(value, str):
            return value
        if isinstance(node, ast.Call) and self.qualified(node.func) in {'Path', 'pathlib.Path'} and len(node.args) == 1 and not node.keywords:
            return self.file_path(node.args[0])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            left, right = self.file_path(node.left), self.file_path(node.right)
            if left is not None and right is not None:
                return str(PurePosixPath(left) / right)
        return None


def analyze(path, content):
    facts = code_index.parse_source(path, content)
    facts['_impact_source'] = content.decode('utf-8-sig') if isinstance(content, bytes) else content
    proof = {'uncertainties': [], 'resolved': [], 'module_targets': [], 'file_reads': [], 'accesses': [], 'protected_effect': False,
             'unbounded_file_read': False}
    facts['impact'] = proof
    if any(reason != 'dynamic_reference' for reason in facts['uncertainties']):
        proof['uncertainties'].extend(facts['uncertainties'])
    try:
        source = Source(path, content)
    except (SyntaxError, ValueError, UnicodeError, RecursionError):
        proof['uncertainties'].append('parse_failed')
        return facts

    def unknown(node, kind):
        proof['uncertainties'].append(kind + ':' + str(node.lineno))

    def resolved(node, kind, targets=()):
        proof['resolved'].append({'kind': kind, 'line': node.lineno, 'targets': list(targets)})
        proof['module_targets'].extend(targets)

    special_nodes = set()
    for node in ast.walk(source.syntax):
        if isinstance(node, ast.Call):
            name = source.qualified(node.func)
            leaf = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ''
            if leaf in {'commit', 'rollback', 'execute', 'transition', 'check_permission', 'authorize',
                        'chmod', 'chown', 'unlink', 'rmdir', 'remove', 'rmtree', 'replace'}:
                proof['protected_effect'] = True
            if isinstance(node.func, ast.Name) and node.func.id in source.dynamic_bindings and node.func.id not in source.bindings:
                unknown(node, 'rebound_dynamic_binding')
            if name in _LOADERS:
                special_nodes.add(node.func)
                targets = source.strings(node.args[0]) if node.args else None
                # Relative import needs its actual package, not a filename guess.
                if targets and len(node.args) == 1 and not any(target.startswith('.') for target in targets) and not node.keywords:
                    resolved(node, 'bounded_import', targets)
                else:
                    unknown(node, 'unbounded_import')
            elif name in _REFLECTION:
                special_nodes.add(node.func)
                owner = source.qualified(node.args[0]) if node.args else None
                fields = source.strings(node.args[1]) if len(node.args) > 1 else None
                if fields and not set(fields).intersection(_NAMESPACES | _OPAQUE_NAMES) and owner and any(owner == binding or owner.startswith(binding + '.') for binding in source.bindings.values()) and owner.split('.')[0] not in {'importlib', 'builtins', 'sys', 'runpy'}:
                    resolved(node, 'bounded_module_attribute', [owner])
                    proof['accesses'].extend(owner + '.' + attribute for attribute in fields)
                else:
                    unknown(node, 'unbounded_reflection')
            elif name and name.startswith('sys.modules.'):
                special_nodes.add(node.func)
                if isinstance(node.func, ast.Attribute):
                    special_nodes.add(node.func.value)
                targets = source.strings(node.args[0]) if len(node.args) == 1 else None
                if name == 'sys.modules.get' and targets and not node.keywords:
                    resolved(node, 'bounded_module_registry', targets)
                else:
                    unknown(node, 'unbounded_module_registry')
            elif name in _EXECUTION or leaf in _OPAQUE_NAMES:
                special_nodes.add(node.func)
                unknown(node, 'unbounded_execution')
            elif name in _PROCESSES or name and name.startswith(('os.exec', 'os.spawn')):
                special_nodes.add(node.func)
                shell = next((source.value(kw.value) for kw in node.keywords if kw.arg == 'shell'), False)
                if any(kw.arg is None for kw in node.keywords):
                    shell = UNKNOWN
                argv = source.value(node.args[0]) if node.args else UNKNOWN
                if shell is not False:
                    unknown(node, 'unbounded_process')
                elif isinstance(argv, list) and len(argv) == 2 and isinstance(argv[0], str) and PurePosixPath(argv[0]).name in {'git', 'gh'} and argv[1] == '--version':
                    resolved(node, 'external_non_python_command')
                elif isinstance(argv, list) and len(argv) >= 3 and argv[1] == '-m' and isinstance(argv[2], str) and (
                        isinstance(argv[0], str) and PurePosixPath(argv[0]).name in {'python', 'python3'}
                        or isinstance(node.args[0], (ast.List, ast.Tuple)) and source.qualified(node.args[0].elts[0]) == 'sys.executable'):
                    resolved(node, 'bounded_python_module', [argv[2]])
                else:
                    unknown(node, 'unbounded_process')
            elif name in {'globals', 'locals', 'vars', 'builtins.globals', 'builtins.locals', 'builtins.vars'}:
                special_nodes.add(node.func)
                unknown(node, 'unbounded_namespace')
            if leaf in _FILE_METHODS or name in _FILE_FUNCTIONS:
                expression = node.args[0] if name in _FILE_FUNCTIONS and node.args else node.func.value if isinstance(node.func, ast.Attribute) else None
                target = source.file_path(expression) if expression is not None else None
                if target is None or any(part in {'.', '..'} for part in target.split('/')):
                    proof['unbounded_file_read'] = True
                else:
                    proof['file_reads'].append(target)
            if leaf in _FILE_ALIASES or name and name.rsplit('.', 1)[-1] in _FILE_ALIASES:
                proof['unbounded_file_read'] = True
        elif isinstance(node, ast.Subscript) and source.qualified(node.value) == 'sys.modules':
            special_nodes.add(node.value)
            targets = source.strings(node.slice)
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                unknown(node, 'module_registry_mutation')
            elif targets:
                resolved(node, 'bounded_module_registry', targets)
            elif isinstance(node.slice, ast.Name) and node.slice.id == '__name__' and not source.writes['__name__']:
                resolved(node, 'own_module_registry', [code_index.module_name(path)])
            else:
                unknown(node, 'unbounded_module_registry')
    # Merely assigning a loader to another name must not bypass the checks.
    for node in ast.walk(source.syntax):
        name = source.qualified(node)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == '__builtins__' or isinstance(node, ast.Attribute) and node.attr in _NAMESPACES:
            unknown(node, 'unbounded_namespace')
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) or isinstance(node, ast.Attribute):
            if name:
                proof['accesses'].append(name)
        if name in _FILE_FUNCTIONS or isinstance(node, ast.Attribute) and node.attr in _FILE_METHODS | _FILE_ALIASES:
            parent = source.parents.get(node)
            if not isinstance(parent, ast.Call) or parent.func is not node:
                proof['unbounded_file_read'] = True
        if (name in _LOADERS | _EXECUTION | _REFLECTION | _PROCESSES | {'sys.modules'} or name and name.startswith('sys.modules.')) and node not in special_nodes:
            parent = source.parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node:
                continue
            unknown(node, 'escaped_dynamic_binding')
    proof['uncertainties'] = sorted(set(proof['uncertainties']))
    proof['module_targets'] = sorted(set(proof['module_targets']))
    proof['file_reads'] = sorted(set(proof['file_reads']))
    proof['accesses'] = sorted(set(proof['accesses']))
    return facts


def passive(source):
    """Only declarations with no project-evaluated expressions are passive."""
    for node in source.syntax.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            expressions = [*node.decorator_list, *node.args.defaults,
                           *(x for x in node.args.kw_defaults if x), node.returns,
                           *(arg.annotation for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))]
            if any(expr is not None and not isinstance(expr, ast.Constant) for expr in expressions):
                return False
            continue
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
            try:
                ast.literal_eval(node.value)
            except (ValueError, TypeError, RecursionError):
                return False
            continue
        return False
    return True


def resolve(facts, aliases=None):
    graph = code_index.resolve(facts, facts, module_aliases=aliases)
    modules = {code_index.module_name(path): path for path in facts}
    modules.update(aliases or {})
    for path, item in graph.items():
        if 'impact' in item:
            item['impact'] = deepcopy(item['impact'])
        proof = item.get('impact')
        if proof is None:
            continue  # Legacy/raw facts remain conservative, never fabricated.
        item['raw_uncertainties'] = list(item['uncertainties'])
        item['selection_uncertainties'] = sorted(set(proof['uncertainties'] + [
            reason for reason in item['uncertainties'] if reason != 'dynamic_reference']))
        for name in proof['module_targets']:
            target = modules.get(name)
            if target:
                item['references'].append({'path': target, 'symbol': '*', 'line': 0})
            elif name.split('.')[0] in {n.split('.')[0] for n in modules}:
                item['selection_uncertainties'].append('unresolved_bounded_import:' + name)

    # Importing a child module also executes its package initializers. Keep
    # those implicit load dependencies, including before/after moves.
    for item in graph.values():
        initializers = set()
        for reference in item['references']:
            parts = PurePosixPath(reference['path']).parts
            for length in range(1, len(parts)):
                initializer = '/'.join((*parts[:length], '__init__.py'))
                if initializer in graph and initializer != reference['path']:
                    initializers.add(initializer)
        item['references'].extend({'path': target, 'symbol': '*', 'line': 0} for target in sorted(initializers))

    sources = {p: Source(p, f['_impact_source']) for p, f in graph.items() if '_impact_source' in f and 'parse_failed' not in f.get('selection_uncertainties', [])}
    # A project can re-export a loader or module registry under an innocent
    # name. Do not mistake access to that alias for an ordinary project API.
    for path, item in graph.items():
        for access in item.get('impact', {}).get('accesses', []):
            parts = access.split('.')
            for length in range(1, len(parts)):
                owner = modules.get('.'.join(parts[:length]))
                if owner in sources:
                    qualified = sources[owner].bindings.get(parts[length])
                    if qualified in _LOADERS | _REFLECTION | _EXECUTION | _PROCESSES | {'sys.modules'}:
                        item['selection_uncertainties'].append('escaped_project_dynamic_binding:' + access)
                    # Usage hints prioritize actual re-export users even when
                    # initialization effects require keeping the broad graph.
                    # They add edges; they never delete initialization coverage.
                    target = modules.get(qualified or '') or modules.get((qualified or '').rsplit('.', 1)[0])
                    if target and target != path:
                        graph[target].setdefault('usage_dependents', []).append(path)
        if 'selection_uncertainties' in item:
            item['selection_uncertainties'] = sorted(set(item['selection_uncertainties']))
    passive_cache = {}
    def imports_passively(path, seen):
        if path in seen:
            return False
        if path in passive_cache:
            return passive_cache[path]
        source = sources.get(path)
        passive_cache[path] = bool(source and passive(source) and not graph[path].get('selection_uncertainties')
                    and all(imports_passively(r['path'], seen | {path}) for r in graph[path]['references']))
        return passive_cache[path]

    for path, item in graph.items():
        source = sources.get(path)
        if not source:
            continue
        refined = []
        for reference in item['references']:
            target = reference['path']
            facade = sources.get(target)
            if not facade or not imports_passively(target, set()):
                refined.append(reference)
                continue
            # Only direct re-exports through top-level absolute from-imports.
            owners = {}
            for declaration in facade.syntax.body:
                if isinstance(declaration, ast.ImportFrom) and not declaration.level:
                    for alias in declaration.names:
                        owner = modules.get('.'.join(filter(None, (declaration.module, alias.name)))) or modules.get(declaration.module)
                        if owner:
                            owners[alias.asname or alias.name] = owner
            bindings = {name for name, qualified in source.bindings.items() if modules.get(qualified) == target}
            uses = []
            if bindings:
                for node in ast.walk(source.syntax):
                    if isinstance(node, ast.Name) and node.id in bindings and isinstance(node.ctx, ast.Load):
                        parent = source.parents.get(node)
                        if not isinstance(parent, ast.Attribute) or parent.value is not node or parent.attr not in owners:
                            uses = None
                            break
                        uses.append(parent.attr)
            elif reference['symbol'] in owners:
                uses = [reference['symbol']]
            if not uses:
                refined.append(reference)
                continue
            for name in sorted(set(uses)):
                refined.append({**reference, 'path': owners[name]})
            graph[target].setdefault('reexport_consumers', []).append(path)
            item['impact']['resolved'].append({'kind': 'passive_reexport', 'line': reference['line'],
                'targets': sorted({owners[name] for name in uses}), 'via': target})
        item['references'] = refined
    for item in graph.values():
        item['dependents'], item['related_tests'] = [], []
    for path, item in graph.items():
        for reference in item['references']:
            target = reference['path']
            graph[target]['dependents'].append(path)
            if path.startswith(('tests/', 'test/')):
                graph[target]['related_tests'].append(path)
    for item in graph.values():
        item['dependents'] = sorted(set(item['dependents']))
        item['related_tests'] = sorted(set(item['related_tests']))
        if 'usage_dependents' in item:
            item['usage_dependents'] = sorted(set(item['usage_dependents']))
    return graph


def review_document(path, graphs, configuration, documents, test_paths, file_inventory):
    """A bounded role AND consumption check; extension/list exclusion is insufficient."""
    parts = PurePosixPath(path).parts
    role = path in {'docs/PRODUCT.md', 'docs/ARCHITECTURE.md'} or (
        len(parts) == 3 and parts[2].endswith('.md') and (
            parts[:2] == ('docs', 'plans') and parts[2].startswith('[G-')
            or parts[:2] == ('docs', 'requirements') and parts[2].startswith('[R-')))
    if not role:
        return None, 'non_python_input:' + path
    versions = (documents or {}).get(path)
    if not versions or any(mode not in {'100644', '100755'} or b'\0' in content
                           for mode, content in versions.values()):
        return None, 'document_source_unproven:' + path
    try:
        for _, content in versions.values():
            content.decode('utf-8-sig')
    except UnicodeError:
        return None, 'document_source_unproven:' + path
    stages = configuration.get('stages')
    if not isinstance(stages, (tuple, list)) or not stages or any(stage['kind'] not in {'tests', 'command'} or stage['kind'] == 'command' and (
            stage.get('argv', [])[:3] != ['{python}', '{runner}', '--compile']
            or len(stage['argv']) < 4 or any('docs' in arg or arg.startswith('-') for arg in stage['argv'][3:])) for stage in stages):
        return None, 'document_command_consumption_unproven:' + path
    if any(stage['kind'] == 'tests' and stage.get('cwd', '.') != '.' for stage in stages):
        return None, 'document_execution_context_unproven:' + path
    for label, graph in zip(('base', 'head'), graphs):
        pending, visited = list(test_paths), set()
        while pending:
            consumer = pending.pop()
            if consumer in visited or consumer not in graph:
                continue
            visited.add(consumer)
            facts = graph[consumer]
            pending.extend(reference['path'] for reference in facts['references'])
            proof = facts.get('impact')
            if proof is None or proof['unbounded_file_read']:
                return None, 'document_consumption_unproven:' + consumer
            for literal in proof['file_reads']:
                if literal in {path, PurePosixPath(path).name, 'docs', 'docs/'} or path.startswith(literal.rstrip('/') + '/') or '*' in literal and literal.startswith(('docs', '*.md')):
                    return None, 'document_test_input:' + consumer + ':' + path
                # Even a different extension may be a symlink to this document.
                # Unknown/generated/external paths also lack an exclusion proof.
                if (file_inventory or {}).get(literal, {}).get(label) not in {'100644', '100755'}:
                    return None, 'document_read_source_unproven:' + consumer + ':' + literal
    return {'kind': 'review_only_document', 'path': path,
            'source_identity': {label: hashlib.sha256(mode.encode() + b'\0' + content).hexdigest()
                                for label, (mode, content) in versions.items()},
            'evidence': 'reserved_review_role;bounded_reachable_reads;no_document_command'}, None
