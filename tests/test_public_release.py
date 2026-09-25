import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_public_release as gate


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        subprocess.run(['git', '-c', 'init.templateDir=', 'init', '-q', str(self.root)], check=True)

    def add(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        subprocess.run(['git', '-C', str(self.root), 'add', '--', name], check=True)
        return path

    def test_environment_variants_and_backups_block_publication(self):
        self.add('.env.example', b'OPTION=placeholder\n')
        self.add('.env.production', b'OPTION=private\n')
        self.add('ops/backups/code.orig', b'fixture\n')
        findings = gate.audit(self.root)
        self.assertTrue(any(name == '.env.production' for _, name, _ in findings))
        self.assertTrue(any(name == 'ops/backups/code.orig' for _, name, _ in findings))
        self.assertFalse(any(name == '.env.example' for _, name, _ in findings))

    def test_staged_secret_is_detected_after_worktree_becomes_clean(self):
        secret = b'-----BEGIN ' + b'PRIVATE KEY-----\nsynthetic fixture\n'
        path = self.add('config.txt', secret)
        path.write_text('clean worktree\n')
        findings = gate.audit(self.root)
        self.assertIn(('index', 'config.txt', 'private-key'), findings)
        self.assertFalse(any(scope == 'worktree' for scope, _, _ in findings))

    def test_media_approval_is_bound_to_content(self):
        self.add('assets/example.png', b'synthetic image')
        self.assertTrue(gate.audit(self.root))
        approvals = {'assets/example.png': hashlib.sha256(b'synthetic image').hexdigest()}
        (self.root / 'public-assets.json').write_text(json.dumps(approvals))
        self.assertEqual([], gate.audit(self.root))
        (self.root / 'assets/example.png').write_bytes(b'changed image')
        self.assertTrue(any(reason == 'media-approval-hash-mismatch' for _, _, reason in gate.audit(self.root)))

    def test_deleted_historical_secret_still_blocks_release(self):
        secret = b'-----BEGIN ' + b'PRIVATE KEY-----\nsynthetic\n'
        def fake_git(root, *args):
            if args[0] == 'ls-files':
                return b''
            if args == ('rev-list', '--all'):
                return b'fixturecommit\n'
            if args[0] == 'ls-tree':
                return b'100644 blob fixtureblob\tremoved.txt\0'
            if args[:2] == ('cat-file', '-s'):
                return str(len(secret)).encode()
            if args[:2] == ('cat-file', 'blob'):
                return secret
            raise AssertionError(args)
        with patch.object(gate, 'git', fake_git):
            findings = gate.audit(self.root, history=True)
        self.assertEqual([('history:fixturecommi', 'removed.txt', 'private-key')], findings)

    def test_symlink_is_not_followed(self):
        (self.root / 'link').symlink_to('/does-not-exist-private-file')
        self.assertIn(('worktree', 'link', 'symlink-needs-review'), gate.audit(self.root))
