"""catalog legacy migration boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.recovery import scoped_blocking
from loop_manager_internal.storage import catalog_migration
from loop_manager_internal.storage import execplan_catalog_state
from loop_manager_internal.storage import runtime_state
from pathlib import Path
import contextlib
import hashlib
import re
import sqlite3


@dataclass(frozen=True)
class LegacyPlanShaEffects:
    """Named effects for _legacy_plan_sha; no entry-point/module access."""
    git: Callable[..., object]


def _legacy_plan_sha(effects: LegacyPlanShaEffects, root: Path, revision: str, relative_path: str) -> str:
    result = effects.git(
        root,
        "log",
        revision,
        "--diff-filter=A",
        "--format=%H",
        "--",
        relative_path,
        check=False,
    )
    commits = [
        value
        for value in result.stdout.splitlines()
        if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value)
    ]
    if len(commits) != 1:
        raise RuntimeError(
            f"Legacy ExecPlan의 게시 commit을 안전하게 확인할 수 없습니다: {relative_path}"
        )
    return commits[0]


@dataclass(frozen=True)
class CurrentExecplanShaEffects:
    """Named effects for _current_execplan_sha; no entry-point/module access."""
    git: Callable[..., object]
    git_bytes: Callable[..., object]


def _current_execplan_sha(
    effects: CurrentExecplanShaEffects, root: Path,
    revision: str,
    entry: execplan_catalog.ExecPlanCatalogEntry,
) -> str:
    """Find the oldest revision that contains the current active plan bytes."""

    result = effects.git(
        root,
        "log",
        revision,
        "--format=%H",
        "--",
        entry.active_path,
        check=False,
    )
    for commit in reversed(result.stdout.splitlines()):
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit):
            continue
        try:
            content = effects.git_bytes(
                root, "show", f"{commit}:{entry.active_path}"
            )
        except RuntimeError:
            continue
        if hashlib.sha256(content).hexdigest() == entry.content_hash:
            return commit
    raise RuntimeError(
        "현재 active ExecPlan 본문의 게시 commit을 안전하게 확인할 수 없습니다: "
        f"{entry.active_path}"
    )


@dataclass(frozen=True)
class LegacyCatalogMergeProofEffects:
    """Named effects for _LegacyCatalogMergeProof; no entry-point/module access."""
    _git_commit_is_ancestor: Callable[..., object]
    git: Callable[..., object]
    git_bytes: Callable[..., object]
    pr_for_branch: Callable[..., object]


class _LegacyCatalogMergeProof:
    """GitHub/Git adapter kept outside the parser and catalog repository."""

    def __init__(
        self,
        effects: LegacyCatalogMergeProofEffects, root: Path,
        revision: str,
        stored: Mapping[str, execplan_catalog_state.StoredPlanIdentity],
    ):
        self._effects = effects
        self.root = root
        self.revision = revision
        self.stored = stored

    def _stored_git_proof(
        self, entry: execplan_catalog.ExecPlanCatalogEntry
    ) -> execplan_catalog_state.MergeProof:
        identity = self.stored.get(entry.plan_id)
        if identity is None:
            return execplan_catalog_state.MergeProof(False)
        return self.verify_stored(entry, identity)

    def verify_stored(
        self,
        entry: execplan_catalog.ExecPlanCatalogEntry,
        identity: execplan_catalog_state.StoredPlanIdentity,
    ) -> execplan_catalog_state.MergeProof:
        if (
            identity.plan_id != entry.plan_id
            or identity.status != "MERGED"
            or not identity.plan_sha
        ):
            return execplan_catalog_state.MergeProof(False)
        try:
            revision_sha = self._effects.git(
                self.root, "rev-parse", f"{self.revision}^{{commit}}"
            ).stdout.strip()
            published = self._effects.git_bytes(
                self.root,
                "show",
                f"{identity.plan_sha}:{identity.path}",
            )
            ancestor = self._effects._git_commit_is_ancestor(
                self.root, identity.plan_sha, revision_sha
            )
        except RuntimeError as error:
            return execplan_catalog_state.MergeProof(False, detail=str(error))
        published_hash = hashlib.sha256(published).hexdigest()
        if not ancestor or published_hash != identity.content_hash:
            return execplan_catalog_state.MergeProof(False)
        return execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.STORED_AND_GIT,
            plan_sha=identity.plan_sha,
            publication_content_hash=published_hash,
            revision=revision_sha,
        )

    def proof_for(
        self, entry: execplan_catalog.ExecPlanCatalogEntry
    ) -> execplan_catalog_state.MergeProof:
        # SQLite's MERGED transition is the durable result of the first
        # GitHub observation.  Archive reconciliation may still need local
        # publication evidence, but it must never turn an incomplete local
        # proof into another PR query.
        identity = self.stored.get(entry.plan_id)
        if identity is not None and identity.status == "MERGED":
            return self._stored_git_proof(entry)

        pull_request = self._effects.pr_for_branch(
            self.root, f"loop-manager/{entry.plan_id}"
        )
        if pull_request is None:
            return self._stored_git_proof(entry)
        merged = bool(
            pull_request["state"] == "MERGED" or pull_request.get("mergedAt")
        )
        if not merged:
            return execplan_catalog_state.MergeProof(
                False,
                pr_url=str(pull_request.get("url") or "") or None,
                detail=str(pull_request.get("state") or "unknown"),
            )
        if identity is not None and identity.plan_sha:
            plan_sha = identity.plan_sha
        else:
            # Use the latest active publication, including maintenance revisions.
            # Matching archive bytes against any older revision would accept
            # an archive reverted to an obsolete publication.
            plan_sha = self._effects.git(
                self.root, "log", self.revision, "--no-renames",
                "--diff-filter=AM", "--format=%H", "-1", "--",
                entry.active_path,
            ).stdout.strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", plan_sha):
                raise RuntimeError("archived ExecPlan의 게시 revision이 없습니다.")
        published_hash = hashlib.sha256(
            self._effects.git_bytes(self.root, "show", f"{plan_sha}:{entry.active_path}")
        ).hexdigest()
        return execplan_catalog_state.MergeProof(
            True,
            kind=execplan_catalog_state.MergeProofKind.GITHUB_PR,
            plan_sha=plan_sha,
            publication_content_hash=published_hash,
            revision=(
                str(
                    (pull_request.get("mergeCommit") or {}).get("oid")
                    or ""
                )
                or str(pull_request.get("mergedAt") or "")
                or None
            ),
            pr_url=str(pull_request.get("url") or "") or None,
        )


@dataclass(frozen=True)
class MigrateExecplanCatalogEffects:
    """Named effects for _migrate_execplan_catalog; no entry-point/module access."""
    _catalog_migration_effects: Callable[..., object]


def _migrate_execplan_catalog(
    effects: MigrateExecplanCatalogEffects, connection: sqlite3.Connection,
    *,
    reconcile_history: bool,
    target_repo: str | None,
    manage_transaction: bool = True,
    pinned_catalog: execplan_catalog_state.PinnedCatalog | None = None,
) -> None:
    return catalog_migration._migrate_execplan_catalog(
        connection, effects=effects._catalog_migration_effects(),
        reconcile_history=reconcile_history, target_repo=target_repo,
        manage_transaction=manage_transaction, pinned_catalog=pinned_catalog,
    )


@dataclass(frozen=True)
class MigrateLegacyExecplansEffects:
    """Named effects for migrate_legacy_execplans; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]
    _migrate_execplan_catalog: Callable[..., object]


def migrate_legacy_execplans(
    effects: MigrateLegacyExecplansEffects, connection: sqlite3.Connection,
    *,
    reconcile_history: bool = False,
    target_repo: str | None = None,
    ensure_schema: bool = True,
    manage_transaction: bool = True,
    pinned_catalog: execplan_catalog_state.PinnedCatalog | None = None,
) -> None:
    """Conservatively move legacy project-owned execution state into the catalog."""

    if ensure_schema:
        effects._ensure_execplans_schema(connection)
    effects._migrate_execplan_catalog(
        connection,
        reconcile_history=reconcile_history,
        target_repo=target_repo,
        manage_transaction=manage_transaction,
        pinned_catalog=pinned_catalog,
    )
    with connection if manage_transaction else contextlib.nullcontext():
        scoped_blocking.migrate_legacy_blocks(connection)
    return


@dataclass(frozen=True)
class ActiveExecplanEffects:
    """Named effects for active_execplan; no entry-point/module access."""
    _ensure_execplans_schema: Callable[..., object]


def active_execplan(effects: ActiveExecplanEffects, connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Return the only plan currently occupying the repository mutation slot."""

    effects._ensure_execplans_schema(connection)
    return runtime_state.RuntimeStateRepository(connection).active_row(repo)


def project_execplan(connection: sqlite3.Connection, repo: str) -> sqlite3.Row | None:
    """Return the plan explicitly mirrored by ``projects``, regardless of status."""

    return runtime_state.RuntimeStateRepository(connection).projected_row(repo)
