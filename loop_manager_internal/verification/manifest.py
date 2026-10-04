"""원래 unittest discovery와 load_tests 결과를 모듈 단위 manifest로 보존한다."""
from collections import Counter
from dataclasses import dataclass
import unittest
import importlib
from pathlib import Path


def test_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from test_cases(test)
        else:
            yield test


def test_ids(suite):
    return (test.id() for test in test_cases(suite))


@dataclass(frozen=True)
class Module:
    name: str
    ids: tuple[str, ...]
    owners: tuple[tuple[str, str, str], ...] = ()
    pattern: str | None = None


class ManifestLoader(unittest.TestLoader):
    def __init__(self):
        super().__init__()
        self.modules, self.depth = [], 0

    def loadTestsFromModule(self, module, *args, **kwargs):
        outer = self.depth == 0
        self.depth += 1
        try:
            suite = super().loadTestsFromModule(module, *args, **kwargs)
            if outer:
                ids = tuple(test_ids(suite))
                if ids:
                    owners = tuple((test.id(), test.__class__.__module__,
                                    f"{test.__class__.__module__}.{test.__class__.__qualname__}") for test in test_cases(suite))
                    self.modules.append(Module(module.__name__, ids, owners, kwargs.get("pattern")))
            return suite
        finally:
            self.depth -= 1


def discover(directory):
    loader = ManifestLoader()
    suite = loader.discover(str(directory))
    if loader.errors:
        raise RuntimeError("unittest 수집 실패:\n" + "\n".join(loader.errors))
    expected = tuple(test_ids(suite))
    grouped = tuple(identity for module in loader.modules for identity in module.ids)
    if not expected or expected != grouped or len(set(expected)) != len(expected):
        raise RuntimeError("unittest manifest가 비었거나 수집 범위가 중복/불일치합니다.")
    return loader.modules


def load_module(module):
    loader = unittest.TestLoader()
    loaded = importlib.import_module(module.name)
    loader._top_level_dir = str(Path.cwd() / "tests")
    if hasattr(loaded, "__path__"):
        loader._loading_packages.add(module.name)
    try:
        suite = loader.loadTestsFromModule(loaded, pattern=module.pattern)
    finally:
        loader._loading_packages.discard(module.name)
    if loader.errors or tuple(test_ids(suite)) != module.ids:
        raise RuntimeError(f"unittest 모듈 수집 범위 불일치: {module.name}")
    owners = tuple((test.id(), test.__class__.__module__,
                    f"{test.__class__.__module__}.{test.__class__.__qualname__}") for test in test_cases(suite))
    if module.owners and owners != module.owners:
        raise RuntimeError(f"unittest fixture 소유자 불일치: {module.name}")
    return suite


def validate_report(module, report):
    if report.get("module") != module.name or not isinstance(report.get("passed"), bool):
        raise RuntimeError("unittest 자식 결과 형식 오류")
    completed = report.get("completed", [])
    if not isinstance(completed, list) or not all(isinstance(identity, str) for identity in completed):
        raise RuntimeError("unittest 완료 결과 형식 오류")
    sources = set(report.get("fixture_skip_sources", []))
    allowed_skips = {identity for identity, owner, klass in module.owners
                     if f"setUpModule ({owner})" in sources or f"setUpClass ({klass})" in sources}
    if not set(report.get("fixture_skipped", [])).issubset(allowed_skips):
        raise RuntimeError("unittest fixture skip 근거가 없습니다.")
    covered = completed + report.get("fixture_skipped", [])
    if len(covered) != len(set(covered)) or not (Counter(covered) <= Counter(module.ids)):
        raise RuntimeError("unittest 결과 중복/범위 오류")
    if report["passed"] and Counter(covered) != Counter(module.ids):
        raise RuntimeError("unittest 성공 결과에 누락이 있습니다.")
    return report["passed"]
