"""Planning port for source-bound self assessment evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from loop_manager_internal.maintenance import (
    maintenance_file_inventory, maintenance_planning, maintenance_validation,
)

AssessmentError = maintenance_file_inventory.AssessmentError


class AssessmentPort(Protocol):
    def inventory(self, repository: Path, revision: str) -> dict[str, str]: ...

    def prompt(
        self, inventory: dict[str, str], revision: str, saved_result: str | None,
    ) -> str: ...

    def validate(
        self, output: str, inventory: dict[str, str], revision: str,
        new_plan_ids: tuple[str, ...], known_plan_ids: set[str],
    ) -> str: ...


class MaintenanceAssessmentPort:
    """Use the maintenance inventory, prompt and validation boundaries."""

    def inventory(self, repository: Path, revision: str) -> dict[str, str]:
        return maintenance_file_inventory.source_inventory(repository, revision)

    def prompt(
        self, inventory: dict[str, str], revision: str, saved_result: str | None,
    ) -> str:
        if saved_result is not None:
            saved_result = maintenance_validation.validate_saved_assessment_result(
                saved_result, inventory, revision,
            )
        return maintenance_planning.assessment_prompt(inventory, revision, saved_result)

    def validate(
        self, output: str, inventory: dict[str, str], revision: str,
        new_plan_ids: tuple[str, ...], known_plan_ids: set[str],
    ) -> str:
        return maintenance_validation.parse_assessment_result(
            output, inventory, revision, new_plan_ids, known_plan_ids,
        )
