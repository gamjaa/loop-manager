"""Source-grounded symbol and review citations for provisional file reviews."""

from __future__ import annotations

import ast
from collections.abc import Sequence

from loop_manager_internal.maintenance.file_review_protocol import MIN_REVIEW_CITATION_CHARS


class _DefinedSymbols(ast.NodeVisitor):
    def __init__(self) -> None:
        self.parents: list[str] = []
        self.names: set[str] = set()

    def _definition(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.names.add(".".join((*self.parents, node.name)))
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    visit_ClassDef = _definition
    visit_FunctionDef = _definition
    visit_AsyncFunctionDef = _definition


def unverified_symbols(symbols: Sequence[str], path: str, content: bytes, text: str) -> tuple[str, ...]:
    """Resolve dotted Python definitions without guessing from their name parts."""
    missing = tuple(symbol for symbol in symbols if symbol not in text)
    if not missing or not path.endswith((".py", ".pyi")):
        return missing
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, UnicodeError):
        return missing
    defined = _DefinedSymbols()
    defined.visit(tree)
    return tuple(symbol for symbol in missing if symbol not in defined.names)


def review_cites_source(note: str, anchors: Sequence[str]) -> bool:
    """Require an exact source fragment; allow whitespace used for line wrapping."""
    if any(anchor in note for anchor in anchors):
        return True
    normalized_note = " ".join(note.split())
    for anchor in anchors:
        normalized_anchor = " ".join(anchor.split())
        if not normalized_anchor:
            continue
        width = min(MIN_REVIEW_CITATION_CHARS, len(normalized_anchor))
        if any(normalized_anchor[start:start + width] in normalized_note
               for start in range(len(normalized_anchor) - width + 1)):
            return True
    return False
