from tests.support.lifecycle import fixture_cleanup
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from loop_manager_internal.execution import execution_handlers as handlers
from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
import loop_manager as lm
from loop_manager_internal.storage import runtime_state
from loop_manager_internal.execution.execution_state import ExecutionStatePort
from tests.support.database import runtime_connection
from loop_manager_internal.execution.worker_session import SessionBinding
from loop_manager_internal.agents.common_guidance import plan_content_hash
import json


READY_WORKER_RESULT = 'WORKER_OUTCOME: {"kind":"ready_for_validation"}\nresult'
VALID_WORKER_RESULT = 'WORKER_OUTCOME: {"kind":"ready_for_validation"}\nvalid result'


def recovered_rebase_workspace(workspace, *, connection, repo_path, state_dir,
                               repo, plan_id, worktree):
    """Share the Manager workspace bindings between real Git and unit fixtures."""
    git_port = mock.Mock(wraps=lm.git)
    return replace(workspace, git=git_port,
        live_remote_branch_oid=mock.Mock(side_effect=lambda repo, branch:
            handlers.manager_git_journal.live_remote_branch_oid(git_port, repo, branch)),
        _rebase_in_progress_for_branch=lm._rebase_in_progress_for_branch,
        git_operation_entries=lm._git_operation_entries,
        current_rebase_service=lambda context, **options: lm._current_rebase_service(
            connection, repo_path, state_dir, repo, plan_id,
            worktree, context.branch, **options))

def recovered_rebase_publication(publication, state):
    """Use the Manager's snapshot-bound mutating publication adapters."""
    names = ('record_remote_publication_branch', 'push_managed_publication_branch',
             'push_owned_pr_conflict_recovery')
    return replace(publication, **{name: mock.Mock(side_effect=state.bind(
        getattr(lm, name), connected=True, mutates=True, name=name)) for name in names})


def effect_bundle(cls, partial=None, *, optional_missing=False):
    """Supply external-operation fakes, without performing phase transitions."""
    return cls(**{field.name: getattr(partial, field.name,
                     None if optional_missing and field.name in ('current_rebase_service',
                                           'live_remote_branch_oid', 'git_operation_entries')
                     else mock.Mock(name=field.name, return_value=None))
                  for field in fields(cls)})


def complete_execution_effects(partial=None):
    """Complete small evidence-test fakes at the composition boundary."""
    bundles = {
        'workspace': handlers.WorkspaceEffects, 'agent': handlers.AgentEffects,
        'validation': handlers.ValidationEffects, 'archive': handlers.ArchiveEffects,
        'publication': handlers.PublicationEffects, 'blocking': handlers.BlockingEffects,
        'events': handlers.EventsEffects, 'revision': handlers.RevisionEffects,
    }
    return handlers.ExecutionEffects(
        state=getattr(partial, 'state', mock.Mock()),
        contract=handlers.ExecutionContract.from_source(getattr(partial, 'contract', None)),
        checkpoint=getattr(partial, 'checkpoint', None),
        feedback=getattr(partial, 'feedback', None),
        **{name: effect_bundle(cls, getattr(partial, name, None), optional_missing=True)
           for name, cls in bundles.items()},
    )


def execution_handler_stub():
    """Compose isolated ownership tests without allocating a database or Git repo."""
    handler = object.__new__(handlers.ExecutionHandlers)
    handler.effects = complete_execution_effects()
    handler.recovery = mock.Mock()
    handler.rebase_corrections = mock.Mock()
    return handler


class ExecutionFixture:
    """Reusable setup and adapters; no discoverable test methods."""

    def setUp(self):
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.repository = runtime_state.RuntimeStateRepository(self.connection)
        self.repository.activate('repo', 'G-047', activation_sha='base',
                                 branch='loop-manager/G-047', worktree=str(Path('/state/G-047')))
        self.state = ExecutionStatePort(self.connection, self.repository, 'repo')
        # Existing saved-session fixtures represent sessions started by this
        # configured provider; explicit None still exercises legacy recovery.
        original_update = self.state.update_project
        def update_with_binding(repo, **values):
            if values.get('worker_session_id') and 'worker_session_binding' not in values:
                values['worker_session_binding'] = SessionBinding(
                    'codex', 'model', 'high',
                ).serialize()
            return original_update(repo, **values)
        self.state.update_project = update_with_binding
        self.plan = {'id': 'G-047', 'title': 'Runtime', 'sources': ['docs/PRODUCT.md']}
        self.calls = []
        self.remote = 'base'
        self.dirty = True

        self.effects = handlers.ExecutionEffects(
            state=self.state, contract=handlers.ExecutionContract(
                reviewer_targeted_test_reason=lm.reviewer_targeted_test_reason),
            workspace=effect_bundle(handlers.WorkspaceEffects),
            agent=effect_bundle(handlers.AgentEffects),
            validation=effect_bundle(handlers.ValidationEffects),
            archive=effect_bundle(handlers.ArchiveEffects),
            blocking=effect_bundle(handlers.BlockingEffects),
            publication=effect_bundle(handlers.PublicationEffects),
            events=effect_bundle(handlers.EventsEffects),
            revision=effect_bundle(handlers.RevisionEffects),
        )
        self.effects = replace(self.effects, workspace=replace(self.effects.workspace, current_rebase_service=None))
        self.effects = replace(self.effects, workspace=replace(self.effects.workspace,
            live_remote_branch_oid=self.effects.workspace.remote_branch_oid))
        self.effects.workspace.is_directory.return_value = False
        self.effects.workspace.branch_has_changes.return_value = True
        self.effects.workspace.git.side_effect = self.git
        self.effects.agent.validate_worker_workspace_scope.return_value = self.plan
        self.effects.agent.capture_git_snapshot.return_value = SimpleNamespace(
            head='owned-head', branch='loop-manager/G-047',
            dirty={'app.py': (('staged',), 'accepted-content')},
        )
        self.effects.validation.validate_active_execplan_metadata.return_value = self.plan
        self.effects.validation.parse_worker_result.return_value = SimpleNamespace(
            commit_title='Concrete change', test_evidence='targeted test: PASS',
            summary='Implement the selected product change.',
        )
        self.effects.validation.verify.side_effect = lambda _: (self.calls.append('verify') or True, 'OK')
        self.effects.validation.parse_reviewer_result.return_value = ('PASS', ())
        self.effects.validation.ensure_worker_level_state.return_value = 1
        self.effects.agent.run_agent_with_retries.side_effect = lambda call, session_id: call(session_id, False)
        self.effects.agent.run_agent.side_effect = self.agent
        self.effects.agent.resolve_worker_profile.return_value = lm.ResolvedProfile(
            'model', 'high',
        )
        self.effects.publication.published_execplan.return_value = self.plan
        def push(*args):
            self.calls.append('push')
            self.effects.workspace.remote_branch_oid.return_value = self.remote
            return self.remote
        self.effects.publication.push_managed_publication_branch.side_effect = push
        self.effects.publication.command.side_effect = self.publication_command
        self.effects.revision.reconcile_catalog.side_effect = lambda *_: SimpleNamespace(
            kind=SimpleNamespace(value='reconciled'), revision_sha=self.remote
        )
        self.effects.revision.body_hash.return_value = 'body'
        self.effects.revision.source_fingerprint.return_value = 'source'
        self.effects.revision.changed_paths.return_value = ()
        self.effects.revision.unmet_dependencies.return_value = ()
        self.effects.archive.requirement_archive_sources.return_value = ()
        self.handler = handlers.ExecutionHandlers(
            self.effects, repo='repo', repo_path=Path('/repo'), state_dir=Path('/state'),
            base='main', base_sha='base', provider='codex', config_path=Path('/config'),
            newly_activated=False,
        )
        self.handler.plan = self.plan
        self.handler.catalog_plan = self.repository.active_row('repo')
        # This class exercises phase policy with fake Git effects. Journal
        # persistence and ownership are covered by test_managed_mutation_evidence;
        # keep these legacy policy tests at the Git operation boundary.
        journal_start = mock.patch.object(
            handlers.manager_git_journal, 'begin_rebase',
            return_value='test-rebase-token',
        )
        self.journal_start = journal_start
        journal_start.start()
        self.addCleanup(journal_start.stop)
        self.handler._rebase_journal_state = mock.Mock(return_value={})
        self.handler._begin_rebase_continuation = mock.Mock(
            return_value='test-rebase-token',
        )
        self.handler._finish_rebase_journal = mock.Mock(return_value=True)
        def fake_complete_rebase(context, *, project_values=None):
            checkpoint = self.handler.effects.checkpoint
            if checkpoint is not None:
                current = checkpoint.get('repo', context.plan.plan_id)
                if current.operation == 'rebase':
                    checkpoint.record(
                        'repo', context.plan.plan_id,
                        expected_generation=current.generation, operation=None,
                        expected_head=self.remote, target_base_oid=current.target_base_oid,
                        observed_remote_oid=current.observed_remote_oid,
                        snapshot=current.snapshot,
                    )
            from loop_manager_internal.git.managed_rebase_adoption import revalidation_values
            target = (project_values or {}).get('active_sha') or self.remote
            self.state.update_project('repo', **revalidation_values(target))
        self.handler._complete_rebase_intent = mock.Mock(side_effect=fake_complete_rebase)
        def fake_current_service(context, **options):
            from loop_manager_internal.git.managed_rebase_validation import RebaseDecision, RebaseStatus, Remote
            current = self.handler.effects.checkpoint.get('repo', context.plan.plan_id)
            in_progress = bool(self.effects.workspace._rebase_in_progress_for_branch(
                context.worktree, context.branch))
            stages = self.handler._unmerged_stages(context.worktree)
            remote = Remote('repo', context.branch, options.get('remote_oid', current.observed_remote_oid))
            observed = SimpleNamespace(remote=remote, head=self.remote,
                metadata=(('onto', current.target_base_oid),) if in_progress else None,
                index=(SimpleNamespace(stage=1),) if stages else ())
            decision = RebaseDecision(RebaseStatus.WAITING if in_progress or stages else RebaseStatus.COMPLETE)
            return SimpleNamespace(expected=SimpleNamespace(remote=remote),
                observe=lambda **_options: (observed, decision),
                require_owned=lambda **_options: (observed, decision),
                adopt=lambda *_args: fake_complete_rebase(context, project_values={'active_sha': current.target_base_oid}))
        self.handler._current_rebase_service = fake_current_service


        def fake_rebase(context, target_oid, *, autostash=False):
            arguments = ('--autostash', target_oid) if autostash else (target_oid,)
            return self.effects.workspace.git(
                context.worktree, 'rebase', *arguments, check=False,
            )

        self.handler._run_managed_rebase = fake_rebase

    def git(self, path, *args, **kwargs):
        self.calls.append(args[0])
        if args[0] == 'rebase' and '--skip' not in args:
            self.assertEqual(self.state.row()['last_error'], lm.REBASE_IN_PROGRESS_ERROR)
        if 'rebase' in args and ('--continue' in args or '--skip' in args):
            # The fake command succeeds, so its operation metadata must be
            # cleared just as a completed real rebase would be.
            self.effects.workspace._rebase_in_progress_for_branch.return_value = False
        if args[0] == 'commit':
            self.dirty = False
        output = self.remote if args[0] == 'rev-parse' else ''
        if args[:2] == ('ls-files', '--cached'):
            output = 'app.py\0'
        if args[0] == 'status' and self.dirty:
            output = ' M app.py'
        return SimpleNamespace(stdout=output, returncode=0)

    def publication_command(self, arguments, **_kwargs):
        if arguments[:3] == ['gh', 'pr', 'view']:
            row = self.state.row()
            return SimpleNamespace(returncode=0, stdout=json.dumps({
                'body': '', 'state': 'OPEN',
                'headRefOid': row['publication_remote_oid'] or row['pr_head_oid'] or self.remote,
            }))
        self.calls.append('pr')
        return SimpleNamespace(returncode=0, stdout='https://example/pr/1')

    def agent(self, *args, **kwargs):
        self.calls.append(args[4])
        kwargs['on_session_started']('session')
        return lm.AgentResult(0, READY_WORKER_RESULT, session_id='session')

    def context(self, phase=None):
        if phase is not None:
            self.state.update_project('repo', phase=phase)
        return self.handler._context()

    def review_context(self):
        snapshot = DefinitionSnapshot(
            revision_sha='base', path='docs/plans/[G-047] Runtime.md',
            title='Runtime', sources=('docs/PRODUCT.md',), depends_on=(),
            complexity='medium', risk='medium', priority='normal',
            body_hash='body', source_fingerprint='source', archive_candidates=(),
            guidance_hash=plan_content_hash(self.connection, 'repo', 'G-047'),
        )
        self.handler._definition_snapshot = snapshot
        self.state.update_project(
            'repo', phase='REVIEWING', active_sha='base',
            validated_base_sha='base', worker_result='worker result',
            verify_result='successful verify output',
            worker_input_token=snapshot.worker_token,
            archive_token=snapshot.archive_token,
            verify_token=snapshot.verify_token(
                'base', 'base', 'successful verify output',
            ),
        )
        return self.handler._context()

    def observed_git(self, path, *args, **kwargs):
        if args[0] == 'symbolic-ref':
            return SimpleNamespace(stdout='loop-manager/G-047\n', returncode=0)
        if args[0] == 'ls-files':
            return SimpleNamespace(
                stdout='app.py\0' if args[1:2] == ('--cached',) else '',
                returncode=0,
            )
        if args[:2] == ('diff', '--cached'):
            return SimpleNamespace(stdout='', returncode=0)
        if args[0] == 'merge-base':
            return SimpleNamespace(stdout='', returncode=0)
        if args == ('rev-parse', 'HEAD'):
            return SimpleNamespace(stdout='owned-head\n', returncode=0)
        return self.git(path, *args, **kwargs)


def execution_fixture(test):
    fixture = fixture_cleanup(test, ExecutionFixture())
    fixture.setUp()
    return fixture
