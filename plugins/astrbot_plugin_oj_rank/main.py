from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
import secrets
from sys import maxsize

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, register

from . import rank_data as data

from .rank_data import (
    RandomPlayerRateLimiter,
    RankDataError,
    build_period_statistics_csv,
    build_statistics_csv,
    change_label,
    find_changes,
    find_users,
    find_exact_users,
    format_change,
    format_user,
    is_allowed_group_command,
    OJ_GUIDE_COMMAND_PATTERN,
    PTA_GUIDE_COMMAND_PATTERN,
    is_class_id,
    is_major_id,
    load_academic_labels,
    load_class_image_registry,
    load_major_image_registry,
    load_daily,
    load_image_manifest,
    load_snapshot,
    load_statistics_snapshot,
    load_statistics_snapshot_index,
    nearest_statistics_snapshot,
    lookup_update_text,
    short_iso_time,
    short_time,
    validate_submission_response,
)


PLUGIN_NAME = "astrbot_plugin_oj_rank"
DATA_FILE = "/oj-rank-share/latest.json"
LOOKUP_FILE = "/oj-rank-share/lookup.json"
DAILY_FILE = "/oj-rank-share/daily.json"
HISTORICAL_DAILY_FILE = "/oj-rank-share/historical-daily.json"
HISTORICAL_DAILY_IMAGE_MANIFEST_FILE = "/oj-rank-share/historical-daily-images/manifest.json"
WEEKLY_FILE = "/oj-rank-share/weekly.json"
MONTHLY_FILE = "/oj-rank-share/monthly.json"
WEEKLY_IMAGE_MANIFEST_FILE = "/oj-rank-share/weekly-images/manifest.json"
MONTHLY_IMAGE_MANIFEST_FILE = "/oj-rank-share/monthly-images/manifest.json"
CLASS_INTENSITY_FILE = "/oj-rank-share/class-intensity.json"
MAJOR_INTENSITY_FILE = "/oj-rank-share/major-intensity.json"
RENDER_SOCKET = "/oj-rank-render/render.sock"
SUBMISSION_SOCKET = "/oj-rank-render/submission.sock"
IMAGE_MANIFEST_FILE = "/oj-rank-share/rank-images/manifest.json"
COMPUTER_COLLEGE_FILE = "/oj-rank-share/computer-college.json"
SOFTWARE_COLLEGE_FILE = "/oj-rank-share/software-college.json"
COMPUTER_COLLEGE_IMAGE_MANIFEST_FILE = (
    "/oj-rank-share/college-images/computer/manifest.json"
)
SOFTWARE_COLLEGE_IMAGE_MANIFEST_FILE = (
    "/oj-rank-share/college-images/software/manifest.json"
)
CLASS_IMAGE_REGISTRY_FILE = "/oj-rank-share/class-images/manifest.json"
CLASS_IMAGE_ROOT = "/oj-rank-share/class-images"
CLASS_DATA_ROOT = "/oj-rank-share/class-ranklists"
MAJOR_IMAGE_REGISTRY_FILE = "/oj-rank-share/major-images/manifest.json"
MAJOR_IMAGE_ROOT = "/oj-rank-share/major-images"
MAJOR_DATA_ROOT = "/oj-rank-share/major-ranklists"
STATISTICS_EXPORT_DIR = Path(__file__).parent / "statistics_exports"
OJ_GUIDE_ASSET_DIR = Path(__file__).parent / "assets" / "oj_guide"
PTA_GUIDE_ASSET_DIR = Path(__file__).parent / "assets" / "pta_guide"
CLASS_INTENSITY_IMAGE_MANIFEST_FILE = "/oj-rank-share/class-intensity-images/manifest.json"
ACADEMIC_LABELS_FILE = "/oj-rank-share/academic-labels.json"
STATISTICS_SNAPSHOT_MANIFEST_FILE = "/oj-rank-share/statistics-snapshots.json"
STATISTICS_SNAPSHOT_ROOT = "/oj-rank-share/statistics-snapshots"
MAJOR_INTENSITY_IMAGE_MANIFEST_FILE = "/oj-rank-share/major-intensity-images/manifest.json"
HELP_PRIORITY = 1000
GROUP_GUARD_PRIORITY = maxsize + 100
FORWARD_TEXT_LENGTH = 100

HELP_TEXT = """OJ 榜单帮助
/榜单 [页码]：查看总榜
/计院榜单 [页码]：查看计算机学院榜单
/软院榜单 [页码]：查看软件学院榜单
/班级榜单 [专业班级]：如 /班级榜单 计科01班
/专业榜单 [专业简称] [页码]：如 /专业榜单 计科
/翻页 [页码]：查看当前榜单某一页；不填默认下一页
/最卷班级：查看按 AC 量排序的班级
/最卷专业：查看按 AC 量排序的专业
/查榜 [学号或 OJ 昵称]：查询配置范围内的个人成绩
/提交记录 [昵称/学号/初始昵称]：查看该同学最近一页提交记录

周期榜
/周榜 [页码]：本周总榜
/月榜 [页码]：本月总榜

今日数据
/变化 [学号或昵称]：今日整体或个人进展
/冲榜 [人数]：今日名次提升榜
/刷题榜 [人数]：今日新增通过题数榜
/历史单日榜 [页码]：历史所有单日新增纪录总榜（同一人可多次上榜）
/提交榜 [人数]：今日新增提交次数榜
/新星榜 [人数]：今天从百名外冲出的同学

/oj：查看电脑端和手机端 OJ 登录教程
/pta：查看 PTA 注册、绑定学号和做题教程
/随机选手：随机看看一位同学
/更新日志：查看最近更新内容
/帮助：查看本帮助

榜单数据约每 5 分钟更新。
如有建议或想法，请联系本系统管理员。"""

CHANGELOG_TEXT = """OJ 榜单更新日志
最近更新：2026-10-13

2026-10-13
• 新增历史单日榜，后台逐日计算，未完成时标注统计进度
• 修复旧时间格式兼容和图片不可用时的历史榜翻页

2026-09-23
• 新增 /提交记录，可按昵称、学号或初始昵称查看最近一页提交记录

2026-09-21
• 超过 100 字的文字回复改用合并转发，减少群聊刷屏
• 修复长文字按字数拆分时切断命令和句子的问题

2026-09-19
• 修复采集服务长时间运行后内存持续增长的问题，提升长期运行稳定性
• 服务器繁忙时自动暂缓采集和图片生成，压力恢复后继续更新
• 图片暂不可用时回退文字榜单，保留最近一次成功采集的数据
• 合并重复图片请求、限制排队数量，减少重复渲染
• 采集失败后逐步延长重试间隔，避免连续重试加重服务器负担"""

DEVELOPER_HELP_TEXT = """开发者帮助
维护与备用命令
/开发者帮助：查看本帮助
/文字榜单 [页码]、/榜单状态、/日榜

统计导出（仅 AstrBot 管理员可用）
/统计数据 [全量]：导出当前快照 CSV
/统计数据 班级 <班级>、/统计数据 专业 <专业>：导出指定范围 CSV
/统计数据 #<快照编号>：导出指定历史快照 CSV
/统计数据 本周、/统计数据 本月：导出截至今日 00:00 的周期变化 CSV（不含今日数据）"""


@register(
    PLUGIN_NAME,
    "bzsjmz",
    "HUSTOJ 榜单只读查询",
    "1.8.0",
    "https://github.com/bzsjmz/hustoj-rank-suite",
)
class OjRankPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._last_views: dict[
            str, tuple[str, str | None, str, int, str | None, str | None]
        ] = {}
        self._random_player_limiter = RandomPlayerRateLimiter()

    async def initialize(self):
        logger.info(f"[{PLUGIN_NAME}] 已初始化，只读数据源: {DATA_FILE}")
        logger.info(f"[{PLUGIN_NAME}] 群静默守卫已启用，仅放行榜单及教程斜杠命令")

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=GROUP_GUARD_PRIORITY,
    )
    async def group_silence_guard(self, event: AstrMessageEvent):
        raw_text = "".join(
            component.text
            for component in event.get_messages()
            if isinstance(component, Plain)
        )
        if is_allowed_group_command(raw_text):
            return

        # Mark the event as handled without sending anything. stop_event() prevents
        # lower-priority plugin handlers, while _has_send_oper also prevents the
        # default LLM fallback for @bot, replies to bot messages, and unknown /commands.
        event._has_send_oper = True
        event.stop_event()

    @staticmethod
    def _string_set(value) -> set[str]:
        if not isinstance(value, list):
            return set()
        return {str(item).strip() for item in value if str(item).strip()}

    def _allowed(self, event: AstrMessageEvent) -> bool:
        allowed_users = self._string_set(self.config.get("allowed_users", []))
        allowed_groups = self._string_set(self.config.get("allowed_groups", []))
        if not allowed_users and not allowed_groups:
            return True

        sender_id = str(event.get_sender_id() or "")
        group_id = str(event.get_group_id() or "")
        return sender_id in allowed_users or group_id in allowed_groups

    @staticmethod
    def _text_result(event: AstrMessageEvent, content: str):
        if len(content) <= FORWARD_TEXT_LENGTH:
            return event.plain_result(content)
        sender = str(event.get_self_id() or "0")
        return event.chain_result([
            Node(uin=sender, name="OJ 榜单", content=[Plain(content)])
        ])

    def _limits(self) -> tuple[int, int]:
        try:
            default_limit = int(self.config.get("default_limit", 10))
        except (TypeError, ValueError):
            default_limit = 10
        try:
            max_limit = int(self.config.get("max_limit", 20))
        except (TypeError, ValueError):
            max_limit = 20
        return max(1, default_limit), min(50, max(1, max_limit))

    def _load(self):
        return load_snapshot(DATA_FILE)

    def _load_daily(self):
        return load_daily(DAILY_FILE)

    def _load_lookup(self):
        return load_snapshot(LOOKUP_FILE)


    def _oj_guide_nodes(self, event: AstrMessageEvent) -> list[Node]:
        campus = str(self.config.get("oj_campus_url", "")).strip()
        vpn = str(self.config.get("oj_webvpn_url", "")).strip()
        text = ("OJ 登录与使用指南\n\n"
                f"校内入口：{campus or '请向管理员索取'}\n"
                f"校外入口：{vpn or '请向管理员索取'}\n\n"
                "请按学校要求连接网络并手动完成统一认证，再进入 OJ。\n"
                "OJ 账号、初始密码及绑定规则由学校提供；不要在群内发送密码或验证码。\n"
                "登录后可选择题目，按题目要求编写代码并提交；通过记录将在后续榜单采集中更新。\n"
                "忘记密码或找不到入口时，请联系本系统管理员。")
        return [Node(uin=str(event.get_self_id() or "0"), name="OJ 使用帮助", content=[Plain(text)])]



    def _pta_guide_nodes(self, event: AstrMessageEvent) -> list[Node]:
        text = ("PTA 使用指南\n\nhttps://pintia.cn/\n\n"
                "请在官方网站完成注册与登录，再按教师提供的课程说明绑定身份或加入题目集。\n"
                "学号、班级和绑定码请使用学校发放的信息，不要照抄他人的截图或示例。\n"
                "进入题目集后选择题目、编写代码并提交，以平台显示的评测结果为准。")
        return [Node(uin=str(event.get_self_id() or "0"), name="PTA 使用帮助", content=[Plain(text)])]




    @staticmethod
    def _load_images(manifest_file: str = IMAGE_MANIFEST_FILE):
        return load_image_manifest(manifest_file)
    @staticmethod
    def _load_academic_labels():
        return load_academic_labels(ACADEMIC_LABELS_FILE)

    @staticmethod
    def _load_statistics_snapshot_index():
        return load_statistics_snapshot_index(STATISTICS_SNAPSHOT_MANIFEST_FILE)

    @staticmethod
    def _load_statistics_snapshot(reference):
        return load_statistics_snapshot(STATISTICS_SNAPSHOT_ROOT, reference)


    @staticmethod
    def _write_statistics_csv(content: str, snapshot_id: int, scope: str) -> Path:
        STATISTICS_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        STATISTICS_EXPORT_DIR.chmod(0o755)
        output = STATISTICS_EXPORT_DIR / f"oj-statistics-{snapshot_id}-{scope}.csv"
        temporary = output.with_suffix(".csv.tmp")
        temporary.write_text("\ufeff" + content, encoding="utf-8")
        temporary.chmod(0o644)
        temporary.replace(output)
        output.chmod(0o644)
        exports = sorted(STATISTICS_EXPORT_DIR.glob("oj-statistics-*.csv"), key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in exports[20:]:
            stale.unlink()
        return output


    @staticmethod
    def _load_scoped(path: str):
        return load_snapshot(path, allow_empty=True)

    def _requested_count(self, limit: int) -> int:
        default_limit, max_limit = self._limits()
        requested = default_limit if limit <= 0 else limit
        return min(max_limit, max(1, requested))

    def _freshness(self, snapshot) -> str:
        try:
            stale_after = int(self.config.get("stale_after_minutes", 15))
        except (TypeError, ValueError):
            stale_after = 15
        age = snapshot.age_minutes()
        return f"（数据已延迟 {age} 分钟）" if age > max(1, stale_after) else ""

    @staticmethod
    def _page_key(event: AstrMessageEvent) -> str:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        return f"{origin}:{event.get_sender_id() or ''}"

    @staticmethod
    def _random_player_rate_keys(event: AstrMessageEvent) -> tuple[str | None, str]:
        origin = str(getattr(event, "unified_msg_origin", "") or "")
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        return (f"{origin}:{group_id}" if group_id else None), user_id

    async def _leaderboard_image(
        self,
        event: AstrMessageEvent,
        page_number: int,
        manifest_file: str = IMAGE_MANIFEST_FILE,
        snapshot_file: str | None = DATA_FILE,
        title: str = "榜单",
        render_board: str | None = None,
        render_entity: str | None = None,
    ):
        try:
            manifest = self._load_images(manifest_file)
            if snapshot_file is not None:
                source_snapshot = self._load_scoped(snapshot_file)
                if manifest.snapshot_id != source_snapshot.snapshot_id:
                    raise RankDataError("图片尚未同步至最新快照")
            image_path = manifest.page_path(page_number)
        except RankDataError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 读取{title}图片失败: {exc}")
            render_error = str(exc)
            if render_board is not None:
                try:
                    image_path = await self._request_render(
                        render_board, render_entity, page_number
                    )
                except (OSError, RankDataError, asyncio.TimeoutError) as render_exc:
                    render_error = str(render_exc)
                    logger.warning(
                        f"[{PLUGIN_NAME}] 按需渲染{title}失败: {render_exc}"
                    )
                else:
                    exc = None
            if exc is not None:
                if snapshot_file is None:
                    yield self._text_result(event, f"暂时无法查询{title}：{render_error}")
                    return
                async for result in self._leaderboard_text(
                    event,
                    page_number,
                    snapshot_file=snapshot_file,
                    title=title,
                    prefix=f"图片暂不可用（{render_error}），已切换文字榜单\n",
                ):
                    yield result
                return
        self._last_views[self._page_key(event)] = (
            manifest_file, snapshot_file, title, page_number,
            render_board, render_entity,
        )
        try:
            manifest = self._load_images(manifest_file)
            if manifest.page_path(page_number) != image_path:
                raise RankDataError("图片发布已变化，请重试")
            if snapshot_file is not None and manifest.snapshot_id != self._load_scoped(snapshot_file).snapshot_id:
                raise RankDataError("图片尚未同步至最新快照")
        except RankDataError as exc:
            if snapshot_file is not None:
                async for result in self._leaderboard_text(event, page_number, snapshot_file=snapshot_file,
                                                          title=title, prefix="图片更新中，已切换文字榜单\n"):
                    yield result
            else:
                yield self._text_result(event, f"暂时无法查询{title}：{exc}")
            return
        caption = f"{title}｜数据更新 {short_iso_time(manifest.fetched_at)}\n图内标注图片生成时间；内容未变时复用图片。\n"
        yield event.chain_result([Plain(caption), Image(file=str(image_path))])

    async def _request_render(
        self, board: str, entity: str | None, page_number: int
    ) -> Path:
        try:
            timeout = max(5.0, float(self.config.get("render_timeout_seconds", 90)))
        except (TypeError, ValueError):
            timeout = 90.0

        async def request() -> dict:
            reader, writer = await asyncio.open_unix_connection(RENDER_SOCKET)
            try:
                message = {
                    "board": board,
                    "entity": entity,
                    "page": page_number,
                }
                writer.write(
                    (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
                )
                await writer.drain()
                raw = await reader.readline()
                if not raw or len(raw) > 8192:
                    raise RankDataError("渲染服务响应无效")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RankDataError("渲染服务响应无效")
                return payload
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            response = await asyncio.wait_for(request(), timeout=timeout)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RankDataError("渲染服务响应无效") from exc
        if not response.get("ok"):
            raise RankDataError(str(response.get("error") or "图片渲染失败"))
        relative = Path(str(response.get("relative_path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise RankDataError("渲染服务返回了不安全路径")
        image = (Path("/oj-rank-share") / relative).resolve()
        try:
            image.relative_to(Path("/oj-rank-share").resolve())
        except ValueError as exc:
            raise RankDataError("渲染服务返回了不安全路径") from exc
        if not image.is_file():
            raise RankDataError("渲染图片尚未发布")
        return image

    async def _request_submissions(self, user_id: str) -> tuple[str, list[dict]]:
        try:
            timeout = max(5.0, min(180.0, float(
                self.config.get("submission_timeout_seconds", 95)
            )))
        except (TypeError, ValueError):
            timeout = 95.0

        async def request() -> dict:
            reader, writer = await asyncio.open_unix_connection(SUBMISSION_SOCKET)
            try:
                writer.write(
                    (json.dumps({"user_id": user_id}, ensure_ascii=False) + "\n")
                    .encode("utf-8")
                )
                await writer.drain()
                raw = await reader.readline()
                if not raw or len(raw) > 65536:
                    raise RankDataError("提交记录服务响应无效")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RankDataError("提交记录服务响应无效")
                return payload
            finally:
                writer.close()
                await writer.wait_closed()

        try:
            response = await asyncio.wait_for(request(), timeout=timeout)
        except FileNotFoundError as exc:
            raise RankDataError("提交记录服务尚未启动") from exc
        except asyncio.TimeoutError as exc:
            raise RankDataError("提交记录查询超时，请稍后重试") from exc
        except (ConnectionError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RankDataError("提交记录服务暂时无法连接") from exc
        if not response.get("ok"):
            raise RankDataError(str(response.get("error") or "提交记录查询失败"))
        return validate_submission_response(response, user_id)

    async def _leaderboard_text(
        self,
        event: AstrMessageEvent,
        page_number: int,
        snapshot_file: str = DATA_FILE,
        title: str = "榜单",
        prefix: str = "",
    ):
        try:
            snapshot = self._load_scoped(snapshot_file)
        except RankDataError as exc:
            yield self._text_result(event, f"暂时无法查询{title}：{exc}")
            return
        history_updating = (Path(snapshot_file).name == "historical-daily.json"
                            and getattr(snapshot, "history_status", "ready") == "updating")
        if history_updating:
            prefix += "历史统计正在后台更新，当前仅展示已完成记录。\n"
        if not snapshot.users:
            yield self._text_result(event, prefix + f"{snapshot.prefix} {title}目前还没有数据。")
            return
        page_size = snapshot.user_count if Path(snapshot_file).parent == Path(CLASS_DATA_ROOT) else 20
        page_count = math.ceil(snapshot.user_count / page_size)
        if page_number < 1 or page_number > page_count:
            yield self._text_result(event, f"页码超出范围（1-{page_count}）")
            return
        start = (page_number - 1) * page_size
        rows = []
        for row in snapshot.users[start : start + page_size]:
            if Path(snapshot_file).name == "historical-daily.json":
                text = (f"{row['rank']}. {row['user_id']} {row['nickname'] or '未设置昵称'}｜"
                        f"{row['date']}｜新增 {row['accepted_change']} 题｜"
                        f"实际区间 {row['start_at']} → {row['end_at']}")
            else:
                text = format_user(row, compact=True)
            if row.get("is_historical"):
                text += "｜当前 ranklist 暂未收录（历史记录）"
            rows.append(text)
        header = (
            f"OJ {title}文字版 第 {page_number}/{page_count} 页（{snapshot.prefix}）\n"
            f"更新 {short_time(snapshot)}｜共 {snapshot.user_count} "
            f"{'条记录（北京时间，已结束日期）' if Path(snapshot_file).name == 'historical-daily.json' else '人'}"
            f"{self._freshness(snapshot)}"
        )
        yield self._text_result(event, prefix + "\n".join([header, *rows]))

    @filter.command("榜单")
    async def leaderboard(self, event: AstrMessageEvent, page_number: int = 1):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            if page_number < 1:
                yield self._text_result(event, "页码必须从 1 开始，例如：/榜单 2")
                return
            async for result in self._leaderboard_image(
                event, page_number, render_board="total"
            ):
                yield result

    @filter.command("翻页")
    async def turn_page(self, event: AstrMessageEvent, page_number: int = 0):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            view = self._last_views.get(self._page_key(event))
            manifest_file, snapshot_file, title, previous, render_board, render_entity = view or (
                IMAGE_MANIFEST_FILE,
                DATA_FILE,
                "榜单",
                0,
                "total",
                None,
            )
            if page_number <= 0:
                try:
                    if snapshot_file and Path(snapshot_file).name == "historical-daily.json":
                        page_count = max(1, math.ceil(self._load_scoped(snapshot_file).user_count / 20))
                    else:
                        page_count = self._load_images(manifest_file).page_count
                except RankDataError as exc:
                    yield self._text_result(event, f"暂时无法查看{title}：{exc}")
                    return
                page_number = previous + 1
                if page_number > page_count:
                    page_number = 1
            if snapshot_file and Path(snapshot_file).name == "historical-daily.json":
                self._last_views[self._page_key(event)] = (
                    manifest_file, snapshot_file, title, page_number, render_board, render_entity,
                )
            async for result in self._leaderboard_image(
                event, page_number, manifest_file, snapshot_file, title,
                render_board, render_entity,
            ):
                yield result

    @filter.command("计院榜单")
    async def computer_college_leaderboard(
        self, event: AstrMessageEvent, page_number: int = 1
    ):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            if page_number < 1:
                yield self._text_result(event, "页码必须从 1 开始，例如：/计院榜单 2")
                return
            async for result in self._leaderboard_image(
                event,
                page_number,
                COMPUTER_COLLEGE_IMAGE_MANIFEST_FILE,
                COMPUTER_COLLEGE_FILE,
                "计院榜单",
                "computer",
            ):
                yield result

    @filter.command("软院榜单")
    async def software_college_leaderboard(
        self, event: AstrMessageEvent, page_number: int = 1
    ):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            if page_number < 1:
                yield self._text_result(event, "页码必须从 1 开始，例如：/软院榜单 2")
                return
            async for result in self._leaderboard_image(
                event,
                page_number,
                SOFTWARE_COLLEGE_IMAGE_MANIFEST_FILE,
                SOFTWARE_COLLEGE_FILE,
                "软院榜单",
                "software",
            ):
                yield result

    @filter.command("最卷班级")
    async def class_intensity_leaderboard(self, event: AstrMessageEvent):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            async for result in self._leaderboard_image(
                event, 1, CLASS_INTENSITY_IMAGE_MANIFEST_FILE,
                CLASS_INTENSITY_FILE, "最卷班级", "class-intensity"
            ):
                yield result

    @filter.command("最卷专业")
    async def major_intensity_leaderboard(self, event: AstrMessageEvent):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            async for result in self._leaderboard_image(
                event, 1, MAJOR_INTENSITY_IMAGE_MANIFEST_FILE,
                MAJOR_INTENSITY_FILE, "最卷专业", "major-intensity"
            ):
                yield result

    @filter.command("专业榜单")
    async def major_leaderboard(
        self, event: AstrMessageEvent, query: str = "", page_number: int = 1
    ):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            query = query.strip()
            try:
                labels = self._load_academic_labels()
            except RankDataError as exc:
                yield self._text_result(event, f"暂时无法识别专业：{exc}")
                return
            major_id = labels.resolve_major(query)
            if major_id is None or page_number < 1:
                yield self._text_result(event,
                    "格式：/专业榜单 专业简称 [页码]\n"
                    "例如：/专业榜单 示例专业\n"
                    "备用格式：/专业榜单 20261001 [页码]\n"
                    "输入 /帮助 查询榜单帮助。"
                )
                return
            try:
                major_ids = load_major_image_registry(MAJOR_IMAGE_REGISTRY_FILE)
            except RankDataError as exc:
                yield self._text_result(event, f"暂时无法查询专业榜单：{exc}")
                return
            if major_id not in major_ids:
                yield self._text_result(event, f"当前榜单中没有该专业：{query}。")
                return
            title = labels.major_name(major_id)
            async for result in self._leaderboard_image(
                event, page_number, f"{MAJOR_IMAGE_ROOT}/{major_id}/manifest.json",
                f"{MAJOR_DATA_ROOT}/{major_id}.json", f"{title}专业榜单",
                "major", major_id,
            ):
                yield result

    @filter.command("班级榜单")
    async def class_leaderboard(
        self, event: AstrMessageEvent, query: str = "", page_number: int = 1
    ):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            query = query.strip()
            try:
                labels = self._load_academic_labels()
            except RankDataError as exc:
                yield self._text_result(event, f"暂时无法识别班级：{exc}")
                return
            class_id = labels.resolve_class(query)
            if class_id is None:
                yield self._text_result(event,
                    "格式：/班级榜单 专业简称+两位班号\n"
                    "例如：/班级榜单 示例专业01班\n"
                    "备用格式：/班级榜单 2026100101\n"
                    "输入 /帮助 查询榜单帮助。"
                )
                return
            if page_number != 1:
                yield self._text_result(event,
                    "班级榜单每班仅 1 页。\n"
                    "格式：/班级榜单 示例专业01班\n"
                    "备用格式：/班级榜单 2026100101\n"
                    "输入 /帮助 查询榜单帮助。"
                )
                return
            try:
                class_ids = load_class_image_registry(CLASS_IMAGE_REGISTRY_FILE)
            except RankDataError as exc:
                yield self._text_result(event, f"暂时无法查询班级榜单：{exc}")
                return
            if class_id not in class_ids:
                yield self._text_result(event, f"当前榜单中没有该班级：{query}。")
                return
            title = labels.class_name(class_id)
            async for result in self._leaderboard_image(
                event, 1, f"{CLASS_IMAGE_ROOT}/{class_id}/manifest.json",
                f"{CLASS_DATA_ROOT}/{class_id}.json", f"{title}榜单",
                "class", class_id,
            ):
                yield result


    @filter.command("统计数据")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def statistics_data(
        self, event: AstrMessageEvent, scope: str = "", target: str = ""
    ):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            scope = scope.strip()
            target = target.strip()
            usage = (
                "格式：/统计数据\n"
                "/统计数据 全量\n"
                "/统计数据 班级 示例专业01班\n"
                "/统计数据 专业 示例专业\n"
                "/统计数据 #1234\n"
                "/统计数据 本周 或 /统计数据 本月\n"
                "本周、本月均截至今日 00:00，不包含今日数据；端点容差 30 分钟，实际范围见 CSV。统计 CSV 对公式文本加保护前缀；仅 AstrBot 管理员可导出。"
            )
            if scope in {"本周", "本月"}:
                if target:
                    yield self._text_result(event, usage)
                    return
                try:
                    references = self._load_statistics_snapshot_index()
                    today_start = datetime.now().astimezone().replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    days = 7 if scope == "本周" else 30
                    start_reference = nearest_statistics_snapshot(
                        references, today_start - timedelta(days=days)
                    )
                    end_reference = nearest_statistics_snapshot(references, today_start, not_after=today_start)
                    if start_reference.snapshot_id == end_reference.snapshot_id:
                        raise RankDataError("该周期内可用快照不足")
                    start_snapshot = self._load_statistics_snapshot(start_reference)
                    end_snapshot = self._load_statistics_snapshot(end_reference)
                    content = build_period_statistics_csv(start_snapshot, end_snapshot, scope)
                    output = self._write_statistics_csv(
                        content, end_snapshot.snapshot_id,
                        f"{scope}-{start_snapshot.snapshot_id}-to-{end_snapshot.snapshot_id}",
                    )
                except (OSError, RankDataError) as exc:
                    yield self._text_result(event, f"暂时无法导出{scope}统计数据：{exc}")
                    return
            elif scope.startswith("#"):
                if target or not scope[1:].isdigit() or int(scope[1:]) <= 0:
                    yield self._text_result(event, usage)
                    return
                try:
                    requested_id = int(scope[1:])
                    references = self._load_statistics_snapshot_index()
                    reference = next(
                        (item for item in references if item.snapshot_id == requested_id), None
                    )
                    if reference is None:
                        raise RankDataError(f"未找到快照 #{requested_id}")
                    snapshot = self._load_statistics_snapshot(reference)
                    labels = self._load_academic_labels()
                    content = build_statistics_csv(snapshot, labels)
                    output = self._write_statistics_csv(content, snapshot.snapshot_id, "snapshot")
                except (OSError, RankDataError) as exc:
                    yield self._text_result(event, f"暂时无法导出指定快照统计数据：{exc}")
                    return
            else:
                include_inactive = scope == "全量"
                class_id = None
                profession_id = None
                if scope in ("", "全量"):
                    if target:
                        yield self._text_result(event, usage)
                        return
                elif scope in ("班级", "专业") and target:
                    try:
                        labels = self._load_academic_labels()
                    except RankDataError as exc:
                        yield self._text_result(event, f"暂时无法识别统计范围：{exc}")
                        return
                    if scope == "班级":
                        class_id = labels.resolve_class(target)
                    else:
                        profession_id = labels.resolve_major(target)
                    if (scope == "班级" and class_id is None) or (scope == "专业" and profession_id is None):
                        yield self._text_result(event, usage)
                        return
                else:
                    yield self._text_result(event, usage)
                    return
                try:
                    snapshot = self._load()
                    labels = self._load_academic_labels()
                    content = build_statistics_csv(
                        snapshot, labels, include_inactive_students=include_inactive,
                        profession_id=profession_id, class_id=class_id
                    )
                    scope_name = (
                        "all" if include_inactive else
                        f"class-{class_id[len(labels.prefix):]}" if class_id else
                        f"profession-{profession_id[len(labels.prefix):]}" if profession_id else "active"
                    )
                    output = self._write_statistics_csv(content, snapshot.snapshot_id, scope_name)
                except (OSError, RankDataError) as exc:
                    yield self._text_result(event, f"暂时无法导出统计数据：{exc}")
                    return
            try:
                yield event.chain_result([File(name=output.name, file=str(output))])
            except (OSError, RuntimeError, TypeError) as exc:
                logger.warning(f"[{PLUGIN_NAME}] CSV 文件组件不可用，回退文字输出: {exc}")
                yield self._text_result(event, content)


    @filter.command("文字榜单")
    async def text_leaderboard(
        self,
        event: AstrMessageEvent,
        page_number: int = 1,
    ):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            async for result in self._leaderboard_text(event, page_number):
                yield result

    @filter.command("查榜")
    async def lookup(self, event: AstrMessageEvent, query: str = ""):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            if not query.strip():
                yield self._text_result(event, "用法：/查榜 [学号或 OJ 昵称]")
                return

            try:
                snapshot = self._load_lookup()
            except RankDataError as exc:
                logger.warning(f"[{PLUGIN_NAME}] 读取查榜扩展数据失败: {exc}")
                yield self._text_result(event, f"查榜扩展数据暂时无法读取：{exc}")
                return

            matches = find_users(snapshot, query, limit=10)
            if not matches:
                yield self._text_result(event,
                    f"没有找到“{query.strip()}”。当前 /查榜 范围为 23 级至 26 级学生。"
                )
                return

            body = []
            for row in matches:
                text = format_user(row) + "\n" + lookup_update_text(snapshot, row)
                if row.get("is_historical"):
                    text += (
                        "\n当前 ranklist 暂未收录；以下为最近一次历史记录（"
                        f"{short_iso_time(str(row.get('last_seen_at', '')))}）。"
                    )
                body.append(text)
            if len(matches) == 10:
                body.append("结果较多，仅显示前 10 条；用完整学号可精确查询。")
            footer = (
                "查询范围：23 级至 26 级｜各年级更新时间见对应记录"
            )
            yield self._text_result(event, "\n\n".join([*body, footer]))

    @filter.command("提交记录")
    async def submission_history(self, event: AstrMessageEvent, query: str = ""):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            if not query.strip():
                yield self._text_result(
                    event, "用法：/提交记录 <昵称、完整学号或初始昵称>；昵称需完整匹配。"
                )
                return
            try:
                snapshot = self._load_lookup()
            except RankDataError as exc:
                yield self._text_result(event, f"查询数据暂时无法读取：{exc}")
                return
            matches = find_exact_users(snapshot, query)
            if not matches:
                yield self._text_result(
                    event, "未找到对应同学，请使用完整学号、完整昵称或完整初始昵称。"
                )
                return
            if len(matches) > 1:
                choices = "\n".join(
                    f"{row['nickname']}（{row['user_id']}）" for row in matches[:10]
                )
                yield self._text_result(
                    event, "匹配到多位同学，请改用完整学号：\n" + choices
                )
                return

            row = matches[0]
            user_id = str(row["user_id"])
            try:
                fetched_at, records = await self._request_submissions(user_id)
            except RankDataError as exc:
                logger.warning(f"[{PLUGIN_NAME}] 查询提交记录失败: {exc}")
                yield self._text_result(event, f"提交记录暂时无法读取：{exc}")
                return
            if not records:
                yield self._text_result(
                    event, f"{row['nickname']}（{user_id}）暂时没有提交记录。"
                )
                return

            sender = str(event.get_self_id() or "0")
            nodes = [Node(
                uin=sender, name="OJ 提交记录",
                content=[Plain(
                    f"{row['nickname']}（{user_id}）\n"
                    f"最近一页共 {len(records)} 条｜查询时间 {short_iso_time(fetched_at)}"
                )],
            )]
            for record in records:
                nodes.append(Node(
                    uin=sender, name=str(row["nickname"]),
                    content=[Plain(
                        f"#{record['submission_id']}｜题目 {record['problem_id']}\n"
                        f"{record['result']}｜{record['language']}｜{record['code_length']}\n"
                        f"{record['time_ms']} ms｜{record['memory_kb']} KB｜{record['judge'] or '—'}\n"
                        f"{record['submitted_at']}"
                    )],
                ))
            yield event.chain_result([Nodes(nodes=nodes)])

    @filter.command("榜单状态")
    async def status(self, event: AstrMessageEvent):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            try:
                snapshot = self._load()
            except RankDataError as exc:
                yield self._text_result(event, f"榜单状态：异常（{exc}）")
                return

            age = snapshot.age_minutes()
            state = "正常" if not self._freshness(snapshot) else "数据延迟"
            yield self._text_result(event,
                f"数据状态：{state}\n"
                f"最后更新：{short_time(snapshot)}（{age} 分钟前）\n"
                f"历史名册人数：{snapshot.historical_user_count}\n"
                f"当前 ranklist 人数：{snapshot.user_count}\n"
                f"当前缺失人数：{snapshot.missing_user_count}\n"
                f"覆盖率：{snapshot.coverage_rate:.2%}"
            )

    @filter.command("变化")
    async def changes(self, event: AstrMessageEvent, query: str = ""):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            try:
                snapshot = self._load_daily()
            except RankDataError as exc:
                yield self._text_result(event, f"暂时无法查询每日变化：{exc}")
                return

            if query.strip():
                matches = find_changes(snapshot, query, limit=10)
                if not matches:
                    yield self._text_result(event, f"每日变化中没有找到“{query.strip()}”。")
                    return
                body = [format_change(row) for row in matches]
                footer = (
                    f"统计从 {short_iso_time(snapshot.baseline_fetched_at)} 开始，截至 "
                    f"{short_iso_time(snapshot.fetched_at_text)}。"
                    f"{self._freshness(snapshot)}"
                )
                yield self._text_result(event, "\n\n".join([*body, footer]))
                return

            new_count = sum(1 for row in snapshot.changes if row.get("is_new"))
            up_count = sum(
                1 for row in snapshot.changes if (row.get("rank_change") or 0) > 0
            )
            down_count = sum(
                1 for row in snapshot.changes if (row.get("rank_change") or 0) < 0
            )
            unchanged = snapshot.user_count - new_count - up_count - down_count
            yield self._text_result(event,
                "今日榜单变化\n"
                f"统计从 {short_iso_time(snapshot.baseline_fetched_at)} 开始，截至 "
                f"{short_iso_time(snapshot.fetched_at_text)}。\n"
                f"今天上升 {up_count} 人｜下降 {down_count} 人｜排名不变 {unchanged} 人｜"
                f"首次上榜 {new_count} 人\n"
                "输入 /变化 学号或昵称，可查看个人今天的进展。"
                f"{self._freshness(snapshot)}"
            )

    @filter.command("冲榜")
    async def climbers(self, event: AstrMessageEvent, limit: int = 0):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            count = self._requested_count(limit)
            try:
                snapshot = self._load_daily()
            except RankDataError as exc:
                yield self._text_result(event, f"暂时无法查询冲榜：{exc}")
                return

            candidates = [
                row
                for row in snapshot.changes
                if not row.get("is_new") and (row.get("rank_change") or 0) > 0
            ]
            candidates.sort(
                key=lambda row: (
                    -int(row.get("rank_change") or 0),
                    -int(row.get("accepted_change") or 0),
                    int(row["rank"]),
                )
            )
            rows = candidates[:count]
            if not rows:
                yield self._text_result(event, "今天暂时还没有名次上升记录。")
                return
            body = [
                f"{index}. {row['user_id']} {str(row.get('nickname') or '未设置昵称')}｜"
                f"第 {int(row['baseline_rank'])} 名 → 第 {int(row['rank'])} 名｜"
                f"提升 {int(row['rank_change'])} 名"
                for index, row in enumerate(rows, 1)
            ]
            header = (
                f"今日名次提升榜（前 {len(rows)} 名）\n"
                f"统计从 {short_iso_time(snapshot.baseline_fetched_at)} 开始，截至 "
                f"{short_iso_time(snapshot.fetched_at_text)}。"
                f"{self._freshness(snapshot)}"
            )
            yield self._text_result(event, "\n".join([header, *body]))

    async def _scoped_leaderboard(
        self,
        event: AstrMessageEvent,
        path: str,
        title: str,
        limit: int,
    ):
        if not self._allowed(event):
            yield self._text_result(event, "当前会话未获准查询榜单。")
            return
        try:
            snapshot = self._load_scoped(path)
        except RankDataError as exc:
            yield self._text_result(event, f"暂时无法查询{title}：{exc}")
            return
        if not snapshot.users:
            yield self._text_result(event, f"{snapshot.prefix} {title}目前还没有数据。")
            return
        count = self._requested_count(limit)
        rows = [
            f"{index}. {format_user(row, compact=True)}"
            for index, row in enumerate(snapshot.users[:count], 1)
        ]
        header = (
            f"{title}（前 {len(rows)} 名）\n"
            f"共 {snapshot.user_count} 人｜更新于 {short_time(snapshot)}"
            f"{self._freshness(snapshot)}"
        )
        yield self._text_result(event, "\n".join([header, *rows]))

    @filter.command("历史单日榜")
    async def historical_daily(self, event: AstrMessageEvent, page_number: int = 1):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            if page_number < 1:
                yield self._text_result(event, "页码必须从 1 开始，例如：/历史单日榜 2")
                return
            self._last_views[self._page_key(event)] = (
                HISTORICAL_DAILY_IMAGE_MANIFEST_FILE, HISTORICAL_DAILY_FILE,
                "历史单日新增总榜", page_number, "historical-daily", None,
            )
            async for result in self._leaderboard_image(
                event, page_number, HISTORICAL_DAILY_IMAGE_MANIFEST_FILE,
                HISTORICAL_DAILY_FILE, "历史单日新增总榜", "historical-daily",
            ):
                yield result

    @filter.command("周榜")
    async def weekly(self, event: AstrMessageEvent, page_number: int = 1):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            if page_number < 1:
                yield self._text_result(event, "页码必须从 1 开始，例如：/周榜 2")
                return
            async for result in self._leaderboard_image(
                event,
                page_number,
                WEEKLY_IMAGE_MANIFEST_FILE,
                WEEKLY_FILE,
                "本周 OJ 排名",
                "weekly",
            ):
                yield result

    @filter.command("月榜")
    async def monthly(self, event: AstrMessageEvent, page_number: int = 1):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            if page_number < 1:
                yield self._text_result(event, "页码必须从 1 开始，例如：/月榜 2")
                return
            async for result in self._leaderboard_image(
                event,
                page_number,
                MONTHLY_IMAGE_MANIFEST_FILE,
                MONTHLY_FILE,
                "本月 OJ 排名",
                "monthly",
            ):
                yield result

    async def _activity_leaderboard(
        self,
        event: AstrMessageEvent,
        field: str,
        label: str,
        limit: int,
    ):
        if not self._allowed(event):
            yield self._text_result(event, "当前会话未获准查询榜单。")
            return
        try:
            snapshot = self._load_daily()
        except RankDataError as exc:
            yield self._text_result(event, f"暂时无法查询{label}：{exc}")
            return
        rows = [
            row
            for row in snapshot.changes
            if not row.get("is_new") and int(row.get(field) or 0) > 0
        ]
        rows.sort(
            key=lambda row: (
                -int(row.get(field) or 0),
                -int(row.get("rank_change") or 0),
                int(row["rank"]),
            )
        )
        selected = rows[: self._requested_count(limit)]
        if not selected:
            yield self._text_result(event, f"今天暂时还没有{label}记录。")
            return
        metric_name = "新增通过题数" if field == "accepted_change" else "新增提交次数"
        unit = "题" if field == "accepted_change" else "次"
        body = []
        for index, row in enumerate(selected, 1):
            nickname = str(row.get("nickname") or "未设置昵称")
            body.append(
                f"{index}. {row['user_id']} {nickname}｜"
                f"{metric_name} {int(row[field])} {unit}｜当前第 {int(row['rank'])} 名"
            )
        header = (
            f"今日{metric_name}榜（前 {len(selected)} 名）\n"
            f"统计从 {short_iso_time(snapshot.baseline_fetched_at)} 开始，截至 "
            f"{short_iso_time(snapshot.fetched_at_text)}。"
            f"{self._freshness(snapshot)}"
        )
        yield self._text_result(event, "\n".join([header, *body]))

    @filter.command("刷题榜")
    async def accepted_activity(self, event: AstrMessageEvent, limit: int = 0):
        with data.pin_publication():
            async for result in self._activity_leaderboard(
                event, "accepted_change", "刷题", limit
            ):
                yield result

    @filter.command("提交榜")
    async def submitted_activity(self, event: AstrMessageEvent, limit: int = 0):
        with data.pin_publication():
            async for result in self._activity_leaderboard(
                event, "submitted_change", "提交", limit
            ):
                yield result

    @filter.command("新星榜")
    async def rising_stars(self, event: AstrMessageEvent, limit: int = 0):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            try:
                snapshot = self._load_daily()
            except RankDataError as exc:
                yield self._text_result(event, f"暂时无法查询新星榜：{exc}")
                return
            rows = [
                row
                for row in snapshot.changes
                if row.get("baseline_rank") is not None
                and int(row["baseline_rank"]) > 100
                and int(row.get("accepted_change") or 0) > 0
            ]
            rows.sort(
                key=lambda row: (
                    -int(row.get("accepted_change") or 0),
                    -int(row.get("rank_change") or 0),
                    int(row["rank"]),
                )
            )
            selected = rows[: self._requested_count(limit)]
            if not selected:
                yield self._text_result(event, "今天暂时还没有符合条件的新星选手。")
                return
            body = []
            for index, row in enumerate(selected, 1):
                nickname = str(row.get("nickname") or "未设置昵称")
                body.append(
                    f"{index}. {row['user_id']} {nickname}｜"
                    f"今天新增通过 {int(row.get('accepted_change') or 0)} 题｜"
                    f"当前第 {int(row['rank'])} 名"
                )
            header = (
                f"今日新星榜（前 {len(selected)} 名）\n"
                "范围：今天首次记录时排在第 100 名以后，并且今天有新增通过。\n"
                f"统计截至 {short_iso_time(snapshot.fetched_at_text)}。"
                f"{self._freshness(snapshot)}"
            )
            yield self._text_result(event, "\n".join([header, *body]))

    @filter.command("随机选手")
    async def random_player(self, event: AstrMessageEvent):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            group_key, user_id = self._random_player_rate_keys(event)
            if group_key is not None:
                decision = self._random_player_limiter.check(group_key, user_id)
                if not decision.allowed:
                    notices = []
                    if decision.notify_user:
                        notices.append("你每分钟最多可使用 /随机选手 3 次，请稍后再试。")
                    if decision.notify_group:
                        notices.append("本群每分钟最多可使用 /随机选手 10 次，请稍后再试。")
                    if notices:
                        yield self._text_result(event, "\n".join(notices))
                    return
            try:
                snapshot = self._load()
            except RankDataError as exc:
                yield self._text_result(event, f"暂时无法随机选人：{exc}")
                return
            candidates = [
                row
                for row in snapshot.users
                if int(row.get("accepted") or 0) > 0
                or int(row.get("submitted") or 0) > 0
            ]
            if not candidates:
                yield self._text_result(event, "当前榜单暂时没有有 AC 或提交记录的同学。")
                return
            row = secrets.choice(candidates)
            yield self._text_result(event,
                "随机选手\n" + format_user(row) + f"\n更新 {short_time(snapshot)}"
            )

    @filter.command("日榜")
    async def daily_migration(self, event: AstrMessageEvent):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查询榜单。")
                return
            yield self._text_result(event,
                "今日数据已分为以下几个榜单：\n"
                "/变化：查看全榜或个人今天的变化\n"
                "/冲榜：查看今天名次提升最多的同学\n"
                "/刷题榜：查看今天通过题目最多的同学\n"
                "/提交榜：查看今天提交次数最多的同学"
            )

    @filter.command("开发者帮助", priority=HELP_PRIORITY)
    async def developer_help(self, event: AstrMessageEvent):
        with data.pin_publication():
            if self._allowed(event):
                yield self._text_result(event, DEVELOPER_HELP_TEXT)

    @filter.command("帮助", priority=HELP_PRIORITY)
    async def help_cn(self, event: AstrMessageEvent):
        with data.pin_publication():
            if self._allowed(event):
                yield self._text_result(event, HELP_TEXT)

    @filter.command("更新日志", priority=HELP_PRIORITY)
    async def changelog(self, event: AstrMessageEvent):
        with data.pin_publication():
            if self._allowed(event):
                yield self._text_result(event, CHANGELOG_TEXT)

    # AstrBot removes the wake prefix (/) before filters run, so this matches "oj".
    @filter.regex(OJ_GUIDE_COMMAND_PATTERN, priority=HELP_PRIORITY)
    async def oj_guide(self, event: AstrMessageEvent):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查看 OJ 使用帮助。")
                return
            try:
                nodes = self._oj_guide_nodes(event)
            except OSError as exc:
                logger.error(f"[{PLUGIN_NAME}] 读取 OJ 教程图片失败: {exc}")
                yield self._text_result(event, f"OJ 使用教程暂时无法读取：{exc}")
                return
            yield event.chain_result([Nodes(nodes=nodes)])

    @filter.regex(PTA_GUIDE_COMMAND_PATTERN, priority=HELP_PRIORITY)
    async def pta_guide(self, event: AstrMessageEvent):
        with data.pin_publication():
            if not self._allowed(event):
                yield self._text_result(event, "当前会话未获准查看 PTA 使用帮助。")
                return
            try:
                nodes = self._pta_guide_nodes(event)
            except OSError as exc:
                logger.error(f"[{PLUGIN_NAME}] 读取 PTA 教程图片失败: {exc}")
                yield self._text_result(event, f"PTA 使用教程暂时无法读取：{exc}")
                return
            yield event.chain_result([Nodes(nodes=nodes)])
