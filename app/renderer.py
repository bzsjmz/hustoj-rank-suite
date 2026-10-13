from __future__ import annotations

import html
import hashlib
import json
import math
import re
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import AbstractSet, Sequence

from .models import RankEntry
from .student_ids import StudentIdLayout


class LeaderboardRenderer:
    LAYOUT_VERSION = 4
    PAGE_SIZE = 20
    WIDTH = 1200
    HEIGHT = 1600
    CLASS_MIN_HEIGHT = 1500
    CLASS_ROW_HEIGHT = 59
    CLASS_BASE_HEIGHT = 460
    HISTORY_NOTICE_HEIGHT = 94

    @classmethod
    def class_image_height(cls, entry_count: int, has_historical_entries: bool) -> int:
        """Size a one-page class image to its actual content without wasted space."""
        if entry_count <= 0:
            raise ValueError("class entry count must be positive")
        return max(
            cls.CLASS_MIN_HEIGHT,
            cls.CLASS_BASE_HEIGHT + cls.CLASS_ROW_HEIGHT * entry_count
            + (cls.HISTORY_NOTICE_HEIGHT if has_historical_entries else 0),
        )

    def __init__(
        self,
        share_dir: Path,
        image_directory: str = "rank-images",
        page_size: int = PAGE_SIZE,
        height: int = HEIGHT,
    ):
        relative_directory = Path(image_directory)
        if relative_directory.is_absolute() or ".." in relative_directory.parts:
            raise ValueError("image directory must be a relative child of share_dir")
        if page_size <= 0 or height <= 0:
            raise ValueError("page size and image height must be positive")
        self.root = share_dir / relative_directory
        self.page_size = page_size
        self.height = height

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o644)
        temporary.replace(path)
        path.chmod(0o644)

    @staticmethod
    def _display_ratio(value: float) -> str:
        return f"{value:.1f}%"

    @staticmethod
    def _display_time(value: str) -> str:
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return value

    @staticmethod
    def _rank_class(rank: int) -> str:
        return {1: "rank-gold", 2: "rank-silver", 3: "rank-bronze"}.get(rank, "")

    def build_page_html(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        page_number: int,
        page_count: int,
        total_users: int,
        title: str = "天梯总榜",
        column_labels: tuple[str, str, str] | None = None,
        historical_user_ids: AbstractSet[str] = frozenset(),
        subtitle: str | None = None,
        entity_type: str | None = None,
    ) -> str:
        intensity = column_labels is not None
        historical_user_id_set = frozenset(historical_user_ids)
        grid_template = (
            "82px 220px 170px minmax(300px, 1fr)"
            if intensity
            else "66px 162px minmax(400px, 1fr) 68px 78px 88px 118px"
        )
        table_head = (
            '<div class="table-head"><div>排名</div>'
            f'<div>{html.escape(column_labels[0])}</div>'
            f'<div style="text-align:right">{html.escape(column_labels[1])}</div>'
            f'<div>{html.escape(column_labels[2])}</div></div>'
            if column_labels
            else ('<div class="table-head"><div>排名</div><div>学号</div><div>昵称</div>'
                  '<div style="text-align:right">AC</div><div style="text-align:right">提交</div>'
                  '<div style="text-align:right">通过率</div><div style="text-align:right">等级</div></div>')
        )
        historical_count = len(historical_user_id_set)
        history_notice = (
            '<aside class="history-banner">'
            '<span class="history-badge">重要提示</span>'
            f'<strong>蓝色姓名的 {historical_count} 位同学暂未出现在当前 ranklist</strong>'
            '<small>这些同学曾在历史快照出现；下方成绩为最近一次记录。</small>'
            '</aside>'
            if historical_user_id_set
            else ""
        )
        rows = []
        for entry in entries:
            is_historical = entry.user_id in historical_user_id_set
            row_class = "historical-row" if is_historical else self._rank_class(entry.rank)
            nickname = html.escape(entry.nickname or "未设置昵称")
            nickname_text = nickname + ("（历史）" if is_historical else "")
            rank_text = "历史" if is_historical else str(entry.rank)
            nickname_class = "nickname historical-nickname" if is_historical else "nickname"
            if intensity:
                rows.append(
                    f"""
                    <div class="rank-row {row_class}">
                      <div class="rank"><span>{rank_text}</span></div>
                      <div class="user-id">{html.escape(entry.user_id)}</div>
                      <div class="number accepted">{entry.accepted}</div>
                      <div class="{nickname_class}" title="{nickname_text}">{nickname_text}</div>
                    </div>
                    """
                )
                continue
            level = html.escape(entry.level or "-")
            rows.append(
                f"""
                <div class="rank-row {self._rank_class(entry.rank)}">
                  <div class="rank"><span>{rank_text}</span></div>
                  <div class="user-id">{html.escape(entry.user_id)}</div>
                  <div class="{nickname_class}" title="{nickname_text}">{nickname_text}</div>
                  <div class="number accepted">{entry.accepted}</div>
                  <div class="number">{entry.submitted}</div>
                  <div class="number ratio">{self._display_ratio(entry.ratio)}</div>
                  <div class="level">{level}</div>
                </div>
                """
            )

        first_rank = (page_number - 1) * self.page_size + 1
        last_rank = first_rank + len(entries) - 1
        count_label = "条记录" if entity_type == "记录" else (f"个{entity_type}" if entity_type else "名选手")
        page_count_label = "条记录" if entity_type == "记录" else (f"个{entity_type}" if entity_type else "人")
        position_label = "记录序号 " if entity_type == "记录" else "排名 "
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; width: {self.WIDTH}px; height: {self.height}px; overflow: hidden; }}
body {{
  font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  color: #e9f7ff;
  background:
    radial-gradient(circle at 86% 5%, rgba(0, 229, 255, .18), transparent 28%),
    radial-gradient(circle at 8% 92%, rgba(47, 107, 255, .16), transparent 30%),
    linear-gradient(145deg, #07101f 0%, #0b1830 48%, #081322 100%);
}}
.noise {{ position: absolute; inset: 0; opacity: .13; background-image: linear-gradient(rgba(255,255,255,.025) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,.025) 1px, transparent 1px); background-size: 34px 34px; }}
.frame {{ position: relative; width: 100%; height: 100%; padding: 42px 48px 38px; display: flex; flex-direction: column; }}
.frame::before {{ content: ""; position: absolute; inset: 20px; border: 1px solid rgba(78, 214, 255, .20); border-radius: 28px; pointer-events: none; }}
.header {{ height: 184px; display: flex; justify-content: space-between; align-items: flex-start; }}
.eyebrow {{ color: #52d9ff; font-size: 18px; font-weight: 700; letter-spacing: 5px; margin-bottom: 8px; }}
h1 {{ margin: 0; font-size: 52px; line-height: 1.12; letter-spacing: 3px; text-shadow: 0 0 24px rgba(54, 211, 255, .22); }}
.subtitle {{ margin-top: 13px; color: #89a6bd; font-size: 20px; }}
.snapshot {{ min-width: 250px; padding: 18px 22px; text-align: right; border: 1px solid rgba(85, 218, 255, .28); border-radius: 18px; background: rgba(11, 31, 55, .72); box-shadow: inset 0 0 26px rgba(41, 177, 255, .06); }}
.snapshot b {{ display: block; color: #fff; font-size: 28px; }}
.snapshot span {{ color: #718ea7; font-size: 16px; }}
.table {{ flex: 1; min-height: 0; border: 1px solid rgba(75, 188, 231, .22); border-radius: 20px; background: rgba(4, 16, 31, .72); overflow: hidden; box-shadow: 0 22px 55px rgba(0,0,0,.22); }}
.table-head, .rank-row {{ display: grid; grid-template-columns: {grid_template}; column-gap: 10px; align-items: center; padding: 0 22px; }}
.table-head {{ height: 50px; color: #6f91aa; background: rgba(29, 70, 102, .34); font-size: 15px; font-weight: 700; letter-spacing: 2px; border-bottom: 1px solid rgba(83, 205, 247, .18); }}
.rank-row {{ height: 59px; font-size: 18px; border-bottom: 1px solid rgba(111, 181, 211, .10); }}
.rank-row:nth-child(odd) {{ background: rgba(29, 64, 94, .11); }}
.rank-row:last-child {{ border-bottom: 0; }}
.rank {{ display: flex; align-items: center; }}
.rank span {{ width: 42px; height: 42px; display: grid; place-items: center; border-radius: 13px; color: #8db0c8; background: #10263d; font-weight: 900; }}
.rank-gold {{ background: linear-gradient(90deg, rgba(255,190,48,.16), transparent 68%) !important; }}
.rank-silver {{ background: linear-gradient(90deg, rgba(194,220,239,.13), transparent 68%) !important; }}
.rank-bronze {{ background: linear-gradient(90deg, rgba(224,139,82,.14), transparent 68%) !important; }}
.rank-gold .rank span {{ color: #2b1b00; background: linear-gradient(145deg, #ffe480, #eaa927); box-shadow: 0 0 19px rgba(255,194,55,.32); }}
.rank-silver .rank span {{ color: #17222c; background: linear-gradient(145deg, #f0f7ff, #9eb4c8); }}
.rank-bronze .rank span {{ color: #24130c; background: linear-gradient(145deg, #f0ad77, #a95b34); }}
.user-id {{ color: #c8e9fa; font-variant-numeric: tabular-nums; font-weight: 650; }}
.nickname {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #fff; font-size: 16px; font-weight: 700; }}
.historical-nickname {{ color: #57d9ff; }}
.historical-row {{ background: linear-gradient(90deg, rgba(49, 176, 255, .25), rgba(29, 104, 177, .10) 68%, transparent) !important; box-shadow: inset 5px 0 0 #38c7ff; }}
.historical-row .rank span {{ color: #06243a; background: linear-gradient(145deg, #82e7ff, #38aee9); box-shadow: 0 0 16px rgba(64, 204, 255, .22); }}
.history-banner {{ min-height: 78px; margin: 0 0 16px; padding: 12px 18px; display: flex; align-items: center; gap: 15px; border: 1px solid rgba(83, 214, 255, .82); border-radius: 16px; background: linear-gradient(90deg, rgba(18, 124, 184, .54), rgba(22, 69, 124, .46)); box-shadow: 0 0 28px rgba(53, 193, 255, .22), inset 0 0 22px rgba(110, 222, 255, .12); }}
.history-badge {{ flex: 0 0 auto; padding: 7px 10px; color: #062033; background: #85e6ff; border-radius: 9px; font-weight: 900; font-size: 16px; letter-spacing: 1px; }}
.history-banner strong {{ color: #eafaff; font-size: 19px; }}
.history-banner small {{ color: #b9eaff; font-size: 14px; margin-left: auto; text-align: right; }}
.number {{ color: #a9bfd0; text-align: right; font-variant-numeric: tabular-nums; }}
.accepted {{ color: #4fe4a2; font-weight: 850; }}
.ratio {{ color: #58d8ff; }}
.level {{ color: #c4d2df; text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.footer {{ height: 91px; display: flex; align-items: flex-end; justify-content: space-between; padding: 0 5px 2px; }}
.footer-left {{ color: #6f8ca3; font-size: 16px; line-height: 1.7; }}
.footer-left b {{ color: #b9d1e1; }}
.page {{ color: #fff; text-align: right; }}
.page b {{ display: block; font-size: 31px; letter-spacing: 2px; }}
.page span {{ color: #57d9ff; font-size: 16px; }}
</style>
</head>
<body>
<div class="noise"></div>
<main class="frame">
  <header class="header">
    <div>
      <div class="eyebrow">HUSTOJ RANK SERVICE</div>
      <h1>{html.escape(prefix)} {html.escape(title)}</h1>
      <div class="subtitle">{html.escape(subtitle or f"代码为阶，热爱登峰 · 共 {total_users} {count_label}")}</div>
    </div>
    <div class="snapshot"><span>图片生成快照</span><b>#{snapshot_id}</b><span>{html.escape(self._display_time(fetched_at))}</span></div>
  </header>
  {history_notice}
  <section class="table">
    {table_head}
    {''.join(rows)}
  </section>
  <footer class="footer">
    <div class="footer-left"><b>{("当前 ranklist " + str(total_users - len(historical_user_id_set)) + " 人｜历史补全 " + str(len(historical_user_id_set)) + " 人") if historical_user_id_set else (position_label + str(first_rank) + "–" + str(last_rank))}</b><br>{"蓝色姓名：历史上曾出现，但当前 ranklist 无法获取；成绩为最近一次历史记录。" if historical_user_id_set else ("使用 /翻页 " + str(page_number + 1 if page_number < page_count else 1) + " 查看" + ('下一页' if page_number < page_count else '第一页'))}</div>
    <div class="page"><b>第 {page_number} / {page_count} 页</b><span>每页 {self.page_size} {page_count_label}</span></div>
  </footer>
</main>
</body>
</html>"""

    def render(
        self,
        browser_context,
        entries: Sequence[RankEntry],
        prefix: str,
        fetched_at: str,
        snapshot_id: int,
        title: str = "天梯总榜",
        column_labels: tuple[str, str, str] | None = None,
        historical_user_ids: AbstractSet[str] = frozenset(),
        subtitle: str | None = None,
        entity_type: str | None = None,
        page_numbers: Sequence[int] | None = None,
    ) -> dict:
        if not entries:
            raise ValueError("refusing to render an empty leaderboard")

        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o755)
        page_count = math.ceil(len(entries) / self.page_size)
        requested_pages = (
            set(range(1, page_count + 1))
            if page_numbers is None
            else {int(page_number) for page_number in page_numbers}
        )
        if not requested_pages or any(
            page_number < 1 or page_number > page_count
            for page_number in requested_pages
        ):
            raise ValueError(f"page numbers must be within 1..{page_count}")

        cache_dir = self.root / "pages"
        cache_dir.mkdir(mode=0o755, exist_ok=True)
        existing_pages = self._load_cached_pages()
        published_pages: dict[str, dict[str, str]] = {}
        pending: list[tuple[int, Sequence[RankEntry], str, Path]] = []
        for page_number in range(1, page_count + 1):
            page_entries = entries[
                (page_number - 1) * self.page_size : page_number * self.page_size
            ]
            digest = self._page_content_hash(
                page_entries,
                prefix,
                page_number,
                page_count,
                len(entries),
                title,
                column_labels,
                historical_user_ids,
                subtitle,
                entity_type,
            )
            cached = existing_pages.get(str(page_number))
            if cached and cached.get("content_hash") == digest:
                cached_path = self.root / str(cached.get("file", ""))
                if cached_path.is_file() and cached_path.parent == cache_dir:
                    published_pages[str(page_number)] = cached
                    continue
            if page_number in requested_pages:
                relative_path = Path("pages") / (
                    f"page-{page_number:03d}-{digest[:16]}.png"
                )
                pending.append(
                    (page_number, page_entries, digest, self.root / relative_path)
                )

        page = browser_context.new_page() if pending else None
        try:
            if page is not None:
                page.set_viewport_size({"width": self.WIDTH, "height": self.height})
            for page_number, page_entries, digest, image_path in pending:
                content = self.build_page_html(
                    page_entries,
                    prefix,
                    fetched_at,
                    snapshot_id,
                    page_number,
                    page_count,
                    len(entries),
                    title,
                    column_labels,
                    historical_user_ids,
                    subtitle,
                    entity_type,
                )
                page.set_content(content, wait_until="load")
                layout = page.evaluate(
                    """() => {
                        const table = document.querySelector('.table');
                        const rows = document.querySelectorAll('.rank-row');
                        if (!table || !rows.length) return null;
                        const tableBox = table.getBoundingClientRect();
                        const lastBox = rows[rows.length - 1].getBoundingClientRect();
                        return {
                            tableBottom: tableBox.bottom,
                            lastRowBottom: lastBox.bottom,
                        };
                    }"""
                )
                if (
                    not isinstance(layout, dict)
                    or float(layout["lastRowBottom"])
                    > float(layout["tableBottom"]) + 0.5
                ):
                    raise RuntimeError(
                        f"leaderboard page {page_number} rows overflow the table"
                    )
                temporary = image_path.with_suffix(".png.tmp")
                page.screenshot(
                    path=str(temporary),
                    type="png",
                    animations="disabled",
                )
                temporary.replace(image_path)
                image_path.chmod(0o644)
                published_pages[str(page_number)] = {
                    "rendered_snapshot_id": snapshot_id,
                    "rendered_fetched_at": fetched_at,
                    "content_hash": digest,
                    "file": str(image_path.relative_to(self.root)),
                }
        except Exception:
            for temporary in cache_dir.glob("*.tmp"):
                temporary.unlink(missing_ok=True)
            raise
        finally:
            if page is not None:
                page.close()

        manifest = {
            "schema_version": 2,
            "snapshot_id": snapshot_id,
            "fetched_at": fetched_at,
            "prefix": prefix,
            "title": title,
            "user_count": len(entries),
            "current_user_count": len(entries) - len(historical_user_ids),
            "historical_user_count": len(historical_user_ids),
            "page_size": self.page_size,
            "page_count": page_count,
            "layout_version": self.LAYOUT_VERSION,
            "pages": dict(
                sorted(published_pages.items(), key=lambda item: int(item[0]))
            ),
            "rendered_page_count": len(pending),
            "cached_page_count": len(published_pages) - len(pending),
        }
        self._atomic_write(
            self.root / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n",
        )
        self._cleanup_page_cache(published_pages)
        return manifest

    def _page_content_hash(
        self,
        entries: Sequence[RankEntry],
        prefix: str,
        page_number: int,
        page_count: int,
        total_users: int,
        title: str,
        column_labels: tuple[str, str, str] | None,
        historical_user_ids: AbstractSet[str],
        subtitle: str | None,
        entity_type: str | None,
    ) -> str:
        document = {
            "layout_version": self.LAYOUT_VERSION,
            "width": self.WIDTH,
            "height": self.height,
            "page_size": self.page_size,
            "prefix": prefix,
            "page_number": page_number,
            "page_count": page_count,
            "total_users": total_users,
            "title": title,
            "column_labels": column_labels,
            "historical_user_ids": sorted(historical_user_ids),
            "subtitle": subtitle,
            "entity_type": entity_type,
            "entries": [asdict(entry) for entry in entries],
        }
        encoded = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _load_cached_pages(self) -> dict[str, dict[str, str]]:
        manifest_path = self.root / "manifest.json"
        try:
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}
        pages = document.get("pages") if document.get("schema_version") == 2 else None
        if not isinstance(pages, dict):
            return {}
        return {
            str(page_number): page
            for page_number, page in pages.items()
            if isinstance(page, dict)
        }

    def _cleanup_page_cache(self, published_pages: dict[str, dict[str, str]]) -> None:
        cache_dir = self.root / "pages"
        referenced = {
            str(page["file"])
            for page in published_pages.values()
            if isinstance(page.get("file"), str)
        }
        grouped: dict[str, list[Path]] = {}
        for path in cache_dir.glob("page-*.png"):
            match = re.match(r"(page-\d{3})-", path.name)
            if match:
                grouped.setdefault(match.group(1), []).append(path)
        for paths in grouped.values():
            unreferenced = sorted(
                (
                    path
                    for path in paths
                    if str(path.relative_to(self.root)) not in referenced
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in unreferenced[1:]:
                path.unlink(missing_ok=True)


def publish_class_image_manifest(
    share_dir: Path,
    snapshot_id: int,
    fetched_at: str,
    prefix: str,
    class_manifests: dict[str, dict],
    layout: StudentIdLayout | None = None,
) -> None:
    """Atomically publish the active set after every class image is complete."""
    layout = layout or StudentIdLayout(prefix, len(prefix) + 8, 4, 6)
    root = share_dir / "class-images"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    classes = []
    for class_id, manifest in sorted(class_manifests.items()):
        if not layout.is_class_id(class_id):
            raise ValueError(f"invalid class ID: {class_id}")
        if int(manifest.get("snapshot_id", 0)) != snapshot_id:
            raise ValueError(f"class image snapshot mismatch: {class_id}")
        classes.append(
            {
                "class_id": class_id,
                "user_count": int(manifest["user_count"]),
                "page_count": int(manifest["page_count"]),
            }
        )
    if not classes:
        raise ValueError("refusing to publish an empty class image manifest")
    document = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "fetched_at": fetched_at,
        "prefix": prefix,
        "class_id_length": layout.class_id_length,
        "classes": classes,
    }
    LeaderboardRenderer._atomic_write(
        root / "manifest.json",
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
    )


def publish_major_image_manifest(
    share_dir: Path,
    snapshot_id: int,
    fetched_at: str,
    prefix: str,
    major_manifests: dict[str, dict],
    layout: StudentIdLayout | None = None,
) -> None:
    """Atomically publish the active set after every major image is complete."""
    layout = layout or StudentIdLayout(prefix, len(prefix) + 8, 4, 6)
    root = share_dir / "major-images"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    majors = []
    for major_id, manifest in sorted(major_manifests.items()):
        if not layout.is_major_id(major_id):
            raise ValueError(f"invalid major ID: {major_id}")
        if int(manifest.get("snapshot_id", 0)) != snapshot_id:
            raise ValueError(f"major image snapshot mismatch: {major_id}")
        page_count = int(manifest["page_count"])
        if page_count <= 0:
            raise ValueError(f"major image page count is invalid: {major_id}")
        majors.append(
            {
                "major_id": major_id,
                "user_count": int(manifest["user_count"]),
                "page_count": page_count,
            }
        )
    if not majors:
        raise ValueError("refusing to publish an empty major image manifest")
    document = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "fetched_at": fetched_at,
        "prefix": prefix,
        "major_id_length": layout.major_id_length,
        "majors": majors,
    }
    LeaderboardRenderer._atomic_write(
        root / "manifest.json",
        json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n",
    )
