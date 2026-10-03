"""maintenance review ports boundaries and named composition effects."""
from __future__ import annotations
from collections.abc import Callable
from dataclasses import dataclass
from loop_manager_internal.maintenance import maintenance_file_inventory
from loop_manager_internal.maintenance import maintenance_file_review
from loop_manager_internal.maintenance import maintenance_findings
from loop_manager_internal.maintenance import maintenance_reconciliation
from loop_manager_internal.storage import maintenance_file_ledger
from pathlib import Path
import re
import sqlite3


class _PlanningFileReviewPorts:
    """Keep provisional file units outside the success ledger and Planner row."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection
        self._repository = maintenance_file_review.ProvisionalReviewRepository(connection)

    def empty_selection_inventory(
        self, repo: str, repository: Path, base: str, source_revision: str,
    ) -> maintenance_file_inventory.SourceInventory | None:
        inventory = maintenance_file_inventory.cached_source_inventory(
            self._connection, repository, base, source_revision,
        )
        selected = maintenance_file_ledger.FileReviewRepository(
            self._connection,
        ).selected_sources(repo, inventory)
        reusable = maintenance_file_ledger.FileReviewRepository(
            self._connection,
        ).reusable_root_reviews(repo, inventory)
        return None if selected or reusable is None or maintenance_file_ledger.root_move_candidates(reusable) else inventory

    def progress(
        self, repo: str, repository: Path, base: str, cycle_revision: int,
        source_revision: str,
    ) -> maintenance_file_review.FileReviewProgress:
        inventory = maintenance_file_inventory.cached_source_inventory(
            self._connection, repository, base, source_revision,
        )
        return self._repository.progress(
            repo, repository, base, cycle_revision, inventory,
        )

    def content(self, repository: Path, base: str, source: maintenance_file_inventory.TrackedSource) -> bytes:
        return maintenance_file_review.source_bytes(repository, base, source)

    def start(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource) -> None:
        self._repository.start(repo, cycle_revision, source)

    def session(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, session_id: str) -> None:
        self._repository.session(repo, cycle_revision, source, session_id)

    def complete(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, result: dict) -> None:
        self._repository.complete(repo, cycle_revision, source, result)

    def pending_units(self, repo: str, progress):
        return self._repository.pending_units(repo, progress)

    def fail(self, repo: str, cycle_revision: int, source: maintenance_file_inventory.TrackedSource, error: str) -> None:
        self._repository.fail(repo, cycle_revision, source, error)

    def completed_summary(self, repo: str) -> dict:
        return self._repository.completed_summary(repo)


@dataclass(frozen=True)
class PlanningReconciliationPortsEffects:
    """Named effects for _PlanningReconciliationPorts; no entry-point/module access."""
    _git_tree_entries: Callable[..., object]
    git_bytes: Callable[..., object]
    parse_frontmatter: Callable[..., object]


class _PlanningReconciliationPorts:
    """Keep relation evidence provisional until G-086 publication succeeds."""

    def __init__(self, effects: PlanningReconciliationPortsEffects, connection: sqlite3.Connection):
        self._effects = effects
        self._connection = connection
        self._repository = maintenance_reconciliation.ReconciliationRepository(connection)
        self._synthesis = maintenance_reconciliation.SynthesisRepository(connection)
        self._findings = maintenance_findings.FindingsRepository(connection)
        self._reviews = maintenance_file_review.ProvisionalReviewRepository(connection)

    def progress(self, repo: str, repository: Path, base: str, cycle_revision: int,
                 source_revision: str) -> maintenance_reconciliation.ReconciliationProgress:
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, source_revision)
        reviews = self._reviews.progress(repo, repository, base, cycle_revision, inventory)
        return self._repository.progress(repo, repository, base, cycle_revision, inventory, reviews)

    def synthesis(self, repo: str, cycle: int, revision: str,
                  progress: maintenance_reconciliation.ReconciliationProgress):
        return self._synthesis.prepare(repo, cycle, revision, progress)

    def synthesis_export(self, repo: str, page, progress, directory: Path) -> Path:
        return self._synthesis.export(repo, page, progress, directory)

    def synthesis_session(self, repo: str, page, session_id: str) -> None:
        self._synthesis.session(repo, page, session_id)

    def synthesis_complete(self, repo: str, page, output: str) -> None:
        self._synthesis.complete(repo, page, output)

    def synthesis_batch(self, page, progress, count: int):
        texts = maintenance_reconciliation.synthesis_pages(progress)
        end = min(page.total, page.index + (1 if page.session_id else count))
        return tuple(maintenance_reconciliation.SynthesisPage(
            page.digest, index, page.total, texts[index],
            page.session_id if index == page.index else None,
        ) for index in range(page.index, end))

    def content(self, repository: Path, base: str, source: maintenance_file_inventory.TrackedSource) -> bytes:
        return maintenance_file_review.source_bytes(repository, base, source)

    def start(self, repo: str, cycle_revision: int, revision: str,
              relation: maintenance_reconciliation.Relation) -> None:
        self._repository.start(repo, cycle_revision, revision, relation)

    def session(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
                session_id: str) -> None:
        self._repository.session(repo, cycle_revision, relation, session_id)

    def complete(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
                 result: dict) -> None:
        self._repository.complete(repo, cycle_revision, relation, result)

    def fail(self, repo: str, cycle_revision: int, relation: maintenance_reconciliation.Relation,
             error: str) -> None:
        self._repository.fail(repo, cycle_revision, relation, error)

    def unit(self, repo: str, relation: maintenance_reconciliation.Relation) -> tuple[str | None, str | None]:
        return self._repository.unit(repo, relation)

    def resolution_session(self, repo, unit, session_id):
        self._synthesis.resolution_session(repo, unit, session_id)

    def resolution_batch(self, repo, unit, count):
        return self._synthesis.resolution_batch(repo, unit, count)

    def resolution_complete(self, repo, unit, output):
        self._synthesis.resolution_complete(repo, unit, output)

    def resolution_fail(self, repo, unit, error):
        self._synthesis.resolution_fail(repo, unit, error)

    def _plan_texts(self, repository: Path, revision: str, existing_plans: dict[str, str]) -> dict[str, str]:
        texts = {}
        for raw_path in self._effects._git_tree_entries(repository, revision):
            if not re.fullmatch(rb"docs/plans/[^/]+\.md", raw_path):
                continue
            path = raw_path.decode("utf-8")
            text = self._effects.git_bytes(repository, "show", f"{revision}:{path}").decode("utf-8")
            plan_id = str(self._effects.parse_frontmatter(text).get("id", ""))
            if plan_id in existing_plans:
                texts[plan_id] = text
        if set(texts) != set(existing_plans):
            raise maintenance_reconciliation.ReconciliationError("고정 비종료 계획의 원문이 누락됐습니다.")
        return texts

    def findings_unit(self, repo, repository, base, cycle, revision, existing_plans, directory):
        progress = self.progress(repo, repository, base, cycle, revision)
        synthesis = self.synthesis(repo, cycle, revision, progress)
        if synthesis.index != synthesis.total:
            raise maintenance_reconciliation.ReconciliationError("발견 수집 전 모든 증거 페이지가 필요합니다.")
        texts = self._plan_texts(repository, revision, existing_plans)
        unit = self._findings.prepare(repo, cycle, revision, synthesis.digest, existing_plans, texts)
        evidence = self.synthesis_export(repo, synthesis, progress, directory)
        manifest = self._findings.export(repo, unit, existing_plans, texts, evidence)
        return unit, manifest

    def findings_session(self, repo, unit, session_id):
        self._findings.session(repo, unit, session_id)

    def findings_fail(self, repo, unit, error):
        self._findings.fail(repo, unit, error)

    def findings_complete(self, repo, repository, base, cycle, revision, existing_plans, unit, output):
        progress = self.progress(repo, repository, base, cycle, revision)
        synthesis = self.synthesis(repo, cycle, revision, progress)
        texts = self._plan_texts(repository, revision, existing_plans)
        fresh = self._findings.prepare(repo, cycle, revision, synthesis.digest, existing_plans, texts)
        if (fresh.manifest_id, fresh.index) != (unit.manifest_id, unit.index):
            raise maintenance_reconciliation.ReconciliationError("발견 수집 중 source·계획 집합이 바뀌었습니다.")
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, revision)
        sources = {item.path: item for item in inventory.sources}
        class Contents(dict):
            def __missing__(self, path):
                self[path] = maintenance_file_review.source_bytes(repository, base, sources[path])
                return self[path]
        def validate(value):
            new_ids = tuple(sorted({item.get("plan_id", "") for item in value["findings"] if item.get("status") == "new_plan"}))
            maintenance_reconciliation.parse_findings(value, progress, inventory, Contents(),
                existing_plans, new_ids, texts, synthesis_digest=synthesis.digest)
        self._findings.complete(repo, unit, output, existing_plans, validate)

    def validate(self, repo: str, repository: Path, base: str, cycle_revision: int,
                 revision: str, output: str, existing_plans: dict[str, str],
                 new_plan_ids: tuple[str, ...], *, resolution_directory: Path | None = None
                 ) -> dict | maintenance_reconciliation.ResolutionUnit:
        progress = self.progress(repo, repository, base, cycle_revision, revision)
        synthesis = self.synthesis(repo, cycle_revision, revision, progress)
        if synthesis.index != synthesis.total:
            raise maintenance_reconciliation.ReconciliationError("종합 증거 페이지 검토가 완료되지 않았습니다.")
        inventory = maintenance_file_inventory.cached_source_inventory(self._connection, repository, base, revision)
        sources = {source.path: source for source in inventory.sources}

        class CurrentContents(dict):
            def __missing__(self, path: str) -> bytes:
                content = maintenance_file_review.source_bytes(repository, base, sources[path])
                self[path] = content
                return content

        plan_texts = self._plan_texts(repository, revision, existing_plans)
        collection = self._findings.prepare(repo, cycle_revision, revision, synthesis.digest, existing_plans, plan_texts)
        payload = self._findings.resolve(repo, collection, output, existing_plans)

        findings = maintenance_reconciliation.parse_findings(
            payload, progress, inventory, CurrentContents(), existing_plans, new_plan_ids, plan_texts,
            synthesis_digest=synthesis.digest,
        )
        if resolution_directory is not None and synthesis.total:
            manifest = self.synthesis_export(repo, synthesis, progress, resolution_directory)
            unit = self._synthesis.next_resolution(repo, synthesis, findings, manifest)
            if unit is not None:
                return unit
        self._synthesis.validate_resolutions(repo, synthesis, findings)
        return findings
