"""Current import inventory and independent unittest shards; no child pool."""
from collections import Counter
from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
SHARD_COUNT = 8
IMPORT_CODE = """
import importlib, sqlite3, subprocess, sys
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError('resource effect during import')
sqlite3.connect = forbidden
subprocess.run = forbidden
# Windows asyncio subclasses Popen during import; forbid construction only.
class ForbiddenPopen(subprocess.Popen):
    __init__ = forbidden
subprocess.Popen = ForbiddenPopen
assert 'loop_manager' not in sys.modules
assert sys.argv[2] not in sys.modules
importlib.import_module(sys.argv[2])
if sys.argv[2] != 'loop_manager':
    assert 'loop_manager' not in sys.modules
if sys.argv[3] == 'package':
    assert all(getattr(m, '__file__', '').endswith('__init__.py')
               for name, m in sys.modules.items()
               if name.startswith('loop_manager_internal'))
"""


@dataclass(frozen=True, order=True)
class ImportTarget:
    kind: str
    module: str

    @property
    def test_name(self):
        # Dots remain visible and distinguish paths containing double underscores.
        return f"test_{self.kind}__{self.module}"

    @property
    def legacy_id(self):
        method = ('test_package_initializers_do_not_initialize_services'
                  if self.kind == 'package' else
                  'test_each_module_imports_first_without_loading_the_entry_point')
        return f"test_internal_packages.InternalPackageMigrationTests.{method}"


def inventory(root=ROOT):
    """Preserve both former loops, including the root package initializer."""
    targets = [ImportTarget('module', 'loop_manager')]
    for path in sorted((root / 'loop_manager_internal').rglob('*.py')):
        parts = (path.parent if path.name == '__init__.py' else path.with_suffix('')).relative_to(root).parts
        targets.append(ImportTarget('package' if path.name == '__init__.py' else 'module', '.'.join(parts)))
    return tuple(targets)


def validate_partition(targets, shards):
    expected, actual = Counter(targets), Counter(target for shard in shards for target in shard)
    if any(count != 1 for count in expected.values()) or actual != expected:
        raise ValueError('fresh import inventory/partition missing, duplicate or unknown target')
    if len({target.test_name for target in targets}) != len(targets):
        raise ValueError('fresh import test identity collision')


def partition(targets):
    """Stable membership across interpreters, independent of inventory size/order."""
    shards = [[] for _ in range(SHARD_COUNT)]
    for target in sorted(targets):
        key = f'{target.kind}:{target.module}'.encode('utf-8')
        number = int.from_bytes(hashlib.sha256(key).digest()[:8], 'big') % SHARD_COUNT
        shards[number].append(target)
    result = tuple(tuple(shard) for shard in shards)
    validate_partition(targets, result)
    return result


def check_import(case, target, root=ROOT):
    """One bounded, isolated interpreter per target, joined before the next."""
    command = [sys.executable, '-I', '-B', '-c', IMPORT_CODE, str(root), target.module, target.kind]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        case.fail(f'{target.kind} {target.module}: fresh interpreter {sys.executable}: {error}')
    case.assertEqual(result.returncode, 0,
        f'{target.kind} {target.module}: fresh interpreter {sys.executable} exited {result.returncode}\n'
        f'{result.stderr}\n{result.stdout}')


def suite(loader, owner, shard):
    if not 0 <= shard < SHARD_COUNT:
        raise ValueError('invalid fresh import shard')
    def test_method(target):
        def test(case):
            check_import(case, target)
        return test
    # A local class owns each shard's fixtures. Rebuild inventory on every load,
    # so the existing manifest loader rejects additions/deletions after discovery.
    methods = {target.test_name: test_method(target) for target in partition(inventory())[shard]}
    klass = type('FreshImportTests', (unittest.TestCase,), {'__module__': owner, **methods})
    return loader.loadTestsFromTestCase(klass)
