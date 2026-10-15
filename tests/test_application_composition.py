"""Dependency assembly isolation and registration failures, without Git or agents."""
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.application.composition import Composition, create_application


@dataclass(frozen=True)
class NextEffects:
    next: object


def alternating(effects: NextEffects, remaining: int) -> int:
    return 0 if remaining == 0 else 1 + effects.next(remaining - 1)


class ApplicationCompositionTests(unittest.TestCase):
    def test_fresh_assembly_opens_no_resources_and_does_not_import_the_cli(self):
        # A fresh interpreter is required: the parent already imports the CLI
        # to exercise its compatibility exports in the other tests below.
        code = """
import sqlite3, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
def forbidden(*args, **kwargs):
    raise AssertionError('resource effect during assembly')
sqlite3.connect = forbidden
subprocess.run = forbidden
class ForbiddenPopen(subprocess.Popen):
    __init__ = forbidden
subprocess.Popen = ForbiddenPopen
from loop_manager_internal.application.composition import create_application
create_application(Path(sys.argv[1]))
assert 'loop_manager' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-c', code, str(Path(__file__).resolve().parents[1])],
            capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_independent_graphs_use_their_own_root_and_command_boundary(self):
        commands = [mock.Mock(return_value=subprocess.CompletedProcess([], 0, sha, ''))
                    for sha in ('first', 'second')]
        applications = [create_application(Path('/runtime') / sha, overrides={'command': command})
                        for sha, command in zip(('first', 'second'), commands)]
        # A CLI compatibility patch must never influence a separately built graph.
        with mock.patch.object(loop_manager, 'command', side_effect=AssertionError('CLI dependency')):
            for application, command, sha in zip(applications, commands, ('first', 'second')):
                exports = application.exports
                self.assertEqual(exports['ROOT'], Path('/runtime') / sha)
                self.assertEqual(exports['git'](Path('/repo'), 'rev-parse', 'HEAD').stdout, sha)
                command.assert_called_once_with(
                    ['git', '-C', str(Path('/repo')), 'rev-parse', 'HEAD'], check=False)
                exports['command'] = mock.Mock(side_effect=AssertionError('export mutation'))
                self.assertIs(application.exports['command'], command)

    def test_reciprocal_dependencies_do_not_depend_on_registration_order(self):
        assembly = Composition({})
        assembly.bind('first', alternating, lambda: NextEffects(assembly.callback('second')))
        assembly.bind('second', alternating, lambda: NextEffects(assembly.callback('first')))
        assembly.validate()
        self.assertEqual(assembly.exports['first'](5), 5)
        with self.assertRaisesRegex(ValueError, '완료된 composition'):
            assembly.add('late', object())

    def test_missing_dependencies_and_duplicate_providers_fail_during_assembly(self):
        assembly = Composition({})
        assembly.bind('first', alternating, lambda: NextEffects(assembly.callback('missing')))
        with self.assertRaisesRegex(ValueError, '등록되지 않은 의존성: missing'):
            assembly.validate()
        with self.assertRaisesRegex(ValueError, '중복 의존성 등록: first'):
            assembly.add('first', object())

    def test_domain_registration_adds_a_service_without_editing_the_entry_point(self):
        def register(assembly):
            assembly.bind('additional', alternating,
                          lambda: NextEffects(assembly.callback('additional')))

        application = create_application(Path('/runtime'), registrars=(register,))
        self.assertEqual(application.exports['additional'](3), 3)
        self.assertFalse(hasattr(loop_manager, 'additional'))

    def test_unknown_override_cannot_silently_leave_a_real_effect_connected(self):
        with self.assertRaisesRegex(ValueError, '등록되지 않은 override: commmand'):
            create_application(Path('/runtime'), overrides={'commmand': mock.Mock()})

    def test_non_callable_override_is_rejected_before_execution(self):
        with self.assertRaisesRegex(ValueError, '호출할 수 없는 의존성: command'):
            create_application(Path('/runtime'), overrides={'command': None})
