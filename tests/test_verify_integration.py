"""Trusted runner ownership and shared budgets, without actual nested runners."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
import ast
from dataclasses import asdict
from contextlib import closing
from unittest import mock

from loop_manager_internal.verification.coordination import VerifyCoordinator
from loop_manager_internal.verification.execution_clock import BUDGET_DIRECTORY, ExecutionClock
from loop_manager_internal.verification.resources import Snapshot, MIB
from loop_manager_internal.verification import integration, runner, contract, unittest_adapter, manifest
from loop_manager_internal.execution import verify_process
from loop_manager_internal.verification.guard import _COORDINATION_ENV, MARKER, worker_environment
from loop_manager_internal.verification.timing import EventWriter, summarize, atomic_json


class CoordinatorFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 0.0
        self.identities = {101: 1, 102: 2, 201: 3}
        self.busy = True
        sampler = SimpleNamespace(sample=lambda: Snapshot(self.now, 4, 4 if self.busy else 0,
                                                         4096*MIB, 'fake'))
        self.coordinator = VerifyCoordinator(sampler=sampler, clock=lambda: self.now,
                                             identity=self.identities.get)
        self.coordinator.server = SimpleNamespace(server_address=('127.0.0.1', 9))
        self.parent, self.secret = self.coordinator.open(1, require_guard=True)
        self.coordinator.guard(self.parent, lambda pid: pid in self.identities)
        self.coordinator.track_waiting(self.parent, self.root)

    def parent_request(self, operation, **data):
        return self.coordinator.request(self.parent, self.secret, operation, **data)

    def child(self, pid=101, *, observer=True):
        lease = self.parent_request('open_child', observer_only=observer)
        self.parent_request('bind_child', child=lease['session'], pid=pid)
        return lease

    def child_request(self, lease, operation, **data):
        return self.coordinator.request(lease['session'], lease['secret'], operation, **data)


class ChildCoordinatorTests(CoordinatorFixture, unittest.TestCase):
    def test_live_children_wait_once_and_preserve_actual_processing_budget(self):
        for count in (1, 2):
            with self.subTest(children=count):
                leases = [self.child(101 + index) for index in range(count)]
                budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
                started = self.now
                for step in range(301):
                    self.now = started + step / 10
                    self.parent_request('children_wait')
                    for lease in leases:
                        self.assertTrue(self.child_request(lease, 'observer_state', waiting=True)['ready'])
                self.assertAlmostEqual(budget.remaining(10), 10)
                for lease in leases:
                    status = self.parent_request('child_status', child=lease['session'])
                    self.assertIsNotNone(status['identity'])
                    self.assertNotIn('secret', json.dumps(status))
                    self.identities.pop(status['pid'])
                    self.parent_request('finish_child', child=lease['session'], stopped=True)
                self.now += 2
                self.assertAlmostEqual(budget.remaining(10), 8)
                self.now += 10
                self.assertEqual(budget.remaining(10), 0)
                self.assertAlmostEqual(budget.elapsed(), 12)
                self.identities.update({101: 1, 102: 2})

    def test_preparation_stale_heartbeat_and_reused_pid_are_not_pure_wait(self):
        lease = self.child()
        budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
        self.child_request(lease, 'observer_state', waiting=True)
        for step in range(21):
            self.now = step / 10
            self.child_request(lease, 'observer_state', waiting=True)
        self.assertAlmostEqual(budget.elapsed(), 2)  # parent is preparing
        for step in range(21):
            self.now = 2 + step / 10
            self.parent_request('children_wait')
        self.assertAlmostEqual(budget.elapsed(), 3)  # child's heartbeat went stale
        self.identities[101] = 99
        with self.assertRaisesRegex(RuntimeError, 'identity'):
            self.child_request(lease, 'observer_state', waiting=True)
        before = budget.waiting()
        for step in range(11):
            self.now = 4 + step / 10
            self.parent_request('children_wait')
        self.assertEqual(budget.waiting(), before)

    def test_active_and_uncertain_sibling_block_wait_and_other_run_is_preserved(self):
        waiting = self.child()
        running = self.child(102, observer=False)
        other, other_secret = self.coordinator.open(1)
        self.busy = False
        reservation = self.child_request(running, 'acquire', run_id=running['session'], unit_id='compile')
        other_reservation = self.coordinator.request(other, other_secret, 'acquire')
        self.assertTrue(reservation['granted'])
        self.assertTrue(other_reservation['granted'])
        budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
        for step in range(11):
            self.now = step / 10
            self.parent_request('children_wait')
            self.child_request(waiting, 'observer_state', waiting=True)
        self.assertEqual(budget.elapsed(), 1)
        self.parent_request('finish_child', child=running['session'], stopped=False)
        self.assertTrue(self.coordinator.reservations[reservation['token']]['uncertain'])
        self.coordinator.finish(self.parent, stopped=False)
        self.assertEqual(set(self.coordinator.retained(self.parent)), {reservation['token']})
        self.assertIn(other_reservation['token'], self.coordinator.reservations)
        self.assertFalse(self.coordinator.reservations[other_reservation['token']]['uncertain'])
        with self.assertRaisesRegex(RuntimeError, '종료'):
            self.parent_request('finish_child', child=running['session'], stopped=True)
        self.identities.pop(102)
        self.parent_request('finish_child', child=running['session'], stopped=True)
        self.assertNotIn(reservation['token'], self.coordinator.reservations)
        self.assertIn(other_reservation['token'], self.coordinator.reservations)

    def test_child_credentials_cannot_cross_ownership_or_expand_observer_authority(self):
        lease = self.parent_request('open_child', observer_only=True)
        self.assertFalse(self.child_request(lease, 'observer_state', waiting=False)['ready'])
        for operation in ('acquire', 'open_child', 'children_wait'):
            with self.subTest(operation=operation), self.assertRaises(RuntimeError):
                self.child_request(lease, operation, observer_only=False)
        self.assertFalse(self.coordinator.reservations)
        for pid in (True, 999):
            with self.subTest(pid=pid), self.assertRaises(RuntimeError):
                self.parent_request('bind_child', child=lease['session'], pid=pid)
        other, other_secret = self.coordinator.open(1)
        for operation in ('bind_child', 'child_status', 'finish_child'):
            with self.subTest(operation=operation), self.assertRaises(RuntimeError):
                self.coordinator.request(other, other_secret, operation,
                                         child=lease['session'], pid=101, stopped=True)
        self.assertIsNone(self.coordinator.sessions[lease['session']]['pid'])

    def test_ancestor_exit_cannot_reclaim_live_descendant_runner_or_worker(self):
        lease = self.child(observer=False)
        nested = self.child_request(lease, 'open_child', observer_only=False)
        with mock.patch('loop_manager_internal.verification.coordination.descendant', return_value=True):
            self.child_request(lease, 'bind_child', child=nested['session'], pid=102)
            self.busy = False
            reservation = self.child_request(nested, 'acquire', unit_id='tests')
            self.child_request(nested, 'bind', token=reservation['token'], pid=201)
        other, other_secret = self.coordinator.open(1)
        independent = self.coordinator.request(other, other_secret, 'acquire')
        self.assertTrue(independent['granted'])
        self.identities.pop(101)
        for remaining in ('runner-and-worker', 'worker'):
            with self.subTest(remaining=remaining):
                with self.assertRaises(verify_process.VerifyTerminationUncertain):
                    self.parent_request('finish_child', child=lease['session'], stopped=True)
                self.assertTrue(self.coordinator.sessions[nested['session']]['uncertain'])
                self.assertEqual(set(self.coordinator.retained(self.parent)), {reservation['token']})
                self.assertFalse(self.coordinator.reservations[independent['token']]['uncertain'])
                if remaining == 'runner-and-worker':
                    self.identities.pop(102)
        self.identities.pop(201)
        self.parent_request('finish_child', child=lease['session'], stopped=True)
        self.assertNotIn(nested['session'], self.coordinator.sessions)
        self.assertEqual(set(self.coordinator.reservations), {independent['token']})

    def test_coordinated_trace_requires_real_bind_and_confirmed_release(self):
        lease = self.child(observer=False)
        self.busy = False
        reservation = self.child_request(lease, 'acquire', run_id=lease['session'], unit_id='tests')
        token = reservation['token']
        with mock.patch('loop_manager_internal.verification.coordination.descendant', return_value=True):
            self.identities[1] = 99
            with self.assertRaises(RuntimeError):
                self.child_request(lease, 'bind', token=token, pid=True)
            self.assertIsNone(self.coordinator.reservations[token]['pid'])
            self.child_request(lease, 'bind', token=token, pid=201)
        self.child_request(lease, 'idle', token=token)
        with self.assertRaises(RuntimeError):
            self.child_request(lease, 'release', token=token)
        self.identities.pop(201)
        self.child_request(lease, 'release', token=token)
        status = self.parent_request('child_status', child=lease['session'])
        self.assertEqual([event['operation'] for event in status['events']], ['acquire', 'bind', 'idle', 'release'])
        self.assertEqual(status['events'][1]['identity'], 3)
        self.assertFalse(status['reservations'])


class TrustedRunnerPortTests(CoordinatorFixture, unittest.TestCase):
    def test_uncertain_process_preserves_redacted_console_before_output_closes(self):
        budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
        process = mock.Mock(pid=101, returncode=None)
        process.poll.return_value = None
        process.wait.side_effect = KeyboardInterrupt
        def launch(*_arguments, **options):
            options['stdout'].write(b'partial diagnostic\nsecret=private-value\n')
            options['stdout'].flush()
            return process
        with mock.patch.object(integration, '_state', SimpleNamespace(audit=None)), \
                mock.patch.object(verify_process, 'os', SimpleNamespace(name='posix', SEEK_END=os.SEEK_END)), \
                mock.patch.object(verify_process, 'sys', SimpleNamespace(platform='linux', executable=sys.executable, exc_info=sys.exc_info)), \
                mock.patch.object(verify_process.subprocess, 'Popen', side_effect=launch), \
                mock.patch.object(verify_process, 'terminate_verify_process',
                    side_effect=verify_process.VerifyTerminationUncertain('still alive')):
            port = integration.TrustedRunnerPort(SimpleNamespace(request=self.parent_request), budget, 10)
            with self.assertRaises(verify_process.VerifyTerminationUncertain):
                port.run(self.root, self.root / 'interrupted', coordinated=False)
        output = (self.root / 'interrupted/console.log').read_text()
        self.assertIn('partial diagnostic', output)
        self.assertNotIn('private-value', output)
        proof = integration._json(self.root / 'interrupted/coordination.json')
        self.assertFalse(proof['stopped'])
        self.assertTrue(self.coordinator.sessions[proof['session']]['uncertain'])

    def test_port_keeps_one_budget_through_live_runner_wait_and_blocks_after_timeout(self):
        for actual in (2, 12):
            with self.subTest(actual=actual):
                self.now = 0
                self.identities[101] = 1
                process = mock.Mock(pid=101, returncode=None)
                lease = {}
                def request(operation, **data):
                    reply = self.parent_request(operation, **data)
                    if operation == 'open_child':
                        lease.update(reply)
                    elif operation == 'bind_child':
                        self.child_request(lease, 'observer_state', waiting=True)
                    return reply
                def wait(timeout):
                    self.assertLessEqual(timeout, .1)
                    self.now = round(self.now + .1, 6)
                    self.child_request(lease, 'observer_state', waiting=self.now <= 30)
                    if self.now >= 30 + actual:
                        process.returncode = 0
                        self.identities.pop(101)
                        return 0
                    raise subprocess.TimeoutExpired(['runner'], timeout)
                def terminate(_process):
                    self.identities.pop(101)
                    process.returncode = -9
                process.wait.side_effect = wait
                process.poll.side_effect = lambda: process.returncode
                budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
                with mock.patch.object(integration, '_state', SimpleNamespace(audit=None)), \
                        mock.patch.object(verify_process, 'os', SimpleNamespace(name='posix', SEEK_END=os.SEEK_END)), \
                        mock.patch.object(verify_process, 'sys', SimpleNamespace(platform='linux', executable=sys.executable, exc_info=sys.exc_info)), \
                        mock.patch.object(verify_process.subprocess, 'Popen', return_value=process) as launch, \
                        mock.patch.object(verify_process, 'terminate_verify_process', side_effect=terminate) as stop:
                    port = integration.TrustedRunnerPort(SimpleNamespace(request=request), budget, 10)
                    passed, timed_out, _output = port.run(self.root, self.root / f'run-{actual}', coordinated=False)
                    self.assertEqual(launch.call_args.args[0][1:4], ('-I', '-X', 'utf8'))
                    self.assertEqual((passed, timed_out), (actual == 2, actual == 12))
                    self.assertEqual(stop.call_count, int(actual == 12))
                    if actual == 2:
                        self.assertAlmostEqual(budget.elapsed(), 2)
                    else:
                        with self.assertRaises(TimeoutError):
                            port.run(self.root, self.root / 'not-started', coordinated=False)
                        self.assertEqual(launch.call_count, 1)
                self.assertEqual(budget.active_processes, 0)
                self.assertEqual(set(self.coordinator.sessions), {self.parent})

    def test_launch_uses_fixed_runtime_and_sanitizes_parent_and_ordinary_children(self):
        for coordinated in (False, True):
            with self.subTest(coordinated=coordinated):
                self.identities[101] = 1
                budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
                environment = dict(zip(_COORDINATION_ENV, ('endpoint', 'secret', 'session', 'budget', 'observer')))
                environment.update({MARKER: '1', 'LOOP_MANAGER_VERIFY_RUNNER': '/untrusted.py',
                                    'LOOP_MANAGER_VERIFY_PARENT_RUN_ID': 'inherited-pipeline',
                                    'LOOP_MANAGER_VERIFY_PARTIAL_BASE': 'inherited', 'PYTHONPATH': '/candidate'})
                def run(arguments, project, timeout, **options):
                    self.assertEqual(arguments[:6], [sys.executable, '-I', '-X', 'utf8', '-u', str(Path(integration.__file__).with_name('entrypoint.py'))])
                    self.assertNotIn('/untrusted.py', arguments)
                    self.assertNotIn('--timeout', arguments)
                    self.assertIsNone(timeout)
                    child = options['environment']
                    self.assertNotIn(MARKER, child)
                    self.assertNotIn('PYTHONPATH', child)
                    self.assertNotIn('LOOP_MANAGER_VERIFY_PARTIAL_BASE', child)
                    self.assertEqual(child['LOOP_MANAGER_VERIFY_PARENT_RUN_ID'], 'owned-parent')
                    self.assertEqual('LOOP_MANAGER_VERIFY_OBSERVER' in child, not coordinated)
                    self.assertEqual('LOOP_MANAGER_VERIFY_SECRET' in child, coordinated)
                    self.assertTrue(all(key not in worker_environment(child) for key in _COORDINATION_ENV))
                    options['on_started'](SimpleNamespace(pid=101), None)
                    options['on_wait']()
                    self.identities.pop(101)
                    return True, False, verify_process.VerifyProcessOutput('complete', exit_code=0)
                with mock.patch.dict(os.environ, environment), \
                        mock.patch.object(integration, '_state', SimpleNamespace(audit=None)), \
                        mock.patch.object(integration, 'run_verify_process', side_effect=run):
                    before = dict(os.environ)
                    port = integration.TrustedRunnerPort(SimpleNamespace(request=self.parent_request), budget, None,
                                                         parent_run_id='owned-parent')
                    self.assertTrue(port.run(self.root, self.root / str(coordinated), coordinated=coordinated)[0])
                    self.assertEqual(dict(os.environ), before)
                proof = json.loads((self.root / str(coordinated) / 'coordination.json').read_text())
                start = json.loads((self.root / str(coordinated) / 'runner-start.json').read_text())
                self.assertEqual(start['state'], 'bound')
                self.assertEqual(start['identity'], proof['identity'])
                self.assertNotIn('secret', json.dumps(proof))
                self.assertTrue(proof['stopped'])

    def test_guarded_process_cannot_create_or_reuse_the_port(self):
        client = mock.Mock()
        with mock.patch.object(integration, '_state', SimpleNamespace(audit=object())):
            with self.assertRaisesRegex(RuntimeError, 'guard'):
                integration.TrustedRunnerPort(client, mock.Mock(), 10)
        with mock.patch.object(integration, '_state', SimpleNamespace(audit=None, wrapper=subprocess.Popen.__init__)):
            with self.assertRaisesRegex(RuntimeError, 'guard'):
                integration.TrustedRunnerPort(client, mock.Mock(), 10)
        with mock.patch.object(integration, '_state', SimpleNamespace(audit=None)):
            port = integration.TrustedRunnerPort(client, mock.Mock(), 10)
        with mock.patch.object(integration, '_state', SimpleNamespace(audit=object())):
            with self.assertRaisesRegex(RuntimeError, 'guard'):
                port.run(self.root, self.root / 'denied', coordinated=True)
        client.request.assert_not_called()

    def test_trusted_launch_rejects_unexpected_bootstrap_before_popen(self):
        budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
        def run(arguments, *_args, **options):
            try:
                return options['launch_process']([sys.executable, '-c', 'untrusted', *arguments])
            finally:
                options['on_stopped']()  # this fake boundary proved non-start
        with mock.patch.object(integration, '_state', SimpleNamespace(audit=None)), \
                mock.patch.object(integration, 'run_verify_process', side_effect=run), \
                mock.patch.object(integration.subprocess, 'Popen') as launch:
            port = integration.TrustedRunnerPort(SimpleNamespace(request=self.parent_request), budget, 10)
            with self.assertRaisesRegex(RuntimeError, 'bootstrap'):
                port.run(self.root, self.root / 'denied-bootstrap', coordinated=False)
            launch.assert_not_called()
        self.assertEqual(set(self.coordinator.sessions), {self.parent})

    def test_linux_windows_and_direct_bootstraps_all_isolate_candidate_imports(self):
        for bootstrap in (None, integration._LINUX_SUBREAPER, integration._WINDOWS_VERIFY_BOOTSTRAP):
            with self.subTest(bootstrap=bootstrap is not None):
                self.identities[101] = 1
                budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
                def run(arguments, project, _timeout, **options):
                    command = [sys.executable, '-c', bootstrap, *arguments] if bootstrap else arguments
                    process = options['launch_process'](command, cwd=project, env=options['environment'])
                    options['on_started'](process, None)
                    self.identities.pop(101)
                    return True, False, verify_process.VerifyProcessOutput('', exit_code=0)
                with mock.patch.object(integration, '_state', SimpleNamespace(audit=None)), \
                        mock.patch.object(integration, 'run_verify_process', side_effect=run), \
                        mock.patch.object(integration.subprocess, 'Popen', return_value=SimpleNamespace(pid=101)) as launch:
                    port = integration.TrustedRunnerPort(SimpleNamespace(request=self.parent_request), budget, 10)
                    self.assertTrue(port.run(self.root, self.root / 'bootstrap', coordinated=False)[0])
                    command = launch.call_args.args[0]
                    self.assertEqual(command[:4], (sys.executable, '-I', '-X', 'utf8'))
                    self.assertEqual(command[4], '-c' if bootstrap else '-u')
                    if bootstrap:
                        self.assertEqual(command[5], bootstrap)
                        self.assertEqual(command[6:10], (sys.executable, '-I', '-X', 'utf8'))

    def test_uncertain_stop_retains_lease_and_diagnostic_failure_keeps_uncertain_type(self):
        for fail_diagnostic in (False, True):
            with self.subTest(fail_diagnostic=fail_diagnostic):
                self.identities[101] = 1
                budget = ExecutionClock({BUDGET_DIRECTORY: str(self.root)}, clock=lambda: self.now)
                def request(operation, **data):
                    if operation == 'child_status' and fail_diagnostic:
                        raise OSError('diagnostic unavailable')
                    return self.parent_request(operation, **data)
                def run(*_args, **options):
                    options['on_started'](SimpleNamespace(pid=101), None)
                    if fail_diagnostic:
                        raise OSError('still alive')  # no stop receipt despite a generic error
                    raise verify_process.VerifyTerminationUncertain('still alive')
                with mock.patch.object(integration, '_state', SimpleNamespace(audit=None)), \
                        mock.patch.object(integration, 'run_verify_process', side_effect=run):
                    port = integration.TrustedRunnerPort(SimpleNamespace(request=request), budget, 10)
                    with self.assertRaisesRegex(verify_process.VerifyTerminationUncertain, 'still alive'):
                        port.run(self.root, self.root / f'uncertain-{fail_diagnostic}', coordinated=False)
                child, = [key for key, owner in self.coordinator.sessions.items() if owner['parent'] == self.parent]
                start = json.loads((self.root / f'uncertain-{fail_diagnostic}' / 'runner-start.json').read_text())
                self.assertEqual(start['identity'], 1)
                self.assertTrue(self.coordinator.sessions[child]['uncertain'])
                with self.assertRaises(RuntimeError):
                    self.coordinator.request(child, self.coordinator.sessions[child]['secret'], 'observer_state', waiting=True)
                self.identities.pop(101)
                self.parent_request('finish_child', child=child, stopped=True)


class StandaloneObserverTests(CoordinatorFixture, unittest.TestCase):
    def test_uncertain_body_or_final_stop_preserves_owned_budget_directory(self):
        for origin in ('body', 'finish'):
            with self.subTest(origin=origin):
                budget = ExecutionClock({})
                self.addCleanup(budget.close)
                local = mock.Mock(reservations={})
                local.open.return_value = ('session', 'secret')
                error = verify_process.VerifyTerminationUncertain('child alive')
                if origin == 'finish':
                    local.finish.side_effect = error
                stage = {'id': 'tests', 'kind': 'tests', 'cwd': '.', 'execution': 'serial'}
                connection = contract.Connection(self.root, 'unittest', Path(sys.executable), (stage,))
                with mock.patch.dict(os.environ, {}, clear=True), \
                        mock.patch.object(runner, 'load', return_value=connection), \
                        mock.patch.object(runner, 'ExecutionClock', return_value=budget), \
                        mock.patch.object(runner, 'VerifyCoordinator', return_value=local), \
                        mock.patch.object(runner, 'install_guard'), \
                        mock.patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(stdout='head')), \
                        mock.patch.object(unittest_adapter, 'collect_project', return_value=([manifest.Module('unit', ('a',))], sys.version)), \
                        mock.patch.object(unittest_adapter, 'execute', return_value=True,
                                          side_effect=error if origin == 'body' else None), \
                        mock.patch('sys.stdout'):
                    with self.assertRaises(verify_process.VerifyTerminationUncertain):
                        runner.run(self.root / origin, 1, 10)
                    self.assertNotIn(BUDGET_DIRECTORY, os.environ)
                self.assertTrue(budget.directory.is_dir())
                if origin == 'body':
                    local.finish.assert_called_once_with('session', stopped=False)
                local.close.assert_called_once()

    def test_observed_standalone_inherits_missing_parent_timeout(self):
        lease = self.child()
        with mock.patch.dict(os.environ, {'LOOP_MANAGER_VERIFY_OBSERVER': json.dumps(lease)}, clear=True), \
                mock.patch.object(sys, 'argv', ['runner', '--execute']), \
                mock.patch.object(runner, 'run', return_value=0) as run:
            self.assertEqual(runner.main(), 0)
        self.assertIsNone(run.call_args.args[2])

    def test_local_resource_decisions_feed_parent_budget_without_second_wait_source(self):
        lease = self.child()
        sampler = SimpleNamespace(sample=lambda: Snapshot(self.now, 4, 4 if self.busy else 0, 4096*MIB, 'fake'))
        local = VerifyCoordinator(sampler=sampler, clock=lambda: self.now, identity=self.identities.get)
        remote = mock.Mock()
        remote.request.side_effect = lambda operation, **data: self.child_request(lease, operation, **data)
        stage = {'id': 'tests', 'kind': 'tests', 'cwd': '.', 'execution': 'serial'}
        connection = contract.Connection(self.root, 'unittest', Path(sys.executable), (stage,))
        def collect(_connection, _stage, _directory, _run, _entrypoint, _timeout, _execute, _writer, client):
            self.parent_request('children_wait')
            self.assertFalse(client.request('acquire')['granted'])
            self.now = .1
            self.parent_request('children_wait')
            self.assertFalse(client.request('acquire')['granted'])
            self.busy = False
            self.now = .2
            reservation = client.request('acquire')
            self.assertTrue(reservation['granted'])
            client.request('not_started', token=reservation['token'])
            session, = local.sessions.values()
            self.assertEqual(Path(session['budget_directory']), self.root / 'logs/local-budget')
            return [manifest.Module('unit', ('a',))], sys.version
        environment = {BUDGET_DIRECTORY: str(self.root), 'LOOP_MANAGER_VERIFY_OBSERVER': json.dumps(lease)}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(runner, 'load', return_value=connection), \
                mock.patch.object(runner, 'install_guard'), \
                mock.patch.object(runner, 'VerifyCoordinator', return_value=local), \
                mock.patch.object(runner, 'CoordinatorClient', return_value=remote), \
                mock.patch.object(runner.time, 'monotonic', side_effect=lambda: self.now), \
                mock.patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(stdout='head')), \
                mock.patch.object(unittest_adapter, 'collect_project', side_effect=collect), \
                mock.patch.object(unittest_adapter, 'execute', return_value=True), \
                mock.patch('sys.stdout'):
            self.assertEqual(runner.run(self.root / 'logs', 1, 10), 0)
            self.assertEqual(os.environ[BUDGET_DIRECTORY], str(self.root))
        data = json.loads((self.root / 'logs/summary.json').read_text())
        self.assertFalse(data['metadata']['coordinated'])
        self.assertEqual(data['metadata']['run_id'], lease['session'])
        self.assertFalse(self.coordinator.sessions[lease['session']]['waiting'])
        self.assertIn(mock.call('observer_state', waiting=True), remote.request.call_args_list)
        self.assertEqual(len(list(self.root.glob('*.json'))), 1)
        self.assertEqual(len(list((self.root / 'logs/local-budget').glob('*.json'))), 1)

    def test_invalid_or_mixed_observation_context_fails_before_process_or_clock(self):
        connection = contract.Connection(self.root, 'unittest', Path(sys.executable), ())
        good = {'endpoint': ['127.0.0.1', 9], 'session': 'child', 'secret': 'secret'}
        cases = [None, {}, {**good, 'endpoint': ['example.com', 9]},
                 {**good, 'endpoint': ['127.0.0.1', True]}, {**good, 'session': ''}]
        for context in cases:
            with self.subTest(context=context), \
                    mock.patch.dict(os.environ, {'LOOP_MANAGER_VERIFY_OBSERVER': json.dumps(context)}, clear=True), \
                    mock.patch.object(runner, 'load', return_value=connection), \
                    mock.patch.object(runner, 'ExecutionClock') as clock:
                with self.assertRaises(contract.ContractError):
                    runner.run(self.root / 'denied', 1)
                clock.assert_not_called()
        with mock.patch.dict(os.environ, {'LOOP_MANAGER_VERIFY_OBSERVER': json.dumps(good),
                'LOOP_MANAGER_VERIFY_ENDPOINT': '[]'}, clear=True), \
                mock.patch.object(runner, 'load', return_value=connection):
            with self.assertRaisesRegex(contract.ContractError, 'cannot be combined'):
                runner.run(self.root / 'denied', 1)


class IntegrationResultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_fixed_example_preflight_rejects_replaced_launcher_and_compile(self):
        repository = Path(__file__).resolve().parents[1]
        source = self.root / 'source/tests/support/verify_examples/python'
        integration.shutil.copytree(repository / 'tests/support/verify_examples/python', source)
        declaration = source / 'scripts/verify.json'
        original = integration._json(declaration)
        (source / 'tool').write_text('untrusted interpreter')
        for change in ('launcher', 'compile'):
            with self.subTest(change=change):
                data = json.loads(json.dumps(original))
                if change == 'launcher':
                    data['environment']['python'] = 'tool'
                else:
                    data['stages'][0]['argv'] = ['untrusted', 'command']
                atomic_json(declaration, data)
                port = mock.Mock()
                with self.assertRaisesRegex(contract.ContractError, 'fixed example declaration'):
                    integration.language_case(self.root / 'source', self.root / change,
                                              port, 'python', False, False, 'head')
                port.run.assert_not_called()

    def example(self, language='python', coordinated=False, failing=False):
        root = self.root / f'{language}-{coordinated}-{failing}'
        root.mkdir(exist_ok=True)
        (root / 'stage-1').mkdir(exist_ok=True)
        run = 'child'
        budget = {'wall_seconds': 32, 'execution_seconds': 2, 'waiting_seconds': 30, 'timeout': 10, 'elapsed': 2}
        expected = {f'test_example.ExampleTests.test_{name}': result for name, result in
                    (('expected_failure', 'expected_failure'), ('optional', 'skip'), ('sum', 'failure' if failing else 'success'))}
        module = manifest.Module('test_example', tuple(expected),
            tuple((identity, 'test_example', 'test_example.ExampleTests') for identity in expected), 'test*.py') if language == 'python' else manifest.Module('tests/example.t', ('tests/example.t',), pattern='*.t')
        compile_argv = ([str(Path(sys.executable).absolute()), str(Path(integration.__file__).with_name('entrypoint.py').resolve()), '--compile', 'tests/test_example.py']
            if language == 'python' else [str(self.root / 'perl'), '-I' + str(integration.perl_adapter.REPORTER.parent),
                                         '-MLoopManagerVerifyGuard', '-c', 'tests/example.t'])
        trace = []
        if coordinated:
            for index, unit in enumerate(('compile', 'tests:collect', module.name)):
                token = str(index)
                trace.append({'operation': 'acquire', 'token': token, 'run_id': run, 'unit_id': unit})
                for operation in (('bind', 'idle', 'release') if unit == module.name else ('bind', 'release')):
                    trace.append({'operation': operation, 'token': token, 'pid': 200 + index, 'identity': 77 + index})
        values = {
            'coordination.json': {'version': 1, 'session': run, 'pid': 100, 'identity': 7, 'observer_only': not coordinated,
                                   'stopped': True, 'reservations': {}, 'events': trace},
            'runner-start.json': {'version': 1, 'session': run, 'pid': 100, 'identity': 7, 'state': 'bound'},
            'process.json': {'version': 1, 'session': run, 'passed': not failing, 'timed_out': False,
                             'exit_code': int(failing), 'budget': budget},
            'manifest.json': {'version': 1, 'run_id': run, 'adapter': 'unittest' if language == 'python' else 'perl-tap',
                'adapter_version': 1, 'stages': [{'id': 'compile', 'kind': 'command', 'argv': compile_argv},
                    {'id': 'tests', 'kind': 'tests', 'units': [{'id': module.name, 'ids': list(module.ids), 'payload': asdict(module)}]}]},
            'summary.json': {'metadata': {'run_id': run, 'revision': 'head', 'mode': 'full', 'coordinated': coordinated},
                'result': {'run_id': run, 'passed': not failing, 'budget': budget}, 'active': [], 'fixture_results': [],
                'tests': [{'run_id': run, 'id': identity, 'result': outcome} for identity, outcome in expected.items()]},
            'stage-1/module-0.json': {'version': 1, 'run_id': run, 'stage_id': 'tests', 'unit_id': module.name,
                                     'module': module.name, 'passed': not failing, 'completed': list(module.ids)},
        }
        if language == 'perl':
            native_identity = {'version': 1, 'run_id': run, 'stage_id': 'tests', 'unit_id': module.name}
            native = [{**native_identity, 'type': 'test', 'number': index + 1, 'ok': not failing if index == 0 else True,
                'actual_ok': not failing if index == 0 else index != 2, 'skip': index == 1, 'todo': index == 2} for index in range(4)]
            native.append({**native_identity, 'type': 'summary', 'planned': 4, 'run': 4, 'good_plan': True,
                           'problems': failing, 'errors': [], 'exit': int(failing), 'wait': 0, 'skip_all': None})
            values['stage-1/module-0.json'].update(native=native, exit_code=0)
        for path, value in values.items():
            atomic_json(root / path, value)
        (root / 'runner.jsonl').write_text(''.join(json.dumps({'kind': 'unit_result', 'kind_of_unit': 'command',
            'unit_id': unit, 'run_id': run, 'passed': True, 'timed_out': False, 'exit_code': 0}) + '\n'
            for unit in ('compile', 'tests:collect')), encoding='utf-8')
        return root

    def test_python_and_perl_normal_and_native_failure_require_complete_proof(self):
        for language in ('python', 'perl'):
            for coordinated in (False, True):
                for failing in (False, True):
                    with self.subTest(language=language, coordinated=coordinated, failing=failing):
                        root = self.example(language, coordinated, failing)
                        evidence = integration.validate_example(root, language, coordinated, failing, 'head')
                        self.assertEqual(evidence['run_id'], 'child')

    def test_native_failure_cannot_be_fixture_timeout_partial_or_missing_result(self):
        for change in ('fixture', 'timeout', 'partial', 'revision', 'missing', 'duplicate', 'budget', 'compile', 'identity'):
            with self.subTest(change=change):
                root = self.example(failing=True)
                name = 'summary.json'
                data = integration._json(root / name)
                if change == 'fixture':
                    data['tests'][-1]['result'] = 'fixture_error'
                    data['fixture_results'] = [{'result': 'error', 'scope': 'setUp'}]
                elif change == 'timeout':
                    name = 'process.json'; data = integration._json(root / name); data['timed_out'] = True
                elif change == 'partial':
                    data['metadata']['mode'] = 'partial'
                elif change == 'revision':
                    data['metadata']['revision'] = 'other'
                elif change == 'missing':
                    data['tests'].pop()
                elif change == 'duplicate':
                    data['tests'][-1] = data['tests'][0]
                elif change == 'budget':
                    data['result']['budget']['waiting_seconds'] = 29
                elif change == 'compile':
                    name = 'manifest.json'; data = integration._json(root / name); data['stages'][0]['argv'][-1] = 'other.py'
                else:
                    name = 'runner-start.json'; data = integration._json(root / name); data['identity'] = 99
                atomic_json(root / name, data)
                with self.assertRaises(contract.ContractError):
                    integration.validate_example(root, 'python', False, True, 'head')

    def test_coordinated_flag_alone_cannot_replace_acquire_bind_and_release(self):
        for operation in ('acquire', 'bind', 'release'):
            with self.subTest(operation=operation):
                root = self.example(coordinated=True)
                proof = integration._json(root / 'coordination.json')
                proof['events'] = [event for event in proof['events'] if event['operation'] != operation]
                atomic_json(root / 'coordination.json', proof)
                with self.assertRaises(contract.ContractError):
                    integration.validate_example(root, 'python', True, False, 'head')

    def test_partial_fixture_keeps_self_assertions_and_uses_no_actual_git_in_unit(self):
        port = mock.Mock()
        port.budget.remaining.return_value = 10
        def inspect(project, *_args, **_options):
            for path in (project / 'tests').glob('*.py'):
                names = {node.id for node in ast.walk(ast.parse(path.read_text())) if isinstance(node, ast.Name)}
                self.assertNotIn('check', names)
                self.assertIn('self', names)
            raise RuntimeError('stop before native runner')
        port.run.side_effect = inspect
        with mock.patch.object(integration.subprocess, 'run', return_value=SimpleNamespace(stdout='head')):
            with self.assertRaisesRegex(RuntimeError, 'stop before native runner'):
                integration.partial_case(port, self.root)

    def test_parent_summary_keeps_child_native_failures_in_their_own_run(self):
        child = self.root / 'stage-2/child'
        child.mkdir(parents=True)
        with closing(EventWriter(self.root / 'runner.jsonl', identity={'run_id': 'parent'})) as parent:
            parent.emit('run_start', mode='full')
            parent.emit('test_end', id='own', result='success', duration=1)
            parent.emit('test_start', id='stale-worker-start')
            parent.emit('module_result', unit_id='own-module', passed=True)
            parent.emit('integration_result', result={'status': 'PASS'}, diagnostic='receipt')
            parent.emit('run_end', passed=True)
        with closing(EventWriter(child / 'runner.jsonl', identity={'run_id': 'child', 'parent_run_id': 'parent'})) as nested:
            nested.emit('run_start', mode='full')
            nested.emit('test_end', id='native', result='failure', duration=1)
            nested.emit('fixture_result', id='fixture', result='error')
            nested.emit('run_end', passed=False)
        result = summarize(self.root)
        self.assertEqual(result['metadata']['run_id'], 'parent')
        self.assertTrue(result['result']['passed'])
        self.assertEqual(result['counts'], {'success': 1})
        self.assertFalse(result['fixture_results'])
        self.assertFalse(result['active'])
        self.assertEqual(len(result['execution_runs']), 1)
        self.assertGreaterEqual(result['elapsed_seconds'], 0)
        self.assertEqual([item['unit_id'] for item in result['module_results']], ['own-module'])
        self.assertEqual(result['integrations'][0]['result']['status'], 'PASS')


class IntegrationStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'tests').mkdir()
        (self.root / 'scripts').mkdir()
        self.declaration = {'version': 1, 'adapter': 'unittest', 'environment': {'python': 'launcher'}, 'stages': [
            {'id': 'tests', 'kind': 'tests', 'cwd': '.', 'execution': 'serial',
             'collection': {'directory': 'tests', 'pattern': 'test*.py'}},
            {'id': 'compile', 'kind': 'command', 'cwd': '.', 'argv': ['{python}', 'compile.py']},
            {'id': 'integration', 'kind': 'runner-integration', 'cwd': '.', 'suite': 'loop-manager-v1'}]}

    def result(self, run_id='parent', stage_id='integration', revision='head'):
        cases = []
        for identity in integration.case_ids():
            if identity.startswith('perl-') and not sys.platform.startswith('linux'):
                cases.append({'id': identity, 'status': 'SKIP', 'reason': 'Perl native examples support Linux'})
            else:
                evidence = ({'partial_run': 'partial', 'full_run': 'full', 'fixture_revision': 'fixture'} if identity == 'partial-full'
                    else {'run_id': identity, 'revision': revision, 'adapter': 'perl-tap' if identity.startswith('perl-') else 'unittest'})
                cases.append({'id': identity, 'status': 'PASS', 'evidence': evidence})
        return {'version': 1, 'kind': 'runner_integration', 'run_id': run_id, 'stage_id': stage_id,
                'revision': revision, 'platform': sys.platform, 'status': 'PASS', 'cases': cases}

    def test_fixed_declaration_rejects_arbitrary_suite_cwd_fields_and_order(self):
        original = json.loads(json.dumps(self.declaration))
        for mutation in ('valid', 'suite', 'cwd', 'argv', 'order', 'duplicate'):
            with self.subTest(mutation=mutation):
                data = json.loads(json.dumps(original))
                stage = data['stages'][-1]
                if mutation == 'suite': stage['suite'] = 'arbitrary-plugin'
                elif mutation == 'cwd': stage['cwd'] = 'tests'
                elif mutation == 'argv': stage['argv'] = ['untrusted']
                elif mutation == 'order': data['stages'].reverse()
                elif mutation == 'duplicate': data['stages'].append(dict(stage, id='other'))
                atomic_json(self.root / 'scripts/verify.json', data)
                if mutation == 'valid':
                    self.assertEqual(contract.load(self.root, sys.executable).stages[-1]['suite'], 'loop-manager-v1')
                else:
                    with self.assertRaises(contract.ContractError): contract.load(self.root, sys.executable)

    def test_missing_duplicate_failed_partial_and_wrong_scope_receipts_never_pass(self):
        for change in ('missing', 'duplicate', 'failed', 'partial', 'revision', 'run', 'child', 'version'):
            with self.subTest(change=change):
                result = self.result()
                if change == 'missing': result['cases'].pop()
                elif change == 'duplicate': result['cases'][-1] = result['cases'][0]
                elif change == 'failed': result['cases'][0]['status'] = 'RUNNING'
                elif change == 'partial': result['kind'] = 'partial_verify'
                elif change == 'revision': result['revision'] = 'stale'
                elif change == 'run': result['run_id'] = 'stale'
                elif change == 'child': result['cases'][1]['evidence']['run_id'] = result['cases'][0]['evidence']['run_id']
                else: result['version'] = True
                with self.assertRaises(contract.ContractError): integration.validate_stage(result, 'parent', 'integration', 'head')
        self.assertTrue(integration.validate_stage(self.result(), 'parent', 'integration', 'head'))

    def test_required_orchestration_stops_at_first_failure_and_preserves_receipt(self):
        with mock.patch.object(integration, 'TrustedRunnerPort'), \
                mock.patch.object(integration, 'language_case', side_effect=RuntimeError('native infrastructure failure')) as language, \
                mock.patch.object(integration, 'partial_case') as partial:
            result = integration.execute(self.root, self.root, mock.Mock(), mock.Mock(), 10, 'parent', 'integration', 'head')
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual(len(result['cases']), 1)
        self.assertEqual(integration._json(self.root / 'integration-result.json'), result)
        language.assert_called_once()
        partial.assert_not_called()

    def test_full_and_partial_share_declaration_but_only_full_completes_integration(self):
        from loop_manager_internal.verification import impact
        atomic_json(self.root / 'scripts/verify.json', self.declaration)
        unit = manifest.Module('unit', ('a',))
        selection = impact.Selection('head', 'base', ('unit',), ('a',), {}, (), 1, 1,
                                     impact.digest([asdict(unit)]), 'configuration')
        def execute(root, directory, client, budget, timeout, run_id, stage_id, revision):
            result = self.result(run_id, stage_id, revision)
            atomic_json(directory / 'integration-result.json', result)
            return result
        for partial in (False, True):
            with self.subTest(partial=partial), \
                    mock.patch.dict(os.environ, {'LOOP_MANAGER_VERIFY_PARENT_RUN_ID': 'pipeline'}, clear=True), \
                    mock.patch.object(runner.Path, 'cwd', return_value=self.root), \
                    mock.patch.object(runner, 'os', SimpleNamespace(environ=os.environ, name='posix')), \
                    mock.patch.object(integration, '_require_orchestrator'), \
                    mock.patch.object(runner, 'install_guard') as guard, \
                    mock.patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(stdout='head')), \
                    mock.patch.object(unittest_adapter, 'collect_project', return_value=([unit], sys.version)), \
                    mock.patch.object(unittest_adapter, 'execute', return_value=True), \
                    mock.patch.object(runner, 'execute_command', return_value=True) as command, \
                    mock.patch.object(integration, 'execute', side_effect=execute) as stage, \
                    mock.patch.object(impact, 'select', return_value=selection), \
                    mock.patch.object(impact, 'assert_candidate'), mock.patch('sys.stdout'):
                logs = self.root / str(partial)
                self.assertEqual(runner.run(logs, 1, 10, partial_base='base' if partial else None), 0)
                self.assertEqual(stage.call_count, int(not partial))
                self.assertEqual(command.call_count, int(not partial))
                guard.assert_not_called()
                data = integration._json(logs / 'manifest.json')
                self.assertEqual([item['kind'] for item in data['stages']], ['tests'] if partial else ['tests', 'command', 'runner-integration'])
                summary = integration._json(logs / 'summary.json')
                self.assertEqual(len(summary['integrations']), int(not partial))
                self.assertEqual(summary['metadata']['parent_run_id'], 'pipeline')

    def test_native_windows_cannot_bypass_required_wsl_full_phase(self):
        atomic_json(self.root / 'scripts/verify.json', self.declaration)
        connection = contract.load(self.root, sys.executable)
        with mock.patch.object(runner, 'load', return_value=connection), \
                mock.patch.object(integration, '_require_orchestrator'), \
                mock.patch.object(runner, 'os', SimpleNamespace(name='nt')), \
                mock.patch.object(runner, 'ExecutionClock') as clock:
            with self.assertRaisesRegex(contract.ContractError, 'requires WSL'):
                runner.run(self.root / 'not-started', 1)
            clock.assert_not_called()
