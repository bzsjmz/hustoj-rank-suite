from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.render_service import OnDemandRenderer, RenderRequest, SingleFlightQueue


class RenderServiceTests(unittest.TestCase):
    def test_singleflight_merges_concurrent_identical_requests(self) -> None:
        coordinator = SingleFlightQueue()
        request = RenderRequest("total", None, 6)
        first, first_created = coordinator.submit(request)
        second, second_created = coordinator.submit(request)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertIs(first, second)
        queued, _ = coordinator.queue.get_nowait()
        self.assertEqual(request, queued)
        coordinator.complete(request, {"ok": True, "relative_path": "x.png"})
        self.assertTrue(second.wait(0.1)["ok"])

    def test_board_definition_rejects_untrusted_entity_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings.from_env()
            object.__setattr__(settings, "share_dir", root)
            renderer = OnDemandRenderer(settings, object(), object())

            with self.assertRaises(ValueError):
                renderer.definition("class", "../../etc/passwd")
            with self.assertRaises(ValueError):
                renderer.definition("unknown")


if __name__ == "__main__":
    unittest.main()
