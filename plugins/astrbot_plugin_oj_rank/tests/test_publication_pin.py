import json
import tempfile
import unittest
from pathlib import Path
from publication import pin_publication, published_path


class PublicationPinTests(unittest.TestCase):
    def test_command_keeps_one_generation_during_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);versions=root/'.data-versions';versions.mkdir()
            for n in ['one','two']:
                (versions/n).mkdir();(versions/n/'latest.json').write_text(n)
            (root/'current-data').symlink_to('.data-versions/one')
            with pin_publication(root):
                self.assertEqual('one',published_path(root/'latest.json').read_text())
                (root/'next').symlink_to('.data-versions/two');(root/'next').replace(root/'current-data')
                self.assertEqual('one',published_path(root/'latest.json').read_text())
                self.assertEqual(root/'statistics-snapshots.json',published_path(root/'statistics-snapshots.json'))
                self.assertEqual(root/'rank-images/manifest.json',published_path(root/'rank-images/manifest.json'))
            with pin_publication(root):self.assertEqual('two',published_path(root/'latest.json').read_text())
    def test_legacy_paths_and_context_reset_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(RuntimeError):
                with pin_publication(root):
                    self.assertEqual(root/'daily.json',published_path(root/'daily.json'));raise RuntimeError()
            self.assertEqual(root/'daily.json',published_path(root/'daily.json'))
