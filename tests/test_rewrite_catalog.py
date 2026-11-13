"""Git 대신 작은 원격 트리로 문서/계획 계약을 검증한다."""
from pathlib import Path
import tempfile
import unittest

from loop_manager_internal.catalog.observer import Document, catalog, planning_targets, synchronize
from loop_manager_internal.storage.state import Store


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


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name)/'loop-manager.db')
        self.store.initialize()
        self.store.register('owner/repo','/repo','remote','main','codex')

    def sync(self, documents, revision='base'):
        snapshot = catalog(revision,documents)
        synchronize(self.store,'owner/repo',snapshot)
        return snapshot

    def planned(self):
        with self.store.transaction() as db:
            db.execute("UPDATE requirement_states SET last_planned_source_oid=source_oid,last_planned_base_sha='base'")

    def test_only_requirement_change_is_independent_planner_trigger(self):
        snapshot = self.sync([requirement(),plan()])
        self.assertEqual(planning_targets(self.store,'owner/repo',snapshot), [('R-001','requirement_changed')])
        self.planned()
        snapshot = self.sync([requirement(),plan(oid='g2')],revision='code-and-product-changed')
        self.assertEqual(planning_targets(self.store,'owner/repo',snapshot), [])
        snapshot = self.sync([requirement(oid='r2'),plan(oid='g2')])
        self.assertEqual(planning_targets(self.store,'owner/repo',snapshot), [('R-001','requirement_changed')])

    def test_rename_and_archive_are_not_cancellation(self):
        self.sync([requirement(),plan()])
        snapshot = self.sync([requirement(path='docs/requirements/[R-001] 이름 변경.md'),
                              plan(path='docs/plans/archive/[G-001] 작업.md')])
        self.assertFalse(snapshot.errors)
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'READY')
        self.assertFalse(self.store.rows('SELECT * FROM exec_plan_tombstones'))

    def test_confirmed_deletion_cancels_and_forbids_reuse(self):
        self.sync([requirement(),plan()])
        self.sync([requirement()])
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'CANCELLED')
        snapshot = self.sync([requirement(),plan()])
        self.assertIn(plan().path,snapshot.errors)

    def test_invalid_document_does_not_cancel_preserved_work(self):
        self.sync([requirement(),plan()])
        snapshot = self.sync([requirement(),Document(plan().path,'bad','broken')])
        self.assertIn(plan().path,snapshot.errors)
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'READY')

    def test_duplicate_ids_invalid_dag_and_independent_plan(self):
        snapshot = catalog('base',[plan(),plan(path='docs/plans/duplicate.md'),
                                    plan('G-002',dependencies=['G-003']),plan('G-003',dependencies=['G-002']),
                                    plan('G-004'),plan('G-005',dependencies=['G-999'])])
        self.assertIn(plan().path,snapshot.errors)
        self.assertIn('docs/plans/duplicate.md',snapshot.errors)
        self.assertIn(plan('G-002').path,snapshot.errors)
        self.assertIn(plan('G-003').path,snapshot.errors)
        self.assertIn(plan('G-005').path,snapshot.errors)
        self.assertNotIn(plan('G-004').path,snapshot.errors)

    def test_definition_change_invalidates_running_generation(self):
        self.sync([requirement(),plan()])
        generation = self.store.claim('owner/repo','G-001','READY','WORKING','old')
        self.sync([requirement(oid='r2'),plan()])
        self.assertFalse(self.store.apply('owner/repo','G-001',generation,'old','WORKING','VERIFYING'))
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'READY')

    def test_terminal_plan_is_not_reopened_by_document_change(self):
        self.sync([requirement(),plan()])
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='MERGED'")
        self.sync([requirement(),plan(oid='g2')])
        self.assertEqual(self.store.plan('owner/repo','G-001')['status'],'MERGED')

    def test_draft_and_priority_default(self):
        snapshot = self.sync([requirement(),plan(draft=True)])
        self.assertFalse(snapshot.errors)
        row = self.store.plan('owner/repo','G-001')
        self.assertTrue(row['draft'])
        self.assertEqual(row['priority'],'normal')
        self.assertIsNone(self.store.claim('owner/repo','G-001','READY','WORKING','input'))

    def test_final_acceptance_waits_until_all_related_g_are_terminal(self):
        snapshot = self.sync([requirement(),plan(),plan('G-002')])
        self.planned()
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-001'")
        self.assertEqual(planning_targets(self.store,'owner/repo',snapshot), [])
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-002'")
        self.assertEqual(planning_targets(self.store,'owner/repo',snapshot), [('R-001','final_acceptance')])

    def test_dependency_cannot_be_claimed_before_actual_merge(self):
        self.sync([requirement(),plan(),plan('G-002',dependencies=['G-001'])])
        self.assertIsNone(self.store.claim('owner/repo','G-002','READY','WORKING','input'))
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='PR_OPEN' WHERE plan_id='G-001'")
        self.assertIsNone(self.store.claim('owner/repo','G-002','READY','WORKING','input'))
        with self.store.transaction() as db:
            db.execute("UPDATE exec_plans SET status='MERGED' WHERE plan_id='G-001'")
        self.assertEqual(self.store.claim('owner/repo','G-002','READY','WORKING','input'),1)
