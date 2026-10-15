import contextlib
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

import loop_manager
from loop_manager_internal.application.manual_work import ManualWorkError
from loop_manager_internal.execution.manual_candidate import ManualCandidate
from tests.support.manual_work import completion, generated
from tests.support.git import git_command, isolated_git_test
from tests.support.recovery_boundary import RecoveryBoundary
from types import SimpleNamespace


from tests.support.manual_work_case import ManualWorkCase


@isolated_git_test
class ManualWorkTest(ManualWorkCase):
    def test_submit_registers_all_g_on_owned_branch_without_mutating_source(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,_=self.fixture(directory)
            source='user-implementation'
            git_command(service.repo_path,'branch',source)
            git_command(service.repo_path,'push','origin',source)
            sha=git_command(service.repo_path,'rev-parse',source)
            service.effects.generate_plans.return_value=json.dumps({'plans':[
                generated('First',identity='a'),generated('Second',identity='b')]})
            args=completion()
            args.branch=source
            args.requirement=['docs/requirements/Active.md']
            self.assertEqual(service.submit(args),1)
            group=service.store.for_source(service.repo,source,args.requirement)
            self.assertEqual(len(json.loads(group['members'])),2)
            self.assertEqual(group['state'],'AUTOMATIC')
            self.assertEqual(git_command(service.repo_path,'rev-parse',source),sha)
            self.assertNotEqual(group['prepared_head'],sha)
            rows=service.connection.execute('SELECT status,branch FROM exec_plans WHERE plan_id IN (?,?)',
                tuple(json.loads(group['members']))).fetchall()
            self.assertEqual({row['status'] for row in rows},{'MANUAL_PENDING'})
            self.assertEqual({row['branch'] for row in rows},{group['branch']})
            self.assertFalse(any(row['status']=='READY' for row in rows))
            result=loop_manager.reconcile_execplan_catalog(service.connection,service.repo_path,service.repo,'main')
            self.assertTrue(result.usable)
            self.assertEqual(service.submit(args),1)
            self.assertEqual(service.effects.generate_plans.call_count,1)


    def test_repeated_source_head_preserves_members_source_and_owned_history(self):
        with tempfile.TemporaryDirectory() as directory:
            service,_,_=self.fixture(directory)
            git_command(service.repo_path,'branch','source')
            git_command(service.repo_path,'push','origin','source')
            service.effects.generate_plans.return_value=json.dumps({'plans':[generated('Submitted')]})
            args=completion()
            args.branch='source'
            args.requirement=['docs/requirements/Active.md']
            service.submit(args)
            original=service.store.for_source(service.repo,'source',args.requirement)
            user=Path(directory)/'user'
            git_command(service.repo_path,'worktree','add',str(user),'source')
            (user/'src/app.py').write_text('VALUE = 42\n')
            git_command(user,'add','src/app.py')
            git_command(user,'commit','-m','Source followup')
            git_command(user,'push','origin','source')
            incoming=git_command(user,'rev-parse','HEAD')
            service.submit(args)
            self.assertIsNone(service.store.for_source(service.repo,'source',args.requirement)['pending_source_head'])
            self.assertEqual(service.store.for_source(service.repo,'source',args.requirement)['source_head'],incoming)
            service.runtime.transition_and_clear(service.repo,original['owner_id'],plan_values={'status':'MANUAL_PENDING'},enqueue=False)
            service.resume()
            current=service.store.for_source(service.repo,'source',args.requirement)
            self.assertEqual(current['group_id'],original['group_id'])
            self.assertEqual(current['members'],original['members'])
            self.assertIsNone(current['pending_source_head'])
            head=git_command(service.repo_path,'ls-remote','--heads','origin',original['branch']).split()[0]
            self.assertEqual(git_command(service.repo_path,'show',f'{head}:src/app.py'),'VALUE = 42')
            self.assertEqual(git_command(user,'rev-parse','HEAD'),incoming)
            self.assertEqual(service.effects.generate_plans.call_count,1)


    def test_source_update_recovers_each_boundary_and_preserves_remote_followups(self):
        from loop_manager_internal.git import manager_git_journal as journal
        from loop_manager_internal.git.manual_source_update import update_source
        preparation=self.enterContext(RecoveryBoundary())
        directory=preparation.directory
        service,group,user=self.pending_source(directory)
        incoming=group['pending_source_head']
        request_id=group['request_id']
        before=group['prepared_head']
        real_git=service.effects.git
        worktree=Path(group['worktree'])
        (worktree/'staged.txt').write_text('Local staged work\n')
        git_command(worktree,'add','staged.txt')
        (worktree/'untracked.txt').write_text('Local untracked work\n')
        local_status=git_command(worktree,'status','--porcelain')
        effects=service.effects
        preparation.save(service.connection)
        for boundary in ('candidate','before_push','after_push','receipt','sqlite'):
            for followup in (False,True):
                with self.subTest(boundary=boundary,followup=followup):
                    preparation.restore(service.connection)
                    service.effects=effects
                    def interrupt(target,*arguments,**kwargs):
                        if boundary=='before_push' and arguments[0]=='push':
                            raise KeyboardInterrupt('before push')
                        result=real_git(target,*arguments,**kwargs)
                        if boundary=='after_push' and arguments[0]=='push':
                            raise KeyboardInterrupt('after push')
                        return result
                    with contextlib.ExitStack() as stack:
                        if boundary=='candidate':
                            stack.enter_context(self.interrupt_source_push())
                        elif boundary=='sqlite':
                            stack.enter_context(mock.patch.object(service.runtime,'update_plan',side_effect=KeyboardInterrupt))
                        elif boundary=='receipt':
                            write=journal._write_new
                            slot=journal._slot(service.state_dir,service.repo,'manual_branch_push',
                                               f"refs/heads/{group['branch']}")
                            def interrupt_receipt(path,value):
                                write(path,value)
                                if path.parent==slot and path.name.endswith('.result.json'):
                                    raise KeyboardInterrupt('after result before active removal')
                            stack.enter_context(mock.patch.object(journal,'_write_new',side_effect=interrupt_receipt))
                        else:
                            service.effects=replace(service.effects,git=interrupt)
                        with self.assertRaises(KeyboardInterrupt):
                            update_source(service,group)
                    service.effects=replace(service.effects,git=real_git)
                    saved=service.store.get(service.repo,group['group_id'])
                    candidate=saved['source_update_candidate']
                    self.assertEqual(saved['source_update_remote_oid'],before)
                    self.assertEqual(git_command(service.repo_path,'rev-list','--parents','-n','1',candidate).split(),
                                     [candidate,before,incoming])
                    if followup:
                        service.fetch(group['branch'])
                        later=self.remote_followup(service,group,directory)
                    spy=mock.Mock(wraps=real_git)
                    service.effects=replace(service.effects,git=spy)
                    current=update_source(service,saved)
                    pushes=[call.args for call in spy.call_args_list if call.args[1]=='push']
                    published=boundary in ('after_push','receipt','sqlite')
                    self.assertEqual(len(pushes),0 if published else 1)
                    head=service.fetch(group['branch'])
                    if followup:
                        git_command(service.repo_path,'merge-base','--is-ancestor',later,head)
                    git_command(service.repo_path,'merge-base','--is-ancestor',incoming,head)
                    git_command(service.repo_path,'merge-base','--is-ancestor',before,head)
                    if published:
                        git_command(service.repo_path,'merge-base','--is-ancestor',candidate,head)
                    elif followup:
                        self.assertNotEqual(head,candidate)
                        self.assertIn(journal.exact_branch_lease(group['branch'],later),pushes[0])
                    else:
                        self.assertIn(journal.exact_branch_lease(group['branch'],before),pushes[0])
                    self.assertEqual(git_command(service.repo_path,'show',f'{head}:src/app.py'),'VALUE = 42')
                    self.assertEqual(service.fetch('source'),incoming)
                    self.assertEqual(git_command(user,'rev-parse','HEAD'),incoming)
                    self.assertEqual(git_command(worktree,'status','--porcelain'),local_status)
                    self.assertEqual((worktree/'staged.txt').read_text(),'Local staged work\n')
                    self.assertEqual((worktree/'untracked.txt').read_text(),'Local untracked work\n')
                    self.assertEqual(current['members'],group['members'])
                    self.assertEqual(current['request_id'],request_id)
                    self.assertIsNone(current['source_update_candidate'])
                    self.assertIsNone(current['source_update_remote_oid'])
                    self.assertIsNone(current['pending_source_head'])
                    self.assertFalse(journal.has_pending_project_mutation(service.state_dir,service.repo))
                    self.assertEqual(service.effects.generate_plans.call_count,1)
                    spy.reset_mock()
                    update_source(service,current)
                    spy.assert_not_called()


    def test_source_update_descendant_equal_reinterruptions_and_rewrite(self):
        from loop_manager_internal.git import manager_git_journal as journal
        from loop_manager_internal.git.manual_source_update import update_source
        preparation=self.enterContext(RecoveryBoundary())
        directory=preparation.directory
        service,group,_=self.pending_source(directory)
        real_git=service.effects.git
        def after_push(target,*arguments,**kwargs):
            result=real_git(target,*arguments,**kwargs)
            if arguments[0]=='push':
                raise KeyboardInterrupt('after exact push')
            return result
        service.effects=replace(service.effects,git=after_push)
        with self.assertRaises(KeyboardInterrupt):
            update_source(service,group)
        service.effects=replace(service.effects,git=real_git)
        saved=service.store.get(service.repo,group['group_id'])
        service.fetch(group['branch'])
        later=self.remote_followup(service,group,directory)
        effects=service.effects
        remote_head=later
        preparation.save(service.connection)
        for boundary in ('before_result','after_result','sqlite','aborted_receipt','before_equal'):
            for rewrite in (False,True):
                with self.subTest(boundary=boundary,rewrite=rewrite):
                    preparation.restore(service.connection)
                    service.effects=effects
                    later=remote_head
                    slot=journal._slot(service.state_dir,service.repo,'manual_branch_push',
                                       f"refs/heads/{group['branch']}")
                    write=journal._write_new
                    def interrupt_equal(path,value):
                        equal=path.parent==slot and value.get('status')=='equal'
                        if equal and boundary=='before_result':
                            raise KeyboardInterrupt('before equal receipt')
                        write(path,value)
                        if equal and boundary=='after_result':
                            raise KeyboardInterrupt('after equal receipt')
                        if path.parent==slot and value.get('status')=='aborted' and boundary=='aborted_receipt':
                            raise KeyboardInterrupt('after retirement receipt')
                    execute=journal.execute
                    def interrupt_before_equal(*args,**kwargs):
                        if kwargs['kind']=='manual_branch_push' and kwargs['before']==kwargs['expected']:
                            raise KeyboardInterrupt('before equal intent')
                        return execute(*args,**kwargs)
                    with contextlib.ExitStack() as stack:
                        stack.enter_context(mock.patch.object(journal,'_write_new',side_effect=interrupt_equal))
                        if boundary=='before_equal':
                            stack.enter_context(mock.patch.object(journal,'execute',side_effect=interrupt_before_equal))
                        if boundary=='sqlite':
                            stack.enter_context(mock.patch.object(service.runtime,'update_plan',side_effect=KeyboardInterrupt))
                        with self.assertRaises(KeyboardInterrupt):
                            update_source(service,saved)
                    ref=f"refs/heads/{group['branch']}"
                    if rewrite:
                        tree=git_command(service.repo_path,'rev-parse',f"{group['prepared_head']}^{{tree}}")
                        later=git_command(service.repo_path,'commit-tree',tree,'-p',group['prepared_head'],
                                          '-m','Remove published candidate and descendant')
                        git_command(service.repo_path,'push','--force','origin',f'{later}:{ref}')
                    spy=mock.Mock(wraps=real_git)
                    service.effects=replace(service.effects,git=spy)
                    if rewrite:
                        with self.assertRaisesRegex(RuntimeError,'게시 완료한 source 후보'):
                            update_source(service,saved)
                        self.assertEqual(dict(service.store.get(service.repo,group['group_id'])),dict(saved))
                    else:
                        current=update_source(service,saved)
                        self.assertEqual(current['prepared_head'],later)
                        self.assertEqual(current['request_id'],group['request_id'])
                        self.assertEqual(current['members'],group['members'])
                        self.assertIsNone(current['pending_source_head'])
                        self.assertFalse(journal.has_pending_project_mutation(service.state_dir,service.repo))
                    self.assertFalse(any(call.args[1]=='push' for call in spy.call_args_list))
                    self.assertEqual(service.fetch(group['branch']),later)
                    self.assertEqual(service.fetch('source'),group['pending_source_head'])


    def test_saved_source_candidate_without_matching_basis_never_pushes(self):
        from loop_manager_internal.git.manual_source_update import update_source
        with tempfile.TemporaryDirectory() as directory:
            service,group,_=self.pending_source(directory)
            with self.interrupt_source_push(),self.assertRaises(KeyboardInterrupt):
                update_source(service,group)
            saved=service.store.get(service.repo,group['group_id'])
            spy=mock.Mock(wraps=service.effects.git)
            service.effects=replace(service.effects,git=spy)
            for changes in ({'source_update_remote_oid':None},
                            {'source_update_remote_oid':group['source_head']},
                            {'pending_source_head':group['initial_source_head']}):
                with self.subTest(changes=changes),service.connection:
                    altered=service.store.update(saved,**changes)
                    with self.assertRaisesRegex(RuntimeError,'근거가 없습니다'):
                        update_source(service,altered)
                    self.assertEqual(service.fetch(group['branch']),group['prepared_head'])
                    self.assertEqual(altered['source_update_candidate'],saved['source_update_candidate'])
                    service.store.update(saved,source_update_remote_oid=saved['source_update_remote_oid'],
                                         pending_source_head=saved['pending_source_head'])
            self.assertFalse(any(call.args[1]=='push' for call in spy.call_args_list))


    def test_source_update_lease_race_retains_candidate_and_reintegrates_conflict(self):
        from loop_manager_internal.git import manager_git_journal as journal
        from loop_manager_internal.git.manual_source_update import update_source
        with tempfile.TemporaryDirectory() as directory:
            service,group,user=self.pending_source(directory)
            real_git=service.effects.git
            later=None
            def compete(target,*arguments,**kwargs):
                nonlocal later
                if arguments[0]=='push':
                    later=self.remote_followup(service,group,directory,conflict=True)
                return real_git(target,*arguments,**kwargs)
            service.effects=replace(service.effects,git=compete)
            with self.assertRaises(RuntimeError):
                update_source(service,group)
            service.effects=replace(service.effects,git=real_git)
            saved=service.store.get(service.repo,group['group_id'])
            self.assertEqual(saved['source_update_remote_oid'],group['prepared_head'])
            self.assertEqual(service.fetch(group['branch']),later)
            other=Path(directory)/'other-device'
            def compete_again(target,*arguments,**kwargs):
                nonlocal later
                if arguments[0]=='push':
                    (other/'second-followup.txt').write_text('Another user commit\n')
                    git_command(other,'add','second-followup.txt')
                    git_command(other,'commit','-m','Compete during recovery')
                    git_command(other,'push','origin',f"HEAD:refs/heads/{group['branch']}")
                    later=git_command(other,'rev-parse','HEAD')
                return real_git(target,*arguments,**kwargs)
            service.effects=replace(service.effects,git=compete_again)
            with self.assertRaises(RuntimeError):
                update_source(service,saved)
            service.effects=replace(service.effects,git=real_git)
            saved=service.store.get(service.repo,group['group_id'])
            self.assertEqual(service.fetch(group['branch']),later)
            members=json.loads(group['members'])
            service.runtime.block_plan(service.repo,members[1],reason_code='guidance_approval_required',
                evidence='Separate user decision',release_fingerprint=None)
            service.runtime.update_plan(service.repo,members[0],plan_values={
                'worker_session_id':'existing-session','verify_token':'old-verify','review_token':'old-review',
                'publication_token':'old-publication'},enqueue=False)
            service.connection.commit()
            current=update_source(service,saved)
            head=service.fetch(group['branch'])
            git_command(service.repo_path,'merge-base','--is-ancestor',later,head)
            git_command(service.repo_path,'merge-base','--is-ancestor',group['pending_source_head'],head)
            self.assertEqual(git_command(service.repo_path,'show',f'{head}:src/app.py'),'VALUE = 99')
            self.assertEqual(git_command(service.repo_path,'show',f'{head}:second-followup.txt'),'Another user commit')
            self.assertEqual(current['resume_phase'],'IMPLEMENTING')
            self.assertEqual(current['pending_source_head'],group['pending_source_head'])
            self.assertEqual(current['source_update_conflict'],1)
            self.assertEqual(current['request_id'],group['request_id'])
            owner=service.runtime.get(service.repo,members[0])
            self.assertEqual(owner.worker_session_id,'existing-session')
            self.assertIsNone(owner.verify_token)
            self.assertIsNone(owner.review_token)
            self.assertIsNone(owner.publication_token)
            self.assertEqual(service.runtime.get(service.repo,members[1]).status,'PLAN_BLOCKED')
            context=SimpleNamespace(worktree=Path(group['worktree']),base_branch='main')
            manual=ManualCandidate(service.connection,service.repo,members[0],real_git,service.effects.git_bytes,
                config_path=service.config_path,provider=service.project['provider'],
                resolve_profile=service.effects.resolve_profile)
            self.assertIn('VALUE = 42',manual.prompt('Correct source conflict'))
            self.assertIsNone(manual.waive(context,'verify',record=False))
            self.assertIsNone(manual.waive(context,'review',record=False))
            self.assertFalse(journal.has_pending_project_mutation(service.state_dir,service.repo))
            (user/'src/app.py').write_text('VALUE = 99\n')
            git_command(user,'add','src/app.py')
            git_command(user,'commit','-m','User resolves source conflict')
            git_command(user,'push','origin','source')
            args=completion(branch='source',requirement=json.loads(group['requirements']))
            service.submit(args)
            current=service.store.get(service.repo,group['group_id'])
            self.assertIsNone(current['pending_source_head'])
            self.assertEqual(current['source_update_conflict'],0)
            self.assertEqual(current['resume_phase'],'VERIFYING')
            self.assertEqual(service.runtime.get(service.repo,members[1]).status,'PLAN_BLOCKED')


    def test_new_source_submit_cannot_replace_an_unconfirmed_saved_candidate(self):
        from loop_manager_internal.git.manual_source_update import update_source
        with tempfile.TemporaryDirectory() as directory:
            service,group,user=self.pending_source(directory)
            with self.interrupt_source_push(),self.assertRaises(KeyboardInterrupt):
                update_source(service,group)
            saved=service.store.get(service.repo,group['group_id'])
            (user/'src/app.py').write_text('VALUE = 43\n')
            git_command(user,'add','src/app.py')
            git_command(user,'commit','-m','Next source request')
            git_command(user,'push','origin','source')
            args=completion(branch='source',requirement=json.loads(group['requirements']))
            with self.assertRaisesRegex(ManualWorkError,'이전 source 후보'):
                service.submit(args)
            self.assertEqual(dict(service.store.get(service.repo,group['group_id'])),dict(saved))


    def test_saved_source_candidate_keeps_deleted_or_rewritten_remote_untouched(self):
        from loop_manager_internal.git.manual_source_update import update_source
        for change in ('deleted','rewritten','published_removed'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                service,group,_=self.pending_source(directory)
                interruption=(mock.patch.object(service.runtime,'update_plan',side_effect=KeyboardInterrupt)
                              if change=='published_removed' else self.interrupt_source_push())
                with interruption,self.assertRaises(KeyboardInterrupt):
                    update_source(service,group)
                saved=service.store.get(service.repo,group['group_id'])
                ref=f"refs/heads/{group['branch']}"
                new='' if change=='deleted' else group['base_sha']
                if change=='published_removed':
                    tree=git_command(service.repo_path,'rev-parse',f"{group['prepared_head']}^{{tree}}")
                    new=git_command(service.repo_path,'commit-tree',tree,'-p',group['prepared_head'],
                                    '-m','User rewrites after published candidate')
                git_command(service.repo_path,'push','--force','origin',f'{new}:{ref}')
                spy=mock.Mock(wraps=service.effects.git)
                service.effects=replace(service.effects,git=spy)
                with self.assertRaises(RuntimeError):
                    update_source(service,saved)
                self.assertFalse(any(call.args[1]=='push' for call in spy.call_args_list))
                self.assertEqual(dict(service.store.get(service.repo,group['group_id'])),dict(saved))
                actual=git_command(service.repo_path,'ls-remote','--heads','origin',ref)
                self.assertEqual(actual.split()[0] if actual else '',new)
