import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1]))
from rank_data import load_snapshot, RankDataError, is_allowed_group_command


def payload():
    row = dict(rank=1, user_id='202600000001', nickname='fixture', accepted=20,
               accepted_change=20, submitted=0, ratio=0, level='', date='2026-10-11',
               start_at='2026-10-10T23:55:00+08:00', end_at='2026-10-11T23:55:00+08:00')
    return dict(schema_version=1, board='historical-daily', snapshot_id=5,
                fetched_at='2026-10-13T00:05:00+08:00', prefix='2026',
                user_count=2, record_count=2, users=[row, {**row, 'date': '2026-10-12'}])


class DailyDataTests(unittest.TestCase):
    def load(self, doc):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'historical-daily.json'
            path.write_text(json.dumps(doc))
            return load_snapshot(path, allow_empty=True)

    def test_same_user_on_distinct_dates_allowed(self):
        self.assertEqual(2, self.load(payload()).user_count)
        self.assertTrue(is_allowed_group_command('/历史单日榜 2'))

    def test_duplicate_identity_invalid_date_or_invalid_gain_rejected(self):
        for field, value in [('date', '2026-10-11'), ('date', '2026-02-30'),
                             ('accepted_change', -1), ('rank', 2)]:
            doc = payload()
            doc['users'][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RankDataError):
                self.load(doc)

    def test_normal_leaderboard_still_rejects_duplicate_user(self):
        doc = payload()
        del doc['board']
        with self.assertRaises(RankDataError):
            self.load(doc)

    def test_empty_history_is_valid(self):
        doc = payload()
        doc.update(users=[], user_count=0, record_count=0)
        self.assertEqual([], self.load(doc).users)

class DailyCommandTests(unittest.TestCase):
    def method(self, name, extra):
        import ast
        import math
        import rank_data
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'OjRankPlugin')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        method.decorator_list = []
        namespace = dict(data=rank_data, AstrMessageEvent=object, Path=Path, math=math,
                         RankDataError=RankDataError, IMAGE_MANIFEST_FILE='manifest', DATA_FILE='source',
                         CLASS_DATA_ROOT='/fixture/classes', format_user=rank_data.format_user,
                         short_time=lambda _: 'now')
        namespace.update(extra)
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'handler', 'exec'), namespace)
        return namespace[name]

    def test_text_fallback_includes_record_date_gain_and_interval(self):
        import asyncio
        from types import SimpleNamespace
        method = self.method('_leaderboard_text', {})
        doc = payload()
        plugin = SimpleNamespace(_load_scoped=lambda _: SimpleNamespace(users=doc['users'], user_count=2, prefix='2026'),
                                 _freshness=lambda _: '', _text_result=lambda _, text: text)
        async def run():
            return [r async for r in method(plugin, object(), 1, snapshot_file='/fixture/historical-daily.json')]
        text = asyncio.run(run())[0]
        for phrase in ('2026-10-11', '2026-10-12', '新增 20 题', '实际区间', '共 2 条记录'):
            self.assertIn(phrase, text)

    def test_turn_page_without_image_manifest_uses_source_record_count(self):
        import asyncio
        from types import SimpleNamespace
        method = self.method('turn_page', {})
        calls = []
        def no_manifest(_):
            raise RankDataError('not rendered')
        async def image(*args):
            calls.append(args)
            yield 'ok'
        plugin = SimpleNamespace(_allowed=lambda _: True, _page_key=lambda _: 'fixture',
            _last_views={'fixture': ('manifest', '/fixture/historical-daily.json', '历史单日新增总榜', 1, 'historical-daily', None)},
            _load_images=no_manifest, _load_scoped=lambda _: SimpleNamespace(user_count=21),
            _leaderboard_image=image, _text_result=lambda _, text: text)
        async def run():
            return [r async for r in method(plugin, object())]
        self.assertEqual(['ok'], asyncio.run(run()))
        self.assertEqual(2, calls[0][1])
        self.assertEqual(2, plugin._last_views['fixture'][3])
        self.assertEqual(['ok'], asyncio.run(run()))
        self.assertEqual(1, calls[1][1])
