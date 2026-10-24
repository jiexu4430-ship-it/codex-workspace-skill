"""Behavior tests; every fixture stays in a temporary child of tests/."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / 'workspace' / 'scripts' / 'workspace.py'
MARKER = '.chat-workspace.json'

@unittest.skipUnless(os.name == 'nt', 'Workspace directory management targets Windows')
class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='case_', dir=HERE)
        self.addCleanup(self.tmp.cleanup)
        self.fixture = Path(self.tmp.name)
        self.base = self.fixture / 'base'
        self.base.mkdir()
        self.config = self.fixture / 'config.json'
        self.config.write_text(json.dumps({'schema_version': 1, 'allowed_roots': [str(self.base)]}), encoding='utf-8')
        self.owner = str(uuid.uuid4())

    def call(self, command, *args, ok=True, env=None):
        environ = os.environ.copy()
        for key in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
            environ.pop(key, None)
        if env:
            environ.update(env)
        proc = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPT), '--config', str(self.config), command, *map(str, args)],
                              cwd=self.fixture, env=environ, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(proc.returncode == 0, ok, proc.stdout + proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data['ok'], ok, data)
        return data

    def create(self, title='Original', thread=None):
        result = self.call('init', '--base', self.base, '--thread-id', thread or self.owner, '--title', title, '--confirm-new')
        return Path(result['root'])

    def record(self, root):
        return json.loads((root / MARKER).read_text(encoding='utf-8'))

    def snapshot(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def test_init_reuse_no_nesting_and_confirmation(self):
        self.call('init', '--base', self.base, '--thread-id', self.owner, '--title', 'Original', ok=False)
        self.assertEqual(list(self.base.iterdir()), [])
        root = self.create()
        (root / 'data.txt').write_bytes(b'preserve\x00bytes')
        before = self.snapshot()
        self.assertEqual(self.create(), root)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(list(self.base.rglob(MARKER))), 1)

    def test_rename_preserves_bytes_and_workspace_identity(self):
        root = self.create()
        (root / 'result.bin').write_bytes(bytes(range(256)))
        old = self.record(root)
        result = self.call('rename', '--base', self.base, '--thread-id', self.owner, '--title', 'Changed')
        new = Path(result['root'])
        self.assertFalse(root.exists())
        self.assertEqual((new / 'result.bin').read_bytes(), bytes(range(256)))
        record = self.record(new)
        self.assertEqual(record['workspace_id'], old['workspace_id'])
        self.assertEqual(record['owner_title'], 'Changed')
        self.assertEqual(Path(record['workspace_path']), new)
        self.assertGreater(record['revision'], old['revision'])
        self.assertIsNone(record['pending_rename'])

    def test_fork_shares_but_cannot_rename_owner_directory(self):
        root = self.create()
        child = str(uuid.uuid4())
        shared = self.call('share', '--base', self.base, '--thread-id', child, '--parent-thread-id', self.owner)
        self.assertEqual(Path(shared['root']), root)
        before = self.snapshot()
        self.call('rename', '--base', self.base, '--thread-id', child, '--title', 'Child')
        self.assertEqual(self.snapshot(), before)
        changed = self.call('rename', '--base', self.base, '--thread-id', self.owner, '--title', 'Owner changed')
        status = self.call('status', '--base', self.base, '--thread-id', child)
        self.assertEqual(status['root'], changed['root'])

    def test_same_name_chats_separate_and_unmarked_conflict(self):
        first = self.create('Shared title')
        second = self.create('Shared title', str(uuid.uuid4()))
        self.assertNotEqual(first, second)
        self.assertNotEqual(self.record(first)['workspace_id'], self.record(second)['workspace_id'])
        occupied = self.base / 'Occupied'
        occupied.mkdir()
        (occupied / 'user.txt').write_bytes(b'user original')
        before = self.snapshot()
        self.call('init', '--base', self.base, '--thread-id', str(uuid.uuid4()), '--title', 'Occupied', '--confirm-new', ok=False)
        self.call('rename', '--base', self.base, '--thread-id', self.owner, '--title', 'Occupied', ok=False)
        self.assertEqual(self.snapshot(), before)

    def test_missing_known_workspace_never_recreates(self):
        root = self.create()
        workspace_id = self.record(root)['workspace_id']
        moved = self.fixture / 'manual-move'
        root.rename(moved)
        self.call('status', '--base', self.base, '--thread-id', self.owner, '--workspace-id', workspace_id, '--known-path', root, ok=False)
        self.call('init', '--base', self.base, '--thread-id', self.owner, '--title', 'Original', '--workspace-id', workspace_id, '--known-path', root, '--confirm-new', ok=False)
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertTrue((moved / MARKER).exists())

    def test_corrupt_and_duplicate_markers_stop_mutation(self):
        root = self.create()
        duplicate = self.base / 'Duplicate'
        shutil.copytree(root, duplicate)
        before = self.snapshot()
        self.call('rename', '--base', self.base, '--thread-id', self.owner, '--title', 'Changed', ok=False)
        self.assertEqual(self.snapshot(), before)
        (duplicate / MARKER).write_text('{broken', encoding='utf-8')
        before = self.snapshot()
        self.call('init', '--base', self.base, '--thread-id', str(uuid.uuid4()), '--title', 'New', '--confirm-new', ok=False)
        self.assertEqual(self.snapshot(), before)

    def test_allowlist_and_traversal_denied(self):
        root = self.create()
        self.call('check-path', '--base', self.base, '--thread-id', self.owner, '--path', root / 'new' / 'result.txt')
        self.call('check-path', '--base', self.base, '--thread-id', self.owner, '--path', root / '..' / 'escape.txt', ok=False)
        sibling = self.fixture / 'base-sibling'
        sibling.mkdir()
        self.call('init', '--base', sibling, '--thread-id', self.owner, '--title', 'Wrong', '--confirm-new', ok=False)
        self.assertEqual(list(sibling.iterdir()), [])
        self.call('bind', '--base', self.base, '--thread-id', str(uuid.uuid4()), '--title', 'Wrong', '--path', sibling, '--user-approved', ok=False)
        self.assertFalse((sibling / MARKER).exists())

    def test_lock_requires_exact_token_and_explicit_unlock(self):
        root = self.create()
        token = str(uuid.uuid4())
        lock = root / '.workspace.lock'
        lock.write_text(json.dumps({'schema_version': 1, 'token': token, 'thread_id': self.owner, 'operation': 'rename', 'pid': 123456, 'created_at': '2020-01-01T00:00:00Z'}), encoding='utf-8')
        before = self.snapshot()
        self.call('rename', '--base', self.base, '--thread-id', self.owner, '--title', 'Changed', ok=False)
        self.call('unlock', '--base', self.base, '--thread-id', self.owner, '--token', str(uuid.uuid4()), '--user-approved', ok=False)
        self.call('unlock', '--base', self.base, '--thread-id', self.owner, '--token', token, ok=False)
        self.assertEqual(self.snapshot(), before)
        self.call('unlock', '--base', self.base, '--thread-id', self.owner, '--token', token, '--user-approved')
        self.assertFalse(lock.exists())
        self.assertTrue((root / MARKER).exists())

    def test_recover_interrupted_rename_preserves_data(self):
        root = self.create()
        (root / 'keep.txt').write_bytes(b'safe data')
        marker = self.record(root)
        marker['revision'] += 1
        marker['pending_rename'] = {'from_name': root.name, 'to_name': 'Recovered', 'requested_title': 'Recovered', 'token': str(uuid.uuid4())}
        (root / MARKER).write_text(json.dumps(marker), encoding='utf-8')
        moved = self.base / 'Recovered'
        root.rename(moved)
        recovered = self.call('recover', '--base', self.base, '--thread-id', self.owner)
        self.assertEqual(Path(recovered['root']), moved)
        final = self.record(moved)
        self.assertIsNone(final['pending_rename'])
        self.assertEqual(final['workspace_id'], marker['workspace_id'])
        self.assertEqual(final['owner_title'], 'Recovered')
        self.assertEqual(Path(final['workspace_path']), moved)
        self.assertEqual((moved / 'keep.txt').read_bytes(), b'safe data')

    def test_context_never_guesses_identity_and_rejects_mismatch(self):
        self.call('context', ok=False)
        self.call('context', ok=False, env={'CODEX_THREAD_ID': self.owner, 'CODEX_SESSION_ID': str(uuid.uuid4())})
        self.call('context', ok=False, env={'CODEX_THREAD_ID': 'invalid-identity'})
        self.assertEqual(list(self.base.iterdir()), [])

if __name__ == '__main__':
    unittest.main(verbosity=2)
