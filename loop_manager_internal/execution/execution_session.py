"""Immutable preparation snapshots shared by narrow phase ports."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from loop_manager_internal.catalog.requirement_archive import ArchiveSnapshot
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
from loop_manager_internal.execution.execution_runtime import ExecutionPlanMetadata

@dataclass(frozen=True)
class ExecutionIdentity:
    repo: str = ''
    repo_path: Path = Path('.')
    state_dir: Path = Path('.')
    base: str = ''
    base_sha: str = ''
    provider: str = ''
    config_path: Path = Path('.')
    newly_activated: bool = False

@dataclass(frozen=True)
class PreparationSnapshot:
    catalog_plan: Mapping[str, object] | None = None
    plan: Mapping[str, object] | None = None
    level: int = 1
    archive_sources: tuple[str, ...] = ()
    archive_candidates: tuple[str, ...] = ()
    archive_scope_sources: tuple[str, ...] = ()
    _archive_snapshot: ArchiveSnapshot | None = None
    _latest_verify_output: str = ''
    _definition_snapshot: DefinitionSnapshot | None = None

@dataclass(frozen=True)
class RebaseWorkspaceContext:
    """Owned workspace identity used before an execution phase can be reconstructed."""
    plan: ExecutionPlanMetadata
    worktree: Path
    branch: str
