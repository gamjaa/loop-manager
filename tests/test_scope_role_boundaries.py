"""역할·경로 정책은 snapshot으로 검사한다. 파싱/index는 scope_git_metadata 회귀가 맡는다."""
from pathlib import Path
import unittest
from unittest import mock

import loop_manager
from tests.support.scope import snapshot


class ScopeRoleBoundariesTest(unittest.TestCase):
    plan_path = 'docs/plans/[G-001] Scope Safety.md'

    def setUp(self):
        process = mock.patch('subprocess.Popen', side_effect=AssertionError('예상 밖 process 실행'))
        self.process = process.start()
        self.addCleanup(process.stop)
        self.addCleanup(self.process.assert_not_called)

    def validate(self, role, after, selected=None):
        loop_manager.validate_agent_scope(role, Path('/managed'), snapshot(), after,
                                          selected_plan_path=selected)

    def test_role_boundaries_reject_protected_and_read_only_changes(self):
        scenarios = (
            ('planner', snapshot(dirty={'docs/PRODUCT.md': (('tracked',), 'changed')}),
             None, 'docs/PRODUCT.md'),
            ('worker', snapshot(dirty={'docs/requirements/archive/Active.md': (('tracked',), 'changed')}),
             self.plan_path, 'docs/requirements/archive/Active.md'),
            ('reviewer', snapshot(index={'src/app.py': ('H 100644 changed 0',)},
                                  dirty={'review.txt': (('untracked',), 'new')}),
             None, 'review.txt'),
        )
        for role, after, selected, expected in scenarios:
            with self.subTest(role=role), self.assertRaisesRegex(loop_manager.AgentScopeError, expected):
                self.validate(role, after, selected)

    def test_planner_new_plan_and_worker_selected_plan_are_allowed(self):
        for role, path, kinds, selected in (
            ('planner', 'docs/plans/[G-002] New Plan.md', ('untracked',), None),
            ('worker', self.plan_path, ('tracked',), self.plan_path),
        ):
            with self.subTest(role=role):
                self.validate(role, snapshot(dirty={path: (kinds, 'changed')}), selected)

    def test_architecture_changes_are_worker_only(self):
        after = snapshot(dirty={'docs/ARCHITECTURE.md': (('tracked',), 'changed')})
        for role in ('worker', 'planner', 'maintenance_planner', 'reviewer', 'planner_check'):
            with self.subTest(role=role):
                if role == 'worker':
                    self.validate(role, after, self.plan_path)
                else:
                    with self.assertRaises(loop_manager.AgentScopeError):
                        self.validate(role, after)

    def test_worker_related_documents_allow_active_requirements_and_guidance_without_approval(self):
        for path in ('docs/requirements/Active.md', 'docs/PRODUCT.md', 'docs/ARCHITECTURE.md',
                     'docs/AGENTS.common.md', 'docs/AGENTS.worker.md', 'AGENTS.md'):
            with self.subTest(path=path):
                self.validate('worker', snapshot(dirty={path: (('tracked',), 'changed')}), self.plan_path)
                for role in ('planner', 'reviewer', 'planner_check'):
                    with self.assertRaises(loop_manager.AgentScopeError):
                        self.validate(role, snapshot(dirty={path: (('tracked',), 'changed')}))

    def test_planner_existing_plan_change_reaches_snapshot_validator(self):
        self.validate('planner', snapshot(dirty={self.plan_path: (('tracked',), 'changed')}))

    def test_planner_check_rejects_plan_and_code_changes(self):
        for path in (self.plan_path, 'src/app.py'):
            with self.subTest(path=path), self.assertRaisesRegex(
                loop_manager.AgentScopeError, 'planner_check Git 범위 위반'
            ):
                self.validate('planner_check', snapshot(dirty={path: (('tracked',), 'changed')}))
