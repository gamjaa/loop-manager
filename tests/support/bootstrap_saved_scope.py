from tests.support.git import isolated_git_test
from tests.support.fixed_samples import use_fixed_guidance
import contextlib
import json
import sys
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

import loop_manager
from loop_manager_internal.agents.agent_invocation import InvocationContext


from tests.support.origin import git, configure_identity, commit_all
from tests.support.fixed_samples import PROJECT_FILES
from tests.support.workspace import pin_common_guidance
from tests.support.profiles import use_empty_provider_home




def _collect_bootstrap_costs(state, records):
    """Forward complete owned events once before the fixture directory disappears."""
    from loop_manager_internal.verification.timing import _current_writer

    writer = _current_writer.get()
    complete = True
    try:
        paths = sorted((state / 'bootstrap-costs').glob('*.jsonl'))
        if not paths:
            complete = False
        for path in paths:
            try:
                with path.open('rb') as stream:
                    for line in stream:
                        if not line.endswith(b'\n'):
                            complete = False
                            break
                        try:
                            text = line.decode('utf-8')
                            event = json.loads(text)
                            if (not isinstance(event, dict) or not isinstance(event.get('pid'), int)
                                    or not isinstance(event.get('at'), (int, float))
                                    or event.get('kind') not in {'phase_start', 'phase_end', 'git_cost', 'git_cost_incomplete'}
                                    or (event['kind'].startswith('phase_') and not isinstance(event.get('name'), str))
                                    or (event['kind'] == 'git_cost' and not all(
                                        isinstance(event.get(key), (int, float)) for key in ('count', 'seconds')))):
                                raise ValueError('invalid event')
                        except (UnicodeError, ValueError):
                            complete = False
                            continue
                        records.append(event)
                        if writer is not None:
                            writer.stream.write(text)
            except (OSError, UnicodeError, ValueError):
                complete = False
        if writer is not None:
            writer.stream.flush()
    except (OSError, UnicodeError, ValueError):
        complete = False
    if not complete:
        print('bootstrap cost collection incomplete', file=sys.stderr)
    return complete


def _track_bootstrap_costs(cleanup, state):
    records, collected = [], []
    def collect():
        if not collected:
            collected.append(_collect_bootstrap_costs(state, records))
        return collected[0]
    cleanup.callback(collect)
    return records, collect


@isolated_git_test
class BootstrapSavedScopeFixture(unittest.TestCase):
    def setUp(self):
        use_fixed_guidance(self)
        use_empty_provider_home(self)
        super().setUp()


    def _run_child(self, source, manager_file, state, config):
        import sys
        from loop_manager_internal.execution.verify_process import run_verify_process
        # Native Windows file observations exceed 90s under parallel verify.
        timeout = 180 if sys.platform == 'win32' else 90
        command = [sys.executable, '-B', str(manager_file), '--state-dir', str(state),
                   '--config', str(config), 'run']
        _, timed_out, output = run_verify_process(command, source, timeout, job_scope=True)
        self.assertFalse(timed_out, output)
        return subprocess.CompletedProcess(command, output.exit_code, output)


    def _assert_saved_scope_resume(self, advance_after_selection=False):
        import json
        import shutil
        import sys
        from tests.support.git import make_remote_repository, copy_manager_import_fixture
        from tests.support.catalog import write_execplan
        from tests.support.profiles import profile_config
        from tests.support.worker import worker_result
        from tests.support.runtime import record_saved_scope_invocation
        from loop_manager_internal.execution.worker_session import SessionBinding
        from loop_manager_internal.planning.planning_workspace import planning_workspace_path

        # 공통 bootstrap의 기본·비기본 브랜치와 역할별 저장 상태를 각각 연결한다.
        for base, roles in (('main', ('worker', 'reviewer')), ('develop', ('planner',))):
            for role in roles:
                with self.subTest(base=base, role=role), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as cleanup:
                    source = make_remote_repository(directory)
                    git(source, 'config', 'core.longpaths', 'true')
                    state = Path(directory) / 'state'
                    state.mkdir()
                    records, collect_costs = _track_bootstrap_costs(cleanup, state)
                    copy_manager_import_fixture(source)
                    # Keep bootstrap, identity, SQLite, scope and dispatch real.
                    # Optional source-fact prompt enrichment has no assertion
                    # here; its Git/source parsing is covered by code-map tests.
                    manager_file = source / 'loop_manager.py'
                    manager_file.write_text(manager_file.read_text().replace(
                        'if __name__ == "__main__":\n    raise SystemExit(main())',
                        '''if __name__ == "__main__":
    def fake_agent(worktree, state_dir, config, role, prompt, **options):
        proof = state_dir / 'resumed-agent.json'
        proof.write_text(json.dumps({'role': role, 'session': options.get('session_id'),
                                    'runtime': str(ROOT),
                                    'sha': git(ROOT, 'rev-parse', 'HEAD').stdout.strip(),
                                    'worktree': str(worktree)}))
        # Scope restoration is the asserted effect; quota policy is tested separately.
        from loop_manager_internal.runtime.refresh import RuntimePaused
        raise RuntimePaused()
    if '--runtime-child' not in sys.argv:
        stop_file = Path(sys.argv[sys.argv.index('--state-dir') + 1]) / 'stop-after-registration'
        if stop_file.exists():
            from loop_manager_internal.git import manager_git_journal
            finish = manager_git_journal._finish
            def interrupted_finish(slot, active, intent, *args, **kwargs):
                if intent['kind'] == 'runtime_create':
                    stop_file.unlink()
                    raise RuntimeError('test interruption after runtime registration')
                return finish(slot, active, intent, *args, **kwargs)
            manager_git_journal._finish = interrupted_finish
    github_repository = lambda value: 'owner/repo'
    from loop_manager_internal.git import git_adapter as _git_adapter
    _git_adapter.github_repository = github_repository
    validate_runtime_prerequisites = lambda *args, **kwargs: None
    pr_for_branch = lambda *args, **kwargs: None
    codex_model_efforts = lambda *args: {model: frozenset({'medium', 'high', 'max', 'xhigh'})
        for model in ('test-fast', 'test-balanced', 'test-strong')}
    run_codex = fake_agent
    from loop_manager_internal.maintenance import tiered_ports
    tiered_ports.prepare_query_context = lambda *args, **kwargs: ''
    if '--runtime-child' in sys.argv:
        from loop_manager_internal.runtime.bootstrap import BootstrapService
        validate_mutation = BootstrapService.validate_runtime_mutation
        def advance_remote_then_validate(service, source, state_dir, *args, **kwargs):
            advance = state_dir / 'advance-after-selection.json'
            if advance.exists():
                writer, base = json.loads(advance.read_text())
                subprocess.run(['git', '-C', writer, 'push', 'origin', 'HEAD:' + base], check=True)
                subprocess.run(['git', '-C', str(source), 'fetch', 'origin', base], check=True)
                advance.unlink()
            return validate_mutation(service, source, state_dir, *args, **kwargs)
        BootstrapService.validate_runtime_mutation = advance_remote_then_validate
    from loop_manager_internal.verification.timing import EventWriter, phase
    from loop_manager_internal.verification.git_timing import git_command_timing
    costs = Path(sys.argv[sys.argv.index('--state-dir') + 1]) / 'bootstrap-costs'
    costs.mkdir(exist_ok=True)
    cost_writer = EventWriter(costs / (str(os.getpid()) + '.jsonl'))
    cost_writer.current_test = BOOTSTRAP_TEST_LABEL
    try:
        with cost_writer.activate(), phase('bootstrap:runtime_child_cli' if '--runtime-child' in sys.argv
                                          else 'bootstrap:parent_cli'), git_command_timing():
            code = main()
    finally:
        cost_writer.close()
    raise SystemExit(code)''').replace('BOOTSTRAP_TEST_LABEL', repr(self.id())), encoding='utf-8')
                    for name in ('PLANS.md', 'AGENTS.common.md', 'AGENTS.planner.md',
                                 'AGENTS.planner_check.md', 'AGENTS.worker.md', 'AGENTS.reviewer.md',
                                 'AGENTS.ponytail.md'):
                        shutil.copy(PROJECT_FILES / 'docs' / name, source / 'docs' / name)
                    (source / 'scripts').mkdir(exist_ok=True)
                    (source / 'scripts/verify.sh').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
                    (source / 'scripts/verify.ps1').write_text('exit 0\n', encoding='utf-8')
                    (source / 'docs/plans').mkdir()
                    plan_file = write_execplan(source, 'G-001', 'Resume saved scope')
                    plan_path = plan_file.relative_to(source).as_posix()
                    if base != 'main':
                        git(source, 'checkout', '-b', base)
                    sha_a = commit_all(source, 'Manager fixture at A')
                    git(source, 'push', 'origin', f'HEAD:{base}')
                    git(source, 'fetch', 'origin')
                    if base == 'develop':
                        # Keep main at another SHA: it cannot serve as the runtime base.
                        git(source, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/develop')

                    connection = loop_manager.connect_database(state, create=True)
                    cleanup.callback(connection.close)
                    with connection:
                        connection.execute('INSERT INTO projects (repo, base_branch, origin) VALUES (?, ?, ?)',
                                           (str(source), base, 'owner/repo'))
                    fingerprint = loop_manager.planning_input_fingerprint(source, base)
                    if role == 'planner':
                        workspace = planning_workspace_path(state, str(source), fingerprint)
                        workspace.parent.mkdir(parents=True, exist_ok=True)
                        git(source, 'worktree', 'add', '--detach', str(workspace), sha_a)
                        branch = None
                        with connection:
                            connection.execute("UPDATE projects SET phase='PLANNING', worktree=?, "
                                "plan_sha=?, active_sha=?, planning_attempt_fingerprint=?, planning_workspace_owned=1, "
                                "planner_session_id='planner-existing' WHERE repo=?",
                                (str(workspace), sha_a, sha_a, fingerprint, str(source)))
                    else:
                        loop_manager.register_execplans(connection, str(source), source,
                            [(plan_path, loop_manager._parse_execplan(source, plan_file))], sha_a, fingerprint)
                        from loop_manager_internal.storage import planning_input_state
                        with connection:
                            planning_input_state.record_general(connection, str(source), sha_a,
                                loop_manager.plan_declarations(source, sha_a))
                        branch, workspace = loop_manager.expected_execplan_workspace(state, str(source), 'G-001')
                        loop_manager.make_worktree(source, workspace, branch, base)
                        with connection:
                            connection.execute("UPDATE exec_plans SET status='IMPLEMENTING', active_sha=?, "
                                "branch=?, worktree=?, workspace_owned=1, worker_session_id='worker-existing', "
                                "reviewer_session_id='reviewer-existing', worker_session_binding=? "
                                "WHERE repo=? AND plan_id='G-001'", (sha_a, branch, str(workspace),
                                    SessionBinding('codex', 'test-fast', 'medium').serialize(), str(source)))
                    if role != 'planner':
                        with connection:
                            connection.execute("UPDATE projects SET phase='WORKING', worktree=?, branch=?, "
                                "plan_path=?, plan_sha=?, active_sha=?, worker_session_id='worker-existing', "
                                "reviewer_session_id='reviewer-existing', worker_session_binding=? WHERE repo=?",
                                (str(workspace), branch, plan_path, sha_a, sha_a,
                                 SessionBinding('codex', 'test-fast', 'medium').serialize(), str(source)))
                    if role == 'reviewer':
                        (workspace / 'src/app.py').write_text('VALUE = 2\n', encoding='utf-8')
                        commit_all(workspace, 'Saved Worker candidate awaiting Reviewer')
                    before = loop_manager.begin_agent_scope(connection, str(source), workspace, role)
                    if role == 'worker':
                        (workspace / 'src/app.py').write_text('VALUE = 2\n', encoding='utf-8')
                    saved_result = replace(loop_manager.capture_git_snapshot(workspace),
                        scope_id=before.scope_id, scope_start_digest=before.scope_start_digest)
                    record_saved_scope_invocation(connection, source, state, workspace, role, before,
                        role + '-existing', plan_id='G-001' if role != 'planner' else None)
                    with connection:
                        connection.execute('UPDATE projects SET agent_role=?, agent_snapshot=?, '
                            'agent_result_snapshot=? WHERE repo=?',
                            (role, before.serialize(), saved_result.serialize(), str(source)))
                        if role != 'planner':
                            connection.execute('UPDATE exec_plans SET agent_role=?, agent_snapshot=?, '
                                'agent_result_snapshot=? WHERE repo=? AND plan_id=?',
                                (role, before.serialize(), saved_result.serialize(), str(source), 'G-001'))
                    context = InvocationContext(str(source), 'G-001' if role != 'planner' else None,
                                                task_id='planning:' + fingerprint if role == 'planner' else None)
                    sessions = (('planner-existing',) if role == 'planner'
                                else ('worker-existing', 'reviewer-existing'))
                    pin_common_guidance(connection, context, workspace, state,
                                        sessions=sessions, role=role)
                    if role == 'reviewer':
                        with connection:
                            connection.execute("UPDATE exec_plans SET status='REVIEWING', "
                                "worker_result=?, verify_result=? WHERE repo=? AND plan_id='G-001'",
                                (worker_result(),
                                 'historical fixture verification', str(source)))
                            connection.execute("UPDATE projects SET phase='REVIEWING', worker_result=?, "
                                "verify_result=? WHERE repo=?",
                                (worker_result(),
                                 'historical fixture verification', str(source)))
                        # 완료한 Worker의 실제 definition token을 남겨 bootstrap에서 구현을 재실행하지 않는다.
                        from tests.support.publication import seed_validated_candidate
                        seed_validated_candidate(connection, source, workspace, 'G-001')
                    connection.commit()
                    connection.close()

                    writer = Path(directory) / 'writer'
                    git(source, 'clone', '-c', 'core.longpaths=true', '-c', 'core.autocrlf=false',
                        str(Path(directory) / 'remote.git'), str(writer))
                    configure_identity(writer)
                    git(writer, 'checkout', base)
                    # B changes executable-independent input, retaining the saved plan definition.
                    (writer / 'README.md').write_text('Remote B\n', encoding='utf-8')
                    sha_b = commit_all(writer, 'Advance to B')
                    git(writer, 'push', 'origin', f'HEAD:{base}')
                    if advance_after_selection:
                        (writer / 'README.md').write_text('External C\n', encoding='utf-8')
                        sha_c = commit_all(writer, 'External base C after runtime B selection')
                        (state / 'advance-after-selection.json').write_text(json.dumps([str(writer), base]), encoding='utf-8')
                    config = profile_config(self)
                    # Each role still exercises the real child. The
                    # common crash and same-SHA reuse need one process regression;
                    # registration/scope rules are paired with BootstrapRuntimeTests
                    # and BootstrapSavedScopeFixtureUnitTests below. B/C transitions
                    # retain both resumes for each role's distinct saved state.
                    # ponytail: crash/reuse process coverage is one main Worker;
                    # add a role/base case if those pre-dispatch steps gain such logic.
                    interrupted_registration = not advance_after_selection and (base, role) == ('main', 'worker')
                    if interrupted_registration:
                        (state / 'stop-after-registration').touch()
                        interrupted = self._run_child(source, manager_file, state, config)
                        self.assertNotEqual(interrupted.returncode, 0, interrupted.stdout)
                        self.assertIn('test interruption after runtime registration', interrupted.stdout)
                        self.assertFalse((state / 'resumed-agent.json').exists())
                        self.assertTrue(list((state / 'manager-git-journal').glob('*/active.json')))
                    attempts = 2 if advance_after_selection or interrupted_registration else 1
                    for attempt in range(attempts):
                        completed = self._run_child(source, manager_file, state, config)
                        proof_path = state / 'resumed-agent.json'
                        self.assertTrue(proof_path.exists(), completed.stdout)
                        proof = json.loads(proof_path.read_text())
                        expected_role = role
                        self.assertEqual(proof['role'], expected_role)
                        self.assertEqual(proof['session'], expected_role + '-existing')
                        self.assertEqual(proof['sha'], sha_c if advance_after_selection and attempt else sha_b)
                        self.assertEqual(proof['worktree'], str(workspace))
                        self.assertIn('runtime mutation 검증', completed.stdout)
                        if attempt == 0:
                            self.assertNotIn('Agent 범위 선검증 실패', completed.stdout)
                        if role != 'planner':
                            self.assertEqual((workspace / 'src/app.py').read_text(), 'VALUE = 2\n')
                        with contextlib.closing(sqlite3.connect(state / 'loop-manager.db')) as check, check:
                            self.assertEqual(check.execute('SELECT COUNT(*) FROM project_blocks').fetchone()[0], 0, completed.stdout)
                            if role != 'planner':
                                row = check.execute("SELECT status, worker_session_id, reviewer_session_id, retry_count "
                                    "FROM exec_plans WHERE plan_id='G-001'").fetchone()
                                self.assertEqual(row, ('REVIEWING' if role == 'reviewer' else 'IMPLEMENTING',
                                                       'worker-existing', 'reviewer-existing', 0))
                        # A later parent must dispatch again, using the same SHA
                        # in the crash/reuse case or C after the B/C transition.
                        proof_path.unlink()
                    # Preserve the normal phase and Git-cost checks when collection is complete.
                    if collect_costs():
                        ends = [event for event in records if event['kind'] == 'phase_end']
                        self.assertEqual(sum(event['name'] == 'bootstrap:parent_cli' for event in ends),
                                         attempts + int(interrupted_registration))
                        self.assertEqual(sum(event['name'] == 'bootstrap:runtime_child_cli' for event in ends), attempts)
                        self.assertTrue(any(event['kind'] == 'git_cost' and event['count'] > 0 for event in records))
