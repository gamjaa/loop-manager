"""Real Git regression for successful rebase with failed autostash application.

The Worker does not run integration tests; the external verify script runs this.
"""

from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from execution_checkpoint import CheckpointAction, assess, observe
from execution_checkpoint_state import ExecutionCheckpointRepository


class AutostashCheckpointGitTests(unittest.TestCase):
    def test_zero_exit_rebase_retains_unmerged_index_after_metadata_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'repo'
            repo.mkdir()

            def git(_worktree, *arguments, check=True):
                result = subprocess.run(
                    ['git', '-C', str(repo), *arguments],
                    text=True, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, check=False,
                )
                if check and result.returncode:
                    raise RuntimeError(result.stdout)
                return SimpleNamespace(stdout=result.stdout,
                                       returncode=result.returncode)

            git(repo, 'init', '-b', 'main')
            git(repo, 'config', 'user.name', 'Checkpoint Test')
            git(repo, 'config', 'user.email', 'checkpoint@example.invalid')
            (repo / 'app.txt').write_text('initial\n')
            (repo / 'feature.txt').write_text('initial\n')
            git(repo, 'add', '.')
            git(repo, 'commit', '-m', 'initial')
            git(repo, 'checkout', '-b', 'feature')
            (repo / 'feature.txt').write_text('feature\n')
            git(repo, 'commit', '-am', 'feature')
            before_head = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'checkout', 'main')
            (repo / 'app.txt').write_text('base\n')
            git(repo, 'commit', '-am', 'base')
            target_base = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'checkout', 'feature')
            (repo / 'app.txt').write_text('local\n')

            database = sqlite3.connect(Path(directory) / 'checkpoints.db')
            database.row_factory = sqlite3.Row
            checkpoint = ExecutionCheckpointRepository(database)
            checkpoint.record(
                'repo', 'G-044', expected_generation=0,
                operation='rebase', expected_head=before_head,
                target_base_oid=target_base, observed_remote_oid=None,
                snapshot={'staged_paths': []},
            )
            database.close()

            rebased = git(repo, 'rebase', '--autostash', target_base, check=False)
            self.assertEqual(rebased.returncode, 0, rebased.stdout)
            self.assertFalse((repo / '.git' / 'rebase-merge').exists())
            self.assertFalse((repo / '.git' / 'rebase-apply').exists())

            reopened = sqlite3.connect(Path(directory) / 'checkpoints.db')
            reopened.row_factory = sqlite3.Row
            self.addCleanup(reopened.close)
            intent = ExecutionCheckpointRepository(reopened).get('repo', 'G-044')
            actual = observe(
                plan_id='G-044',
                row=dict(phase='WORKING', active_sha=before_head,
                         validated_base_sha=None, verify_token=None,
                         review_token=None, archive_token=None,
                         publication_token=None, publication_remote_oid=None,
                         pr_head_oid=None),
                definition_revision='definition', base_oid=target_base,
                branch='feature', worktree=repo, owned=True, git=git,
                rebase_in_progress=lambda *_: False,
                remote_oid=None, pull_request=None,
                rebase_intent=intent.operation == 'rebase',
                rebase_before_head=intent.expected_head,
                rebase_target_oid=intent.target_base_oid, stash_oid=None,
            )
            self.assertTrue(actual.unmerged_stages)
            self.assertEqual(assess(actual).action, CheckpointAction.CONFLICT)
