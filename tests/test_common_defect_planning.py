"""Pinned cause evidence is adopted only with a confirmed planning receipt."""
import copy
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from loop_manager_internal.planning.common_defect_links import validate
from loop_manager_internal.storage import common_defect_state, planning_review_state
from loop_manager_internal.storage.validation_feedback_state import ValidationFeedbackRepository, ensure_schema
from loop_manager_internal.planning.state_ports import _PlanningStatePorts
from loop_manager_internal.planning.planning_runtime import PlanningContext, PlanningLifecycleCoordinator
from tests.support.planning_case import FakePorts, planning_snapshot


class CommonDefectPlanningLinksTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        ensure_schema(self.connection)
        self.connection.execute("CREATE TABLE exec_plans (repo TEXT,plan_id TEXT,path TEXT,sources TEXT,"
                                "status TEXT,current_revision_sha TEXT,plan_sha TEXT)")
        self.connection.execute("INSERT INTO exec_plans VALUES "
            "('repo','G-003','docs/plans/[G-003] Correction.md','[\"docs/PRODUCT.md\"]',"
            "'READY','original-plan','original-plan')")
        self.feedback = ValidationFeedbackRepository(self.connection)
        report = dict(problem="same symptom", scope="outside selected work", evidence="traceback",
                      locations=[dict(path="src/shared.py", line=1)])
        for plan in ("G-001", "G-002"):
            self.feedback.record_result("repo", plan, "definition", "worker", "returned",
                'COMMON_DEFECT_REPORT: ' + json.dumps(report) + '\nOriginal failure and question')
        self.documents = {"docs/PRODUCT.md": "# Product\nRequired shared contract.",
                          "src/shared.py": "def shared():\n    return incorrect_value\n"}
        self.link = dict(reports=[1, 2], plan_id="G-003", source="docs/PRODUCT.md",
                         plan_path="docs/plans/[G-003] Correction.md",
                         quote="Required shared contract.", cause="missing shared guard",
                         completion="both callers preserve the shared contract",
                         code=[dict(path="src/shared.py", quote="return incorrect_value")])
        self.connection.commit()

    def git(self, repository, command, *args, **kwargs):
        self.assertFalse(self.connection.in_transaction, 'Git observation must precede state writes')
        if command == 'show':
            revision, path = args[0].split(':', 1)
            self.assertEqual(revision, 'base')
            return SimpleNamespace(returncode=0 if path in self.documents else 1,
                                   stdout=self.documents.get(path, ''))
        self.assertEqual(command, 'ls-tree')
        return SimpleNamespace(returncode=0, stdout='')

    def validated(self, raw=None, *, result=None, reports=None):
        return validate('COMMON_DEFECT_LINKS: ' + json.dumps(raw if raw is not None else
                        dict(revision='base', links=[self.link])), 'base',
                        reports if reports is not None else self.feedback.common_defect_reports('repo'),
                        list(self.connection.execute('SELECT * FROM exec_plans')), result, self.git, Path('/repo'))

    def propose(self, links):
        with self.connection:
            planning_review_state.begin(self.connection, 'repo', purpose='full', revision='base',
                input_fingerprint='input', specification_fingerprint='spec', work_cycle='cycle', comparison_revision=None)
            planning_review_state.bind_common_defect_snapshot(self.connection, 'repo', 'base', [1, 2])
            planning_review_state.validate(self.connection, 'repo', 'base', 'Planner original', 'reviewed',
                                           common_defect_links=links)

    def test_a_and_b_link_to_one_goal_only_after_receipt_confirmation(self):
        links = self.validated()
        self.propose(links)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_defect_links').fetchone()[0], 0)
        self.assertIsNone(planning_review_state.get(self.connection, 'repo', 'general'))
        with self.connection:
            planning_review_state.confirm(self.connection, 'repo', 'base', 'published')
            planning_review_state.confirm(self.connection, 'repo', 'base', 'published')
        rows = list(self.connection.execute('SELECT * FROM common_defect_links ORDER BY result_id'))
        self.assertEqual([row['result_id'] for row in rows], [1, 2])
        self.assertEqual({row['correction_plan_id'] for row in rows}, {'G-003'})
        self.assertEqual(len({row['goal_key'] for row in rows}), 1)
        self.assertEqual([row['confirmed_revision'] for row in rows], ['published', 'published'])
        self.assertEqual(self.connection.execute('SELECT status FROM exec_plans').fetchone()[0], 'READY')
        self.assertTrue(all('Original failure and question' in row['raw_output']
                            for row in self.feedback.common_defect_reports('repo')))

    def test_unconfirmed_or_different_causes_are_not_implicitly_combined(self):
        self.assertEqual(validate('no cause confirmed', 'base', [], [], None, self.git, Path('/repo')), [])
        second = {**self.link, 'reports': [2], 'cause': 'different confirmed cause'}
        first = {**self.link, 'reports': [1]}
        links = self.validated(dict(revision='base', links=[first, second]))
        self.assertNotEqual(common_defect_state.goal_key(links[0]), common_defect_state.goal_key(links[1]))
        self.propose(links)
        with self.connection:
            planning_review_state.confirm(self.connection, 'repo', 'base', 'published')
        self.assertEqual(len({row[0] for row in self.connection.execute('SELECT goal_key FROM common_defect_links')}), 2)

    def test_invalid_claims_never_acquire_a_link(self):
        mutations = [dict(quote='not in specification'), dict(source='docs/requirements/archive/Old.md'),
                     dict(plan_id='G-999'), dict(plan_path='docs/plans/Other.md'),
                     dict(reports=[True]), dict(reports=[1, 1]), dict(reports=[999]),
                     dict(code=[]), dict(code=[dict(path='../shared.py', quote='x')]),
                     dict(code=[dict(path='src/shared.py', quote='not in code')])]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.validated(dict(revision='base', links=[{**self.link, **change}]))
        with self.assertRaises(ValueError):
            self.validated(dict(revision='stale', links=[self.link]))
        with self.assertRaises(ValueError):
            self.validated(reports=[])
        for output in ('COMMON_DEFECT_LINKS: {bad}',
                       'COMMON_DEFECT_LINKS: {"revision":"base","revision":"stale","links":[]}',
                       'COMMON_DEFECT_LINKS: {}\nCOMMON_DEFECT_LINKS: {}'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                validate(output, 'base', [], [], None, self.git, Path('/repo'))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_defect_links').fetchone()[0], 0)

    def test_publication_failure_preserves_proposal_then_retry_adopts_once(self):
        self.propose(self.validated())
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            with self.connection:
                planning_review_state.confirm(self.connection, 'repo', 'base', 'published')
                raise RuntimeError('interrupted DB application')
        self.assertIsNotNone(planning_review_state.get(self.connection, 'repo', 'attempt'))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_defect_links').fetchone()[0], 0)
        with self.connection:
            planning_review_state.confirm(self.connection, 'repo', 'base', 'published')
        for sql in ('DELETE FROM common_defect_links', "UPDATE common_defect_links SET evidence='changed'"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                self.connection.execute(sql)
            self.connection.rollback()
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_defect_links').fetchone()[0], 2)

    def test_catalog_race_does_not_apply_a_validated_proposal(self):
        self.propose(self.validated())
        self.connection.execute("UPDATE exec_plans SET current_revision_sha='concurrent-change'")
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            with self.connection:
                planning_review_state.confirm(self.connection, 'repo', 'base', 'published')
        self.assertIsNotNone(planning_review_state.get(self.connection, 'repo', 'attempt'))
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_defect_links').fetchone()[0], 0)

    def test_ready_draft_and_open_pr_goals_cannot_acquire_a_second_live_owner(self):
        links = self.validated()
        self.propose(links)
        with self.connection:
            planning_review_state.confirm(self.connection, 'repo', 'base', 'published')
        for status in ('READY', 'PLAN_BLOCKED', 'PR_OPEN'):
            with self.subTest(status=status):
                self.connection.execute('UPDATE exec_plans SET status=?', (status,))
                self.connection.commit()
                common_defect_state.check_reuse(self.connection, 'repo', links)
                different_owner = copy.deepcopy(links)
                different_owner[0]['plan_id'] = 'G-004'
                with self.assertRaisesRegex(ValueError, '재사용'):
                    common_defect_state.check_reuse(self.connection, 'repo', different_owner)

    def test_new_plan_id_allocation_is_bound_to_its_actual_published_revision(self):
        new_plan = dict(id='G-004', sources=['docs/PRODUCT.md'])
        result = SimpleNamespace(plans=[('docs/plans/[G-004] New.md', new_plan)], revisions=[], deletions=[],
                                 id_allocations=[SimpleNamespace(source_id='G-999', target_id='G-004',
                                     source_path='docs/plans/[G-999] New.md', target_path='docs/plans/[G-004] New.md')])
        links = self.validated(dict(revision='base', links=[{**self.link, 'plan_id': 'G-999',
                               'plan_path': 'docs/plans/[G-999] New.md'}]), result=result)
        self.assertEqual(links[0]['plan_id'], 'G-004')
        self.assertIsNone(links[0]['target_revision'])
        self.propose(links)
        with self.connection:
            self.connection.execute("INSERT INTO exec_plans VALUES "
                "('repo','G-004','docs/plans/[G-004] New.md','[\"docs/PRODUCT.md\"]','READY','published','published')")
            planning_review_state.confirm(self.connection, 'repo', 'base', 'published')
        self.assertEqual({row[0] for row in self.connection.execute('SELECT correction_plan_id FROM common_defect_links')}, {'G-004'})

    def test_assessment_stores_validated_links_in_existing_review_receipt(self):
        output = 'COMMON_DEFECT_LINKS: ' + json.dumps(dict(revision='base', links=[self.link]))
        self.propose([])
        ports = FakePorts(planning_snapshot(phase='PLANNING', active_sha='base',
            worktree='/planning', planning_result_completed=True, planning_result=output))
        state = _PlanningStatePorts(None, self.connection, None, None, Path('/state'))
        ports.state.planning_review = lambda repo, slot: planning_review_state.get(self.connection, repo, slot)
        ports.state.review_work_cycle = lambda repo: 'cycle'
        ports.state.common_defect_reports = self.feedback.common_defect_reports
        ports.state.validate_planning_review = state.validate_planning_review
        ports.state.execplan_rows = lambda repo: list(self.connection.execute('SELECT * FROM exec_plans WHERE repo=?', (repo,)))
        ports.validation.result = None
        ports.git.run = self.git
        context = PlanningContext('repo', Path('/repo'), Path('/state'), 'main', 'base', 'codex',
                                  Path('/config'), False, 5)
        coordinator = PlanningLifecycleCoordinator(ports)
        run = coordinator._new_run(context)
        run.planning_worktree = Path('/planning')
        run.mutable_ready_snapshot = ports.validation.snapshot
        self.assertIsNone(coordinator._assessment._validate_planner_result(run))
        attempt = planning_review_state.get(self.connection, 'repo', 'attempt')
        self.assertEqual(json.loads(attempt['common_defect_links'])[0]['plan_id'], 'G-003')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM common_defect_links').fetchone()[0], 0)

    def test_receipt_rejects_reports_not_bound_to_the_actual_prompt(self):
        proposal = self.validated()
        self.propose([])
        with self.connection:
            planning_review_state.bind_common_defect_snapshot(self.connection, 'repo', 'base', [1])
        with self.assertRaisesRegex(ValueError, '전달되지 않은'):
            with self.connection:
                planning_review_state.validate(self.connection, 'repo', 'base', 'output', 'reviewed',
                                               common_defect_links=proposal)
        attempt = planning_review_state.get(self.connection, 'repo', 'attempt')
        self.assertIsNone(attempt['result'])
        self.assertEqual(json.loads(attempt['common_defect_links']), [])

    def test_renamed_requirement_uses_existing_resolver_without_rewriting_declared_source(self):
        source = 'docs/requirements/[R-002] Previous.md'
        current = 'docs/requirements/[R-002] Current.md'
        document = '# [R-002] Current\n\nRequired shared contract.'
        self.connection.execute('UPDATE exec_plans SET sources=?', (json.dumps([source]),))
        self.connection.commit()
        output = dict(revision='base', links=[{**self.link, 'source': source}])
        with mock.patch('loop_manager_internal.planning.common_defect_links.revision_requirement_documents',
                        return_value={current: document}):
            links = self.validated(output)
        self.assertEqual(links[0]['source'], source)
        for documents in ({current.replace('requirements/', 'requirements/archive/'): document},
                          {current: document, 'docs/requirements/[R-002] Duplicate.md': '# [R-002] Duplicate\nRequired shared contract.'}):
            with self.subTest(documents=documents), mock.patch(
                    'loop_manager_internal.planning.common_defect_links.revision_requirement_documents', return_value=documents):
                with self.assertRaises(ValueError):
                    self.validated(output)
