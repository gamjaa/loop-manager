from loop_manager_internal.storage import database
from loop_manager_internal.storage import execplan_catalog_state
from tests.support.lifecycle import fixture_cleanup
import sqlite3
from pathlib import Path
import loop_manager
from loop_manager_internal.catalog import execplan_catalog


def make_catalog_root(directory: str) -> Path:
    root = Path(directory)
    (root / "docs/plans").mkdir(parents=True)
    (root / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")
    return root


def execplan_header(
    plan_id: str,
    title: str,
    *,
    sources: tuple[str, ...] = ("docs/PRODUCT.md",),
    depends_on: tuple[str, ...] = (),
    complexity: str = "low",
    risk: str = "low",
    priority: str | None = "normal",
    include_priority: bool = True,
    empty_sources_as_list: bool = True,
) -> str:
    if sources:
        source_lines = "sources:\n" + "".join(
            f"  - {source}\n" for source in sources
        ).rstrip("\n")
    elif empty_sources_as_list:
        source_lines = "sources: []"
    else:
        source_lines = "sources:\n"
    if depends_on:
        dependency_lines = "depends_on:\n" + "\n".join(
            f"  - {dependency}" for dependency in depends_on
        )
    else:
        dependency_lines = "depends_on: []"
    fields = [
        "---",
        f"id: {plan_id}",
        f"title: {title}",
        source_lines,
        dependency_lines,
        f"complexity: {complexity}",
        f"risk: {risk}",
    ]
    if include_priority:
        fields.append(f"priority: {priority}")
    fields.append("---")
    return "\n".join(fields)


def write_execplan(
    root: Path,
    plan_id: str,
    title: str,
    *,
    sources: tuple[str, ...] = ("docs/PRODUCT.md",),
    depends_on: tuple[str, ...] = (),
    complexity: str = "low",
    risk: str = "low",
    priority: str | None = "normal",
) -> Path:
    plan = root / "docs/plans" / f"[{plan_id}] {title}.md"
    plan.write_text(
        f"""{execplan_header(
            plan_id, title, sources=sources, depends_on=depends_on,
            complexity=complexity, risk=risk, priority=priority,
            include_priority=priority is not None,
        )}

## Purpose

Test plan.
""",
        encoding="utf-8", newline="\n",
    )
    return plan


def plan_text(
    plan_id: str,
    title: str,
    *,
    depends_on: tuple[str, ...] = (),
    priority: str = "normal",
    purpose: str = (
        "A concrete structural responsibility problem crosses the module boundary. "
        "Preserve user behavior, the public contract, and storage format."
    ),
    acceptance_criteria: str = (
        "Preserve user behavior, the public API contract, and storage format "
        "for `legacy/module.py`."
    ),
    implementation: str = "Separate the responsibility into a testable module boundary.",
) -> str:
    return f"""{execplan_header(plan_id, title, depends_on=depends_on, priority=priority)}

## Purpose

{purpose}

## Acceptance Criteria

{acceptance_criteria}

## Implementation

{implementation}

## Validation

- Run the targeted unit test for this responsibility boundary.
- The full verify must preserve existing behavior.

## Progress

- [ ] Pending

## Decision Log

- Keep the change structural.
"""


def archived_entry(
    plan_id: str = "G-001",
    title: str = "Archived",
    *,
    text_suffix: str = "",
) -> execplan_catalog.ExecPlanCatalogEntry:
    text = f"""{execplan_header(plan_id, title)}

## Purpose

Archived fixture.{text_suffix}
"""
    return execplan_catalog.snapshot_from_documents(
        {f"docs/plans/archive/[{plan_id}] {title}.md": text}
    ).archived[plan_id]

class FakeDefinitionPort:
    def __init__(self, pinned, revisions=None):
        self.pinned = pinned
        self.revisions = list(revisions or [pinned.revision_sha] * 8)

    def load(self):
        return self.pinned

    def current_revision(self):
        return self.revisions.pop(0)

def active_snapshot(revision, documents):
    snapshot = execplan_catalog.snapshot_from_documents(
        documents, revision_sha=revision
    )
    return execplan_catalog_state.PinnedCatalog(
        revision, snapshot, frozenset({"docs/PRODUCT.md"})
    )


class CatalogReconciliationFixture:
    """Reusable setup and adapters; no discoverable test methods."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(database.SCHEMA)
        database._ensure_execplans_schema(self.connection)
        loop_manager.notifications.ensure_schema(self.connection)
        self.connection.execute(
            "INSERT INTO projects (repo, base_branch, phase, plan_path, "
            "branch, worktree, worker_session_id, pr_url) "
            "VALUES ('/repo', 'main', 'WORKING', ?, 'branch', '/worktree', "
            "'project-worker', 'https://example/pr/1')",
            ("docs/plans/[G-001] Old.md",),
        )
        self._insert(
            "G-001", "Old", status="IMPLEMENTING", effective=8,
            branch="branch", worktree="/worktree", workspace_owned=1,
            worker_session="worker", reviewer_session="reviewer",
            pr_url="https://example/pr/1", retry_count=4,
        )
        self._insert("G-002", "Dependency", status="MERGED")
        self.connection.commit()
        self.repository = execplan_catalog_state.DefinitionStateRepository(
            self.connection
        )

    def tearDown(self):
        self.connection.close()

    def _insert(
        self,
        plan_id,
        title,
        *,
        status,
        effective=1,
        branch=None,
        worktree=None,
        workspace_owned=0,
        worker_session=None,
        reviewer_session=None,
        pr_url=None,
        retry_count=0,
    ):
        path = f"docs/plans/[{plan_id}] {title}.md"
        self.connection.execute(
            """
            INSERT INTO exec_plans (
                repo, plan_id, path, title, sources, depends_on, priority,
                content_hash, current_complexity, current_risk,
                current_revision_sha, publication_path,
                publication_content_hash, publication_sha, status, plan_sha,
                initial_worker_level, effective_worker_level, retry_count,
                branch, worktree, workspace_owned, worker_session_id,
                reviewer_session_id, pr_url
            ) VALUES ('/repo', ?, ?, ?, '["docs/PRODUCT.md"]', '[]',
                      'normal', ?, 'low', 'low', ?, ?, ?, ?, ?, ?, 1, ?,
                      ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                path,
                title,
                f"old-hash-{plan_id}",
                "a" * 40,
                path,
                f"old-hash-{plan_id}",
                "a" * 40,
                status,
                "a" * 40,
                effective,
                retry_count,
                branch,
                worktree,
                workspace_owned,
                worker_session,
                reviewer_session,
                pr_url,
            ),
        )

    @staticmethod
    def _text(
        plan_id,
        title,
        *,
        sources=("docs/PRODUCT.md",),
        depends_on=(),
        complexity="low",
        risk="low",
        priority="normal",
        body="Body.",
    ):
        return f"""{execplan_header(
            plan_id, title, sources=sources, depends_on=depends_on,
            complexity=complexity, risk=risk, priority=priority,
            empty_sources_as_list=False,
        )}

## Purpose

{body}
"""

    def _service(self, pinned, *, fault_hook=None, revisions=None):
        repository = execplan_catalog_state.DefinitionStateRepository(
            self.connection, fault_hook=fault_hook
        )
        return execplan_catalog_state.CatalogReconciliationService(
            repository,
            FakeDefinitionPort(pinned, revisions),
        )


def catalog_reconciliation_fixture(test):
    fixture = fixture_cleanup(test, CatalogReconciliationFixture())
    fixture.setUp()
    test.addCleanup(fixture.tearDown)
    return fixture
