from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.models import RankEntry
from app.renderer import LeaderboardRenderer


class FakePage:
    def __init__(self):
        self.contents: list[str] = []
        self.viewport = None
        self.closed = False
        self.evaluation_count = 0

    def set_viewport_size(self, viewport):
        self.viewport = viewport

    def set_content(self, content: str, wait_until: str):
        self.contents.append(content)

    def screenshot(self, path: str, **_kwargs):
        Path(path).write_bytes(b"fake-png")

    def evaluate(self, _script: str):
        self.evaluation_count += 1
        return {"tableBottom": 1500, "lastRowBottom": 1490}

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.page = FakePage()

    def new_page(self):
        return self.page


def entry(rank: int, nickname: str = "昵称") -> RankEntry:
    return RankEntry(rank, f"2026{rank:08d}", nickname, rank, rank + 1, 50.0, "L1")


class RendererTests(unittest.TestCase):
    def test_html_escapes_user_content_and_contains_page_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = LeaderboardRenderer(Path(directory))
            content = renderer.build_page_html(
                [entry(1, "<script>alert(1)</script>")],
                "2026",
                "2026-08-23T12:00:00+08:00",
                9,
                1,
                58,
                1157,
            )
            self.assertIn("&lt;script&gt;", content)
            self.assertNotIn("<script>alert", content)
            self.assertIn("第 1 / 58 页", content)
            self.assertIn("排名 1–1", content)
            self.assertIn("2026-08-23 12:00", content)
            self.assertNotIn("2026-08-23T12:00:00+08:00", content)

    def test_standard_board_reserves_space_for_long_nicknames(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = LeaderboardRenderer(Path(directory))
            nickname = "示例长昵称(这是用于验证布局的虚构文本ABCDEFGHIJ)"
            content = renderer.build_page_html(
                [entry(1, nickname)], "2026", "2026-09-06T10:19:00+08:00",
                2286, 1, 1, 1,
            )

            self.assertIn(nickname, content)
            self.assertIn(
                "66px 162px minmax(400px, 1fr) 68px 78px 88px 118px",
                content,
            )
            self.assertIn(
                ".nickname { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #fff; font-size: 16px;",
                content,
            )
            self.assertEqual(4, renderer.LAYOUT_VERSION)

    def test_renders_twenty_per_page_and_publishes_manifest_last(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = LeaderboardRenderer(Path(directory))
            context = FakeContext()
            entries = [entry(rank) for rank in range(1, 42)]

            manifest = renderer.render(
                context,
                entries,
                "2026",
                "2026-08-23T12:00:00+08:00",
                9,
            )

            self.assertEqual(3, manifest["page_count"])
            self.assertEqual(20, manifest["page_size"])
            image_dir = Path(directory) / "rank-images" / "pages"
            self.assertEqual(3, len(list(image_dir.glob("page-*.png"))))
            saved = json.loads(
                (Path(directory) / "rank-images" / "manifest.json").read_text()
            )
            self.assertEqual(9, saved["snapshot_id"])
            self.assertEqual(3, context.page.evaluation_count)
            self.assertTrue(context.page.closed)


    def test_renders_a_class_of_sixty_on_one_tall_page(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = LeaderboardRenderer(
                Path(directory), "class-images/2026072306", page_size=60,
                height=LeaderboardRenderer.class_image_height(60, False),
            )
            context = FakeContext()
            manifest = renderer.render(
                context,
                [entry(rank) for rank in range(1, 61)],
                "2026",
                "2026-08-23T12:00:00+08:00",
                9,
                title="2026072306 班级榜单",
            )

            self.assertEqual(1, manifest["page_count"])
            self.assertEqual(60, manifest["page_size"])
            self.assertEqual(4000, context.page.viewport["height"])
            self.assertIn("2026072306 班级榜单", context.page.contents[0])


    def test_class_history_uses_blue_name_and_disclaimer(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = LeaderboardRenderer(Path(directory))
            historical = entry(2, "历史同学")
            content = renderer.build_page_html(
                [entry(1), historical],
                "2026",
                "2026-08-23T10:00:00+08:00",
                9,
                1,
                1,
                2,
                historical_user_ids={historical.user_id},
                subtitle="当前 ranklist 1 人｜历史补全 1 人｜共 2 名选手",
            )
            self.assertIn("historical-nickname", content)
            self.assertIn("historical-row", content)
            self.assertIn("history-banner", content)
            self.assertIn("重要提示", content)
            self.assertIn("蓝色姓名的 1 位同学", content)
            self.assertIn("历史</span>", content)
            self.assertIn("当前 ranklist 无法获取", content)
            self.assertIn("当前 ranklist 1 人", content)

    def test_class_image_height_fits_content_without_large_blank_area(self):
        self.assertEqual(4000, LeaderboardRenderer.class_image_height(60, False))
        self.assertEqual(2147, LeaderboardRenderer.class_image_height(27, True))
        self.assertLess(
            LeaderboardRenderer.class_image_height(27, True),
            LeaderboardRenderer.class_image_height(60, False),
        )

    def test_intensity_html_uses_compact_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = LeaderboardRenderer(Path(directory))
            content = renderer.build_page_html(
                [entry(1, "卷王（10 AC）")], "2026", "2026-08-23T12:00:00+08:00",
                9, 1, 1, 1, title="最卷班级",
                column_labels=("班级", "班级 AC 总量", "班级卷王"),
                entity_type="班级",
            )
            self.assertIn("班级 AC 总量", content)
            self.assertIn("班级卷王", content)
            self.assertIn("共 1 个班级", content)
            self.assertIn("每页 20 个班级", content)
            self.assertNotIn("名选手", content)
            self.assertNotIn("通过率</div>", content)

            major_content = renderer.build_page_html(
                [entry(1, "卷王班级与同学")], "2026",
                "2026-08-23T12:00:00+08:00", 9, 1, 1, 1,
                title="最卷专业",
                column_labels=("专业", "专业 AC 总量", "卷王班级与同学"),
                entity_type="专业",
            )
            self.assertIn("共 1 个专业", major_content)
            self.assertIn("每页 20 个专业", major_content)
            self.assertNotIn("名选手", major_content)

if __name__ == "__main__":
    unittest.main()
