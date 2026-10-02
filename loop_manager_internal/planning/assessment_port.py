"""Compatibility boundary for the serialized one-time self assessment."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from loop_manager_internal.maintenance import full_assessment

AssessmentError = full_assessment.FullAssessmentError


class AssessmentPort(Protocol):
    def inventory(self, repository: Path, revision: str) -> dict[str, str]: ...

    def prompt(
        self, inventory: dict[str, str], revision: str, saved_result: str | None,
    ) -> str: ...

    def validate(
        self, output: str, inventory: dict[str, str], revision: str,
        new_plan_ids: tuple[str, ...], known_plan_ids: set[str],
    ) -> str: ...


class LegacyAssessmentPort:
    """Preserve the persisted report format until its runtime replacement exists."""

    def inventory(self, repository: Path, revision: str) -> dict[str, str]:
        return full_assessment.source_inventory(repository, revision)

    def prompt(
        self, inventory: dict[str, str], revision: str, saved_result: str | None,
    ) -> str:
        return full_assessment.assessment_prompt(inventory, revision, saved_result)

    def validate(
        self, output: str, inventory: dict[str, str], revision: str,
        new_plan_ids: tuple[str, ...], known_plan_ids: set[str],
    ) -> str:
        return full_assessment.parse_assessment_result(
            output, inventory, revision, new_plan_ids, known_plan_ids,
        )
