"""재구현 계약 테스트의 작은 문서 fixture."""
from loop_manager_internal.catalog.observer import Document


def requirement(identity='R-001', oid='r1', path=None):
    return Document(path or f'docs/requirements/[{identity}] 목표.md',oid,
                    f'# [{identity}] 목표\n\n## Acceptance Criteria\n\n조건 충족\n')


def plan(identity='G-001', oid='g1', dependencies=(), draft=False, path=None):
    dependency_lines = ''.join(f'  - {item}\n' for item in dependencies)
    declaration = 'depends_on:\n'+dependency_lines if dependencies else 'depends_on: []\n'
    text = (f'---\nid: {identity}\ntitle: 작업\nsources:\n  - {requirement().path}\n'+declaration+
            f'complexity: low\nrisk: low\ndraft: {str(draft).lower()}\n---\n\n## Purpose\n목표\n')
    if draft:
        text += '\n## Draft Decision\n### Question\n질문\n### Facts\n근거\n### Options\n선택\n### Impact\n영향\n'
    return Document(path or f'docs/plans/[{identity}] 작업.md',oid,text)


