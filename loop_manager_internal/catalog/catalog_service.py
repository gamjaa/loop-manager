"""catalog catalog service boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from loop_manager_internal.application.contracts import PLAN_SHA_BACKFILL_ERROR
from loop_manager_internal.catalog import execplan_catalog, requirement_sources
from loop_manager_internal.catalog import project_catalog_refresh
from loop_manager_internal.git import command_adapter
from loop_manager_internal.notifications import notifications
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.recovery.scoped_blocking import BlockReasonCode
from loop_manager_internal.recovery.scoped_blocking import PR_CLOSED_BLOCK_REASON
from loop_manager_internal.storage import database, execplan_catalog_state
from pathlib import Path
from types import SimpleNamespace
import os
import re
import sqlite3


@dataclass(frozen=True)
class BackfillPlanShasEffects:
    """Named effects for backfill_plan_shas; no entry-point/module access."""
    _plan_id_from_path: Callable[..., object]
    git: Callable[..., object]


def backfill_plan_shas(
    effects: BackfillPlanShasEffects, connection: sqlite3.Connection,
    *,
    target_repo: str | None = None,
    manage_transaction: bool = True,
) -> None:
    rows = connection.execute(
        """
        SELECT repo, plan_path
        FROM projects
        WHERE plan_sha IS NULL
          AND plan_path IS NOT NULL
          AND phase IN ('WORKING', 'VERIFYING', 'REVIEWING', 'PUBLISHING')
        """
    ).fetchall()
    if target_repo is not None:
        rows = [row for row in rows if row["repo"] == target_repo]
    for row in rows:
        result = effects.git(
            Path(row["repo"]),
            "log",
            "--all",
            "--diff-filter=A",
            "--format=%H",
            "--",
            row["plan_path"],
        )
        if result.returncode:
            raise RuntimeError("ExecPlan plan SHA backfill Git 이력을 읽을 수 없습니다.")
        commits = [
            value
            for value in result.stdout.splitlines()
            if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
        ]
        if len(commits) == 1:
            connection.execute(
                "UPDATE projects SET plan_sha = ? WHERE repo = ?",
                (commits[0], row["repo"]),
            )
        else:
            project = connection.execute(
                "SELECT phase FROM projects WHERE repo = ?", (row["repo"],)
            ).fetchone()
            scoped_blocking.record_project_block(
                connection,
                str(row["repo"]),
                BlockReasonCode.REPOSITORY_STATE_UNSAFE,
                PLAN_SHA_BACKFILL_ERROR,
                None,
                plan_id=effects._plan_id_from_path(str(row["plan_path"] or "")),
                previous_phase=(str(project["phase"] or "") or None),
            )
            notifications.enqueue_current_project_events(connection, str(row["repo"]))
    if manage_transaction:
        connection.commit()


@dataclass(frozen=True)
class GitBytesEffects:
    """Named effects for git_bytes; no entry-point/module access."""
    _command_adapter_effects_git_bytes: Callable[..., object]


def git_bytes(effects: GitBytesEffects, repo: Path, *arguments: str) -> bytes:
    return command_adapter.git_bytes(effects._command_adapter_effects_git_bytes(), repo, *arguments)


@dataclass(frozen=True)
class GitTreeEntriesEffects:
    """Named effects for _git_tree_entries; no entry-point/module access."""
    git_bytes: Callable[..., object]


def _git_tree_entries(effects: GitTreeEntriesEffects, repo: Path, revision: str) -> dict[bytes, tuple[bytes, bytes]]:
    tree = effects.git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", revision)
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("Git tree 항목 형식이 올바르지 않습니다.")
        _mode, object_type, object_id = parts
        if object_type not in {b"blob", b"commit"}:
            raise RuntimeError("지원하지 않는 Git tree 항목 형식입니다.")
        entries[path] = (object_type, object_id)
    return entries


@dataclass(frozen=True)
class RevisionExecplanSnapshotEffects:
    """Named effects for revision_execplan_snapshot; no entry-point/module access."""
    git: Callable[..., object]
    git_bytes: Callable[..., object]


def revision_execplan_snapshot(
    effects: RevisionExecplanSnapshotEffects, repo: Path,
    revision: str,
    *,
    archive_conflict_plan_ids: frozenset[str] = frozenset(),
    satisfied_dependencies: frozenset[str] = frozenset(),
    isolate_errors: bool = False,
) -> execplan_catalog.ExecPlanCatalogSnapshot:
    """Read and fully validate active/archive plans from one immutable tree."""

    revision_sha = effects.git(
        repo, "rev-parse", "--verify", f"{revision}^{{commit}}"
    ).stdout.strip()
    tree = effects.git_bytes(repo, "ls-tree", "-r", "-z", "--full-tree", revision_sha)
    documents: dict[str, bytes] = {}
    regular_paths: set[str] = set()
    invalid_plan_entries: dict[str, str] = {}
    for record in tree.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise RuntimeError("Git tree 항목 형식이 올바르지 않습니다.")
        mode, object_type, object_id = parts
        try:
            relative_path = raw_path.decode("utf-8")
        except UnicodeDecodeError as error:
            if raw_path.startswith(b"docs/plans/"):
                raise execplan_catalog.ExecPlanCatalogError(
                    "ExecPlan Git 경로가 UTF-8이 아닙니다.",
                    code="invalid-path-encoding",
                ) from error
            continue
        regular = object_type == b"blob" and mode in {b"100644", b"100755"}
        if regular:
            regular_paths.add(relative_path)
        plan_candidate = bool(
            re.fullmatch(r"docs/plans/[^/]+\.md", relative_path)
            or re.fullmatch(
                r"docs/plans/archive/[^/]+\.md", relative_path
            )
        )
        if not plan_candidate:
            continue
        if not regular:
            invalid_plan_entries[relative_path] = (
                "symlink" if mode == b"120000" else mode.decode("ascii")
            )
            continue
        documents[relative_path] = effects.git_bytes(
            repo, "cat-file", "blob", object_id.decode("ascii")
        )
    from loop_manager_internal.catalog.requirement_sources import RequirementSourceLookupError
    try:
        requirement_documents = command_adapter.requirement_documents_from_tree(
            tree,
            lambda object_id: effects.git_bytes(
                repo, "cat-file", "blob", object_id,
            ),
        )
    except RequirementSourceLookupError as error:
        raise execplan_catalog.ExecPlanCatalogError(
            str(error), code="source-lookup-failed", path=error.path,
        ) from error
    return execplan_catalog.snapshot_from_documents(
        documents,
        validate_sources=True,
        available_regular_paths=frozenset(regular_paths),
        requirement_documents=requirement_documents,
        invalid_plan_entries=invalid_plan_entries,
        revision_sha=revision_sha,
        archive_conflict_plan_ids=archive_conflict_plan_ids,
        satisfied_dependencies=satisfied_dependencies,
        isolate_errors=isolate_errors,
    )


@dataclass(frozen=True)
class CatalogArchiveConflictIdsEffects:
    """Named effects for _catalog_archive_conflict_ids; no entry-point/module access."""
    _git_tree_entries: Callable[..., object]
    _merged_execplan_ids: Callable[..., object]
    pr_for_branch: Callable[..., object]


def _catalog_archive_conflict_ids(
    effects: CatalogArchiveConflictIdsEffects, connection: sqlite3.Connection, repo_path: Path, repo: str, revision: str,
) -> frozenset[str]:
    """Use read-only, identity-bound PR evidence for archive path conflicts.

    Declaration validation precedes PR transitions. A newly merged PR may
    therefore still be PR_OPEN or blocked on a previously closed PR in SQLite
    when its archive collision is read.
    This proof permits only the existing collision diagnostic, never cleanup
    or an execution-state transition.
    """
    confirmed = set(effects._merged_execplan_ids(connection, repo))
    paths = (os.fsdecode(path) for path in effects._git_tree_entries(repo_path, revision))
    conflicts = execplan_catalog.conflicting_archived_plan_paths(paths)
    project = connection.execute(
        "SELECT base_branch FROM projects WHERE repo = ?", (repo,),
    ).fetchone()
    if project is None:
        return frozenset(confirmed)
    for plan_id in conflicts.keys() - confirmed:
        plan = connection.execute(
            "SELECT * FROM exec_plans WHERE repo = ? AND plan_id = ?",
            (repo, plan_id),
        ).fetchone()
        if plan is None or not plan["branch"]:
            continue
        pull_request = effects.pr_for_branch(repo_path, str(plan["branch"]), plan["pr_url"])
        if pull_request is None:
            continue
        if (pull_request.get("baseRefName") != project["base_branch"]
                or pull_request.get("headRefName") != plan["branch"]
                or (plan["pr_url"] and pull_request.get("url") != plan["pr_url"])):
            continue
        if pull_request.get("state") == "MERGED" or pull_request.get("mergedAt"):
            confirmed.add(plan_id)
    return frozenset(confirmed)


@dataclass(frozen=True)
class GitCatalogDefinitionPortEffects:
    """Named effects for _GitCatalogDefinitionPort; no entry-point/module access."""
    _git_tree_entries: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., object]
    revision_execplan_snapshot: Callable[..., object]


class _GitCatalogDefinitionPort:
    """Pin catalog reads to a fetched remote ref without consulting worktrees."""

    def __init__(
        self,
        effects: GitCatalogDefinitionPortEffects, repo: Path,
        remote_ref: str,
        archive_conflict_plan_ids: Callable[[], frozenset[str]],
        pinned_revision: str | None = None,
        satisfied_dependencies: frozenset[str] = frozenset(),
    ):
        self._effects = effects
        self.repo = repo
        self.remote_ref = remote_ref
        self.archive_conflict_plan_ids = archive_conflict_plan_ids
        self.pinned_revision = pinned_revision
        self.satisfied_dependencies = satisfied_dependencies
        self.loaded: execplan_catalog_state.PinnedCatalog | None = None

    def current_revision(self) -> str:
        return self._effects.git(
            self.repo,
            "rev-parse",
            "--verify",
            f"{self.remote_ref}^{{commit}}",
        ).stdout.strip()

    def load(self) -> execplan_catalog_state.PinnedCatalog:
        if self.loaded is not None:
            return self.loaded
        revision_sha = self.pinned_revision or self.current_revision()
        try:
            snapshot = self._effects.revision_execplan_snapshot(
                self.repo,
                revision_sha,
                archive_conflict_plan_ids=self.archive_conflict_plan_ids(),
                satisfied_dependencies=self.satisfied_dependencies,
                isolate_errors=True,
            )
        except execplan_catalog.ExecPlanCatalogError as error:
            tree = self._effects._git_tree_entries(self.repo, revision_sha)
            evidence = []
            for path in error.related_paths:
                entry = tree.get(path.encode("utf-8"))
                if entry is not None:
                    evidence.append(
                        f"{path}:{entry[0].decode('ascii')}:"
                        f"{entry[1].decode('ascii')}"
                    )
                else:
                    evidence.append(f"{path}:missing")
            diagnostic = execplan_catalog_state.ReconciliationDiagnostic(
                revision_sha=revision_sha,
                code=error.code,
                message=str(error),
                path=error.path,
                source=error.source,
                related_paths=error.related_paths,
                fingerprint=execplan_catalog_state.diagnostic_fingerprint(
                    revision_sha,
                    error.code,
                    str(error),
                    tuple(evidence),
                ),
            )
            raise execplan_catalog_state.InvalidPinnedCatalog(
                diagnostic
            ) from error
        tree = self._effects.git_bytes(
            self.repo, "ls-tree", "-r", "-z", "--full-tree", revision_sha
        )
        regular_paths: set[str] = set()
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, _separator, path = record.partition(b"\t")
            if metadata.split()[:2] not in (
                [b"100644", b"blob"], [b"100755", b"blob"]
            ):
                continue
            try:
                regular_paths.add(path.decode("utf-8"))
            except UnicodeDecodeError:
                continue
        from loop_manager_internal.catalog import execplan_drafts
        requirement_documents = command_adapter.requirement_documents_from_tree(
            tree,
            lambda object_id: self._effects.git_bytes(
                self.repo, "cat-file", "blob", object_id,
            ),
        )
        decision_sources = set()
        input_sources = set()
        for entry in snapshot.active.values():
            # Cleaned decisions retain DB history after their form leaves the document.
            input_sources.update(entry.sources)
            input_sources.update(("docs/PRODUCT.md", "docs/ARCHITECTURE.md"))
            if entry.draft_metadata and (entry.draft_metadata.get("draft") or entry.draft_metadata.get("draft_question")):
                decision_sources.update(entry.sources)
                decision_sources.update(("docs/PRODUCT.md", "docs/ARCHITECTURE.md"))
        from loop_manager_internal.catalog.manager_sources import SOURCE, source_content
        draft_inputs = {}
        for path in sorted(input_sources):
            if path == SOURCE:
                content = source_content(path).encode()
            elif path.startswith("docs/requirements/") and path.count("/") == 2:
                resolution = requirement_sources.resolve_requirement_source(
                    path, requirement_documents, allow_archive=True,
                )
                content = (
                    resolution.contents.encode("utf-8")
                    if resolution.status is requirement_sources.ResolutionStatus.RESOLVED
                    and resolution.contents is not None else None
                )
            elif path in regular_paths:
                content = self._effects.git_bytes(
                    self.repo, "show", f"{revision_sha}:{path}"
                )
            else:
                content = None
            draft_inputs[path] = (
                execplan_drafts.content_digest(content) if content is not None else None
            )
        remote_url = None
        if decision_sources:
            from loop_manager_internal.notifications.rendering import _checkpoint_link
            origin = self._effects.git(self.repo, "remote", "get-url", "origin").stdout.strip()
            link = _checkpoint_link(origin, revision_sha)
            if link:
                remote_url = link.split("/tree/", 1)[0]
        self.loaded = execplan_catalog_state.PinnedCatalog(
            revision_sha, snapshot, frozenset(regular_paths), draft_inputs=draft_inputs,
            remote_url=remote_url, requirement_documents=requirement_documents,
        )
        return self.loaded


@dataclass(frozen=True)
class ReconcileExecplanCatalogEffects:
    """Named effects for reconcile_execplan_catalog; no entry-point/module access."""
    _GitCatalogDefinitionPort: Callable[..., object]
    _catalog_archive_conflict_ids: Callable[..., object]
    _plan_id_from_path: Callable[..., object]
    backfill_plan_shas: Callable[..., object]
    git: Callable[..., object]
    migrate_legacy_execplans: Callable[..., object]
    pr_for_branch: Callable[..., object] | None = None
    assess_cleanup: Callable[..., object] | None = None
    remove_cleanup_worktree: Callable[..., object] | None = None
    recover_cleanup_worktree: Callable[..., object] | None = None
    validate_managed_worktree: Callable[..., object] | None = None
    git_operation_entries: Callable[..., object] | None = None


def reconcile_execplan_catalog(
    effects: ReconcileExecplanCatalogEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base_branch: str,
    *,
    record_invalid: bool = True,
    remote_sha: str | None = None,
    on_reconciled: Callable[[execplan_catalog_state.PinnedCatalog], None] | None = None,
    state_dir: Path | None = None,
) -> execplan_catalog_state.CatalogReconciliationResult:
    """Converge declaration cache or persist a recoverable project diagnostic."""

    remote_ref = f"refs/remotes/origin/{base_branch}"
    if remote_sha is not None:
        current_sha = effects.git(
            repo_path, "rev-parse", "--verify", f"{remote_ref}^{{commit}}"
        ).stdout.strip()
        if current_sha != remote_sha:
            return execplan_catalog_state.CatalogReconciliationResult(
                execplan_catalog_state.ReconciliationKind.STALE,
                revision_sha=remote_sha,
            )
    port = effects._GitCatalogDefinitionPort(
        repo_path,
        remote_ref,
        lambda: effects._catalog_archive_conflict_ids(
            connection, repo_path, repo, remote_sha or port.current_revision(),
        ),
        pinned_revision=remote_sha,
        satisfied_dependencies=execplan_catalog_state.verified_historical_merge_ids(connection, repo),
    )
    # The Git adapter proves a real deletion against the last adopted bytes.
    # Fake/alternate ports may already provide their own pinned evidence.
    repository = execplan_catalog_state.DefinitionStateRepository(connection)
    if isinstance(port, _GitCatalogDefinitionPort) and not repository.has_publication_intent(repo):
        from loop_manager_internal.catalog import remote_deletions
        try:
            pinned = port.load()
            from loop_manager_internal.catalog import draft_decisions
            pinned = replace(pinned, draft_changes=draft_decisions.user_changes(
                connection, repo, repo_path, pinned, git=effects.git, git_bytes=port._effects.git_bytes))
            port.loaded = pinned
            pinned = replace(pinned, requirement_deletions=remote_deletions.requirement_deletions(
                connection, repo, repo_path, pinned, git=effects.git, git_bytes=port._effects.git_bytes))
            port.loaded = pinned
            references = remote_deletions.reference_issues(
                connection, repo, repo_path, pinned, git_bytes=port._effects.git_bytes,
            )
            from loop_manager_internal.catalog import deleted_plan_cleanup
            state_file = connection.execute('PRAGMA database_list').fetchone()[2]
            if state_file and deleted_plan_cleanup.cleanup(
                    connection, repo_path, repo, pinned, references, git=effects.git,
                    git_bytes=port._effects.git_bytes, pr_for_branch=effects.pr_for_branch,
                    state_dir=Path(state_file).parent, assess_cleanup=effects.assess_cleanup,
                    remove_cleanup_worktree=effects.remove_cleanup_worktree,
                    recover_cleanup_worktree=effects.recover_cleanup_worktree,
                    revision_is_current=lambda: port.current_revision() == pinned.revision_sha):
                port.loaded = None
                pinned = port.load()
                references = remote_deletions.reference_issues(
                    connection, repo, repo_path, pinned, git_bytes=port._effects.git_bytes)
            pinned = replace(pinned, rowless_hold_deletions=remote_deletions.rowless_hold_deletion_evidence(
                connection, repo, repo_path, pinned, references, git_bytes=port._effects.git_bytes,
            ))
            port.loaded = pinned
            pinned = replace(pinned, cancellation_recovery=remote_deletions.cancellation_recovery_evidence(
                connection, repo, repo_path, pinned, references,
                git=effects.git, git_bytes=port._effects.git_bytes,
            ))
            port.loaded = pinned
            port.loaded = replace(pinned, deletions=remote_deletions.prove_deletions(
                connection, repo, repo_path, pinned, git=effects.git,
                git_bytes=port._effects.git_bytes, references=references, pr_for_branch=effects.pr_for_branch,
                validate_worktree=effects.validate_managed_worktree, git_operations=effects.git_operation_entries,
                state_dir=state_dir,
            ))
            cancelled = {item.snapshot.plan_id for item in port.loaded.deletions}
            cancelled_paths = {item.snapshot.path for item in port.loaded.deletions}
            held_references = tuple(issue for issue in references
                if issue.plan_id not in cancelled and issue.source not in cancelled_paths)
            if held_references:
                port.loaded = replace(port.loaded, snapshot=execplan_catalog.withhold_catalog_issues(
                    port.loaded.snapshot, held_references,
                ))
            port.loaded = replace(port.loaded, recovery_definitions=remote_deletions.recovery_definitions(
                connection, repo, repo_path, port.loaded, git_bytes=port._effects.git_bytes,
            ))
        except execplan_catalog_state.InvalidPinnedCatalog:
            pass  # The service records the same pinned declaration diagnostic.
    service = execplan_catalog_state.CatalogReconciliationService(repository, port)
    from loop_manager_internal.catalog import manual_catalog
    try:
        pinned = port.load()
        port.loaded = manual_catalog.overlay(connection, repo, pinned, git=effects.git)
    except execplan_catalog_state.InvalidPinnedCatalog:
        pass
    legacy = connection.execute(
        "SELECT 1 FROM execplan_catalog_revisions WHERE repo = ? "
        "UNION SELECT 1 FROM execplan_catalog_views WHERE repo = ?", (repo, repo),
    ).fetchone() is None
    # A bootstrap catalog intentionally has no baseline or legacy runtime rows.
    # Let reconciliation issue its bounded grants before prepare observes PRs.
    if (legacy and not database.has_remote_work_adoption_bootstrap(connection)
            and not repository.has_publication_intent(repo)):
        # Initial migration and declaration reconciliation share a rollback
        # boundary. Validate the remote graph before recovering runtime rows.
        try:
            pinned = port.load()
            effects.backfill_plan_shas(connection, target_repo=repo)
            if pinned.snapshot.isolation_enabled and not pinned.deletions:
                issues = execplan_catalog_state.new_declaration_issues(pinned, repository.capture(repo).plan_ids)
                if issues:
                    pinned = replace(pinned, snapshot=execplan_catalog.withhold_catalog_issues(pinned.snapshot, issues))
                    port.loaded = pinned
            expected = repository.capture(repo)
            missing = expected.plan_ids - pinned.snapshot.observed_ids
            prepared = (effects.migrate_legacy_execplans(connection, reconcile_history=True, target_repo=repo,
                ensure_schema=False, pinned_catalog=pinned, prepare_only=True) if not missing else None)
            observed_revision = port.current_revision()
            for deletion in pinned.deletions:
                if deletion.recheck is not None and (connection.in_transaction or not deletion.recheck()):
                    raise project_catalog_refresh.CatalogRefreshStopped(
                        execplan_catalog_state.CatalogReconciliationResult(
                            execplan_catalog_state.ReconciliationKind.STALE, revision_sha=pinned.revision_sha))
            pinned = replace(pinned, deletions=tuple(replace(item, recheck=None) for item in pinned.deletions))
            observed_service = execplan_catalog_state.CatalogReconciliationService(repository,
                SimpleNamespace(load=lambda: pinned, current_revision=lambda: observed_revision))
            with execplan_catalog_state._transaction(connection):
                if repository.capture(repo) != expected:
                    raise project_catalog_refresh.CatalogRefreshStopped(
                        execplan_catalog_state.CatalogReconciliationResult(
                            execplan_catalog_state.ReconciliationKind.STALE, revision_sha=pinned.revision_sha))
                if missing:
                    result = observed_service.reconcile(repo)
                else:
                    effects.migrate_legacy_execplans(
                        connection, reconcile_history=True, target_repo=repo,
                        ensure_schema=False, manage_transaction=False,
                        pinned_catalog=pinned, prepared=prepared,
                    )
                    result = observed_service.reconcile(repo)
                if not result.usable:
                    raise project_catalog_refresh.CatalogRefreshStopped(result)
        except execplan_catalog_state.InvalidPinnedCatalog as error:
            result = execplan_catalog_state.CatalogReconciliationResult(
                execplan_catalog_state.ReconciliationKind.INVALID_CATALOG,
                revision_sha=error.diagnostic.revision_sha, diagnostic=error.diagnostic,
            )
        except project_catalog_refresh.CatalogRefreshStopped as error:
            result = error.result
        except execplan_catalog.ExecPlanCatalogError as error:
            revision = port.loaded.revision_sha if port.loaded else (remote_sha or "")
            result = execplan_catalog_state.CatalogReconciliationResult(
                execplan_catalog_state.ReconciliationKind.INVALID_CATALOG,
                revision_sha=revision,
                diagnostic=execplan_catalog_state.ReconciliationDiagnostic(
                    revision, error.code, str(error), error.path, error.source,
                    execplan_catalog_state.diagnostic_fingerprint(
                        revision, error.code, str(error), error.related_paths,
                    ), error.related_paths,
                ),
            )
    else:
        result = service.reconcile(repo)
    if (result.kind is execplan_catalog_state.ReconciliationKind.INVALID_CATALOG
            and remote_sha is not None and port.current_revision() != remote_sha):
        return execplan_catalog_state.CatalogReconciliationResult(
            execplan_catalog_state.ReconciliationKind.STALE, revision_sha=remote_sha,
        )
    if (result.usable and on_reconciled is not None):
        assert port.loaded is not None
        on_reconciled(result.catalog or port.loaded)
    catalog_reason = BlockReasonCode.EXECPLAN_CATALOG_INVALID.value
    existing_block = scoped_blocking.project_block(connection, repo)
    if result.kind is execplan_catalog_state.ReconciliationKind.INVALID_CATALOG:
        if (not record_invalid or (existing_block is not None
                and existing_block["reason_code"] != catalog_reason)):
            return result
        diagnostic = result.diagnostic
        assert diagnostic is not None
        location = diagnostic.path or "(catalog)"
        source = f"; source={diagnostic.source}" if diagnostic.source else ""
        evidence = (
            "원격 ExecPlan catalog가 유효하지 않습니다: "
            f"revision={diagnostic.revision_sha}; path={location}{source}; "
            f"code={diagnostic.code}; {diagnostic.message}"
        )
        project = connection.execute(
            "SELECT phase FROM projects WHERE repo = ?", (repo,)
        ).fetchone()
        plan_id = effects._plan_id_from_path(diagnostic.path or "")
        with connection:
            scoped_blocking.record_project_block(
                connection,
                repo,
                BlockReasonCode.EXECPLAN_CATALOG_INVALID,
                evidence,
                diagnostic.fingerprint,
                plan_id=plan_id,
                previous_phase=(
                    str(project["phase"])
                    if project is not None and project["phase"]
                    else None
                ),
            )
            notifications.enqueue_current_project_events(connection, repo)
        return result
    if (
        record_invalid
        and result.usable
        and existing_block is not None
        and existing_block["reason_code"] == catalog_reason
    ):
        with connection:
            scoped_blocking.clear_project_block(connection, repo)
    return result


@dataclass(frozen=True)
class ReconcileExecutionExecplanCatalogEffects:
    """Named effects for reconcile_execution_execplan_catalog; no entry-point/module access."""
    reconcile_execplan_catalog: Callable[..., object]


def reconcile_execution_execplan_catalog(
    effects: ReconcileExecutionExecplanCatalogEffects, connection: sqlite3.Connection,
    repo_path: Path,
    repo: str,
    base_branch: str,
    *, state_dir: Path | None = None,
) -> execplan_catalog_state.CatalogReconciliationResult:
    """Let the execution recovery gate persist its own final decision."""

    return effects.reconcile_execplan_catalog(
        connection, repo_path, repo, base_branch, record_invalid=False, state_dir=state_dir,
    )


def _is_active_requirement(path: bytes) -> bool:
    return re.fullmatch(rb"docs/requirements/[^/]*\.md", path) is not None


@dataclass(frozen=True)
class IsPlanningInputEffects:
    """Named effects for _is_planning_input; no entry-point/module access."""
    _is_active_requirement: Callable[..., object]


class PlanningInputKind(str, Enum):
    DOCUMENT = "document"
    ACTIVE_PLAN = "active_plan"
    OTHER = "other"
    EXCLUDED = "excluded"


def classify_planning_input(
    path: bytes, is_active_requirement: Callable[[bytes], bool],
) -> PlanningInputKind:
    if path in {b"docs/PRODUCT.md", b"docs/ARCHITECTURE.md"} or is_active_requirement(path):
        return PlanningInputKind.DOCUMENT
    if path == b"AGENTS.md":
        return PlanningInputKind.EXCLUDED
    if execplan_catalog.is_active_execplan_path(path):
        return PlanningInputKind.ACTIVE_PLAN
    if not path.startswith(b"docs/"):
        return PlanningInputKind.OTHER
    return PlanningInputKind.EXCLUDED


def _is_planning_input(effects: IsPlanningInputEffects, path: bytes) -> bool:
    return classify_planning_input(path, effects._is_active_requirement) is not PlanningInputKind.EXCLUDED


@dataclass(frozen=True)
class IsSpecificationInputEffects:
    """Named effects for _is_specification_input; no entry-point/module access."""
    _is_active_requirement: Callable[..., object]


def _is_specification_input(effects: IsSpecificationInputEffects, path: bytes) -> bool:
    return classify_planning_input(path, effects._is_active_requirement) is PlanningInputKind.DOCUMENT


def _parse_execplan(
    worktree: Path,
    path: Path,
    *,
    require_priority: bool = False,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
    allow_id_fallback: bool | None = None,
) -> dict[str, object]:
    return execplan_catalog.parse_execplan_file(
        worktree,
        path,
        require_priority=require_priority,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
        allow_id_fallback=allow_id_fallback,
    )


def _validate_dependency_graph(catalog: dict[str, dict[str, object]]) -> None:
    return execplan_catalog.validate_dependency_graph(catalog)


def load_execplan_snapshot(
    worktree: Path,
    *,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
    isolate_errors: bool = False,
) -> execplan_catalog.ExecPlanCatalogSnapshot:
    return execplan_catalog.load_execplan_snapshot(
        worktree,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
        isolate_errors=isolate_errors,
    )


@dataclass(frozen=True)
class LoadExecplanCatalogEffects:
    """Named effects for load_execplan_catalog; no entry-point/module access."""
    load_execplan_snapshot: Callable[..., object]


def load_execplan_catalog(
    effects: LoadExecplanCatalogEffects, worktree: Path,
    *,
    validate_sources: bool = True,
    allow_archive_fallback: bool = True,
) -> dict[str, dict[str, object]]:
    return effects.load_execplan_snapshot(
        worktree,
        validate_sources=validate_sources,
        allow_archive_fallback=allow_archive_fallback,
    ).active_metadata()


@dataclass(frozen=True)
class ValidateExecplanEffects:
    """Named effects for validate_execplan; no entry-point/module access."""
    load_execplan_catalog: Callable[..., object]


def validate_execplan(effects: ValidateExecplanEffects, worktree: Path, relative_path: str) -> dict[str, object]:
    catalog = effects.load_execplan_catalog(worktree)
    match = re.fullmatch(r"docs/plans/\[(G-\d{3,})\] [^/]+\.md", relative_path)
    if match:
        plan = catalog.get(match.group(1))
        if plan is not None:
            expected_path = f"docs/plans/[{plan['id']}] {plan['title']}.md"
            if relative_path == expected_path:
                return plan
    raise RuntimeError(f"검증된 ExecPlan 카탈로그에 계획이 없습니다: {relative_path}")


def _idea_tokens(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[0-9a-zA-Z가-힣]+", text.casefold()))
