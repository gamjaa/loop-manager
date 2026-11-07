from loop_manager_internal.storage import database
from loop_manager_internal.storage import execplan_catalog_state
from tests.support.lifecycle import fixture_cleanup
import sqlite3
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock
from pathlib import Path
import loop_manager
from loop_manager_internal.catalog import execplan_catalog


def make_catalog_root(directory: str) -> Path:
    root = Path(directory)
    (root / "docs/plans").mkdir(parents=True)
    (root / "docs/PRODUCT.md").write_text("# Product\n", encoding="utf-8")
    return root


@contextmanager
def planner_git_observations(documents, *, revision='base'):
    """Supply revision tree/document observations; run actual Planner validation."""
    def git(_repo, *args, **_kwargs):
        if len(args) == 6 and args[:4] == ('ls-tree', '-z', '--full-tree', revision) and args[4] == '--':
            path = args[5]
            output = f'100644 blob observed\t{path}\0' if path in documents else ''
            return SimpleNamespace(returncode=0, stdout=output)
        if len(args) == 2 and args[0] == 'show' and args[1].startswith(revision + ':'):
            path = args[1][len(revision) + 1:]
            return SimpleNamespace(returncode=0 if path in documents else 128,
                                   stdout=documents.get(path, 'missing document'))
        raise AssertionError(f'unexpected Planner Git: {args}')

    with mock.patch.object(loop_manager, 'git', side_effect=git) as observed, \
         mock.patch.object(loop_manager, 'changed_files', return_value=[]), \
         mock.patch('subprocess.Popen', side_effect=AssertionError('unexpected Planner process')):
        yield observed


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


class DeletedPlanCancellationFixture:
    """Native cancellation and terminated-call setup for catalog deletion tests."""

    def rowless_cancellation(self, *, started=False, empty_checkpoint=False, blocked=False,
                             empty_blocked_checkpoint=False, resolved_blocked_checkpoint=False,
                             requested_blocked_checkpoint=False, native_block=False, checkpoint_snapshot=None, owned_workspace=None, published_blocked=False, checkpoint_builder=None, withdrawn=True, publication_oid=None):
        from loop_manager_internal.catalog.execplan_catalog import plan_content_hash
        from loop_manager_internal.maintenance.maintenance_planning import ReadyPlanSnapshot
        from loop_manager_internal.planning.planning_catalog import PlanDeletion
        from loop_manager_internal.storage.planning_publication_state import apply_mutable_catalog_changes, _MUTABLE_READY_EVIDENCE_FIELDS
        from loop_manager_internal.storage.runtime_state import _transaction
        text = self.fixture._text('G-001', 'Gone')
        self.connection.execute('UPDATE exec_plans SET ' + ','.join(field + '=NULL' for field in _MUTABLE_READY_EVIDENCE_FIELDS)
            + ",status='READY',ever_started=0,workspace_owned=0,retry_count=0,pr_conflict_recovery=0 WHERE plan_id='G-001'")
        self.connection.execute('UPDATE projects SET phase=NULL,plan_path=NULL,branch=NULL,worktree=NULL,worker_session_id=NULL,pr_url=NULL')
        self.connection.execute("UPDATE exec_plans SET path='docs/plans/[G-001] Gone.md',title='Gone',"
            "priority='normal',content_hash=?,plan_sha=?,current_revision_sha=? WHERE plan_id='G-001'",
            (plan_content_hash(text), 'a' * 40, 'a' * 40))
        if started:
            self.connection.execute("UPDATE exec_plans SET ever_started=1,retry_count=2,worker_session_id='old-session',"
                "worker_result='saved result',verify_result='saved failure',last_error='historical failure',"
                "verify_failure_history='{}' WHERE plan_id='G-001'")
        if blocked:
            self.connection.execute("UPDATE exec_plans SET status='PLAN_BLOCKED',blocked_reason='saved block' WHERE plan_id='G-001'")
        self.connection.commit()
        original_block = None
        if owned_workspace is not None:
            self.connection.execute("UPDATE exec_plans SET branch='loop-manager/G-001',worktree=?,workspace_owned=1 WHERE plan_id='G-001'",
                                    (str(owned_workspace),))
            self.connection.commit()
        if native_block:
            from loop_manager_internal.storage.runtime_state import RuntimeStateRepository
            RuntimeStateRepository(self.connection).block_plan('/repo', 'G-001', reason_code='plan_input_invalid',
                evidence='saved native block', release_fingerprint='c' * 64, enqueue=False)
            original_block = dict(self.connection.execute("SELECT * FROM execplan_blocks WHERE plan_id='G-001'").fetchone())
        if published_blocked:
            from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
            from loop_manager_internal.recovery.blocked_checkpoint import PreparedCheckpoint, checkpoint_identity
            checkpoints = BlockedCheckpointRepository(self.connection)
            request = checkpoints.get('/repo', 'G-001')
            identity = checkpoint_identity(plan_id='G-001', reason_code=request.reason_code, evidence=request.evidence,
                parent_oid='a' * 40, tree_oid='c' * 40, included_paths=())
            checkpoints.record_prepared(request, PreparedCheckpoint('a' * 40, 'c' * 40, identity, 'checkpoint', ()))
            checkpoints.record_remote_observation(request, None)
            checkpoints.record_local(request, 'b' * 40)
            checkpoints.finish_published(request, 'b' * 40)
            checkpoints.record_rebased_publication(request, 'e' * 40, 'f' * 40)
        if publication_oid is not None:
            self.connection.execute("UPDATE exec_plans SET publication_remote_oid=? WHERE plan_id='G-001'", (publication_oid,))
        row = self.connection.execute("SELECT * FROM exec_plans WHERE plan_id='G-001'").fetchone()
        plan = ReadyPlanSnapshot('G-001', row['path'], row['title'], tuple(json.loads(row['sources'])), (),
            'low', 'low', row['priority'], row['content_hash'], 'a' * 40, text,
            current_complexity=row['current_complexity'], current_risk=row['current_risk'], current_metadata_recorded=True)
        if started:
            from dataclasses import replace
            from loop_manager_internal.planning.planner_validation import planning_execution_state
            plan = replace(plan, execution_state=planning_execution_state(row))
        judgment = {'decision': {'id': 'G-001', 'source': 'docs/PRODUCT.md', 'outcome': 'withdrawn',
            'reason': 'The original goal was withdrawn.'}} if withdrawn else None
        if empty_checkpoint:
            from loop_manager_internal.storage.execution_checkpoint_state import ExecutionCheckpointRepository
            ExecutionCheckpointRepository(self.connection).record('/repo', 'G-001', expected_generation=0,
                operation=None, expected_head=None, target_base_oid=None, observed_remote_oid=None,
                snapshot=checkpoint_snapshot if checkpoint_snapshot is not None else {})
        if checkpoint_builder is not None:
            checkpoint_builder()
        if empty_blocked_checkpoint or resolved_blocked_checkpoint or requested_blocked_checkpoint:
            from loop_manager_internal.storage.blocked_checkpoint_state import BlockedCheckpointRepository
            checkpoints = BlockedCheckpointRepository(self.connection)
            request = checkpoints.request(repo='/repo', plan_id='G-001', reason_code='no-resource',
                evidence='No owned workspace.', branch=None, worktree=None,
                expected_remote_oid=None, rebase_recovery=False)
            if resolved_blocked_checkpoint:
                self.assertIsNone(checkpoints.reconcile_cause(request))
            elif not requested_blocked_checkpoint:
                checkpoints.finish_unpublished(request, 'No workspace was created.', retryable=False)
            else:
                self.connection.commit()
        with _transaction(self.connection):
            apply_mutable_catalog_changes(self.connection, '/repo', deletions=(PlanDeletion(plan, judgment),), revisions=(),
                snapshot_token='saved', plan_commit='b' * 40, content_hash=lambda _: '', agent=False)
        audit = json.loads(self.connection.execute("SELECT recovery_evidence FROM execplan_cancellations WHERE plan_id='G-001'").fetchone()[0])
        self.assertEqual(audit['row'], dict(row))
        self.assertEqual(audit['text'], text)
        if native_block:
            self.assertEqual(audit['block'], original_block)
        return text

    def artifact_call(self, identity, repo, plan_id, receipt=None):
        self.connection.execute('INSERT INTO agent_invocations '
            '(invocation_id,repo,plan_id,role,provider,model,effort,transition,attempt,started_at,finished_at,'
            'execution_status,execution_started_at,execution_finished_at,execution_finish_evidence,process_receipt_path,session_id) '
            "VALUES (?,?,?,'worker','codex','model','high','start',1,'prepared','collected','executed',"
            "'started','stopped','provider_process_finished',?,'shared-session')",
            (identity, repo, plan_id, str(receipt) if receipt else None))



QUESTION = """\n## Draft Decision

Question: Which storage policy should this goal use?
Facts: Both policies preserve the accepted goal.
Options: A retains history; B retains only the current value.
Impact: The choice changes the implementation and recovery tests.
"""

def draft_text(plan_id="G-001", title="Decision"):
    return plan_text(plan_id, title).replace("risk: low\n", "risk: low\ndraft: true\n") + QUESTION

def accepted_text(text, revision, decision="approve", answer="Use policy A and retain history."):
    return text.replace("draft: true\n", "") + (
        f"\n## User Decision\n\nDecision: {decision}\nRevision: {revision}\nAnswer: {answer}\n"
    )
