from contextlib import closing
from tests.support.workspace import symlink_or_skip
import contextlib
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import loop_manager
from loop_manager_internal.git import manager_git_journal
from loop_manager_internal.storage import agent_invocation_state
from loop_manager_internal.agents.agent_invocation import InvocationContext, ResolvedProfile, Usage
from loop_manager_internal.agents.agent_write_isolation import IsolationEvidence, inspect_worktree


from tests.support.origin import (
    git, configure_identity, commit_all, write_registration_files,
    remote_fixture, advance_remote, OriginScopeFixture,
)
from loop_manager_internal.runtime import bootstrap


class OriginRegistrationTests(unittest.TestCase):
    def test_reregistration_archives_legacy_approval_without_approving_current_hash(self):
        from loop_manager_internal.runtime import project_registry
        from loop_manager_internal.catalog.execplan_catalog import ExecPlanCatalogSnapshot
        from types import SimpleNamespace
        for old_hash, old_origin in (('legacy-hash', 'example/project'), (None, None)):
            with self.subTest(old_hash=old_hash), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                revision = 'a' * 40
                connection = sqlite3.connect(':memory:')
                self.addCleanup(connection.close)
                connection.row_factory = sqlite3.Row
                connection.execute(loop_manager.SCHEMA)
                loop_manager._ensure_execplans_schema(connection)
                connection.execute('INSERT INTO projects (repo, base_branch, agents_hash, agents_approved, origin, registered) '
                    'VALUES (?, ?, ?, 1, ?, ?)', (str(root), 'main', old_hash, old_origin, int(old_hash is None)))
                connection.commit()
                if old_hash is None:
                    project_registry.remove_project(connection, str(root))
                else:
                    # A pre-migration unregister left only the original row.
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = "
                        "'project_agents_approval_audit'").fetchone())
                documents = {'AGENTS.md': b'project rules',
                             'docs/PLANS.md': (loop_manager.ROOT / 'docs/PLANS.md').read_bytes(),
                             'docs/ARCHITECTURE.md': b'# Custom architecture',
                             'docs/PRODUCT.md': b'# Custom product'}
                def validate(_path, **options):
                    options['origin_result'].append('example/project')
                    return root
                with mock.patch.object(loop_manager, 'validate_repository', side_effect=validate), \
                     mock.patch.object(loop_manager, 'remote_base', return_value='main'), \
                     mock.patch.object(loop_manager, 'git', return_value=SimpleNamespace(stdout=revision)), \
                     mock.patch.object(loop_manager, '_git_tree_entries', return_value={}), \
                     mock.patch.object(loop_manager, '_revision_blob',
                         side_effect=lambda _root, _entries, path: documents.get(path)), \
                     mock.patch.object(loop_manager, 'migrate_legacy_execplans'), \
                     mock.patch.object(loop_manager, 'revision_execplan_snapshot',
                         return_value=ExecPlanCatalogSnapshot({}, {}, revision_sha=revision)):
                    for approve in (False, True):
                        added = loop_manager.add_registered_project(connection, root,
                            config_path=Path('config.ini'), provider='codex', requested_base='main',
                            approve_agents_mismatch=approve)
                        self.assertFalse(added.agents_approved)
                        self.assertEqual(added.agents_hash, hashlib.sha256(documents['AGENTS.md']).hexdigest())
                        self.assertEqual(project_registry.list_projects(connection), (added,))
                        audit = connection.execute('SELECT repo, agents_hash, agents_approved, origin '
                            'FROM project_agents_approval_audit').fetchall()
                        self.assertEqual([tuple(row) for row in audit], [(str(root), old_hash, 1, old_origin)])
                        project_registry.remove_project(connection, str(root))
                        documents['AGENTS.md'] = b'updated project rules'

    def test_project_only_agents_are_accepted_and_invalid_utf8_missing_inputs_are_rejected(self):
        from loop_manager_internal.runtime import project_registry
        documents = {'AGENTS.md': '프로젝트 규칙\r\n'.encode(),
                     'docs/PLANS.md': (loop_manager.ROOT / 'docs/PLANS.md').read_bytes(),
                     'docs/ARCHITECTURE.md': b'# Custom architecture\n',
                     'docs/PRODUCT.md': b'# Custom product\n'}
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.execute('CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT, provider TEXT)')
        project_registry.ensure_registry_schema(connection)
        connection.execute("INSERT INTO projects (repo, base_branch, provider, agents_hash, agents_approved) "
                           "VALUES ('old', 'main', 'codex', 'legacy-hash', 1)")
        with mock.patch.object(loop_manager, '_git_tree_entries', return_value={}), mock.patch.object(
            loop_manager, '_revision_blob', side_effect=lambda _root, _entries, path: documents.get(path)):
            for approve in (False, True):
                self.assertEqual(loop_manager.inspect_registration_files(Path('/repo'), 'selected',
                    approve_agents_mismatch=approve), (hashlib.sha256(documents['AGENTS.md']).hexdigest(), False))
            documents['AGENTS.md'] = b'\xff'
            with self.assertRaisesRegex(RuntimeError, 'UTF-8'):
                loop_manager.inspect_registration_files(Path('/repo'), 'selected', approve_agents_mismatch=True)
            del documents['AGENTS.md']
            with self.assertRaisesRegex(RuntimeError, 'AGENTS.md'):
                loop_manager.inspect_registration_files(Path('/repo'), 'selected', approve_agents_mismatch=True)
            documents['AGENTS.md'] = b'project'
            del documents['docs/PRODUCT.md']
            with self.assertRaisesRegex(RuntimeError, 'PRODUCT'):
                loop_manager.inspect_registration_files(Path('/repo'), 'selected', approve_agents_mismatch=False)
        self.assertEqual(connection.execute('SELECT agents_hash, agents_approved FROM projects').fetchone(),
                         ('legacy-hash', 1))

    def test_registration_normalizes_document_line_endings_but_hashes_agent_bytes(self):
        for newline in (b'\n', b'\r\n'):
            with self.subTest(newline=newline):
                def common(path):
                    return loop_manager.normalized_text(loop_manager.ROOT / path).encode('utf-8').replace(b'\n', newline)

                documents = {
                    'AGENTS.md': common('AGENTS.md'),
                    'docs/PLANS.md': common('docs/PLANS.md'),
                    'docs/ARCHITECTURE.md': b'# Custom architecture\n',
                    'docs/PRODUCT.md': b'# Custom product\n',
                }
                with mock.patch.object(loop_manager, '_git_tree_entries', return_value={}), mock.patch.object(
                    loop_manager, '_revision_blob', side_effect=lambda _root, _entries, path: documents[path]
                ):
                    self.assertEqual(
                        loop_manager.inspect_registration_files(Path('/repo'), 'revision', approve_agents_mismatch=False),
                        (hashlib.sha256(documents['AGENTS.md']).hexdigest(), False),
                    )
                    for path in ('docs/ARCHITECTURE.md', 'docs/PRODUCT.md'):
                        original = documents[path]
                        documents[path] = common(path.replace('.md', '.template.md'))
                        with self.assertRaisesRegex(RuntimeError, '템플릿'):
                            loop_manager.inspect_registration_files(Path('/repo'), 'revision', approve_agents_mismatch=False)
                        documents[path] = original

    def test_registry_schema_does_not_commit_caller_transaction(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE projects (repo TEXT PRIMARY KEY, base_branch TEXT, provider TEXT)"
        )
        connection.execute("INSERT INTO projects VALUES ('repo', 'main', 'codex')")
        loop_manager.project_registry.ensure_registry_schema(connection)
        self.assertTrue(connection.in_transaction)
        self.assertIn("origin", {
            row[1] for row in connection.execute("PRAGMA table_info(projects)")
        })
        connection.rollback()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
        self.assertNotIn("origin", {
            row[1] for row in connection.execute("PRAGMA table_info(projects)")
        })
        self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name = "
            "'project_agents_approval_audit'").fetchone())

    def test_self_runtime_identity_rejects_alias_and_other_clone(self):
        with tempfile.TemporaryDirectory() as directory:
            _remote, source, other_clone = remote_fixture(directory)
            state_dir = Path(directory) / "state"
            state_dir.mkdir()
            runtime_path = state_dir / "runtime"
            selected = git(source, "rev-parse", "HEAD")
            git(source, "worktree", "add", "--detach", str(runtime_path), selected)
            with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
                connection.execute(loop_manager.SCHEMA)
                connection.execute(
                    "INSERT INTO projects (repo, base_branch, origin) VALUES (?, ?, ?)",
                    (str(source), "develop", "owner/repo"),
                )
            runtime = loop_manager.RuntimeMutation(
                source, runtime_path, selected, "a" * 32,
            )
            alias = Path(directory) / "source-alias"
            symlink_or_skip(alias, source, target_is_directory=True)
            project = lambda path, origin="owner/repo": loop_manager.project_registry.RegisteredProject(
                str(path), "develop", "codex", None, False, origin,
            )
            with mock.patch.object(loop_manager, "ROOT", runtime_path), mock.patch(
                "loop_manager.github_repository", return_value="owner/repo"
            ):
                self.assertTrue(
                    loop_manager._registered_self_runtime_project(
                        project(source), runtime, state_dir,
                    )
                )
                for path in (other_clone, alias, source / "subdir", runtime_path):
                    self.assertFalse(
                        loop_manager._registered_self_runtime_project(
                            project(path), runtime, state_dir,
                        )
                    )
                with self.assertRaisesRegex(RuntimeError, "origin"):
                    loop_manager._registered_self_runtime_project(
                        project(source, "another/repo"), runtime, state_dir,
                    )
                self.assertEqual(
                    loop_manager._bootstrap_service().bootstrap_base_branch(
                        source, state_dir, "owner/repo",
                    ),
                    "develop",
                )
                with closing(sqlite3.connect(state_dir / "loop-manager.db")) as connection, connection:
                    connection.execute(
                        "UPDATE projects SET repo = ?", (str(alias),)
                    )
                self.assertEqual(
                    loop_manager._bootstrap_service().bootstrap_base_branch(
                        source, state_dir, "owner/repo",
                    ),
                    "main",
                )


    def test_registration_reads_only_the_selected_revision_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            git(repo, "init", "-b", "main")
            configure_identity(repo)
            write_registration_files(repo)
            committed_agents = commit_all(repo, "Valid registration")

            (repo / "AGENTS.md").write_text("# local-only mismatch\n", encoding="utf-8")
            (repo / "docs/PLANS.md").unlink()
            agents_hash, approved = loop_manager.inspect_registration_files(
                repo,
                committed_agents,
                approve_agents_mismatch=False,
            )
            self.assertFalse(approved)
            self.assertEqual(
                agents_hash,
                hashlib.sha256(loop_manager.git_bytes(repo, "show", f"{committed_agents}:AGENTS.md")).hexdigest(),
            )

            def validate(_path, **kwargs):
                kwargs["origin_result"].append("example/project")
                return repo

            with mock.patch(
                "loop_manager.validate_repository", side_effect=validate
            ), mock.patch(
                "loop_manager.remote_base", return_value="main"
            ), mock.patch(
                "loop_manager.git",
                return_value=subprocess.CompletedProcess(
                    (), 0, stdout=committed_agents + "\n"
                ),
            ) as revision_git, mock.patch(
                "loop_manager.inspect_registration_files",
                return_value=("agents-hash", False),
            ) as inspect_files:
                inspection = loop_manager.inspect_project_registration(
                    repo,
                    config_path=Path("config.ini"),
                    provider="codex",
                    requested_base=None,
                    approve_agents_mismatch=False,
                )
            revision_git.assert_called_once_with(
                repo,
                "rev-parse",
                "--verify",
                "refs/remotes/origin/main^{commit}",
            )
            inspect_files.assert_called_once_with(
                repo,
                committed_agents,
                approve_agents_mismatch=False,
            )
            self.assertEqual(inspection.origin, "example/project")

            (repo / "AGENTS.md").write_bytes(
                (loop_manager.ROOT / "AGENTS.md").read_bytes()
            )
            (repo / "docs/PLANS.md").write_text("remote-invalid\n", encoding="utf-8")
            git(repo, "add", "-A")
            git(repo, "commit", "-m", "Invalid remote plans")
            (repo / "docs/PLANS.md").write_bytes(
                (loop_manager.ROOT / "docs/PLANS.md").read_bytes()
            )
            with self.assertRaisesRegex(RuntimeError, "PLANS.md"):
                loop_manager.inspect_registration_files(
                    repo,
                    "HEAD",
                    approve_agents_mismatch=True,
                )
