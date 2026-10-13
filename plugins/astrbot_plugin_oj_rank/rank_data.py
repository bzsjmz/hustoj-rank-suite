from __future__ import annotations

try:
    from .publication import published_path, pin_publication
except ImportError:
    from publication import published_path, pin_publication


import csv
import gzip
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from pathlib import Path
from typing import Any


class RankDataError(RuntimeError):
    """The exported rank snapshot is unavailable or malformed."""


SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,32}")
OJ_GUIDE_COMMAND_PATTERN = re.compile(r"^oj\S*(?:\s.*)?$", re.IGNORECASE)
PTA_GUIDE_COMMAND_PATTERN = re.compile(r"^pta$", re.IGNORECASE)


ALLOWED_GROUP_COMMANDS = frozenset(
    {
        "榜单",
        "计院榜单",
        "软院榜单",
        "班级榜单",
        "专业榜单",
        "最卷班级",
        "最卷专业",
        "翻页",
        "文字榜单",
        "查榜",
        "提交记录",
        "榜单状态",
        "周榜",
        "月榜",
        "变化",
        "冲榜",
        "刷题榜",
        "历史单日榜",
        "提交榜",
        "新星榜",
        "随机选手",
        "日榜",
        "帮助",
        "更新日志",
        "统计数据",
        "开发者帮助",
        "群分析",
        "群漫画",
        "增量状态",
    }
)


def is_oj_guide_command(text: str) -> bool:
    """Recognize raw slash commands that should open the OJ usage guide."""
    stripped = text.strip()
    return bool(
        stripped.startswith("/")
        and OJ_GUIDE_COMMAND_PATTERN.fullmatch(stripped[1:])
    )


def is_pta_guide_command(text: str) -> bool:
    """Recognize the exact raw slash command that opens the PTA usage guide."""
    stripped = text.strip()
    return bool(
        stripped.startswith("/")
        and PTA_GUIDE_COMMAND_PATTERN.fullmatch(stripped[1:])
    )


def is_allowed_group_command(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    if is_oj_guide_command(stripped) or is_pta_guide_command(stripped):
        return True
    parts = stripped[1:].split(maxsplit=1)
    if not parts:
        return False
    command = parts[0].strip().casefold()
    return command in ALLOWED_GROUP_COMMANDS


@dataclass(frozen=True)
class RandomPlayerRateLimit:
    allowed: bool
    notify_user: bool = False
    notify_group: bool = False


class RandomPlayerRateLimiter:
    """In-memory rolling-window limiter for the random-player command."""

    def __init__(
        self, *, window_seconds: float = 60.0, user_limit: int = 3,
        group_limit: int = 10,
    ) -> None:
        self.window_seconds = window_seconds
        self.user_limit = user_limit
        self.group_limit = group_limit
        self._user_requests: dict[tuple[str, str], list[float]] = {}
        self._group_requests: dict[str, list[float]] = {}
        self._user_warning_until: dict[tuple[str, str], float] = {}
        self._group_warning_until: dict[str, float] = {}
        self._last_cleanup = 0.0

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self.window_seconds:
            return
        threshold = now - self.window_seconds
        for requests in (self._user_requests, self._group_requests):
            for key, timestamps in list(requests.items()):
                kept = [timestamp for timestamp in timestamps if timestamp > threshold]
                if kept:
                    requests[key] = kept
                else:
                    del requests[key]
        for warnings in (self._user_warning_until, self._group_warning_until):
            for key, until in list(warnings.items()):
                if until <= now:
                    del warnings[key]
        self._last_cleanup = now

    def check(
        self, group_key: str | None, user_id: str, now: float | None = None
    ) -> RandomPlayerRateLimit:
        current = monotonic() if now is None else now
        self._cleanup(current)
        threshold = current - self.window_seconds
        user_key = ((group_key or "private"), user_id)
        user_requests = [
            timestamp for timestamp in self._user_requests.get(user_key, [])
            if timestamp > threshold
        ]
        self._user_requests[user_key] = user_requests
        group_requests: list[float] = []
        if group_key is not None:
            group_requests = [
                timestamp for timestamp in self._group_requests.get(group_key, [])
                if timestamp > threshold
            ]
            self._group_requests[group_key] = group_requests

        user_limited = len(user_requests) >= self.user_limit
        group_limited = group_key is not None and len(group_requests) >= self.group_limit
        if user_limited or group_limited:
            notify_user = False
            notify_group = False
            if user_limited:
                until = user_requests[0] + self.window_seconds
                if self._user_warning_until.get(user_key, 0.0) <= current:
                    self._user_warning_until[user_key] = until
                    notify_user = True
            if group_limited and group_key is not None:
                until = group_requests[0] + self.window_seconds
                if self._group_warning_until.get(group_key, 0.0) <= current:
                    self._group_warning_until[group_key] = until
                    notify_group = True
            return RandomPlayerRateLimit(False, notify_user, notify_group)

        user_requests.append(current)
        self._user_requests[user_key] = user_requests
        if group_key is not None:
            group_requests.append(current)
            self._group_requests[group_key] = group_requests
        return RandomPlayerRateLimit(True)


@dataclass(frozen=True)
class RankSnapshot:
    snapshot_id: int
    fetched_at: datetime
    fetched_at_text: str
    prefix: str
    user_count: int
    users: list[dict[str, Any]]
    historical_roster_user_ids: frozenset[str]
    prefix_fetched_at: dict[str, str] | None = None
    history_status: str = "ready"

    @property
    def historical_user_count(self) -> int:
        return len(self.historical_roster_user_ids)

    @property
    def missing_user_count(self) -> int:
        return len(self.historical_roster_user_ids - {str(row["user_id"]) for row in self.users})

    @property
    def coverage_rate(self) -> float:
        return self.user_count / self.historical_user_count

    def age_minutes(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        fetched = self.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return max(0, int((current - fetched).total_seconds() // 60))


@dataclass(frozen=True)
class StatisticsSnapshotReference:
    snapshot_id: int
    fetched_at: datetime
    fetched_at_text: str
    user_count: int


@dataclass(frozen=True)
class DailySnapshot:
    snapshot_id: int
    fetched_at: datetime
    fetched_at_text: str
    baseline_snapshot_id: int
    baseline_fetched_at: str
    prefix: str
    user_count: int
    changes: list[dict[str, Any]]

    def age_minutes(self, now: datetime | None = None) -> int:
        current = now or datetime.now(timezone.utc)
        fetched = self.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return max(0, int((current - fetched).total_seconds() // 60))


@dataclass(frozen=True)
class RankImageManifest:
    snapshot_id: int
    fetched_at: str
    prefix: str
    user_count: int
    page_size: int
    page_count: int
    directory: str
    root: Path
    pages: dict[int, str] | None = None

    def page_path(self, page_number: int) -> Path:
        if page_number < 1 or page_number > self.page_count:
            raise RankDataError(f"页码超出范围（1-{self.page_count}）")
        if self.pages is None:
            snapshot_dir = (self.root / self.directory).resolve()
            if snapshot_dir.parent != self.root.resolve():
                raise RankDataError("榜单图片路径不安全")
            image = snapshot_dir / f"page-{page_number:03d}.png"
        else:
            relative = self.pages.get(page_number)
            if relative is None:
                raise RankDataError("该页榜单图片尚未生成")
            image = (self.root / relative).resolve()
            try:
                image.relative_to(self.root.resolve())
            except ValueError as exc:
                raise RankDataError("榜单图片路径不安全") from exc
        if not image.is_file():
            raise RankDataError("该页榜单图片尚未生成")
        return image


def load_snapshot(path: str | Path, allow_empty: bool = False) -> RankSnapshot:
    source = published_path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("榜单数据尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("榜单数据暂时无法读取") from exc
    return _snapshot_from_payload(payload, allow_empty=allow_empty)


def _snapshot_from_payload(payload: Any, allow_empty: bool = False) -> RankSnapshot:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("榜单数据格式不兼容")

    users = payload.get("users")
    if not isinstance(users, list) or (not users and not allow_empty):
        raise RankDataError("榜单数据为空")

    required = {
        "rank",
        "user_id",
        "nickname",
        "accepted",
        "submitted",
        "ratio",
        "level",
    }
    normalized: list[dict[str, Any]] = []
    for row in users:
        if not isinstance(row, dict) or not required.issubset(row):
            raise RankDataError("榜单用户记录不完整")
        if "first_seen_name" in row and not isinstance(row["first_seen_name"], str):
            raise RankDataError("首次记录姓名格式无效")
        normalized.append(row)

    declared_count = payload.get("user_count")
    if declared_count != len(normalized):
        raise RankDataError("榜单人数校验失败")
    current_user_ids = frozenset(str(row["user_id"]) for row in normalized)
    if payload.get("board") == "historical-daily":
        keys = set()
        previous_gain = None
        expected_rank = 0
        for index, row in enumerate(normalized, 1):
            try:
                date = row["date"]
                parsed = datetime.strptime(date, "%Y-%m-%d")
                if parsed.strftime("%Y-%m-%d") != date:
                    raise ValueError()
                start = datetime.fromisoformat(row["start_at"])
                end = datetime.fromisoformat(row["end_at"])
                if start >= end:
                    raise ValueError()
                gain = row["accepted_change"]
                if type(gain) is not int or gain <= 0 or row["accepted"] != gain:
                    raise ValueError()
                if previous_gain is not None and gain > previous_gain:
                    raise ValueError()
                if previous_gain != gain:
                    expected_rank = index
                if row["rank"] != expected_rank:
                    raise ValueError()
                previous_gain = gain
                key = (row["user_id"], date)
                if key in keys:
                    raise ValueError()
                keys.add(key)
            except (KeyError, TypeError, ValueError):
                raise RankDataError("历史单日榜记录无效") from None
        if payload.get("record_count") != len(normalized):
            raise RankDataError("历史单日榜记录数校验失败")
    elif len(current_user_ids) != len(normalized):
        raise RankDataError("榜单存在重复学号")

    historical_roster = payload.get("historical_roster")
    if historical_roster is None:
        # Compatibility with snapshots exported before the historical roster field.
        historical_roster_user_ids = current_user_ids
    else:
        if not isinstance(historical_roster, dict):
            raise RankDataError("历史名册格式无效")
        roster_user_ids = historical_roster.get("user_ids")
        if (
            not isinstance(roster_user_ids, list)
            or historical_roster.get("user_count") != len(roster_user_ids)
            or any(not isinstance(user_id, str) or not user_id for user_id in roster_user_ids)
        ):
            raise RankDataError("历史名册字段无效")
        historical_roster_user_ids = frozenset(roster_user_ids)
        if (
            not historical_roster_user_ids
            or len(historical_roster_user_ids) != len(roster_user_ids)
            or not current_user_ids.issubset(historical_roster_user_ids)
        ):
            raise RankDataError("历史名册人数校验失败")

    fetched_at_text = str(payload.get("fetched_at", ""))
    try:
        fetched_at = datetime.fromisoformat(fetched_at_text)
    except ValueError as exc:
        raise RankDataError("榜单更新时间格式错误") from exc

    cohort_times = None
    if "lookup_prefixes" in payload or "prefix_fetched_at" in payload:
        raw_times = payload.get("prefix_fetched_at", {})
        if not isinstance(raw_times, dict):
            raise RankDataError("年级更新时间格式错误")
        cohort_times = {}
        for cohort, value in raw_times.items():
            if not isinstance(cohort, str) or not cohort or not isinstance(value, str):
                raise RankDataError("年级更新时间格式错误")
            try:
                datetime.fromisoformat(value)
            except ValueError:
                raise RankDataError("年级更新时间格式错误") from None
            cohort_times[cohort] = value

    return RankSnapshot(
        snapshot_id=int(payload.get("snapshot_id", 0)),
        fetched_at=fetched_at,
        fetched_at_text=fetched_at_text,
        prefix=str(payload.get("prefix", "")),
        user_count=len(normalized),
        users=normalized,
        historical_roster_user_ids=historical_roster_user_ids,
        prefix_fetched_at=cohort_times,
        history_status=str(payload.get("history_status", "ready")),
    )


def load_statistics_snapshot_index(path: str | Path) -> tuple[StatisticsSnapshotReference, ...]:
    source = published_path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("历史统计快照尚未发布") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("历史统计快照目录暂时无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("历史统计快照目录格式不兼容")
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        raise RankDataError("历史统计快照目录为空")
    references: list[StatisticsSnapshotReference] = []
    seen_ids: set[int] = set()
    for item in snapshots:
        if not isinstance(item, dict):
            raise RankDataError("历史统计快照目录字段无效")
        try:
            snapshot_id = int(item["snapshot_id"])
            fetched_at_text = str(item["fetched_at"])
            fetched_at = datetime.fromisoformat(fetched_at_text)
            user_count = int(item["user_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RankDataError("历史统计快照目录字段无效") from exc
        if snapshot_id <= 0 or user_count <= 0 or snapshot_id in seen_ids:
            raise RankDataError("历史统计快照目录校验失败")
        seen_ids.add(snapshot_id)
        references.append(StatisticsSnapshotReference(
            snapshot_id, fetched_at, fetched_at_text, user_count
        ))
    return tuple(sorted(references, key=lambda item: item.snapshot_id))


def load_statistics_snapshot(
    root: str | Path, reference: StatisticsSnapshotReference
) -> RankSnapshot:
    snapshot_root = Path(root).resolve()
    source = (snapshot_root / f"snapshot-{reference.snapshot_id}.json.gz").resolve()
    if source.parent != snapshot_root:
        raise RankDataError("历史统计快照路径不安全")
    try:
        payload = json.loads(gzip.decompress(source.read_bytes()).decode("utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError(f"快照 #{reference.snapshot_id} 尚未准备完成") from exc
    except (OSError, UnicodeError, gzip.BadGzipFile, json.JSONDecodeError) as exc:
        raise RankDataError("历史统计快照暂时无法读取") from exc
    snapshot = _snapshot_from_payload(payload)
    if (
        snapshot.snapshot_id != reference.snapshot_id
        or snapshot.fetched_at_text != reference.fetched_at_text
        or snapshot.user_count != reference.user_count
    ):
        raise RankDataError("历史统计快照校验失败")
    return snapshot


def nearest_statistics_snapshot(
    references: tuple[StatisticsSnapshotReference, ...], target: datetime,
    *, max_deviation_seconds: int = 1800, not_after: datetime | None = None,
) -> StatisticsSnapshotReference:
    if not references:
        raise RankDataError("历史统计快照目录为空")
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)

    def distance(reference: StatisticsSnapshotReference) -> tuple[float, int]:
        fetched_at = reference.fetched_at
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return (abs((fetched_at - target).total_seconds()), -reference.snapshot_id)

    if not_after is not None:
        if not_after.tzinfo is None:
            not_after = not_after.replace(tzinfo=timezone.utc)
        references = tuple(item for item in references
                           if (item.fetched_at if item.fetched_at.tzinfo else item.fetched_at.replace(tzinfo=timezone.utc)) <= not_after)
    if not references:
        raise RankDataError("零点及之前没有可用统计快照")
    selected = min(references, key=distance)
    if distance(selected)[0] > max_deviation_seconds:
        raise RankDataError(f"{target:%Y-%m-%d %H:%M} 附近缺少快照（允许偏差 {max_deviation_seconds // 60} 分钟）")
    return selected


def load_daily(path: str | Path) -> DailySnapshot:
    source = published_path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("每日变化数据尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("每日变化数据暂时无法读取") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("每日变化数据格式不兼容")

    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes:
        raise RankDataError("每日变化数据为空")
    required = {
        "rank",
        "user_id",
        "nickname",
        "accepted",
        "submitted",
        "ratio",
        "level",
        "baseline_rank",
        "rank_change",
        "accepted_change",
        "submitted_change",
        "is_new",
    }
    for row in changes:
        if not isinstance(row, dict) or not required.issubset(row):
            raise RankDataError("每日变化用户记录不完整")
    if payload.get("user_count") != len(changes):
        raise RankDataError("每日变化人数校验失败")

    fetched_at_text = str(payload.get("fetched_at", ""))
    try:
        fetched_at = datetime.fromisoformat(fetched_at_text)
    except ValueError as exc:
        raise RankDataError("每日变化更新时间格式错误") from exc

    return DailySnapshot(
        snapshot_id=int(payload.get("snapshot_id", 0)),
        fetched_at=fetched_at,
        fetched_at_text=fetched_at_text,
        baseline_snapshot_id=int(payload.get("baseline_snapshot_id", 0)),
        baseline_fetched_at=str(payload.get("baseline_fetched_at", "")),
        prefix=str(payload.get("prefix", "")),
        user_count=len(changes),
        changes=changes,
    )


def load_image_manifest(path: str | Path) -> RankImageManifest:
    source = published_path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("榜单图片尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("榜单图片清单暂时无法读取") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
        raise RankDataError("榜单图片清单格式不兼容")
    schema_version = int(payload["schema_version"])
    try:
        snapshot_id = int(payload["snapshot_id"])
        user_count = int(payload["user_count"])
        page_size = int(payload["page_size"])
        page_count = int(payload["page_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RankDataError("榜单图片清单字段无效") from exc
    if snapshot_id <= 0 or user_count <= 0 or page_size <= 0 or page_count <= 0:
        raise RankDataError("榜单图片清单数值无效")
    if page_count != math.ceil(user_count / page_size):
        raise RankDataError("榜单图片页数校验失败")
    directory = ""
    pages = None
    if schema_version == 1:
        directory = str(payload.get("directory", ""))
        if not re.fullmatch(r"snapshot-\d+", directory):
            raise RankDataError("榜单图片目录无效")
        if directory != f"snapshot-{snapshot_id}":
            raise RankDataError("榜单图片快照校验失败")
    else:
        raw_pages = payload.get("pages")
        if not isinstance(raw_pages, dict):
            raise RankDataError("榜单图片页缓存字段无效")
        pages = {}
        for page_text, item in raw_pages.items():
            try:
                page_number = int(page_text)
            except (TypeError, ValueError) as exc:
                raise RankDataError("榜单图片页缓存字段无效") from exc
            if (
                page_number < 1
                or page_number > page_count
                or not isinstance(item, dict)
                or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("content_hash", "")))
            ):
                raise RankDataError("榜单图片页缓存字段无效")
            relative = str(item.get("file", ""))
            if not re.fullmatch(r"pages/page-\d{3}-[0-9a-f]{16}\.png", relative):
                raise RankDataError("榜单图片页缓存路径无效")
            pages[page_number] = relative

    return RankImageManifest(
        snapshot_id=snapshot_id,
        fetched_at=str(payload.get("fetched_at", "")),
        prefix=str(payload.get("prefix", "")),
        user_count=user_count,
        page_size=page_size,
        page_count=page_count,
        directory=directory,
        root=source.parent,
        pages=pages,
    )


def find_users(snapshot: RankSnapshot, query: str, limit: int = 10) -> list[dict[str, Any]]:
    needle = query.strip().casefold()
    if not needle:
        return []

    user_id_matches = [
        row for row in snapshot.users if str(row['user_id']).casefold() == needle
    ]
    if user_id_matches:
        return user_id_matches[:limit]

    exact_nickname = [
        row for row in snapshot.users if str(row['nickname']).strip().casefold() == needle
        or str(row.get('first_seen_name', '')).strip().casefold() == needle
    ]
    if exact_nickname:
        return exact_nickname[:limit]

    return [
        row
        for row in snapshot.users
        if needle in str(row['nickname']).casefold()
        or needle in str(row.get('first_seen_name', '')).casefold()
        or needle in str(row['user_id']).casefold()
    ][:limit]


def find_exact_users(snapshot: RankSnapshot, query: str) -> list[dict[str, Any]]:
    """Resolve a submission query without fuzzy matching the wrong student."""
    needle = query.strip().casefold()
    if not needle:
        return []
    exact_ids = [
        row for row in snapshot.users if str(row["user_id"]).casefold() == needle
    ]
    if exact_ids:
        return exact_ids
    return [
        row for row in snapshot.users
        if str(row["nickname"]).strip().casefold() == needle
        or str(row.get("first_seen_name", "")).strip().casefold() == needle
    ]


def validate_submission_response(
    payload: Any, expected_user_id: str
) -> tuple[str, list[dict[str, Any]]]:
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("user_id") != expected_user_id
    ):
        raise RankDataError("提交记录响应格式不兼容")
    fetched_at = payload.get("fetched_at")
    records = payload.get("records")
    if not isinstance(fetched_at, str) or not isinstance(records, list) or len(records) > 50:
        raise RankDataError("提交记录响应无效")
    try:
        datetime.fromisoformat(fetched_at)
    except ValueError as exc:
        raise RankDataError("提交记录查询时间无效") from exc
    required = {
        "submission_id", "user_id", "problem_id", "result", "memory_kb",
        "time_ms", "language", "code_length", "submitted_at", "judge",
    }
    normalized = []
    seen_ids = set()
    for row in records:
        if not isinstance(row, dict) or not required.issubset(row):
            raise RankDataError("提交记录字段不完整")
        if row["user_id"] != expected_user_id:
            raise RankDataError("提交记录学号不匹配")
        if (
            type(row["submission_id"]) is not int or row["submission_id"] <= 0
            or type(row["memory_kb"]) is not int or row["memory_kb"] < 0
            or type(row["time_ms"]) is not int or row["time_ms"] < 0
        ):
            raise RankDataError("提交记录数值无效")
        if row["submission_id"] in seen_ids:
            raise RankDataError("提交记录编号重复")
        seen_ids.add(row["submission_id"])
        for key in ("problem_id", "result", "language", "code_length", "submitted_at"):
            if not isinstance(row[key], str) or not row[key]:
                raise RankDataError("提交记录文本字段无效")
        if not isinstance(row["judge"], str):
            raise RankDataError("提交记录判题机字段无效")
        try:
            datetime.strptime(row["submitted_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise RankDataError("提交时间格式无效") from exc
        normalized.append(row)
    return fetched_at, normalized


def find_changes(
    snapshot: DailySnapshot, query: str, limit: int = 10
) -> list[dict[str, Any]]:
    needle = query.strip().casefold()
    if not needle:
        return []
    exact_id = [
        row for row in snapshot.changes if str(row['user_id']).casefold() == needle
    ]
    if exact_id:
        return exact_id[:limit]
    exact_nickname = [
        row
        for row in snapshot.changes
        if str(row['nickname']).strip().casefold() == needle
    ]
    if exact_nickname:
        return exact_nickname[:limit]
    return [
        row
        for row in snapshot.changes
        if needle in str(row['nickname']).casefold()
        or needle in str(row['user_id']).casefold()
    ][:limit]


def short_time(snapshot: RankSnapshot) -> str:
    return snapshot.fetched_at.strftime("%m-%d %H:%M")


def short_iso_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%m-%d %H:%M")
    except ValueError:
        return value


def lookup_update_text(snapshot: RankSnapshot, row: dict[str, Any], now: datetime | None = None) -> str:
    if row.get("is_historical"):
        value = str(row.get("last_seen_at", ""))
        label = "历史记录时间"
    else:
        label = "该年级数据更新"
        if snapshot.prefix_fetched_at is None:
            value = snapshot.fetched_at_text
        else:
            keys = sorted(snapshot.prefix_fetched_at, key=len, reverse=True)
            prefix = next((key for key in keys if str(row["user_id"]).startswith(key)), None)
            value = snapshot.prefix_fetched_at.get(prefix, "") if prefix else ""
    try:
        fetched = datetime.fromisoformat(value)
    except ValueError:
        return label + "时间未知"
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    age = max(0, int((current - fetched).total_seconds() // 60))
    return f"{label} {fetched:%m-%d %H:%M}（距今 {age} 分钟）"


def spreadsheet_safe_value(value: Any) -> Any:
    """Only statistical CSVs use this; raw exports preserve source text."""
    if isinstance(value, str) and (value.startswith(("\t", "\r", "\n"))
                                  or value.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def format_user(row: dict[str, Any], compact: bool = False) -> str:
    nickname = str(row.get("nickname") or "未设置昵称")
    rank_label = "历史名次" if row.get("is_historical") else "当前排名"
    if compact:
        return (
            f"第 {row['rank']} 名｜{row['user_id']} {nickname}｜"
            f"通过 {row['accepted']} 题"
        )
    return (
        f"{row['user_id']} {nickname}\n"
        f"{rank_label}：第 {row['rank']} 名\n"
        f"通过 {row['accepted']} 题｜提交 {row['submitted']} 次｜通过率 {row['ratio']}"
    )


def change_label(row: dict[str, Any]) -> str:
    if row.get("is_new"):
        return "今天首次上榜"
    change = int(row.get("rank_change") or 0)
    if change > 0:
        return f"上升 {change} 名"
    if change < 0:
        return f"下降 {abs(change)} 名"
    return "排名不变"


def format_change(row: dict[str, Any], compact: bool = False) -> str:
    nickname = str(row.get("nickname") or "未设置昵称")
    rank = int(row['rank'])
    accepted_change = row.get("accepted_change")
    submitted_change = row.get("submitted_change")
    if compact:
        accepted_text = (
            "今天首次上榜"
            if accepted_change is None
            else f"新增通过 {int(accepted_change)} 题"
        )
        return f"{row['user_id']} {nickname}｜{accepted_text}｜{change_label(row)}"

    baseline_rank = row.get("baseline_rank")
    movement = change_label(row)
    baseline_text = (
        "今天首次上榜"
        if baseline_rank is None
        else f"今天首次记录时第 {int(baseline_rank)} 名"
    )
    accepted_text = (
        "今天新增通过：暂无记录"
        if accepted_change is None
        else f"今天新增通过：{int(accepted_change)} 题"
    )
    submitted_text = (
        "今天新增提交：暂无记录"
        if submitted_change is None
        else f"今天新增提交：{int(submitted_change)} 次"
    )
    return (
        f"{row['user_id']} {nickname}\n"
        f"当前排名：第 {rank} 名\n"
        f"{baseline_text}，{movement}\n"
        f"{accepted_text}｜{submitted_text}"
    )


def is_class_id(value: str, expected_length: int = 10) -> bool:
    return len(value) == expected_length and bool(SAFE_ID_PATTERN.fullmatch(value))


def load_class_image_registry(path: str | Path) -> frozenset[str]:
    source = published_path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("班级榜单图片尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("班级榜单图片清单暂时无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("班级榜单图片清单格式不兼容")
    try:
        expected_length = int(payload.get("class_id_length", 10))
    except (TypeError, ValueError) as exc:
        raise RankDataError("班级榜单图片清单字段无效") from exc
    classes = payload.get("classes")
    if not isinstance(classes, list) or not classes:
        raise RankDataError("班级榜单图片清单为空")
    class_ids = set()
    for item in classes:
        if not isinstance(item, dict):
            raise RankDataError("班级榜单图片清单字段无效")
        class_id = str(item.get("class_id", ""))
        try:
            user_count = int(item["user_count"])
            page_count = int(item["page_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RankDataError("班级榜单图片清单字段无效") from exc
        if not is_class_id(class_id, expected_length) or user_count <= 0 or page_count != 1:
            raise RankDataError("班级榜单图片清单字段无效")
        class_ids.add(class_id)
    if len(class_ids) != len(classes):
        raise RankDataError("班级榜单图片清单存在重复班级")
    return frozenset(class_ids)


def is_major_id(value: str, expected_length: int = 8) -> bool:
    return len(value) == expected_length and bool(SAFE_ID_PATTERN.fullmatch(value))


def load_major_image_registry(path: str | Path) -> frozenset[str]:
    source = published_path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("专业榜单图片尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("专业榜单图片清单暂时无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("专业榜单图片清单格式不兼容")
    try:
        expected_length = int(payload.get("major_id_length", 8))
    except (TypeError, ValueError) as exc:
        raise RankDataError("专业榜单图片清单字段无效") from exc
    majors = payload.get("majors")
    if not isinstance(majors, list) or not majors:
        raise RankDataError("专业榜单图片清单为空")
    major_ids = set()
    for item in majors:
        if not isinstance(item, dict):
            raise RankDataError("专业榜单图片清单字段无效")
        major_id = str(item.get("major_id", ""))
        try:
            user_count = int(item["user_count"])
            page_count = int(item["page_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RankDataError("专业榜单图片清单字段无效") from exc
        if not is_major_id(major_id, expected_length) or user_count <= 0 or page_count <= 0:
            raise RankDataError("专业榜单图片清单字段无效")
        major_ids.add(major_id)
    if len(major_ids) != len(majors):
        raise RankDataError("专业榜单图片清单存在重复专业")
    return frozenset(major_ids)


@dataclass(frozen=True)
class AcademicLabels:
    prefix: str
    major_labels: dict[str, str]
    student_id_length: int = 12
    major_code_length: int = 4
    class_code_length: int = 6
    student_id_pattern: str = r"[0-9]+"

    def __post_init__(self) -> None:
        try:
            re.compile(self.student_id_pattern)
        except re.error as exc:
            raise ValueError("invalid student ID pattern") from exc
        if not SAFE_ID_PATTERN.fullmatch(self.prefix):
            raise ValueError("invalid student ID prefix")
        if not re.fullmatch(self.student_id_pattern, self.prefix):
            raise ValueError("student ID prefix does not match its pattern")
        if not 0 < self.major_code_length < self.class_code_length:
            raise ValueError("invalid major/class code lengths")
        if len(self.prefix) + self.class_code_length >= self.student_id_length:
            raise ValueError("invalid student ID length")

    @property
    def major_id_length(self) -> int:
        return len(self.prefix) + self.major_code_length

    @property
    def class_id_length(self) -> int:
        return len(self.prefix) + self.class_code_length

    def is_student_id(self, value: str) -> bool:
        return len(value) == self.student_id_length and bool(SAFE_ID_PATTERN.fullmatch(value)) and bool(re.fullmatch(self.student_id_pattern, value)) and value.startswith(self.prefix)

    def is_major_id(self, value: str) -> bool:
        return is_major_id(value, self.major_id_length) and bool(re.fullmatch(self.student_id_pattern, value)) and value.startswith(self.prefix)

    def is_class_id(self, value: str) -> bool:
        return is_class_id(value, self.class_id_length) and bool(re.fullmatch(self.student_id_pattern, value)) and value.startswith(self.prefix)

    def resolve_major(self, value: str) -> str | None:
        normalized = value.strip()
        if self.is_major_id(normalized):
            return normalized
        for code, label in self.major_labels.items():
            if normalized == label:
                return self.prefix + code
        return None

    def resolve_class(self, value: str) -> str | None:
        normalized = value.strip()
        if self.is_class_id(normalized):
            return normalized
        suffix_length = self.class_code_length - self.major_code_length
        for code, label in self.major_labels.items():
            match = re.fullmatch(re.escape(label) + rf"([A-Za-z0-9_-]{{{suffix_length}}})班", normalized)
            if match:
                return self.prefix + code + match.group(1)
        return None

    def major_name(self, major_id: str) -> str:
        code = major_id[len(self.prefix):]
        return self.major_labels.get(code, code)

    def class_name(self, class_id: str) -> str:
        major_id = class_id[:self.major_id_length]
        major_name = self.major_name(major_id)
        class_code = class_id[len(self.prefix):]
        major_code = major_id[len(self.prefix):]
        suffix = class_code[self.major_code_length:]
        return class_code if major_name == major_code else f"{major_name}{suffix}班"


def load_academic_labels(path: str | Path) -> AcademicLabels:
    source = published_path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RankDataError("专业名称映射尚未生成") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RankDataError("专业名称映射暂时无法读取") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RankDataError("专业名称映射格式不兼容")
    prefix = str(payload.get("prefix", ""))
    labels = payload.get("major_labels")
    try:
        student_id_length = int(payload.get("student_id_length", len(prefix) + 8))
        major_code_length = int(payload.get("major_code_length", 4))
        class_code_length = int(payload.get("class_code_length", 6))
        student_id_pattern = str(payload.get("student_id_pattern", r"[0-9]+"))
        layout = AcademicLabels(prefix, {}, student_id_length, major_code_length, class_code_length, student_id_pattern)
    except (TypeError, ValueError) as exc:
        raise RankDataError("专业名称映射字段无效") from exc
    if not isinstance(labels, dict):
        raise RankDataError("专业名称映射字段无效")
    normalized: dict[str, str] = {}
    for code, label in labels.items():
        code_text = str(code)
        label_text = str(label).strip()
        if not SAFE_ID_PATTERN.fullmatch(code_text) or len(code_text) != layout.major_code_length or not label_text:
            raise RankDataError("专业名称映射字段无效")
        if label_text in normalized.values():
            raise RankDataError("专业名称映射存在重复名称")
        normalized[code_text] = label_text
    return AcademicLabels(prefix, normalized, student_id_length, major_code_length, class_code_length, student_id_pattern)



STATISTICS_CSV_FIELDS = (
    "row_type",
    "snapshot_time",
    "total_students",
    "current_ranklist_students",
    "missing_students",
    "ranklist_coverage_rate",
    "total_ac",
    "total_submissions",
    "ac_active_students",
    "submit_active_students",
    "ac_participation_rate",
    "submit_participation_rate",
    "profession_code",
    "profession_name",
    "class_code",
    "class_name",
    "rank",
    "student_id",
    "nickname",
    "ac",
    "submissions",
)


def build_statistics_csv(
    snapshot: RankSnapshot,
    labels: AcademicLabels,
    *,
    include_inactive_students: bool = False,
    profession_id: str | None = None,
    class_id: str | None = None,
) -> str:
    """Build statistics with historical roster counts and current dynamic metrics."""
    if snapshot.prefix != labels.prefix:
        raise RankDataError("榜单与专业名称映射的学号前缀不一致")
    if profession_id is not None and not labels.is_major_id(profession_id):
        raise RankDataError("统计专业代码无效")
    if class_id is not None and not labels.is_class_id(class_id):
        raise RankDataError("统计班级代码无效")
    if class_id is not None and profession_id is not None:
        if class_id[: labels.major_id_length] != profession_id:
            raise RankDataError("统计班级不属于指定专业")

    def identity_from_student_id(student_id: str) -> tuple[str, str]:
        if not labels.is_student_id(student_id):
            raise RankDataError("统计数据包含无法识别学号的学生")
        row_profession_id = student_id[: labels.major_id_length]
        row_class_id = student_id[: labels.class_id_length]
        if not labels.is_major_id(row_profession_id) or not labels.is_class_id(row_class_id):
            raise RankDataError("统计数据包含无法识别专业或班级的学生")
        return row_profession_id, row_class_id

    def in_scope(student_id: str) -> bool:
        row_profession_id, row_class_id = identity_from_student_id(student_id)
        return (
            (profession_id is None or row_profession_id == profession_id)
            and (class_id is None or row_class_id == class_id)
        )

    scoped_roster_ids = sorted(
        student_id
        for student_id in snapshot.historical_roster_user_ids
        if in_scope(student_id)
    )
    if not scoped_roster_ids:
        raise RankDataError("历史名册中没有该范围的学生")
    scoped_users = [
        row for row in snapshot.users if in_scope(str(row["user_id"]))
    ]

    def number(row: dict[str, Any], field: str) -> int:
        try:
            return int(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise RankDataError(f"统计数据字段无效：{field}") from exc

    def summary_values(
        roster_ids: list[str], current_users: list[dict[str, Any]]
    ) -> dict[str, Any]:
        current_ids = {str(row["user_id"]) for row in current_users}
        total_students = len(roster_ids)
        total_ac = sum(number(row, "accepted") for row in current_users)
        total_submissions = sum(number(row, "submitted") for row in current_users)
        ac_active = sum(number(row, "accepted") > 0 for row in current_users)
        submit_active = sum(number(row, "submitted") > 0 for row in current_users)
        return {
            "total_students": total_students,
            "current_ranklist_students": len(current_users),
            "missing_students": len(set(roster_ids) - current_ids),
            "ranklist_coverage_rate": f"{len(current_users) / total_students:.2%}",
            "total_ac": total_ac,
            "total_submissions": total_submissions,
            "ac_active_students": ac_active,
            "submit_active_students": submit_active,
            "ac_participation_rate": f"{ac_active / total_students:.2%}",
            "submit_participation_rate": f"{submit_active / total_students:.2%}",
        }

    grouped_roster_professions: dict[str, list[str]] = {}
    grouped_roster_classes: dict[str, list[str]] = {}
    grouped_current_professions: dict[str, list[dict[str, Any]]] = {}
    grouped_current_classes: dict[str, list[dict[str, Any]]] = {}
    for student_id in scoped_roster_ids:
        row_profession_id, row_class_id = identity_from_student_id(student_id)
        grouped_roster_professions.setdefault(row_profession_id, []).append(student_id)
        grouped_roster_classes.setdefault(row_class_id, []).append(student_id)
    for row in scoped_users:
        row_profession_id, row_class_id = identity_from_student_id(str(row["user_id"]))
        grouped_current_professions.setdefault(row_profession_id, []).append(row)
        grouped_current_classes.setdefault(row_class_id, []).append(row)

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=STATISTICS_CSV_FIELDS)
    writer.writeheader()

    def write(row_type: str, **values: Any) -> None:
        row = {field: "" for field in STATISTICS_CSV_FIELDS}
        row["row_type"] = row_type
        row.update(values)
        writer.writerow({key: spreadsheet_safe_value(value) for key, value in row.items()})

    write(
        "snapshot",
        snapshot_time=snapshot.fetched_at_text,
        **summary_values(scoped_roster_ids, scoped_users),
    )
    for current_profession_id in sorted(grouped_roster_professions):
        write(
            "profession",
            profession_code=current_profession_id[len(labels.prefix) :],
            profession_name=labels.major_name(current_profession_id),
            **summary_values(
                grouped_roster_professions[current_profession_id],
                grouped_current_professions.get(current_profession_id, []),
            ),
        )
    for current_class_id in sorted(grouped_roster_classes):
        current_profession_id = current_class_id[: labels.major_id_length]
        write(
            "class",
            profession_code=current_profession_id[len(labels.prefix) :],
            profession_name=labels.major_name(current_profession_id),
            class_code=current_class_id[len(labels.prefix) :],
            class_name=labels.class_name(current_class_id),
            **summary_values(
                grouped_roster_classes[current_class_id],
                grouped_current_classes.get(current_class_id, []),
            ),
        )
    for row in sorted(scoped_users, key=lambda item: (number(item, "rank"), str(item["user_id"]))):
        ac = number(row, "accepted")
        submissions = number(row, "submitted")
        if not include_inactive_students and ac <= 0 and submissions <= 0:
            continue
        current_profession_id, current_class_id = identity_from_student_id(
            str(row["user_id"])
        )
        write(
            "student",
            profession_code=current_profession_id[len(labels.prefix) :],
            profession_name=labels.major_name(current_profession_id),
            class_code=current_class_id[len(labels.prefix) :],
            class_name=labels.class_name(current_class_id),
            rank=number(row, "rank"),
            student_id=str(row["user_id"]),
            nickname=str(row.get("nickname") or ""),
            ac=ac,
            submissions=submissions,
        )
    return output.getvalue()


PERIOD_STATISTICS_CSV_FIELDS = (
    "row_type", "period", "start_snapshot_id", "start_snapshot_time",
    "end_snapshot_id", "end_snapshot_time", "start_total_students",
    "end_total_students", "start_ranklist_students", "end_ranklist_students",
    "new_roster_students", "missing_at_end", "total_ac_change",
    "total_submissions_change", "student_id", "nickname", "start_rank",
    "end_rank", "rank_change", "start_ac", "end_ac", "accepted_change",
    "start_submissions", "end_submissions", "submitted_change", "status",
)


def build_period_statistics_csv(
    start: RankSnapshot, end: RankSnapshot, period: str
) -> str:
    """Build administrator export for changes between two completed-day snapshots."""
    if start.prefix != end.prefix:
        raise RankDataError("周期统计快照的学号前缀不一致")
    if end.fetched_at < start.fetched_at:
        raise RankDataError("周期统计结束快照早于起始快照")

    def number(row: dict[str, Any], field: str) -> int:
        try:
            return int(row[field])
        except (KeyError, TypeError, ValueError) as exc:
            raise RankDataError(f"周期统计字段无效：{field}") from exc

    start_by_id = {str(row["user_id"]): row for row in start.users}
    end_by_id = {str(row["user_id"]): row for row in end.users}
    all_ids = sorted(set(start_by_id) | set(end_by_id))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=PERIOD_STATISTICS_CSV_FIELDS)
    writer.writeheader()

    def write(row_type: str, **values: Any) -> None:
        row = {field: "" for field in PERIOD_STATISTICS_CSV_FIELDS}
        row.update({"row_type": row_type, "period": period,
                    "start_snapshot_id": start.snapshot_id,
                    "start_snapshot_time": start.fetched_at_text,
                    "end_snapshot_id": end.snapshot_id,
                    "end_snapshot_time": end.fetched_at_text})
        row.update(values)
        writer.writerow({key: spreadsheet_safe_value(value) for key, value in row.items()})

    write(
        "summary",
        start_total_students=start.historical_user_count,
        end_total_students=end.historical_user_count,
        start_ranklist_students=start.user_count,
        end_ranklist_students=end.user_count,
        new_roster_students=len(end.historical_roster_user_ids - start.historical_roster_user_ids),
        missing_at_end=end.missing_user_count,
        total_ac_change=sum(number(row, "accepted") for row in end.users)
        - sum(number(row, "accepted") for row in start.users),
        total_submissions_change=sum(number(row, "submitted") for row in end.users)
        - sum(number(row, "submitted") for row in start.users),
    )
    for user_id in all_ids:
        previous = start_by_id.get(user_id)
        current = end_by_id.get(user_id)
        if previous is None:
            write(
                "student", student_id=user_id,
                nickname=str(current.get("nickname") or ""), end_rank=number(current, "rank"),
                end_ac=number(current, "accepted"), end_submissions=number(current, "submitted"),
                status="period_new",
            )
        elif current is None:
            write(
                "student", student_id=user_id,
                nickname=str(previous.get("nickname") or ""), start_rank=number(previous, "rank"),
                start_ac=number(previous, "accepted"), start_submissions=number(previous, "submitted"),
                status="missing_at_period_end",
            )
        else:
            start_rank, end_rank = number(previous, "rank"), number(current, "rank")
            start_ac, end_ac = number(previous, "accepted"), number(current, "accepted")
            start_submissions, end_submissions = number(previous, "submitted"), number(current, "submitted")
            write(
                "student", student_id=user_id,
                nickname=str(current.get("nickname") or previous.get("nickname") or ""),
                start_rank=start_rank, end_rank=end_rank, rank_change=start_rank - end_rank,
                start_ac=start_ac, end_ac=end_ac, accepted_change=end_ac - start_ac,
                start_submissions=start_submissions, end_submissions=end_submissions,
                submitted_change=end_submissions - start_submissions, status="present_at_both_ends",
            )
    return output.getvalue()
