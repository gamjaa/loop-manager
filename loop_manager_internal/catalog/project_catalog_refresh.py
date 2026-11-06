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
    planned_ids: frozenset[str] = frozenset(),
) -> execplan_catalog.ExecPlanCatalogSnapshot:
    """Preserve pinned errors while allowing new plans to inherit their holds."""
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
        if (local.withheld_ids - held.keys() - planned_ids
                or any(issue.plan_id in planned_ids for issue in local.issues)
                or any(issue.plan_id in editable for issue in local.issues)):
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
    unplanned_withheld = local.withheld_ids - planned_ids
    if ((not declaration_holds and unplanned_withheld)
            or not declaration_holds <= unplanned_withheld
            or any(issue.plan_id in planned_ids for issue in local.issues)
            or any(held[key].get("causes") != causes for key in declaration_holds)
            or any(held[key].get("content_hash") != content_hash
                   for key, _path, content_hash in local.withheld
                   if key not in planned_ids and key in held)):
        raise execplan_catalog.ExecPlanCatalogError(
            "planning 결과가 보류 catalog 오류의 원인이나 범위를 변경했습니다.",
            code="held-planning-mismatch")
    for key, path, _ in local.withheld:
        if key in planned_ids:
            continue
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


def assert_planning_dependency_references(
    catalog, plans, held_ids, *, original_dependencies=None, withheld_ids=(),
    check_cycles=True,
) -> None:
    """A registered held prerequisite is a valid reference, not permission to run."""
    original_dependencies = original_dependencies or {}
    plans = tuple(plans)
    for path, plan in plans:
        pending = list(plan["depends_on"])
        visited = set()
        while pending:
            dependency = str(pending.pop())
            if dependency in visited:
                continue
            visited.add(dependency)
            if dependency in held_ids and dependency not in original_dependencies:
                raise execplan_catalog.ExecPlanCatalogError(
                    f"등록되지 않은 보류 계획에 의존합니다: {path} -> {dependency}",
                    code="held-planning-dependency", path=path)
            current = catalog.get(dependency)
            dependencies = (current.get("depends_on") if current is not None else None)
            if dependencies is None:
                dependencies = original_dependencies.get(dependency, ())
            pending.extend(dependencies)

    if not check_cycles:
        return

    dependencies = {
        str(plan_id): tuple(str(item) for item in metadata.get("depends_on", ()))
        for plan_id, metadata in catalog.items()
    }
    changed_ids = frozenset(str(plan["id"]) for _, plan in plans)
    for _, plan in plans:
        dependencies[str(plan["id"])] = tuple(str(item) for item in plan["depends_on"])
    # Quarantined dependency closure nodes have pinned files but no active
    # metadata. Use their registered edge only when the snapshot withheld them;
    # current declarations above always take priority.
    fallback_ids = frozenset(str(item) for item in held_ids) | frozenset(
        str(item) for item in withheld_ids
    )
    for plan_id in fallback_ids:
        if plan_id not in dependencies and plan_id in original_dependencies:
            dependencies[str(plan_id)] = tuple(
                str(item) for item in original_dependencies[plan_id]
            )
    cycles = execplan_catalog.dependency_cycle_paths(
        dependencies, relevant_ids=changed_ids,
    )
    if cycles:
        cycle = cycles[0]
        changed_id = next(item for item in cycle if item in changed_ids)
        path = next(path for path, plan in plans if str(plan["id"]) == changed_id)
        raise execplan_catalog.ExecPlanCatalogError(
            "이번 planning 선언이 순환 ExecPlan 의존성을 만듭니다: "
            + " -> ".join(cycle),
            code="dependency-cycle", path=path,
        )


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
        pending_deletion = (self.pinned is not None and self.pinned.regular_paths is not None
            and any(row[0] not in self.pinned.regular_paths for row in self.repository.connection.execute(
                "SELECT path FROM exec_plans WHERE repo=? AND status<>'MERGED' "
                "UNION ALL SELECT path FROM execplan_cancellations WHERE repo=? "
                "UNION ALL SELECT CASE WHEN json_valid(evidence) THEN json_extract(evidence,'$.path') END "
                "FROM execplan_catalog_holds WHERE repo=?", (self.repo, self.repo, self.repo),
            )))
        connection = self.repository.connection
        pending_files = (connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_plan_files'"
        ).fetchone() is not None and connection.execute(
            'SELECT 1 FROM deleted_plan_files WHERE repo=? LIMIT 1', (self.repo,),
        ).fetchone() is not None)
        from loop_manager_internal.storage.deleted_diagnostic_state import marked_owners
        pending_diagnostics = bool(marked_owners(connection, self.repo))
        if (revision == self.revision
                and not pending_deletion and not pending_files and not pending_diagnostics
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
        self.pinned = replace(pinned, deletions=())

    def adopt_history(self) -> None:
        """History reconciliation is a known, durable local DB transition."""
        self.declarations = self._declarations()

    def apply_archive(
        self, base_sha: str, commit_oid: str,
        candidates: Sequence[MergedArchiveCandidate], *,
        expected: state.CatalogStateSnapshot | None = None,
        revision_is_current: Callable[[], bool] | None = None,
    ) -> bool:
        """Called inside the archive success transaction after Git proof.

        The publisher proves the commit is exactly these byte-preserving moves.
        Revalidate the resulting graph from the pinned documents; no Git catalog
        reread is necessary. The archive publisher supplies a local lease check
        after observing the ref outside DB access; other consumers retain their
        current-revision callback. Repository CAS still applies to both paths.
        """
        revision_is_current = revision_is_current or (lambda: self.current_revision() == commit_oid)
        pinned = self.pinned
        if (pinned is None or pinned.snapshot.issues or pinned.revision_sha != base_sha
                or not revision_is_current()
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
            revision_is_current=revision_is_current,
        ) is None:
            return False
        # A rolled-back owning transaction changes the declaration signature,
        # forcing refresh to replace this tentative cache on the next boundary.
        self.pinned = result
        self.revision = commit_oid
        self.declarations = self._declarations()
        return True
