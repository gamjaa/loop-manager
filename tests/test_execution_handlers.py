"""Composition contracts shared by the extracted execution phases."""
from unittest import TestCase

import loop_manager as lm
from loop_manager_internal.application.contracts import WorkerPublication
from tests.support.execution import ExecutionFixture, execution_fixture
from loop_manager_internal.execution.execution_runtime import ExecutionPhaseCoordinator


class ExecutionCompositionTests(ExecutionFixture, TestCase):
    def test_phase_parsers_receive_owned_legacy_metadata_without_mutating_snapshot(self):
        self.handler.plan = {**self.plan, 'depends_on': ['G-046']}

        def parse(message, plan):
            self.assertEqual(message, 'saved result')
            self.assertIsInstance(plan, dict)
            self.assertEqual(plan['sources'], ['docs/PRODUCT.md'])
            self.assertEqual(plan['depends_on'], ['G-046'])
            plan['sources'].append('docs/uninstalled.md')
            plan['depends_on'].clear()
            return 'parsed result'

        self.effects.validation.parse_worker_result.side_effect = parse
        for phase in ('revision', 'worker', 'verify', 'review', 'publication'):
            with self.subTest(phase=phase):
                port = getattr(self.handler, f'_{phase}_handler')().ports
                self.assertEqual(port.parse_worker_result('saved result', self.handler.plan),
                                 'parsed result')
                self.assertEqual(self.handler.plan['sources'], ('docs/PRODUCT.md',))
                self.assertEqual(self.handler.plan['depends_on'], ('G-046',))

    def test_publication_renderer_accepts_immutable_plan_sources(self):
        self.effects.publication.build_pr_body.side_effect = lm.build_pr_body
        worker = WorkerPublication('Preserve phase metadata contracts',
                                   'Preserves existing publication metadata.', 'targeted test: PASS')
        body = self.handler._publication_handler().ports.build_pr_body(
            worker, self.handler.plan, 'docs/plans/[G-047] Runtime.md',
            '## Purpose\n\nPreserve phase behavior.\n',
            'git@github.com:octo/loop-manager.git', 'a' * 40,
        )
        self.assertIn(worker.summary, body)
        self.assertIn('Purpose: Preserve phase behavior.', body)
        self.assertIn('[docs/PRODUCT.md](https://github.com/octo/loop-manager/blob/', body)
        self.assertEqual(self.handler.plan['sources'], ('docs/PRODUCT.md',))

    def test_preparation_and_revision_level_helpers_receive_owned_metadata(self):
        def ensure_level(repo, plan):
            self.assertEqual(repo, 'repo')
            self.assertEqual(plan, self.plan)
            plan['sources'].clear()
            return 2

        self.effects.validation.ensure_worker_level_state.side_effect = ensure_level
        for phase in ('workspace', 'revision'):
            with self.subTest(phase=phase):
                port = getattr(self.handler, f'_{phase}_handler')().ports
                self.assertEqual(port.ensure_worker_level_state('repo', self.handler.plan), 2)
                self.assertEqual(self.handler.plan['sources'], ('docs/PRODUCT.md',))
                self.assertEqual(self.plan['sources'], ['docs/PRODUCT.md'])

    def test_reused_context_port_observes_new_preparation_without_mutating_old_context(self):
        context_handler = self.handler._context_handler()
        before = context_handler._context()
        replacement = {**self.plan, 'title': 'Updated definition'}
        self.handler.plan = replacement
        after = context_handler._context()
        self.assertEqual(before.plan.title, 'Runtime')
        self.assertEqual(after.plan.title, 'Updated definition')
        replacement['title'] = 'Uninstalled change'
        self.assertEqual(context_handler._context().plan.title, 'Updated definition')
        with self.assertRaises(TypeError):
            self.handler.plan['title'] = 'Unsafe mutation'
        replacement['sources'].append('docs/requirements/uninstalled.md')
        self.assertEqual(context_handler._context().plan.sources, ('docs/PRODUCT.md',))

    def test_preparation_and_phase_transition_are_isolated_between_selected_plans(self):
        other = execution_fixture(self)
        self.handler.plan = {**self.plan, 'title': 'First selected plan'}
        self.assertEqual(other.handler.plan['title'], 'Runtime')
        self.handler.run(self.context())
        self.assertEqual(self.state.row()['phase'], 'VERIFYING')
        self.assertEqual(other.state.row()['phase'], 'WORKING')
        other.effects.agent.run_agent.assert_not_called()

    def test_retained_phase_context_is_rejected_after_durable_worker_transition(self):
        context = self.context()
        coordinator = ExecutionPhaseCoordinator(self.handler)
        self.handler.run(context)
        with self.assertRaisesRegex(RuntimeError, 'context'):
            coordinator.dispatch(context)
        self.effects.agent.run_agent.assert_called_once()
        self.effects.validation.verify.assert_not_called()

    def test_post_effect_guard_refreshes_uncoordinated_context_after_session_start(self):
        context = self.context()
        self.state.update_project('repo', worker_session_id='started-session')

        refreshed = self.handler._post_effect_guard(context)

        self.assertEqual(refreshed.worker_session_id, 'started-session')
        self.state.check_context(refreshed)
