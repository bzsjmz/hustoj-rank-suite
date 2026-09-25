from __future__ import annotations

import ast
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest

import rank_data as data


class SubmissionHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).parents[1] / "main.py").read_text())
        plugin = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                      and node.name == "OjRankPlugin")
        method = next(node for node in plugin.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name == "submission_history")
        method.decorator_list = []
        namespace = {
            "data": data,
            "AstrMessageEvent": object,
            "RankDataError": data.RankDataError,
            "find_exact_users": data.find_exact_users,
            "short_iso_time": data.short_iso_time,
            "Node": lambda **kwargs: SimpleNamespace(**kwargs),
            "Nodes": lambda **kwargs: SimpleNamespace(**kwargs),
            "Plain": lambda value: SimpleNamespace(text=value),
            "logger": logging.getLogger("submission_history_test"),
            "PLUGIN_NAME": "fixture",
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), "handler", "exec"), namespace)
        cls.handler = staticmethod(namespace["submission_history"])

    def test_returns_one_forward_with_header_and_one_node_per_record(self):
        row = {
            "rank": 1, "user_id": "202600000001", "nickname": "同学",
            "accepted": 1, "submitted": 1, "ratio": 100.0, "level": "L",
            "first_seen_name": "初始名",
        }
        snapshot = data.RankSnapshot(
            1, datetime.fromisoformat("2026-09-23T08:00:00+08:00"),
            "2026-09-23T08:00:00+08:00", "2026", 1, [row],
            frozenset({row["user_id"]}),
        )
        records = [{
            "submission_id": 42, "user_id": row["user_id"], "problem_id": "1087",
            "result": "正确", "memory_kb": 1044, "time_ms": 1,
            "language": "C", "code_length": "471 B",
            "submitted_at": "2026-01-02 12:00:00", "judge": "LOCAL",
        }]

        async def request(_user_id):
            return "2026-09-23T09:00:00+08:00", records

        plugin = SimpleNamespace(
            _allowed=lambda _event: True, _load_lookup=lambda: snapshot,
            _request_submissions=request, _text_result=lambda _event, text: text,
        )
        event = SimpleNamespace(
            get_self_id=lambda: "123", chain_result=lambda components: components,
        )

        async def run():
            return [result async for result in self.handler(plugin, event, "初始名")]

        result = asyncio.run(run())
        forwarded = result[0][0]
        self.assertEqual(2, len(forwarded.nodes))
        self.assertIn("同学", forwarded.nodes[0].content[0].text)
        self.assertIn("#42｜题目 1087", forwarded.nodes[1].content[0].text)

    def test_ambiguous_nickname_does_not_query_source(self):
        rows = [{"user_id": f"20260000000{n}", "nickname": "同名"} for n in (1, 2)]
        called = False

        async def request(_user_id):
            nonlocal called
            called = True
            return "", []

        plugin = SimpleNamespace(
            _allowed=lambda _event: True,
            _load_lookup=lambda: SimpleNamespace(users=rows),
            _request_submissions=request, _text_result=lambda _event, text: text,
        )

        async def run():
            return [result async for result in self.handler(plugin, SimpleNamespace(), "同名")]

        result = asyncio.run(run())
        self.assertFalse(called)
        self.assertIn("完整学号", result[0])
