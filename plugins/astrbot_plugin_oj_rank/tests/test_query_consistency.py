import ast
import asyncio
import csv
import io
import logging
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import rank_data as data


def snapshot():
    timestamp = '2026-09-22T08:00:00+08:00'
    row = dict(rank=1, user_id='202600000001', nickname='=1+1', accepted=1, submitted=2, ratio=50.0, level='L')
    return data.RankSnapshot(1, datetime.fromisoformat(timestamp), timestamp, '2026', 1, [row], frozenset({row['user_id']}))


class QueryConsistencyTests(unittest.TestCase):
    def test_empty_slash_is_silent_and_existing_exceptions_remain(self):
        for text in ('/', '/ ', '/\t', 'hello', '/unknown'):
            self.assertFalse(data.is_allowed_group_command(text))
        for text in ('/OJabc', '/PTA', '/群分析', '/群漫画', '/增量状态'):
            self.assertTrue(data.is_allowed_group_command(text))
        self.assertTrue(data.is_allowed_group_command('/提交记录 202600000001'))

    def test_submission_user_resolution_is_exact(self):
        source = snapshot()
        source.users[0]['first_seen_name'] = '初始名'
        self.assertEqual(source.users, data.find_exact_users(source, '202600000001'))
        self.assertEqual(source.users, data.find_exact_users(source, '=1+1'))
        self.assertEqual(source.users, data.find_exact_users(source, '初始名'))
        self.assertEqual([], data.find_exact_users(source, '2026'))

    def test_submission_response_is_strictly_validated(self):
        payload = {
            'schema_version': 1, 'user_id': '202600000001',
            'fetched_at': '2026-09-23T09:00:00+08:00',
            'records': [{
                'submission_id': 9000002, 'user_id': '202600000001',
                'problem_id': '1087', 'result': '正确', 'memory_kb': 1044,
                'time_ms': 1, 'language': 'C', 'code_length': '471 B',
                'submitted_at': '2026-01-02 12:00:00', 'judge': 'LOCAL',
            }],
        }
        fetched_at, records = data.validate_submission_response(payload, '202600000001')
        self.assertEqual(payload['fetched_at'], fetched_at)
        self.assertEqual(9000002, records[0]['submission_id'])
        with self.assertRaises(data.RankDataError):
            data.validate_submission_response({**payload, 'user_id': '202600000002'}, '202600000001')

    def test_cohort_and_historical_times_do_not_borrow_main_time(self):
        current = snapshot()
        current = replace(current, prefix_fetched_at={'2023': '2026-09-21T01:00:00+08:00'})
        row = {**current.users[0], 'user_id': '202300000001'}
        now = datetime.fromisoformat('2026-09-22T08:00:00+08:00')
        self.assertIn('09-21 01:00', data.lookup_update_text(current, row, now))
        self.assertIn('1860', data.lookup_update_text(current, row, now))
        self.assertIn('未知', data.lookup_update_text(current, current.users[0], now))
        row.update(is_historical=True, last_seen_at='2026-09-20T10:00:00+08:00')
        self.assertIn('09-20 10:00', data.lookup_update_text(current, row, now))
        self.assertIn('历史名次', data.format_user(row))

    def test_period_rejects_missing_boundary_and_never_uses_today(self):
        target = datetime.fromisoformat('2026-09-22T00:00:00+08:00')
        def ref(sid, minutes):
            when = target + timedelta(minutes=minutes)
            return data.StatisticsSnapshotReference(sid, when, when.isoformat(), 1)
        refs = (ref(1, -5), ref(2, 1))
        self.assertEqual(1, data.nearest_statistics_snapshot(refs, target, not_after=target).snapshot_id)
        with self.assertRaises(data.RankDataError):
            data.nearest_statistics_snapshot((ref(3, 400),), target)
        with self.assertRaises(data.RankDataError):
            data.nearest_statistics_snapshot((ref(1, -31), ref(2, 1)), target, not_after=target)

    def test_statistical_csv_protects_text_and_preserves_numeric_deltas(self):
        current = snapshot()
        labels = data.AcademicLabels('2026', {})
        csv_text = data.build_statistics_csv(current, labels)
        self.assertIn("'=1+1", csv_text)
        end = replace(current, snapshot_id=2, fetched_at=current.fetched_at + timedelta(days=7))
        self.assertIn("'=1+1", data.build_period_statistics_csv(current, end, '本周'))
        self.assertEqual(-2, data.spreadsheet_safe_value(-2))
        self.assertEqual("'  @SUM(1)", data.spreadsheet_safe_value('  @SUM(1)'))
        if hasattr(data, 'build_raw_snapshot_csv'):
            raw = list(csv.DictReader(io.StringIO(data.build_raw_snapshot_csv(current))))
            self.assertEqual('=1+1', raw[0]['nickname'])

    def test_image_reply_uses_verified_time_and_falls_back_on_mismatch(self):
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'OjRankPlugin')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_leaderboard_image')
        method.decorator_list = []
        namespace = dict(data=data, AstrMessageEvent=object, PLUGIN_NAME='fixture', IMAGE_MANIFEST_FILE='manifest', DATA_FILE='source',
                         RankDataError=data.RankDataError, logger=logging.getLogger('fixture'), asyncio=asyncio,
                         Plain=lambda text: ('text', text), Image=lambda *, file: ('image', file),
                         short_iso_time=lambda value: value)
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'handler', 'exec'), namespace)
        image = Path('/fixture/page.png')
        manifest = SimpleNamespace(snapshot_id=2, fetched_at='2026-09-22T08:05:00+08:00', page_path=lambda _: image)
        async def fallback(*args, **kwargs):
            yield 'fallback'
        plugin = SimpleNamespace(_load_images=lambda _: manifest, _load_scoped=lambda _: SimpleNamespace(snapshot_id=2),
                                 _page_key=lambda _: 'key', _last_views={}, _leaderboard_text=fallback)
        event = SimpleNamespace(chain_result=lambda parts: parts)
        async def run():
            return [result async for result in namespace['_leaderboard_image'](plugin, event, 1)]
        result = asyncio.run(run())
        self.assertIn('08:05', result[0][0][1])
        self.assertIn('生成时间', result[0][0][1])
        self.assertEqual(('image', str(image)), result[0][1])
        plugin._load_scoped = lambda _: SimpleNamespace(snapshot_id=3)
        self.assertEqual(['fallback'], asyncio.run(run()))


    def test_class_text_fallback_includes_every_student(self):
        tree = ast.parse((Path(__file__).parents[1] / 'main.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'OjRankPlugin')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_leaderboard_text')
        namespace = dict(data=data, AstrMessageEvent=object, DATA_FILE='data', CLASS_DATA_ROOT='/fixture/classes',
                         Path=Path, math=math, RankDataError=data.RankDataError,
                         format_user=data.format_user, short_time=lambda _: 'now')
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'handler', 'exec'), namespace)
        rows = [{**snapshot().users[0], 'rank': number, 'user_id': f'20260000{number:04d}'} for number in range(1, 62)]
        plugin = SimpleNamespace(_load_scoped=lambda _: SimpleNamespace(users=rows, user_count=61, prefix='2026'),
                                 _freshness=lambda _: '', _text_result=lambda _, text: text)
        async def run():
            return [result async for result in namespace['_leaderboard_text'](plugin, object(), 1,
                                                                              snapshot_file='/fixture/classes/2026000000.json')]
        result = asyncio.run(run())[0]
        self.assertIn('第 1/1 页', result)
        self.assertIn(rows[-1]['user_id'], result)

    def test_legacy_lookup_missing_cohort_time_is_unknown(self):
        source = snapshot()
        payload = {'schema_version': 1, 'snapshot_id': 1, 'fetched_at': source.fetched_at_text,
                   'prefix': '2023-2026', 'lookup_prefixes': ['2023', '2026'],
                   'user_count': 1, 'users': source.users}
        loaded = data._snapshot_from_payload(payload)
        self.assertEqual({}, loaded.prefix_fetched_at)
        self.assertIn('未知', data.lookup_update_text(loaded, loaded.users[0]))
