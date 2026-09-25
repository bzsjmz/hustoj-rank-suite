import json
import logging
import tempfile
import unittest
from datetime import datetime
from dataclasses import asdict
from pathlib import Path

from app.exporter import RankExporter
from app.main import _due_lookup_prefixes
from app.models import RankEntry
from app.render_service import OnDemandRenderer
from app.renderer import LeaderboardRenderer
from tests import test_crawler, test_renderer


class PresentationConsistencyTests(unittest.TestCase):
    def test_cold_lookup_does_not_mark_unfetched_cohorts_successful(self):
        with tempfile.TemporaryDirectory() as directory:
            exporter = RankExporter(Path(directory))
            exporter.export_lookup([RankEntry(1, '202600000001', 'synthetic', 1, 2, 50, 'L')],
                                   '2026-09-22T08:00:00+08:00', 1,
                                   ('2023', '2024', '2025', '2026'),
                                   prefix_fetched_at={'2026': '2026-09-22T08:00:00+08:00'})
            _, times = exporter.load_lookup_current_entries(('2023', '2024', '2025', '2026'))
            self.assertEqual({'2026'}, set(times))
            self.assertEqual(('2023',), _due_lookup_prefixes(('2023', '2024', '2025', '2026'), '2026', times,
                                                          datetime.fromisoformat('2026-09-22T08:01:00+08:00')))
            path = Path(directory) / 'lookup.json'
            payload = json.loads(path.read_text())
            del payload['prefix_fetched_at']
            path.write_text(json.dumps(payload))
            self.assertEqual({}, exporter.load_lookup_current_entries(('2026',))[1])

    def test_same_content_reuses_image_with_original_generation_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = LeaderboardRenderer(Path(directory))
            context = test_renderer.FakeContext()
            rows = [test_renderer.entry(1)]
            first = renderer.render(context, rows, '2026', '2026-09-22T08:00:00+08:00', 1)
            self.assertIn('图片生成快照', context.page.contents[0])
            second_context = test_renderer.FakeContext()
            second = renderer.render(second_context, rows, '2026', '2026-09-22T08:05:00+08:00', 2)
            self.assertEqual(0, second['rendered_page_count'])
            self.assertEqual([], second_context.page.contents)
            self.assertEqual(first['pages'], second['pages'])
            self.assertEqual(1, second['pages']['1']['rendered_snapshot_id'])
            self.assertEqual('2026-09-22T08:00:00+08:00', second['pages']['1']['rendered_fetched_at'])
            self.assertEqual(2, second['snapshot_id'])
            changed = renderer.render(test_renderer.FakeContext(), [test_renderer.entry(1, 'changed')],
                                      '2026', '2026-09-22T08:10:00+08:00', 3)
            self.assertEqual(1, changed['rendered_page_count'])
            self.assertEqual(3, changed['pages']['1']['rendered_snapshot_id'])

    def test_class_over_sixty_remains_one_complete_page(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = test_crawler.CrawlerTests().settings(directory)
            path = settings.share_dir / 'class-ranklists' / '2026000000.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'schema_version': 1, 'user_count': 61, 'historical_user_count': 1, 'users': [asdict(test_renderer.entry(i)) for i in range(1, 62)]}))
            service = OnDemandRenderer(settings, None, logging.getLogger('fixture'))
            definition = service.definition('class', '2026000000')
            self.assertEqual(61, definition.page_size)
            renderer = LeaderboardRenderer(Path(directory) / 'images', page_size=definition.page_size,
                                           height=definition.height)
            result = renderer.render(test_renderer.FakeContext(), [test_renderer.entry(i) for i in range(1, 62)],
                                     '2026', '2026-09-22T08:00:00+08:00', 1)
            self.assertEqual(1, result['page_count'])
            self.assertEqual(61, result['user_count'])
