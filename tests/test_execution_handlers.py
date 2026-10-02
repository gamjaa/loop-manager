"""Composition contracts shared by the extracted execution phases."""
from unittest import TestCase

from tests.support.execution import ExecutionFixture, execution_fixture
from loop_manager_internal.execution.execution_runtime import ExecutionPhaseCoordinator


class ExecutionCompositionTests(ExecutionFixture, TestCase):
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
