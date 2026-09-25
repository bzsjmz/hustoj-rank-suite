import gzip
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from app.publication_audit import PRODUCTS, audit


class PublicationAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "rank.db"
        with sqlite3.connect(self.db) as c:
            c.execute("create table snapshots (id integer primary key, fetched_at text, prefix text)")
            c.execute("insert into snapshots values (1, '2026-01-01T00:00:00+08:00', 'demo')")
        self.doc = {"snapshot_id": 1, "fetched_at": "2026-01-01T00:00:00+08:00"}
        for name in PRODUCTS:
            self.write(name, self.doc)
        for kind, key in (("class", "classes"), ("major", "majors")):
            self.write(f"{kind}-images/manifest.json", {**self.doc, key: [{f"{kind}_id": "abc"}]})
            self.write(f"{kind}-ranklists/abc.json", self.doc)
        self.write("statistics-snapshots.json", {"snapshots": [self.doc]})
        path = self.root / "statistics-snapshots/snapshot-1.json.gz"
        path.parent.mkdir()
        with gzip.open(path, "wt") as f:
            json.dump({**self.doc, "users": [], "user_count": 0}, f)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, doc):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc))

    def run_audit(self, **kwargs):
        return audit(self.db, self.root, "demo", **kwargs)

    def test_consistent_data_does_not_require_ready_images(self):
        result = self.run_audit(verify_snapshot=True)
        self.assertEqual("consistent", result["state"])
        self.assertEqual("unavailable", result["images"])

    def test_committed_db_with_missing_product_is_partial(self):
        (self.root / "latest.json").unlink()
        self.assertEqual("partial", self.run_audit()["state"])

    def test_fresh_publication_is_transitional_but_stale_state_is_not(self):
        (self.root / "monthly.json").unlink()
        self.write("runtime.json", {"time": time.time(), "phase": "publishing"})
        self.assertEqual("in_progress", self.run_audit(runtime=self.root / "runtime.json")["state"])
        self.write("runtime.json", {"time": time.time() - 901, "phase": "publishing"})
        self.assertEqual("partial", self.run_audit(runtime=self.root / "runtime.json")["state"])

    def test_concurrent_new_commit_is_not_reported_as_partial(self):
        with patch("app.publication_audit.latest", side_effect=[(1, self.doc["fetched_at"]), (2, "later")]):
            self.assertEqual("in_progress", self.run_audit()["state"])

    def test_corrupt_gzip_requires_explicit_verification(self):
        (self.root / "statistics-snapshots/snapshot-1.json.gz").write_bytes(b"broken")
        self.assertEqual("consistent", self.run_audit()["state"])
        self.assertEqual("partial", self.run_audit(verify_snapshot=True)["state"])

    def test_registry_cannot_escape_share_and_missing_entity_is_detected(self):
        (self.root / "class-ranklists/abc.json").unlink()
        self.assertEqual("partial", self.run_audit()["state"])
        self.write("class-images/manifest.json", {**self.doc, "classes": [{"class_id": "../private"}]})
        self.assertIn("class:invalid_registry", self.run_audit()["issues"])

    def test_missing_db_is_not_created(self):
        path = self.root / "absent.db"
        with self.assertRaises(sqlite3.Error):
            audit(path, self.root, "demo")
        self.assertFalse(path.exists())
