import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.data_publication import DataPublication, REQUIRED, current_data_root


class DataPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.share = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def fill(self, publication):
        for name in REQUIRED:
            p = publication.path/name
            p.parent.mkdir(parents=True, exist_ok=True)
            value = {'snapshot_id': publication.snapshot_id, 'fetched_at': publication.fetched_at}
            if name == 'class-images/manifest.json':value['classes'] = []
            if name == 'major-images/manifest.json':value['majors'] = []
            p.write_text(json.dumps(value) if name.endswith('.json') else 'synthetic\n')
    def publish(self, sid):
        with DataPublication(self.share, sid, str(sid)) as p:
            self.fill(p);p.commit();p.refresh_legacy_aliases()
        return p.path
    def test_atomic_pointer_and_old_generation_stays_readable(self):
        first = self.publish(1)
        second = self.publish(2)
        self.assertEqual(second, current_data_root(self.share))
        self.assertEqual(1, json.loads((first/'latest.json').read_text())['snapshot_id'])
        self.assertEqual(2, json.loads((self.share/'latest.json').read_text())['snapshot_id'])
    def test_missing_product_never_replaces_current(self):
        first = self.publish(1)
        with self.assertRaises(ValueError):
            with DataPublication(self.share, 2, '2') as p:
                self.fill(p);(p.path/'weekly.json').unlink();p.commit()
        self.assertEqual(first, current_data_root(self.share))
        self.assertFalse(p.path.exists())
    def test_mismatched_snapshot_is_rejected(self):
        with self.assertRaises(ValueError):
            with DataPublication(self.share, 1, '1') as p:
                self.fill(p);(p.path/'lookup.json').write_text('{"snapshot_id":2}');p.commit()
        self.assertFalse((self.share/'current-data').exists())
    def test_interrupt_before_commit_leaves_old_generation(self):
        first = self.publish(1)
        with self.assertRaises(RuntimeError):
            with DataPublication(self.share, 2, '2') as p:
                self.fill(p);raise RuntimeError('injected failure')
        self.assertEqual(first, current_data_root(self.share))
    def test_registry_requires_declared_data(self):
        with self.assertRaises(FileNotFoundError):
            with DataPublication(self.share, 1, '1') as p:
                self.fill(p)
                (p.path/'class-images/manifest.json').write_text(json.dumps({'snapshot_id':1,'fetched_at':'1','classes':[{'class_id':'demo','user_count':1}]}))
                p.commit()
    def test_pointer_cannot_escape_share(self):
        (self.share/'current-data').symlink_to('/tmp')
        with self.assertRaises(ValueError):current_data_root(self.share)
    def test_retention_preserves_recent_versions_and_external_history(self):
        first=self.publish(1);second=self.publish(2);third=self.publish(3)
        history=self.share/'statistics-snapshots';history.mkdir();(history/'keep').write_text('original')
        p=DataPublication(self.share,4,'4')
        with p:self.fill(p);p.commit();p.prune(keep=1,minimum_age=3600)
        self.assertTrue(first.exists())
        p.prune(keep=1,minimum_age=-1)
        self.assertFalse(first.exists());self.assertTrue(p.path.exists());self.assertTrue((history/'keep').exists())
