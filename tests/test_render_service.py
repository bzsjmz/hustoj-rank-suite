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

    def test_historical_daily_renders_records_with_date_and_repeat_user(self):
        import json
        import logging
        from tests.test_renderer import FakeContext
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings.from_env()
            object.__setattr__(settings, "share_dir", root)
            rows = [dict(rank=i, user_id="202600000001", nickname="<fixture>",
                         date=f"2026-09-{i:02d}", accepted=100-i, accepted_change=100-i,
                         submitted=0, ratio=0, level="") for i in range(1, 22)]
            payload = dict(schema_version=1, board="historical-daily", snapshot_id=7,
                           fetched_at="2026-10-13T08:00:00+08:00", prefix="2026",
                           user_count=len(rows), record_count=len(rows), users=rows)
            (root / "historical-daily.json").write_text(json.dumps(payload))
            context = FakeContext()
            renderer = OnDemandRenderer(settings, context, logging.getLogger("fixture"))
            response = renderer.render(RenderRequest("historical-daily", None, 2))
            self.assertEqual(2, response["page_count"])
            self.assertTrue((root / response["relative_path"]).exists())
            html = context.page.contents[0]
            for text in ("历史单日新增总榜", "2026-09-21", "202600000001", "&lt;fixture&gt;", "新增 AC", "条记录", "记录序号 21–21"):
                self.assertIn(text, html)
            # Asking again reuses content-addressed images.
            self.assertEqual(0, renderer.render(RenderRequest("historical-daily", None, 2))["rendered_page_count"])


if __name__ == "__main__":
    unittest.main()
