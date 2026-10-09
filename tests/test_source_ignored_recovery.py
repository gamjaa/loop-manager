"""Git 관측만 mock으로 제공하고 실제 ignored 판정·journal 저장을 검사한다."""
from copy import deepcopy
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest import mock

from loop_manager_internal.agents import scope_observation
from loop_manager_internal.agents.scope_contracts import GitSnapshot
from loop_manager_internal.git import git_adapter, manager_git_journal as journal, source_sync
from loop_manager_internal.runtime.contracts import BaseWorktreeSnapshot


OLD, NEW = 'a' * 40, 'b' * 40


class SourceIgnoredRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.reset_fixture()

    def reset_fixture(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.cache = self.repo / 'arbitrary.asset'
        self.cache.write_bytes(b'original')
        self.source = '.gitignore'
        self.pattern = '*'
        self.rule_blob = 'rule-blob'
        self.observation = SimpleNamespace(_handoff_run=self.check_ignore, git=self.git_rule)
        self.effects = SimpleNamespace(_normalize_legacy_ignored_snapshot=lambda *args, **kwargs:
            scope_observation._normalize_legacy_ignored_snapshot(*args, effects=self.observation, **kwargs))
        self.before = dict(head=OLD, branch='main', index={'.gitignore': ['H 100644 rule-blob 0']},
                           changes=[['ignored', 'arbitrary.asset', 'original']], operations=[],
                           config={'local:core.test': ['original'],
                                   'ignore-source:repository-exclude': [str(self.root / 'exclude'), 'original']})
        self.expected = {**deepcopy(self.before), 'head': NEW}
        self.current = deepcopy(self.before)
        self.token = None
        self.apply_calls = 0
        self.options = dict(owner='project', kind='source_fast_forward', repo=str(self.repo), target=str(self.repo),
                            before=self.before, expected=self.expected, observe=lambda: deepcopy(self.current),
                            apply=self.apply, marker=lambda intent: self.token == intent['id'],
                            state_matches=lambda intent, saved, current:
                            source_sync.state_matches(self.effects, self.repo, intent['before'], saved, current))
        self.slot = journal._slot(self.root, str(self.repo), 'source_fast_forward', str(self.repo))

    def git_rule(self, _repo, *args, **kwargs):
        return subprocess.CompletedProcess([], 0, 'rule-blob' if args[1].startswith(OLD + ':') else self.rule_blob)

    def check_ignore(self, _command, *, input, **kwargs):
        output = b''.join((self.source + '\0' + '1\0' + self.pattern + '\0' + path + '\0').encode()
                          for path in input.decode().rstrip('\0').split('\0')) if self.source else b''
        return subprocess.CompletedProcess([], 0 if output else 1, output)

    def apply(self, token):
        self.apply_calls += 1
        self.token = token
        self.current = deepcopy(self.expected)

    def change_ignored(self, action):
        if action == 'create':
            (self.repo / 'second.binary').write_bytes(b'new')
            self.current['changes'].append(['ignored', 'second.binary', 'new'])
        elif action == 'modify':
            self.cache.write_bytes(b'changed')
            self.current['changes'] = [['ignored', 'arbitrary.asset', 'changed']]
        else:
            self.cache.unlink()
            self.current['changes'] = []

    def recover(self):
        return journal.recover_active(self.root, **{key: self.options[key]
            for key in ('repo', 'kind', 'target', 'observe', 'marker', 'state_matches')})

    def interrupt_after_git(self):
        with mock.patch.object(journal, '_finish', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                journal.execute(self.root, **self.options)
        return (self.slot / 'active.json').read_bytes()

    def test_normal_completion_accepts_ignored_creation_modification_and_deletion(self):
        for action in ('create', 'modify', 'delete'):
            with self.subTest(action=action):
                self.reset_fixture()
                def apply(token):
                    self.apply(token)
                    self.change_ignored(action)
                self.options['apply'] = apply
                self.assertEqual(journal.execute(self.root, **self.options), 'applied')
                self.assertFalse((self.slot / 'active.json').exists())
                self.assertEqual(self.apply_calls, 1)
                self.assertEqual(journal._read(self.slot / f'{self.token}.result.json')['observed'], self.current)
                self.assertEqual(self.cache.exists(), action != 'delete')

    def test_unstarted_intent_resumes_after_ignored_change(self):
        original_write = journal._write_new
        def interrupt(path, value):
            original_write(path, value)
            if path.name == 'active.json':
                raise RuntimeError('before Git')
        with mock.patch.object(journal, '_write_new', side_effect=interrupt), self.assertRaises(RuntimeError):
            journal.execute(self.root, **self.options)
        self.change_ignored('modify')
        self.assertEqual(journal.execute(self.root, **self.options), 'applied')
        self.assertEqual(self.apply_calls, 1)
        self.assertEqual(self.cache.read_bytes(), b'changed')

    def test_interrupted_success_and_legacy_intent_preserve_original_evidence(self):
        for legacy in (False, True):
            for action in ('create', 'modify', 'delete'):
                with self.subTest(legacy=legacy, action=action):
                    self.reset_fixture()
                    if legacy:
                        self.before.pop('config')
                        self.expected.pop('config')
                    original = self.interrupt_after_git()
                    self.change_ignored(action)
                    if legacy and action == 'create':
                        with self.assertRaises(journal.MutationEvidenceError):
                            self.recover()
                        self.assertEqual((self.slot / 'active.json').read_bytes(), original)
                        continue
                    self.assertTrue(self.recover())
                    self.assertFalse(self.recover())
                    self.assertEqual(self.apply_calls, 1)
                    self.assertEqual((self.slot / f'{self.token}.intent.json').read_bytes(), original)
                    self.assertEqual(journal._read(self.slot / f'{self.token}.result.json')['observed'], self.current)
                    self.assertEqual(self.cache.exists(), action != 'delete')

    def test_sealed_result_is_not_rewritten_after_ignored_change(self):
        original_unlink = Path.unlink
        def interrupt(path, *args, **kwargs):
            if path == self.slot / 'active.json':
                raise OSError('after result')
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', new=interrupt), self.assertRaises(OSError):
            journal.execute(self.root, **self.options)
        result = self.slot / f'{self.token}.result.json'
        original = result.read_bytes()
        self.change_ignored('modify')
        self.assertTrue(self.recover())
        self.assertEqual(result.read_bytes(), original)
        self.assertEqual(self.cache.read_bytes(), b'changed')
        self.assertEqual(self.apply_calls, 1)

    def test_real_changes_and_missing_start_rules_or_marker_preserve_intent(self):
        for mutation in ('tracked', 'index', 'config', 'rule', 'negation', 'external-legacy', 'marker'):
            with self.subTest(mutation=mutation):
                self.reset_fixture()
                if mutation == 'external-legacy':
                    self.before.pop('config')
                    self.expected.pop('config')
                original = self.interrupt_after_git()
                self.change_ignored('modify')
                if mutation == 'tracked':
                    self.current['changes'].append(['unstaged', 'tracked.txt', 'user edit'])
                elif mutation == 'index':
                    self.current['index']['arbitrary.asset'] = ['H 100644 forced 0']
                elif mutation == 'config':
                    self.current['config']['local:core.test'] = ['changed']
                elif mutation == 'rule':
                    self.rule_blob = 'changed-rule'
                elif mutation == 'negation':
                    self.pattern = '!arbitrary.asset'
                elif mutation == 'external-legacy':
                    self.source = str(self.root / 'exclude')
                else:
                    self.token = 'external'
                with self.assertRaises(journal.MutationEvidenceError):
                    self.recover()
                self.assertEqual((self.slot / 'active.json').read_bytes(), original)
                self.assertEqual(self.cache.read_bytes(), b'changed')
                self.assertEqual(self.apply_calls, 1)

    def test_recorded_external_rules_work_but_changed_exclude_is_rejected(self):
        self.source = str(self.root / 'exclude')
        original = self.interrupt_after_git()
        self.change_ignored('modify')
        self.current['config']['ignore-source:repository-exclude'][1] = 'changed'
        with self.assertRaises(journal.MutationEvidenceError):
            self.recover()
        self.assertEqual((self.slot / 'active.json').read_bytes(), original)
        self.current['config']['ignore-source:repository-exclude'][1] = 'original'
        self.assertTrue(self.recover())

    def test_malformed_comparison_evidence_is_a_safe_journal_refusal(self):
        original = self.interrupt_after_git()
        for field, invalid in (('head', None), ('index', {'path': 3}), ('changes', [['ignored']]),
                               ('config', {'key': 'invalid'}), ('files', {'path': {}})):
            with self.subTest(field=field):
                current = {**self.current, field: invalid}
                with self.assertRaises(journal.MutationEvidenceError):
                    source_sync.state_matches(self.effects, self.repo, self.before, self.expected, current)
                self.assertEqual((self.slot / 'active.json').read_bytes(), original)

    def test_shared_rule_proof_batches_paths_and_reads_each_tracked_rule_once(self):
        index = {'.gitignore': ('H 100644 rule-blob 0',)}
        current = GitSnapshot(NEW, 'main', index, {}, {}, {}, {}, {})
        start = replace(current, head=OLD)
        saved = replace(start, dirty={f'cache/{number}.asset': (('untracked',), 'artifact') for number in range(32)})
        with mock.patch.object(self.observation, '_handoff_run', wraps=self.check_ignore) as check, \
                mock.patch.object(self.observation, 'git', wraps=self.git_rule) as git:
            normalized = scope_observation._normalize_legacy_ignored_snapshot(
                self.repo, saved, current, start=start, effects=self.observation)
        self.assertEqual(normalized.dirty, {})
        self.assertEqual(len(saved.dirty), 32)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(git.call_count, 2)

    def test_comparison_cannot_be_applied_to_other_journal_kinds(self):
        options = dict(owner='project', kind='project_fetch', repo=str(self.repo), target='refs/remotes/origin/main',
                       before={'oid': OLD}, expected={'oid': NEW}, observe=lambda: {'oid': OLD},
                       apply=mock.Mock(), marker=lambda _: True, state_matches=lambda *_: True)
        with self.assertRaises(journal.MutationEvidenceError):
            journal.execute(self.root, **options)
        options['apply'].assert_not_called()

    def test_source_audits_never_become_agent_baselines_or_gate_managed_work(self):
        self.options['apply'] = lambda token: (self.apply(token), self.change_ignored('modify'))
        journal.execute(self.root, **self.options)
        self.assertEqual(journal.verified_candidates(self.root, str(self.repo),
            baseline_refs={}, baseline_worktrees={}, live_refs={}, live_worktrees={}, marker=lambda _: False),
            {'refs': {}, 'worktrees': {}})
        (self.slot / 'active.json').write_text('damaged optional source journal')
        self.assertFalse(journal.has_pending_project_mutation(self.root, str(self.repo)))
        self.assertEqual(journal.verified_managed_transitions(self.root, str(self.repo), 'plan:G-001'), [])
        self.assertEqual(journal.verified_candidates(self.root, str(self.repo),
            baseline_refs={}, baseline_worktrees={}, live_refs={}, live_worktrees={}, marker=lambda _: False),
            {'refs': {}, 'worktrees': {}})

    def test_rebase_file_comparison_ignores_artifacts_but_preserves_tracked_bytes(self):
        self.before['index']['tracked.txt'] = ['H 100644 tracked-blob 0']
        self.before['files'] = {'tracked.txt': 'original tracked', 'arbitrary.asset': 'original'}
        self.current = deepcopy(self.before)
        self.current['files']['arbitrary.asset'] = 'updated cache'
        self.change_ignored('modify')
        self.assertTrue(source_sync.state_matches(self.effects, self.repo, self.before, self.before, self.current))
        self.current['files']['tracked.txt'] = 'user edit'
        self.assertFalse(source_sync.state_matches(self.effects, self.repo, self.before, self.before, self.current))
        self.assertEqual(self.before['files']['arbitrary.asset'], 'original')

    def test_unrelated_ignored_change_does_not_repeat_confirmed_failed_fast_forward(self):
        def fail(token):
            self.token = token
            self.apply_calls += 1
            journal._write_new(self.slot / f'{token}.invocation.json',
                {'id': token, 'returncode': 1, 'observed': deepcopy(self.current), 'locks': {}})
            raise RuntimeError('Git failed')
        self.options['apply'] = fail
        with self.assertRaises(RuntimeError):
            journal.execute(self.root, **self.options)
        original = (self.slot / 'active.json').read_bytes()
        self.change_ignored('modify')
        effects = source_sync.SynchronizeBaseWorktreeEffects(**{field.name: mock.Mock() for field in fields(
            source_sync.SynchronizeBaseWorktreeEffects)})
        effects = replace(effects, _normalize_legacy_ignored_snapshot=self.effects._normalize_legacy_ignored_snapshot)
        effects._source_transition_state.side_effect = lambda _repo: deepcopy(self.current)
        effects._source_transition_marker.return_value = False
        effects._base_worktree_snapshot.return_value = BaseWorktreeSnapshot(
            'main', OLD, tuple((k, tuple(v)) for k, v in self.current['index'].items()),
            tuple(tuple(entry) for entry in self.current['changes']), (),
            tuple((k, tuple(v)) for k, v in self.current['config'].items()))
        effects._base_worktree_index_error.return_value = None
        effects._base_paths_overlap.return_value = False
        effects._nul_git_paths.return_value = ('remote.txt',)
        effects._source_expected_index.return_value = deepcopy(self.before['index'])
        def git(_repo, *args, **kwargs):
            if args[:2] == ('rev-parse', '--git-path'):
                output = str(self.root / args[-1])
            else:
                output = OLD if args == ('rev-parse', 'HEAD') else NEW
            return subprocess.CompletedProcess([], 0, output)
        effects.git.side_effect = git
        result = source_sync.synchronize_base_worktree(effects, self.repo, 'main', state_dir=self.root)
        self.assertEqual(result.status, 'failed', result.reason)
        self.assertIn('동일 입력', result.reason)
        self.assertEqual(self.apply_calls, 1)
        self.assertEqual((self.slot / 'active.json').read_bytes(), original)
        self.assertEqual(self.cache.read_bytes(), b'changed')
        self.assertFalse(any(call.args[1] == 'merge' for call in effects.git.call_args_list))

    def test_late_ignored_changes_continue_but_same_parent_and_child_collisions_do_not_write(self):
        for path, expected_status in (('unrelated.asset', 'updated'), ('remote.txt', 'skipped'),
                                      ('remote.txt/child', 'skipped'), ('remote', 'skipped')):
            with self.subTest(path=path):
                effects = source_sync.SynchronizeBaseWorktreeEffects(**{field.name: mock.Mock() for field in fields(
                    source_sync.SynchronizeBaseWorktreeEffects)})
                effects = replace(effects, _normalize_legacy_ignored_snapshot=self.effects._normalize_legacy_ignored_snapshot)
                effects._base_worktree_index_error.return_value = None
                effects._base_paths_overlap.side_effect = lambda a, b: git_adapter._paths_overlap(a.casefold(), b.casefold())
                effects._nul_git_paths.return_value = ('remote/child' if path == 'remote' else 'remote.txt',)
                before = BaseWorktreeSnapshot('main', OLD, tuple((k, tuple(v)) for k, v in self.before['index'].items()),
                                               (), (), tuple((k, tuple(v)) for k, v in self.before['config'].items()))
                after = BaseWorktreeSnapshot(before.branch, before.head, before.index, (('ignored', path, 'new'),),
                                             before.operations, before.config)
                effects._base_worktree_snapshot.side_effect = [before, after]
                head = OLD
                def git(_repo, *args, **kwargs):
                    nonlocal head
                    if args[0] == 'merge':
                        head = NEW
                    return subprocess.CompletedProcess([], 0, head if args == ('rev-parse', 'HEAD') else NEW)
                effects.git.side_effect = git
                result = source_sync.synchronize_base_worktree(effects, self.repo, 'main', remote_sha=NEW)
                self.assertEqual(result.status, expected_status, result.reason)
                writes = [call for call in effects.git.call_args_list if call.args[1] == 'merge']
                self.assertEqual(len(writes), expected_status == 'updated')
                if writes:
                    self.assertIn('--no-overwrite-ignore', writes[0].args)
