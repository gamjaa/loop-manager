"""Per-step validated catalog boundary, including proven archive deltas."""

from dataclasses import replace
from collections.abc import Callable, Sequence
import sqlite3

from loop_manager_internal.storage.execplan_archive_state import MergedArchiveCandidate
from loop_manager_internal.catalog import execplan_catalog
from loop_manager_internal.storage import execplan_catalog_state as state


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
        # PR/session/cleanup transitions do not change validated declarations.
        snapshot = self.repository.capture(self.repo)
        return (
            tuple(row.values[:state._ROW_VERSION_FIELDS.index("status")]
                  for row in snapshot.rows),
            tuple(item for item in snapshot.reserved if item[0] not in snapshot.plan_ids),
        )

    def refresh(self, revision: str | None = None) -> str:
        revision = revision or self.current_revision()
        if (revision == self.revision
                and self.declarations == self._declarations()
                and self.current_revision() == revision):
            return revision
        result = self.reconcile(revision, self._remember)
        if result.kind is not state.ReconciliationKind.RECONCILED:
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
        if (pinned is None or pinned.revision_sha != base_sha
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
