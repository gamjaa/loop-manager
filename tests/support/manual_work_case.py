import contextlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.application.manual_work import validate_options
from loop_manager_internal.agents.agent_invocation import ResolvedProfile
from loop_manager_internal.execution.manual_candidate import ManualCandidate
from loop_manager_internal.storage.manual_work_state import ManualWorkRepository
from tests.support.manual_work import ManualWorkFixture, completion, generated
from tests.support.git import git_command
from types import SimpleNamespace




class ManualWorkCase(ManualWorkFixture, unittest.TestCase):
    def mocked_service(self, directory):
        from loop_manager_internal.application.manual_work import ManualWorkService
        from tests.support.database import runtime_connection

        repo=Path(directory)/'repo'
        connection=runtime_connection(str(repo))
        self.addCleanup(connection.close)
        connection.execute('UPDATE projects SET registered=1,origin=? WHERE repo=?',
            (str(Path(directory)/'origin.git'),str(repo)))
        connection.commit()
        state_dir=Path(directory)/'state'
        state_dir.mkdir()
        config_path=Path(directory)/'operational-config.ini'
        config_path.write_text('[loop]\nverify_timeout_seconds=600\nverify_parallelism=auto\n',encoding='utf-8')
        unexpected=mock.Mock(side_effect=AssertionError('unexpected Git or external operation'))
        effects=replace(loop_manager._manual_work_effects(),validate_repository=unexpected,
            fetch_base=unexpected,git=unexpected,git_bytes=unexpected,make_worktree=unexpected,
            validate_workspace=unexpected,capture_snapshot=unexpected,pr_for_branch=unexpected,
            generate_plans=unexpected,run_project=unexpected,operation_print=mock.Mock(),
            resolve_profile=mock.Mock(return_value=ResolvedProfile('fixture-reviewer','medium')))
        service=ManualWorkService(effects,connection,state_dir,repo,config_path)
        service.drive=mock.Mock(return_value=0)
        return service

    def seed_manual_group(self, service, directory, *, kind):
        plan={'id':'G-047','title':'Runtime','sources':['docs/PRODUCT.md'],'depends_on':[],
              'complexity':'medium','risk':'medium','priority':'normal'}
        path='docs/plans/[G-047] Runtime.md'
        documents=[{'path':path,'text':'# Runtime\n','plan':plan}]
        head='a'*40
        base='b'*40
        source_branch='source' if kind=='submit' else 'loop-manager/G-047'
        requirements=('docs/requirements/Active.md',) if kind=='submit' else ()
        group=service.store.create(service.repo,kind,source_branch,requirements,head,base,
            members=('G-047',),documents=documents)
        worktree=Path(directory)/'worktree'
        group=service.store.update(group,state='AUTOMATIC' if kind=='submit' else 'MANUAL_WORKING',
            owner_id='G-047',branch='loop-manager/G-047',worktree=str(worktree),
            workspace_created=0,prepared_head=head,definition_head=head)
        status='MANUAL_PENDING' if kind=='submit' else 'MANUAL_WORKING'
        service.connection.execute('UPDATE exec_plans SET status=?,branch=?,worktree=?,workspace_owned=1,active_sha=? '
            'WHERE repo=? AND plan_id=?',
            (status,group['branch'],group['worktree'],base,service.repo,'G-047'))
        service.connection.commit()
        return service.store.get(service.repo,group['group_id']),documents,head,base

    def candidate(self, service, plan_id):
        return ManualCandidate(service.connection,service.repo,plan_id,service.effects.git,
            service.effects.git_bytes,config_path=service.config_path,provider=service.project['provider'],
            resolve_profile=service.effects.resolve_profile)


    def submitted(self, directory, *, count=2, skip=False, skip_reason='External checks'):
        service,_,_=self.fixture(directory)
        user=Path(directory)/'source-worktree'
        git_command(service.repo_path,'worktree','add','-b','source',str(user),'origin/main')
        (user/'src/app.py').write_text('VALUE = 2\n')
        git_command(user,'add','src/app.py')
        git_command(user,'commit','-m','User implementation')
        git_command(user,'push','origin','source')
        service.effects.generate_plans.return_value=json.dumps({'plans':[
            generated(f'Submitted {index}',identity=f'draft-{index}') for index in range(count)]})
        args=completion()
        args.branch='source'
        args.requirement=['docs/requirements/Active.md']
        if skip:
            args.skip_verify=args.skip_review=True
            args.expected_head=git_command(user,'rev-parse','HEAD')
            args.skip_reason=skip_reason
        validate_options(args)
        service.submit(args)
        return service,service.store.for_source(service.repo,'source',args.requirement),user


    def step(self, service, *, verify_result=(True,'verify PASS'), review='VERDICT: PASS',
             stop=None, pr=None, profile=None):
        from loop_manager_internal.config.managed_config import load_managed_config
        from loop_manager_internal.verification.coordination import execution_settings
        config=load_managed_config(service.config_path)
        real_command=loop_manager.command
        def command(arguments,*args,**kwargs):
            if list(arguments)[:3]==['gh','pr','create']:
                return SimpleNamespace(returncode=0,stdout='https://example/pull/1\n')
            return real_command(arguments,*args,**kwargs)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch('loop_manager.validate_repository',return_value=service.repo_path))
            stack.enter_context(mock.patch('loop_manager.ensure_safe_base_worktree'))
            stack.enter_context(mock.patch('loop_manager.pr_for_branch',
                side_effect=pr if callable(pr) else None,return_value=pr))
            stack.enter_context(mock.patch('loop_manager.immutable_github_blob_url',return_value='https://example/blob'))
            stack.enter_context(mock.patch('loop_manager.command',side_effect=command))
            stack.enter_context(mock.patch('loop_manager._agent_profile',
                return_value=profile or ResolvedProfile('fixture-reviewer','medium')))
            verify=stack.enter_context(mock.patch('loop_manager.verify',return_value=verify_result))
            agent=stack.enter_context(mock.patch('loop_manager.run_agent',
                side_effect=lambda *args, **kwargs: (0, '' if args[4] == 'planner' else review)))
            if stop:
                real_ports=loop_manager._ExecutionLifecyclePorts
                def ports(*args,**kwargs):
                    result=real_ports(*args,**kwargs)
                    setattr(result,stop,mock.Mock(return_value=loop_manager.ExecutionOutcome(
                        loop_manager.ExecutionOutcomeKind.STABLE_WAIT,detail='test boundary')))
                    return result
                stack.enter_context(mock.patch('loop_manager._ExecutionLifecyclePorts',side_effect=ports))
            with execution_settings(mock.Mock(),config.verify_parallelism,service.state_dir):
                result=loop_manager._run_project_step(service.connection,service.state_dir,service.repo,
                    service.config_path,'main',False,registered_only=True,
                    verify_timeout_seconds=config.verify_timeout_seconds)
        return result,verify,agent


    def _manual_recovery_candidate(self, directory, *, pushed_head=False):
        service,branch,worktree=self.fixture(directory)
        service.start('G-012')
        (worktree/'src/app.py').write_text('VALUE = 2\n')
        git_command(worktree,'add','src/app.py')
        git_command(worktree,'commit','-m','Manual recovery candidate')
        git_command(worktree,'push','origin',branch)
        head=git_command(worktree,'rev-parse','HEAD')
        args=completion()
        args.skip_verify=args.skip_review=True
        args.expected_head=head
        args.skip_reason='External verify and Review'
        service.complete(args)
        result,_,_=self.step(service,stop='publish')
        self.assertEqual(result,0)
        base=git_command(service.repo_path,'rev-parse','origin/main')
        row=service.connection.execute(
            'SELECT * FROM exec_plans WHERE repo=? AND plan_id=?',
            (service.repo,'G-012'),
        ).fetchone()
        from loop_manager_internal.execution.execution_revision import DefinitionSnapshot
        definition=DefinitionSnapshot.deserialize(row['definition_snapshot'])
        url='https://example/pull/1'
        observed_head=base if pushed_head else head
        service.runtime.update_plan(service.repo,'G-012',plan_values={
            'status':'PUBLISHING',
            'pr_url':url,
            'pr_head_oid':observed_head,
            'publication_remote_oid':observed_head,
            'active_sha':base,
            'validated_base_sha':base,
            'publication_token':definition.publication_token(base,head),
        },enqueue=False)
        service.runtime.hydrate(service.repo,'G-012')
        group=service.store.for_plan(service.repo,'G-012')
        service.store.update(group,state='AUTOMATIC',active_id='G-012')
        project=service.connection.execute(
            'SELECT * FROM projects WHERE repo=?',(service.repo,),
        ).fetchone()
        pull_request={
            'url':url,'headRefName':branch,'baseRefName':'main',
            'headRefOid':head,'state':'OPEN',
        }
        return service,branch,worktree,head,base,project,pull_request


    def _verify_fixture_with_manual_skip(self, *, reason='external verify'):
        from tests.support.execution import execution_fixture
        from loop_manager_internal.execution.manual_candidate import candidate_token, stage_input_tokens
        from loop_manager_internal.storage.manual_work_state import ManualWorkRepository

        fixture=execution_fixture(self)
        head,base='candidate-head','base'
        context=fixture.review_context()
        fixture.state.update_project(fixture.repo,phase='VERIFYING')
        context=fixture.handler._context()
        fixture.handler._post_effect_guard=mock.Mock(return_value=context)
        documents=[{'path':'docs/plans/[G-047] Runtime.md','text':'# Runtime',
                    'plan':dict(fixture.plan)}]
        store=ManualWorkRepository(fixture.connection)
        group=store.create(fixture.repo,'manual-start','loop-manager/G-047',(),head,base,
                           members=('G-047',),documents=documents)
        store.update(group,state='AUTOMATIC',owner_id='G-047',
                     branch='loop-manager/G-047',worktree=str(context.worktree))
        group=store.for_plan(fixture.repo,'G-047')
        candidate_git=lambda _path,*args,**_kwargs: SimpleNamespace(
            stdout=(head if args==('rev-parse','HEAD') else base),returncode=0)
        config_path=Path(self.enterContext(tempfile.TemporaryDirectory()))/'operational-config.ini'
        config_path.write_text('[loop]\nverify_timeout_seconds=600\nverify_parallelism=auto\n',encoding='utf-8')
        profile=ResolvedProfile('fixture-reviewer','medium')
        resolve_profile=mock.Mock(return_value=profile)
        candidate=ManualCandidate(fixture.connection,fixture.repo,'G-047',candidate_git,mock.Mock(),
            config_path=config_path,provider='codex',resolve_profile=resolve_profile,
            verify_timeout_seconds=600,verify_parallelism='auto')
        candidate.documents=mock.Mock(return_value=documents)
        candidate_input=candidate_token(fixture.connection,fixture.repo,head,base,documents)
        verify_input,review_input=stage_input_tokens(config_path,'codex',context.worktree,resolve_profile)
        store.accept(group,head=head,base=base,input_token=candidate_input,
                     verify_input_token=verify_input,review_input_token=review_input,
                     skip_verify=True,reason=reason)
        fixture.state.manual=candidate
        return fixture,context,candidate,config_path


    def pending_source(self, directory):
        service,group,user=self.submitted(directory)
        (user/'src/app.py').write_text('VALUE = 42\n')
        git_command(user,'add','src/app.py')
        git_command(user,'commit','-m','Repeated source')
        git_command(user,'push','origin','source')
        incoming=service.fetch('source')
        with service.connection:
            group=service.store.update(group,pending_source_head=incoming)
            service.store.accept(group,head=incoming,base=group['base_sha'],input_token='pending-source',
                                 skip_verify=True,skip_review=True,reason='External checks')
        return service,service.store.get(service.repo,group['group_id']),user


    def remote_followup(self, service, group, directory, *, conflict=False):
        other=Path(directory)/'other-device'
        git_command(service.repo_path,'worktree','add','--detach',str(other),f"origin/{group['branch']}")
        path='src/app.py' if conflict else 'followup.txt'
        (other/path).write_text('VALUE = 99\n' if conflict else 'Other device\n')
        git_command(other,'add',path)
        git_command(other,'commit','-m','Other device followup')
        git_command(other,'push','origin',f"HEAD:refs/heads/{group['branch']}")
        return git_command(other,'rev-parse','HEAD')


    @contextlib.contextmanager
    def interrupt_source_push(self):
        from loop_manager_internal.git import manager_git_journal as journal
        execute=journal.execute
        def interrupt(*arguments,**kwargs):
            if kwargs['kind']=='manual_branch_push':
                raise KeyboardInterrupt('after candidate before push intent')
            return execute(*arguments,**kwargs)
        with mock.patch.object(journal,'execute',side_effect=interrupt):
            yield
