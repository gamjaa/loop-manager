import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import manager_git_journal as journal
from runtime_scope import proven_runtime_recreation


class RuntimeRecreationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.source = Path('/source')
        self.runtime = Path('/state/runtime')
        self.sha = 'a' * 40
        self.old = (f'HEAD={self.sha}', 'detached=True', 'locked=loop-manager-runtime:old', 'state=same')
        self.new = tuple('locked=loop-manager-runtime:new' if x.startswith('locked=') else x for x in self.old)
        self.validate_lock = mock.Mock()
        identity = 'removed'
        before = {'exists': True, 'registration': {
            'head': self.sha, 'detached': True, 'locked': 'loop-manager-runtime:old'}}
        self.intent = {'version': 1, 'id': identity, 'owner': 'bootstrap',
                       'kind': 'runtime_remove', 'repo': str(self.source),
                       'target': str(self.runtime), 'before': before,
                       'expected': {'exists': False, 'registration': None}}
        self.result = {'id': identity, 'status': 'complete',
                       'observed': self.intent['expected'],
                       'candidates': {'refs': {}, 'worktrees': {str(self.runtime): None}}}
        self.receipt = {'id': identity, 'repo': str(self.source),
                        'target': str(self.runtime), 'before': before}
        slot = journal._slot(self.state, str(self.source), 'runtime_remove', str(self.runtime))
        self.intent_path = slot / f'{identity}.intent.json'
        self.result_path = slot / f'{identity}.result.json'
        self.receipt_path = self.state / 'runtime-removals' / f'{identity}.json'
        for p, data in ((self.intent_path, self.intent), (self.result_path, self.result),
                        (self.receipt_path, self.receipt)):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data))

    def proven(self, current=None):
        return proven_runtime_recreation(
            self.state, self.source, self.runtime, self.sha, self.old,
            self.new if current is None else current, validate_lock=self.validate_lock)

    def test_owned_completed_removal_proves_only_lock_replacement(self):
        self.assertTrue(self.proven())
        self.validate_lock.assert_called_once_with('loop-manager-runtime:old')

    def test_missing_receipt_or_result_does_not_prove_recreation(self):
        for path in (self.receipt_path, self.result_path):
            with self.subTest(path=path):
                saved = path.read_text()
                path.unlink()
                self.assertFalse(self.proven())
                path.write_text(saved)

    def test_unproven_previous_creation_is_rejected(self):
        self.validate_lock.side_effect = RuntimeError('unknown creation')
        self.assertFalse(self.proven())

    def test_changed_runtime_state_is_rejected(self):
        for field in ('state=different', 'HEAD=' + 'b' * 40, 'detached=False', 'branch=refs/heads/main'):
            with self.subTest(field=field):
                key = field.split('=')[0] + '='
                changed = tuple(x for x in self.new if not x.startswith(key)) + (field,)
                self.assertFalse(self.proven(changed))
        self.validate_lock.assert_not_called()

    def test_tampered_journal_and_receipt_are_rejected(self):
        for path, original, key, value in (
            (self.intent_path, self.intent, 'version', 2),
            (self.intent_path, self.intent, 'owner', 'worker'),
            (self.intent_path, self.intent, 'target', '/other'),
            (self.result_path, self.result, 'status', 'aborted'),
            (self.result_path, self.result, 'candidates', {}),
            (self.result_path, self.result, 'observed', {'exists': True, 'registration': None}),
            (self.receipt_path, self.receipt, 'repo', '/other'),
        ):
            with self.subTest(path=path, key=key):
                path.write_text(json.dumps({**original, key: value}))
                self.assertFalse(self.proven())
                path.write_text(json.dumps(original))
