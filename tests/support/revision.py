from loop_manager_internal.execution.execution_revision import DefinitionSnapshot


def definition(**changes):
    values = {
        "revision_sha": "a" * 40,
        "path": "docs/plans/[G-037] Revision.md",
        "title": "Revision",
        "sources": ("docs/PRODUCT.md",),
        "depends_on": ("G-036",),
        "complexity": "high",
        "risk": "critical",
        "priority": "critical",
        "body_hash": "body",
        "source_fingerprint": "source",
        "archive_candidates": (),
    }
    values.update(changes)
    return DefinitionSnapshot(**values)
