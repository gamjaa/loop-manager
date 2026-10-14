"""Per-step validated catalog boundary, including proven archive deltas."""

from dataclasses import replace
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import sqlite3

from loop_manager_internal.storage.execplan_archive_state import MergedArchiveCandidate
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state as state
from loop_manager_internal.storage.runtime_state import planning_owns_execution_slot
from loop_manager_internal.git.command_adapter import ExternalObservationError, revision_path_exists


def validated_held_planning_snapshot(
    connection: sqlite3.Connection, repo: str, worktree: Path, active_sha: str,
    *, git: Callable, load_snapshot: Callable,
    mutable_paths: frozenset[str] = frozenset(),
    recovery_mode: bool = False,
) -> execplan_catalog.ExecPlanCatalogSnapshot:
    """Reuse only unchanged, revision-bound errors for either Planner consumer."""
    view = connection.execute(
        "SELECT revision_sha FROM execplan_catalog_views WHERE repo = ?", (repo,),
    ).fetchone()
    if view is None or view[0] != active_sha:
        raise execplan_catalog.ExecPlanCatalogError(
            "planning 기준과 보류 catalog revision이 다릅니다.", code="held-planning-mismatch")
    local = load_snapshot(worktree, isolate_errors=True)
    known_ids = frozenset(str(row[0]) for row in connection.execute(
        "SELECT plan_id FROM exec_plans WHERE repo = ?", (repo,),
    ))
    issues = state.new_declaration_issues(state.PinnedCatalog(active_sha, local), known_ids, root=worktree)
    local = execplan_catalog.withhold_catalog_issues(local, issues)
    pending = connection.execute(
        "SELECT plan_id, evidence FROM execplan_catalog_holds WHERE repo = ?", (repo,),
    ).fetchall()
    held = {str(row[0]): json.loads(str(row[1])) for row in pending}
    deletion_holds = any(cause["code"] in {"missing-plan", "deleted-goal"}
                         for evidence in held.values() for cause in evidence.get("causes", ()))
    if recovery_mode or deletion_holds:
        # A deletion recovery may repair only its snapshot files. All other
        # quarantined bytes stay pinned, including synthetic missing-file holds.
        editable = {key for key, evidence in held.items() if evidence.get("path") in mutable_paths}
        if local.withheld_ids - (held.keys() - editable):
            raise execplan_catalog.ExecPlanCatalogError(
                "삭제 복구 결과에 새 오류 또는 미해결 개정 대상이 있습니다.", code="held-planning-mismatch")
        for key, evidence in held.items():
            path = evidence.get("path")
            if key in editable or not path:
                continue
            exists = revision_path_exists(git, worktree, active_sha, path)
            result = git(worktree, "diff", "--quiet", active_sha, "--", path, check=False)
            if result.returncode not in (0, 1):
                raise ExternalObservationError(f"보류 catalog 파일의 Git 변경을 확인할 수 없습니다: {path}")
            if result.returncode or (not exists and (worktree / path).exists()):
                raise execplan_catalog.ExecPlanCatalogError(
                    f"snapshot 밖 보류 파일이 변경됐습니다: {path}", code="held-planning-mismatch", path=path)
        return local
    causes = [dict(plan_id=item.plan_id, path=item.path, code=item.code,
                   message=item.message, source=item.source) for item in local.issues]
    declaration_holds = {key for key, evidence in held.items() if "causes" in evidence}
    if (local.withheld_ids != declaration_holds
            or any(held[key].get("causes") != causes for key in local.withheld_ids)
            or any(held[key].get("content_hash") != content_hash
                   for key, _path, content_hash in local.withheld)):
        raise execplan_catalog.ExecPlanCatalogError(
            "planning 결과가 보류 catalog 오류의 원인이나 범위를 변경했습니다.",
            code="held-planning-mismatch")
    for _, path, _ in local.withheld:
        if not revision_path_exists(git, worktree, active_sha, path):
            raise execplan_catalog.ExecPlanCatalogError(
                f"보류 catalog 파일이 변경됐습니다: {path}", code="held-planning-mismatch", path=path)
        result = git(worktree, "diff", "--quiet", active_sha, "--", path, check=False)
        if result.returncode not in (0, 1):
            raise ExternalObservationError(f"보류 catalog 파일의 Git 변경을 확인할 수 없습니다: {path}")
        if result.returncode:
            raise execplan_catalog.ExecPlanCatalogError(
                f"보류 catalog 파일이 변경됐습니다: {path}", code="held-planning-mismatch", path=path)
    return local


def assert_planning_dependencies_unheld(catalog, plans, held_ids) -> None:
    """Apply the same transitive hold gate to general and maintenance batches."""
    for path, plan in plans:
        pending, visited = list(plan["depends_on"]), set()
        while pending:
            dependency = pending.pop()
            if dependency in visited:
                continue
            visited.add(dependency)
            if dependency in held_ids:
                raise execplan_catalog.ExecPlanCatalogError(
                    f"새 planning 결과가 보류 계획에 의존합니다: {path} -> {dependency}",
                    code="held-planning-dependency", path=path)
            pending.extend(catalog.get(dependency, {}).get("depends_on", []))


class CatalogRefreshStopped(RuntimeError):
    def __init__(self, result: state.CatalogReconciliationResult):
        self.result = result
        super().__init__(str(result.kind.value))


class ProjectCatalogRefresh:
    def __init__(
        self,
        connection: sqlite3.Connection,
        repo: str,
        current_revision: Callable[[], str],
        reconcile: Callable[
            [str, Callable[[state.PinnedCatalog], None]], state.CatalogReconciliationResult
        ],
    ):
        self.repository = state.DefinitionStateRepository(connection)
        self.repo = repo
        self.current_revision = current_revision
        self.reconcile = reconcile
        self.pinned: state.PinnedCatalog | None = None
        self.revision: str | None = None
        self.declarations = None

    def _declarations(self):
        # PR and execution-session transitions leave declarations unchanged;
        # a planning slot change must recheck deferred execution holds.
        snapshot = self.repository.capture(self.repo)
        return (
            tuple(row.values[:state._ROW_VERSION_FIELDS.index("status")]
                  for row in snapshot.rows),
            tuple(item for item in snapshot.reserved if item[0] not in snapshot.plan_ids),
            snapshot.holds,
            snapshot.cancellations,
            tuple((row.plan_id, row.values[state._ROW_VERSION_FIELDS.index("status")])
                  for row in snapshot.rows) if snapshot.holds else (),
            planning_owns_execution_slot(dict(zip(state._PROJECT_VERSION_FIELDS, snapshot.project_values)))
            if snapshot.holds and snapshot.project_values else False,
        )

    def refresh(self, revision: str | None = None) -> str:
        revision = revision or self.current_revision()
        if (revision == self.revision
                and self.declarations == self._declarations()
                and self.current_revision() == revision):
            return revision
        result = self.reconcile(revision, self._remember)
        if not result.usable:
            raise CatalogRefreshStopped(result)
        assert result.revision_sha is not None
        self.revision = result.revision_sha
        self.declarations = self._declarations()
        return self.revision

    def _remember(self, pinned: state.PinnedCatalog) -> None:
        self.pinned = pinned

    def adopt_history(self) -> None:
        """History reconciliation is a known, durable local DB transition."""
        self.declarations = self._declarations()

    def apply_archive(
        self, base_sha: str, commit_oid: str,
        candidates: Sequence[MergedArchiveCandidate], *,
        expected: state.CatalogStateSnapshot | None = None,
    ) -> bool:
        """Called inside the archive success transaction after Git proof.

        The publisher proves the commit is exactly these byte-preserving moves.
        Revalidate the resulting graph from the pinned documents; no Git catalog
        reread is necessary. Repository CAS and remote checks still apply.
        """
        pinned = self.pinned
        if (pinned is None or pinned.snapshot.issues or pinned.revision_sha != base_sha
                or self.current_revision() != commit_oid
                or pinned.regular_paths is None):
            return False
        expected = expected or self.repository.capture(self.repo)
        if self.repository.capture(self.repo) != expected:
            return False
        for row in expected.rows:
            entry = pinned.snapshot.all.get(row.plan_id)
            if entry is None:
                return False
            current = self.repository.current_definition(self.repo, row.plan_id)
            if current != state.CurrentPlanDefinition(
                entry.plan_id, entry.path, entry.title, entry.sources, entry.depends_on,
                entry.complexity, entry.risk, entry.priority, entry.content_hash, base_sha,
            ):
                return False
        documents = {entry.path: entry.text for entry in pinned.snapshot.all.values()}
        paths = set(pinned.regular_paths)
        for candidate in candidates:
            entry = pinned.snapshot.active.get(candidate.plan_id)
            if (entry is None or entry.path != candidate.active_path
                    or entry.content_hash != candidate.content_hash
                    or candidate.revision_sha != base_sha
                    or candidate.archive_path in paths):
                return False
            documents[candidate.archive_path] = documents.pop(candidate.active_path)
            paths.remove(candidate.active_path)
            paths.add(candidate.archive_path)
        snapshot = execplan_catalog.snapshot_from_documents(
            documents, validate_sources=True,
            available_regular_paths=frozenset(paths), revision_sha=commit_oid,
            requirement_documents=pinned.requirement_documents,
        )
        result = replace(pinned, revision_sha=commit_oid, snapshot=snapshot,
                         regular_paths=frozenset(paths))
        if self.repository.apply(
            self.repo, result, expected,
            revision_is_current=lambda: self.current_revision() == commit_oid,
        ) is None:
            return False
        # A rolled-back owning transaction changes the declaration signature,
        # forcing refresh to replace this tentative cache on the next boundary.
        self.pinned = result
        self.revision = commit_oid
        self.declarations = self._declarations()
        return True
