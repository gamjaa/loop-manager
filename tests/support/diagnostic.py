"""Shared durable diagnostic bundle fixture; no discoverable tests or Git effects."""
import hashlib
import json
from pathlib import Path
import tempfile

from loop_manager_internal.recovery import blocked_checkpoint
from loop_manager_internal.recovery import diagnostic_preservation as preservation
from loop_manager_internal.storage import blocked_checkpoint_state
from tests.support.database import runtime_connection


class ArtifactRecoveryFixture:
    def setUp(self):
        self.connection = runtime_connection()
        self.addCleanup(self.connection.close)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.identity = 'a' * 64
        self.bundle = self.state / 'diagnostic-artifacts' / self.identity
        self.contents = b'original\0bytes\xff\n'
        digest = hashlib.sha256(self.contents).hexdigest()
        original = dict(mode='100755', blob=digest,
                        oid=hashlib.sha1(b'blob ' + str(len(self.contents)).encode() + b'\0' + self.contents).hexdigest())
        self.manifest = dict(version=1, scope='plan', owner='G-047', original_branch='branch',
            revisions={'base': 'b' * 40, 'head': 'c' * 40}, detail={'reason': 'conflict'},
            files={'app.bin': dict(base={'absent': True}, head=original,
                index={'1': original, '2': original, '3': original}, worktree=original)},
            omitted={}, complete=True)
        self.assets = {'blobs/' + digest: self.contents,
                       'diff/worktree.patch': b'binary worktree diff',
                       'diff/staged.patch': b'binary staged diff',
                       'diff/unstaged.patch': b'binary unstaged diff'}
        self.saved = dict(repo='repo', scope='plan', owner='G-047', input_identity=self.identity)
        self.seal()
        payload = json.loads(self.saved['payload'])
        store = blocked_checkpoint_state.DiagnosticPublicationRepository(self.connection)
        store.preserve('repo', 'plan', 'G-047', self.identity, payload, ref=self.saved['ref'],
                       tree=self.saved['tree_oid'], commit=self.saved['commit_oid'])
        self.saved = dict(store.preservation('repo', 'plan', 'G-047', self.identity))

    def seal(self, algorithm='sha1'):
        self.assets['manifest.json'] = json.dumps(self.manifest, sort_keys=True).encode()
        tree, commit = preservation.artifact_identity(self.assets, algorithm)
        ref = blocked_checkpoint.diagnostic_branch('repo', 'G-047', 'branch',
            self.manifest['revisions']['base'], commit, tree)
        payload = dict(result='published', diagnostic=True, artifact_path=str(self.bundle),
            original_branch='branch', base_oid=self.manifest['revisions']['base'], branch=ref,
            commit_oid=commit, cause=self.manifest['detail'], cause_identity='cause',
            complete=True, omitted={})
        self.saved.update(ref=ref, tree_oid=tree, commit_oid=commit, payload=json.dumps(payload))
        self.bundle.mkdir(parents=True, exist_ok=True)
        for path in self.bundle.rglob('*'):
            if path.is_file():
                path.unlink()
        for name, content in self.assets.items():
            target = self.bundle / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

