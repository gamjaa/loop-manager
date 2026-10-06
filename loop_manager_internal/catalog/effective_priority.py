"""Selection-only priority inheritance over the verified, unheld catalog."""
from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json

from loop_manager_internal.application.contracts import PRIORITY_ORDER


@dataclass(frozen=True)
class EffectivePriority:
    declared: str
    effective: str
    paths: tuple[tuple[str, ...], ...] = ()

    def sort_key(self, plan_id: str) -> tuple[int, int]:
        return PRIORITY_ORDER[self.effective], int(plan_id[2:])

    def reason(self, plan_id: str) -> str:
        paths = '; '.join(' -> '.join(path) for path in self.paths) or 'self'
        return (f'{plan_id}: declared_priority={self.declared}, '
                f'effective_priority={self.effective}, dependency_paths={paths}')


def effective_priorities(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, EffectivePriority]:
    """Recompute without cache writes; incomplete/cyclic closures cannot select.

    Callers supply the existing validated execution view, excluding catalog
    holds and unverified history. MERGED rows terminate inheritance.
    """
    by_id = {str(row['plan_id']): row for row in rows}
    dependencies = {}
    declared = {}
    for plan_id, row in by_id.items():
        priority = str(row['priority']) if 'priority' in row.keys() else 'normal'
        try:
            deps = json.loads(str(row['depends_on']))
        except (ValueError, TypeError):
            continue
        if (priority not in PRIORITY_ORDER or not isinstance(deps, list)
                or any(not isinstance(dep, str) or dep not in by_id or dep == plan_id
                       for dep in deps)):
            continue
        dependencies[plan_id] = tuple(sorted(set(deps), key=lambda dep: int(dep[2:])))
        declared[plan_id] = priority
    # Resolve complete acyclic closures, leaving invalid relationships isolated.
    valid = {plan_id for plan_id in dependencies if by_id[plan_id]['status'] == 'MERGED'}
    pending = set(dependencies) - valid
    while pending:
        resolved = {plan_id for plan_id in pending
                    if set(dependencies[plan_id]).issubset(valid)}
        if not resolved:
            break
        valid.update(resolved)
        pending.difference_update(resolved)
    result = {plan_id: EffectivePriority(declared[plan_id], declared[plan_id])
              for plan_id in valid}
    for goal in sorted(valid, key=lambda plan_id: int(plan_id[2:])):
        if by_id[goal]['status'] == 'MERGED':
            continue
        queue = deque([(goal,)])
        visited = {goal}
        while queue:
            path = queue.popleft()
            for dependency in dependencies[path[-1]]:
                if dependency in visited or by_id[dependency]['status'] == 'MERGED':
                    continue
                visited.add(dependency)
                next_path = (*path, dependency)
                current = result[dependency]
                rank = PRIORITY_ORDER[declared[goal]]
                if rank < PRIORITY_ORDER[current.effective]:
                    result[dependency] = EffectivePriority(current.declared, declared[goal], (next_path,))
                elif rank == PRIORITY_ORDER[current.effective] and rank < PRIORITY_ORDER[current.declared]:
                    result[dependency] = EffectivePriority(current.declared, current.effective,
                                                           (*current.paths, next_path))
                queue.append(next_path)
    return result
