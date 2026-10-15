from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from loop_manager_internal.catalog import frontmatter
from loop_manager_internal.planning import requirement_overview
from loop_manager_internal.planning.validation_ports import _PlanningValidationPorts
from tests.support.git import git_command, git_test_environment, isolated_git_test


ROOT = Path(__file__).resolve().parents[1]
READER = runpy.run_path(str(ROOT / '.agents/skills/specification-docs/scripts/read_requirements.py'))
SKILL = READER['_DOCUMENT_MODULE']


class RequirementOverviewTest(unittest.TestCase):
    def test_independent_parsers_preserve_identity_and_summary_contract(self):
        documents = [
            ('# [R-001] 제목\n\n## Summary\n핵심\n\n### 세부\n조건\n## Context\n제외',
             '핵심\n\n### 세부\n조건'),
            ('# [R-001] Title\n````md\n## Summary\n가짜\n```\n## Summary\n가짜\n````\n'
             '## Summary ##\n실제\n~~~text\n## Context\n~~~\n# End\n제외', '실제\n~~~text\n## Context\n~~~'),
            ('# [R-001] Title\n~~~\n## Summary\n가짜\n~~~\n## Context\n내용', None),
            ('# [R-001] Title\n## Summary\n \n## Requirement\n내용', None),
            ('\ufeff---\nid: R-020\ntitle: 원본 제목\n---\n# [R-001] Other\n## Summary\n요약', '요약'),
            ('# [R-001] Title\n    ## Summary\n    코드\n', None),
        ]
        for text, expected in documents:
            with self.subTest(text=text):
                self.assertEqual(SKILL.requirement_identity(text), frontmatter.requirement_identity(text))
                self.assertEqual(SKILL.requirement_summary(text), expected)
                self.assertEqual(requirement_overview.requirement_summary(text), expected)

    def test_local_existence_combinations_and_untracked_overview_without_mutation(self):
        for active, archive in [(False, False), (True, False), (False, True), (True, True)]:
            with self.subTest(active=active, archive=archive), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / 'docs/requirements'
                directory.mkdir(parents=True)
                if active:
                    (directory / 'local.md').write_text('# [R-003] 로컬\n## Summary\n요약\n## Context\n본문', encoding='utf-8')
                    (directory / 'empty.md').write_text('# [R-002] Empty\n## Summary\n\n', encoding='utf-8')
                if archive:
                    (directory / 'archive').mkdir()
                    (directory / 'archive/old.md').write_bytes(b'\xff')  # Existence does not read history.
                before = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
                with (mock.patch('subprocess.run', side_effect=AssertionError('no process')),
                      mock.patch('sqlite3.connect', side_effect=AssertionError('no database'))):
                    result = READER['read_requirements'](root)
                self.assertEqual((result['has_active'], result['has_archive']), (active, archive))
                expected = ([{'title': 'Empty', 'path': 'docs/requirements/empty.md'},
                             {'title': '로컬', 'path': 'docs/requirements/local.md', 'summary': '요약'}] if active else [])
                self.assertEqual(result['requirements'], expected)
                self.assertEqual({p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)
                self.assertFalse((root / '.git').exists())

    def test_missing_and_empty_directories_are_normal_but_access_and_document_errors_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(READER['read_requirements'](root),
                             {'has_active': False, 'has_archive': False, 'requirements': []})
            directory = root / 'docs/requirements'
            (directory / 'archive').mkdir(parents=True)
            self.assertEqual(READER['read_requirements'](root)['requirements'], [])
            for contents in [b'\xff', b'# Missing ID\n', b'---\nid: R-002\n---\n# [R-002] Body']:
                (directory / 'bad.md').write_bytes(contents)
                with (mock.patch.object(Path, 'cwd', return_value=root),
                      mock.patch.object(sys, 'argv', ['read_requirements.py']),
                      redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr):
                    self.assertEqual(READER['main'](), 1)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('Requirement 조회 오류', stderr.getvalue())
            with mock.patch.object(Path, 'iterdir', side_effect=PermissionError('denied')):
                with self.assertRaises(READER['RequirementIdError']):
                    READER['read_requirements'](root)

    def remote(self, records=None, content=None):
        records = records or [(b'100644', b'blob', 'docs/requirements/[R-001] Title.md')]
        tree = b''.join(mode + b' ' + kind + b' ' + str(index).encode() + b'\t' + path.encode() + b'\0'
                        for index, (mode, kind, path) in enumerate(records))
        values = content or {b'0': b'# [R-001] Title\n## Summary\nSummary\n## Context\nBody'}
        def git_bytes(repo, *args):
            if args[0] == 'ls-tree':
                self.assertEqual(args[4], 'pinned')
                return tree
            self.assertEqual(args[:2], ('cat-file', 'blob'))
            return values[args[2].encode()]
        return mock.Mock(side_effect=git_bytes)

    def test_remote_includes_all_active_documents_and_only_existing_summary(self):
        git_bytes = self.remote([
            (b'100644', b'blob', 'docs/requirements/changed.md'),
            (b'100755', b'blob', 'docs/requirements/unchanged.md'),
            (b'100644', b'blob', 'docs/requirements/empty.md'),
            (b'100644', b'blob', 'docs/requirements/archive/old.md'),
            (b'100644', b'blob', 'docs/REQUIREMENT.template.md')], {
                b'0': '# [R-001] 변경\n## Summary\n기존 요약\n## Context\n본문'.encode(),
                b'1': b'# [R-002] Unchanged\n## Context\nBody',
                b'2': b'# [R-003] Empty\n## Summary\n\n## Context\nBody'})
        result = requirement_overview.instruction(Path('repo'), 'pinned', git_bytes=git_bytes)
        payload = json.loads(result.splitlines()[3])
        self.assertEqual(payload['revision'], 'pinned')
        self.assertEqual(payload['requirements'], [
            {'title': '변경', 'path': 'docs/requirements/changed.md', 'summary': '기존 요약'},
            {'title': 'Unchanged', 'path': 'docs/requirements/unchanged.md'},
            {'title': 'Empty', 'path': 'docs/requirements/empty.md'}])
        self.assertEqual(git_bytes.call_count, 4)

    def test_remote_errors_never_become_missing_summaries(self):
        for raw in [b'\xff', b'# Missing ID', b'---\nid: R-001\n---\n# [R-001] Body']:
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                requirement_overview.instruction(Path('repo'), 'pinned', git_bytes=self.remote(content={b'0': raw}))
        with self.assertRaisesRegex(RuntimeError, '일반 원격 파일'):
            requirement_overview.instruction(Path('repo'), 'pinned', git_bytes=self.remote([
                (b'120000', b'blob', 'docs/requirements/link.md')]))
        with self.assertRaises(PermissionError):
            requirement_overview.instruction(Path('repo'), 'pinned', git_bytes=mock.Mock(side_effect=PermissionError('denied')))

    def test_general_and_combined_maintenance_get_same_overview_and_other_modes_do_not(self):
        effects = SimpleNamespace(planner_prompt=lambda *args: 'general',
            maintenance_planner_prompt=lambda *args, **kwargs: 'maintenance',
            is_loop_manager_repository=lambda repo: False, git_bytes=self.remote())
        with sqlite3.connect(':memory:') as connection:
            connection.row_factory = sqlite3.Row
            port = _PlanningValidationPorts(effects, connection)
            snapshot = SimpleNamespace(repo='repo', active_sha='pinned', plans=())
            with mock.patch.object(port, '_verify_request_prompt', return_value=''):
                general = port.planner_prompt(None, 'catalog', snapshot)
                combined = port.maintenance_prompt(snapshot, 'catalog', '', None, include_general_planning=True)
                calls = effects.git_bytes.call_count
                self.assertEqual(port.maintenance_prompt(snapshot, 'catalog', '', None, include_general_planning=False), 'maintenance')
                self.assertEqual(effects.git_bytes.call_count, calls)
            self.assertEqual(general.removeprefix('general'), combined.removeprefix('maintenance'))
            connection.execute('CREATE TABLE projects (repo TEXT, base_branch TEXT)')
            connection.execute("INSERT INTO projects VALUES ('repo', 'main')")
            effects.is_loop_manager_repository = lambda repo: True
            with (mock.patch('loop_manager_internal.documents.document_drafts.DocumentDraftRepository') as drafts,
                  mock.patch('loop_manager_internal.documents.document_publication.planning_instruction', return_value='draft')):
                drafts.return_value.planning_candidate.return_value = object()
                self.assertEqual(port.planner_prompt(None, 'catalog', snapshot), 'generaldraft')
                self.assertEqual(effects.git_bytes.call_count, calls)


@isolated_git_test
class PinnedRequirementOverviewTest(unittest.TestCase):
    def test_actual_git_objects_preserve_pinned_inputs_despite_new_commit_and_local_edits(self):
        # One real Git boundary proves blob/revision selection; parser combinations use mocks above.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git_command(root, 'init', '-b', 'main')
            git_command(root, 'config', 'user.email', 'test@example.com')
            git_command(root, 'config', 'user.name', 'Test')
            directory = root / 'docs/requirements'
            directory.mkdir(parents=True)
            target = directory / 'one.md'
            target.write_text('# [R-001] Pinned\n## Summary\nOld summary\n', encoding='utf-8')
            git_command(root, 'add', '.')
            git_command(root, 'commit', '-m', 'pinned')
            revision = git_command(root, 'rev-parse', 'HEAD')
            target.write_text('# [R-001] New\n## Summary\nNew summary\n', encoding='utf-8')
            git_command(root, 'commit', '-am', 'new input')
            target.write_text('invalid local edit', encoding='utf-8')
            (directory / 'untracked.md').write_text('# [R-002] Local', encoding='utf-8')
            def git_bytes(repo, *args):
                return subprocess.run(['git', *args], cwd=repo, check=True, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=git_test_environment()).stdout
            result = requirement_overview.instruction(root, revision, git_bytes=git_bytes)
            payload = json.loads(result.splitlines()[3])
            self.assertEqual(payload['revision'], revision)
            self.assertEqual(payload['requirements'], [
                {'title': 'Pinned', 'path': 'docs/requirements/one.md', 'summary': 'Old summary'}])
            self.assertEqual(target.read_text(encoding='utf-8'), 'invalid local edit')
