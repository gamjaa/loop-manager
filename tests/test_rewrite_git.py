"""외부 API는 mock으로, 실제 Git은 보존 효과를 확인하는 대표 경로만 쓴다."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from loop_manager_internal.git.repository import (GitHub, GitError, MergeConflict, Repository, RemoteDeleted,
                                                  command, github_repository, managed_body)
from loop_manager_internal.runtime.revision_runtime import prepare as prepare_runtime
from loop_manager_internal.storage.state import Store


class GitBoundaryTests(unittest.TestCase):
    def test_final_usage_changes_only_usage_inside_manager_region(self):
        api=GitHub('owner/repo')
        old='<!-- loop-manager:usage -->\n이전 집계\n<!-- loop-manager:usage-end -->'
        new='<!-- loop-manager:usage -->\n최종 집계\n<!-- loop-manager:usage-end -->'
        current={'body':managed_body('사용자 머리','검증 근거\n'+old)+'\n사용자 꼬리'}
        api.pull=Mock(return_value=current)
        api.api=Mock()
        api.update_usage(1,new)
        written=api.api.call_args.kwargs['payload']['body']
        self.assertIn('검증 근거',written)
        self.assertTrue(written.startswith('사용자 머리'))
        self.assertTrue(written.endswith('사용자 꼬리'))
        self.assertIn('최종 집계',written)
        self.assertNotIn('이전 집계',written)
        current['body']=written
        api.update_usage(1,new)
        self.assertEqual(api.api.call_count,1)

    def test_github_url_and_managed_body_preserve_user_content(self):
        self.assertEqual(github_repository('git@github.com:owner/repo.git'),'owner/repo')
        self.assertEqual(github_repository('https://github.com/owner/repo'),'owner/repo')
        with self.assertRaises(ValueError):
            github_repository('https://token@github.com/owner/repo')
        old = '사용자 머리\n\n'+managed_body('','이전 집계')+'\n사용자 꼬리'
        updated = managed_body(old,'새 집계')
        self.assertTrue(updated.startswith('사용자 머리\n\n'))
        self.assertTrue(updated.endswith('\n사용자 꼬리'))
        self.assertNotIn('이전 집계',updated)

    def test_existing_closed_pr_is_not_recreated(self):
        api = GitHub('owner/repo')
        api.pulls = Mock(return_value=[{'head':{'ref':'work','repo':{'full_name':'owner/repo'}},'state':'closed'}])
        api.api = Mock()
        with self.assertRaises(GitError):
            api.ensure_pull('work','main','제목','내용')
        api.api.assert_not_called()

    def test_manager_pr_region_redacts_secrets_and_preserves_user_region(self):
        api=GitHub('owner/repo')
        current={'number':1,'draft':False,'body':'사용자 password=그대로',
                 'head':{'ref':'work','repo':{'full_name':'Owner/Repo'}},'state':'open'}
        api.pulls=Mock(return_value=[current])
        api.pull=Mock(return_value=current)
        api.api=Mock(return_value=current)
        api.ensure_pull('work','main','제목','password=민감값')
        body=api.api.call_args.kwargs['payload']['body']
        self.assertIn('사용자 password=그대로',body)
        self.assertNotIn('민감값',body)
        self.assertIn('<REDACTED>',body)

    def test_close_rechecks_user_head_and_preserves_branch(self):
        api = GitHub('owner/repo')
        api.pull = Mock(return_value={'number':2,'head':{'sha':'new'},'state':'open','merged_at':None})
        api.api = Mock()
        with self.assertRaises(GitError):
            api.close({'number':2},'old')
        api.api.assert_not_called()

    def test_deleted_published_branch_never_creates_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Repository({'repo':'owner/repo','remote_url':'remote','base_branch':'main'},directory,Mock())
            repository.remote_head = Mock(return_value=None)
            with patch('loop_manager_internal.git.repository.command') as execute:
                with self.assertRaises(RemoteDeleted):
                    repository.prepare('loop-manager/G-001',published=True)
                execute.assert_not_called()


class GitPreservationTests(unittest.TestCase):
    def test_isolated_merge_checkpoint_restart_and_unpublished_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source,remote,state = root/'사용자 checkout',root/'원격.git',root/'state'
            command(['git','init','--initial-branch=main',str(source)])
            for key,value in (('user.name','Test'),('user.email','test@example.com')):
                command(['git','config',key,value],cwd=source)
            (source/'.gitignore').write_text('cache/\n',encoding='utf-8')
            (source/'base.txt').write_bytes(b'base\r\n')
            (source/'loop_manager.py').write_text('REWRITE_RUNTIME_VERSION = 1\n',encoding='utf-8')
            command(['git','add','.'],cwd=source)
            command(['git','commit','-m','initial'],cwd=source)
            command(['git','clone','--bare',str(source),str(remote)])
            command(['git','remote','add','origin',str(remote)],cwd=source)
            original = command(['git','rev-parse','HEAD'],cwd=source).stdout
            (source/'base.txt').write_bytes('사용자 수정\r\n'.encode('utf-8'))
            store = Store(state/'loop-manager.db')
            store.initialize()
            project = {'repo':'owner/repo','remote_url':str(remote),'base_branch':'main'}
            store.register('owner/repo',source,str(remote),'main','codex')
            repository = Repository(project,state,store)
            repository.refresh()
            runtime=prepare_runtime(repository,state,original.strip())
            self.assertEqual(repository.head(runtime),original.strip())
            self.assertEqual((runtime/'loop_manager.py').read_text(encoding='utf-8'),'REWRITE_RUNTIME_VERSION = 1\n')
            branch = 'loop-manager/G-001'
            work = repository.prepare(branch)
            (work/'cache').mkdir()
            (work/'cache'/'ignored.txt').write_bytes(b'cache\r\n')
            (work/'worker.txt').write_text('구현',encoding='utf-8')
            candidate = repository.commit(work,branch,'구현')
            repository.publish(work,branch)
            self.assertFalse(store.rows('SELECT * FROM external_jobs'))
            self.assertEqual(repository.publish(work,branch,published=True),candidate)
            self.assertEqual(repository.prepare(branch,saved_path=work,published=True),work)
            peer = root/'peer'
            command(['git','clone','--branch',branch,str(remote),str(peer)])
            command(['git','config','user.name','User'],cwd=peer)
            command(['git','config','user.email','user@example.com'],cwd=peer)
            (peer/'remote-user.txt').write_text('원격 사용자',encoding='utf-8')
            command(['git','add','.'],cwd=peer)
            command(['git','commit','-m','user'],cwd=peer)
            command(['git','push','origin',branch],cwd=peer)
            (work/'next.txt').write_text('후속 구현',encoding='utf-8')
            repository.commit(work,branch,'후속')
            with self.assertRaisesRegex(GitError,'다시 검증'):
                repository.publish(work,branch,published=True)
            self.assertEqual((work/'remote-user.txt').read_text(encoding='utf-8'),'원격 사용자')
            repository.publish(work,branch,published=True)
            command(['git','fetch','origin'],cwd=peer)
            command(['git','merge','--no-edit',f'origin/{branch}'],cwd=peer)
            (peer/'worker.txt').write_text('원격 교정',encoding='utf-8')
            command(['git','commit','-am','원격 충돌 변경'],cwd=peer)
            command(['git','push','origin',branch],cwd=peer)
            (work/'worker.txt').write_text('로컬 교정',encoding='utf-8')
            repository.commit(work,branch,'로컬 충돌 변경')
            with self.assertRaises(MergeConflict):
                repository.publish(work,branch,published=True)
            self.assertIn('원격 교정',(work/'worker.txt').read_text(encoding='utf-8'))
            self.assertIn('로컬 교정',(work/'worker.txt').read_text(encoding='utf-8'))
            (work/'worker.txt').write_text('원격 교정과 로컬 교정 보존',encoding='utf-8')
            repository.commit(work,branch,'충돌 해결')
            repository.publish(work,branch,published=True)
            self.assertFalse(repository.cleanup(work,branch),'미게시 ignored 파일을 보존해야 합니다.')
            command(['git','fetch','origin'],cwd=peer)
            command(['git','merge','--no-edit',f'origin/{branch}'],cwd=peer)
            (peer/'cache').mkdir(exist_ok=True)
            (peer/'cache/ignored.txt').write_text('원격에서 추적한 파일',encoding='utf-8')
            command(['git','add','-f','cache/ignored.txt'],cwd=peer)
            command(['git','commit','-m','추적 경로 추가'],cwd=peer)
            command(['git','push','origin',branch],cwd=peer)
            with self.assertRaises(GitError):
                repository.synchronize_head(work,branch)
            self.assertEqual((work/'cache/ignored.txt').read_bytes(),b'cache\r\n')
            (work/'unpublished.txt').write_text('미게시 보존',encoding='utf-8')
            self.assertFalse(repository.cleanup(work,branch))
            self.assertTrue((work/'unpublished.txt').exists())
            self.assertEqual((source/'base.txt').read_bytes(),'사용자 수정\r\n'.encode('utf-8'))
            self.assertEqual(command(['git','rev-parse','HEAD'],cwd=source).stdout,original)
            self.assertEqual((work/'cache'/'ignored.txt').read_bytes(),b'cache\r\n')
