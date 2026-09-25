from __future__ import annotations

import json
from collections import OrderedDict
import logging
import math
import os
import queue
import signal
import socketserver
import threading
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .college import class_display_name, major_display_name
from .config import PROJECT_ROOT, Settings
from .data_publication import current_data_root
from .models import RankEntry
from .renderer import LeaderboardRenderer
from .resource_guard import PressureGuard, write_status


@dataclass(frozen=True)
class RenderRequest:
    board: str
    entity: str | None
    page: int

    @property
    def key(self) -> tuple[str, str | None, int]:
        return (self.board, self.entity, self.page)


class RenderFuture:
    def __init__(self) -> None:
        self.created_at = time.monotonic()
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None

    def wait(self, timeout: float) -> dict[str, Any]:
        if not self.event.wait(timeout) or self.response is None:
            return {"ok": False, "error": "渲染请求超时"}
        return self.response


class SingleFlightQueue:
    """Merge concurrent requests for the same board, entity and page."""

    def __init__(self, limit: int = 32) -> None:
        self.limit = limit
        self.queue: queue.Queue[tuple[RenderRequest, RenderFuture]] = queue.Queue()
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str | None, int], RenderFuture] = {}

    def submit(self, request: RenderRequest) -> tuple[RenderFuture, bool]:
        with self._lock:
            existing = self._active.get(request.key)
            if existing is not None:
                return existing, False
            future = RenderFuture()
            if len(self._active) >= self.limit:
                future.response = {"ok": False, "error": "图片请求较多，请稍后再试"}
                future.event.set()
                return future, False
            self._active[request.key] = future
            self.queue.put((request, future))
            return future, True

    def complete(self, request: RenderRequest, response: dict[str, Any]) -> None:
        with self._lock:
            future = self._active.pop(request.key, None)
        if future is not None:
            future.response = response
            future.event.set()


@dataclass(frozen=True)
class BoardDefinition:
    source: Path
    image_directory: str
    title: str
    page_size: int = 20
    height: int = LeaderboardRenderer.HEIGHT
    column_labels: tuple[str, str, str] | None = None
    entity_type: str | None = None


class BackgroundQueue:
    def __init__(self):
        self.pending = OrderedDict()

    def replace_board(self, board, entity, count):
        for key in list(self.pending):
            if key[:2] == (board, entity):
                del self.pending[key]
        for page in range(1, count + 1):
            request = RenderRequest(board, entity, page)
            self.pending[request.key] = request

    def __len__(self):
        return len(self.pending)

    def pop(self):
        return self.pending.popitem(last=False)[1]


class LazyBrowserContext:
    """Start only for a cache miss; release the renderer browser after idle time."""
    def __init__(self, playwright, settings):
        self.playwright = playwright
        self.settings = settings
        self.browser = None
        self.context = None
        self.last_used = time.monotonic()

    def new_page(self):
        self.last_used = time.monotonic()
        if self.browser is None:
            self.browser = self.playwright.chromium.launch(
                executable_path=self.settings.browser_executable, headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            try:
                self.context = self.browser.new_context()
            except Exception:
                self.close()
                raise
        return self.context.new_page()

    def close(self):
        browser, self.browser = self.browser, None
        self.context = None
        if browser is not None:
            browser.close()

    def release_idle(self, pressured=False):
        if self.browser is not None and (pressured or time.monotonic() - self.last_used >= 120):
            self.close()


class OnDemandRenderer:
    EAGER_BOARDS = (
        RenderRequest("total", None, 1),
        RenderRequest("computer", None, 1),
        RenderRequest("software", None, 1),
        RenderRequest("class-intensity", None, 1),
        RenderRequest("major-intensity", None, 1),
    )

    def __init__(self, settings: Settings, browser_context, logger: logging.Logger):
        self.settings = settings
        self.browser_context = browser_context
        self.logger = logger
        self.layout = settings.student_id_layout
        self.pre_render_pages = max(1, int(os.getenv("PRE_RENDER_PAGES", "3")))

    def definition(self, board: str, entity: str | None = None) -> BoardDefinition:
        share = current_data_root(self.settings.share_dir)
        fixed = {
            "total": BoardDefinition(share / "latest.json", "rank-images", "天梯总榜"),
            "computer": BoardDefinition(
                share / "computer-college.json", "college-images/computer", "计院榜单"
            ),
            "software": BoardDefinition(
                share / "software-college.json", "college-images/software", "软院榜单"
            ),
            "weekly": BoardDefinition(share / "weekly.json", "weekly-images", "周榜"),
            "monthly": BoardDefinition(share / "monthly.json", "monthly-images", "月榜"),
            "class-intensity": BoardDefinition(
                share / "class-intensity.json",
                "class-intensity-images",
                "最卷班级",
                column_labels=("班级", "班级 AC 总量", "班级卷王"),
                entity_type="班级",
            ),
            "major-intensity": BoardDefinition(
                share / "major-intensity.json",
                "major-intensity-images",
                "最卷专业",
                column_labels=("专业", "专业 AC 总量", "卷王班级与同学"),
                entity_type="专业",
            ),
        }
        if board in fixed:
            if entity is not None:
                raise ValueError("该榜单不接受实体参数")
            return fixed[board]
        if board == "class":
            if entity is None or not self.layout.is_class_id(entity):
                raise ValueError("班级编号无效")
            source = share / "class-ranklists" / f"{entity}.json"
            payload = self._load_payload(source)
            has_history = int(payload.get("historical_user_count", 0)) > 0
            count = int(payload.get("user_count", 0))
            return BoardDefinition(
                source,
                f"class-images/{entity}",
                f"{class_display_name(entity, self.layout)}榜单",
                page_size=max(60, count),
                height=LeaderboardRenderer.class_image_height(count, has_history),
            )
        if board == "major":
            if entity is None or not self.layout.is_major_id(entity):
                raise ValueError("专业编号无效")
            return BoardDefinition(
                share / "major-ranklists" / f"{entity}.json",
                f"major-images/{entity}",
                f"{major_display_name(entity, self.layout)}专业榜单",
            )
        raise ValueError("不支持的榜单类型")

    @staticmethod
    def _load_payload(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("榜单数据尚未生成") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("榜单数据暂时无法读取") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("榜单数据格式不兼容")
        users = payload.get("users")
        if not isinstance(users, list) or not users:
            raise ValueError("榜单当前没有可渲染数据")
        if int(payload.get("user_count", -1)) != len(users):
            raise ValueError("榜单人数校验失败")
        return payload

    @staticmethod
    def _entries(payload: dict[str, Any]) -> tuple[list[RankEntry], frozenset[str]]:
        entries = []
        historical = set()
        for row in payload["users"]:
            if not isinstance(row, dict):
                raise ValueError("榜单用户记录无效")
            entry = RankEntry(
                rank=int(row["rank"]),
                user_id=str(row["user_id"]),
                nickname=str(row.get("nickname", "")),
                accepted=int(row["accepted"]),
                submitted=int(row["submitted"]),
                ratio=float(row["ratio"]),
                level=str(row.get("level", "")),
            )
            entries.append(entry)
            if row.get("is_historical"):
                historical.add(entry.user_id)
        return entries, frozenset(historical)

    def render(self, request: RenderRequest, eager: bool = False) -> dict[str, Any]:
        definition = self.definition(request.board, request.entity)
        payload = self._load_payload(definition.source)
        entries, historical = self._entries(payload)
        page_count = math.ceil(len(entries) / definition.page_size)
        if request.page < 1 or request.page > page_count:
            raise ValueError(f"页码超出范围（1-{page_count}）")
        page_numbers = (
            range(1, min(self.pre_render_pages, page_count) + 1)
            if eager
            else (request.page,)
        )
        subtitle = None
        if request.board == "class":
            subtitle = (
                f"当前 ranklist {len(entries) - len(historical)} 人"
                f"｜历史补全 {len(historical)} 人｜共 {len(entries)} 名选手"
            )
        renderer = LeaderboardRenderer(
            self.settings.share_dir,
            definition.image_directory,
            page_size=definition.page_size,
            height=definition.height,
        )
        started = time.monotonic()
        manifest = renderer.render(
            self.browser_context,
            entries,
            str(payload.get("prefix", self.settings.prefix)),
            str(payload.get("fetched_at", "")),
            max(1, int(payload.get("snapshot_id", 0))),
            title=definition.title,
            column_labels=definition.column_labels,
            historical_user_ids=historical,
            subtitle=subtitle,
            entity_type=definition.entity_type,
            page_numbers=page_numbers,
        )
        page = manifest["pages"].get(str(request.page))
        if not isinstance(page, dict) or not isinstance(page.get("file"), str):
            raise RuntimeError("请求页面未发布")
        relative = str(Path(definition.image_directory) / page["file"])
        self.logger.info(
            "render %s entity=%s pages=%s rendered=%d cached=%d elapsed=%.2fs",
            request.board,
            request.entity or "-",
            list(page_numbers),
            manifest["rendered_page_count"],
            manifest["cached_page_count"],
            time.monotonic() - started,
        )
        return {
            "ok": True,
            "relative_path": relative,
            "page_count": page_count,
            "rendered_page_count": manifest["rendered_page_count"],
        }


class RenderSocketHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            # A deadline for the entire line also bounds clients sending one
            # byte at a time; socket idle timeouts alone do not do that.
            deadline = time.monotonic() + self.server.socket_timeout
            raw = bytearray()
            while b"\n" not in raw and len(raw) < 8193:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.connection.settimeout(remaining)
                chunk = self.connection.recv(8193 - len(raw))
                if not chunk:
                    return
                raw.extend(chunk)
            raw = bytes(raw).split(b"\n", 1)[0]
            if not raw or len(raw) > 8192:
                raise ValueError("请求过长")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            entity_value = payload.get("entity")
            entity = None if entity_value in (None, "") else str(entity_value)
            request = RenderRequest(
                board=str(payload.get("board", "")),
                entity=entity,
                page=int(payload.get("page", 0)),
            )
            future, _ = self.server.coordinator.submit(request)  # type: ignore[attr-defined]
            response = future.wait(self.server.request_timeout)  # type: ignore[attr-defined]
        except OSError:
            return  # Disconnected or timed-out clients are normal, not tracebacks.
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc) or "渲染请求无效"}
        try:
            self.connection.settimeout(self.server.socket_timeout)
            self.wfile.write(
                (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                .encode("utf-8")
            )
        except OSError:
            return


class ThreadedUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, handler, *, max_connections=64, socket_timeout=5.0):
        if type(max_connections) is not int or not 1 <= max_connections <= 256:
            raise ValueError("render connection limit must be between 1 and 256")
        if not math.isfinite(socket_timeout) or not 0 < socket_timeout <= 30:
            raise ValueError("render socket timeout must be finite and within 30 seconds")
        self.socket_timeout = socket_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self._connection_lock = threading.Lock()
        self.active_connections = 0
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        # Acquire BEFORE ThreadingMixIn creates a thread. Queue bounds alone
        # cannot limit idle clients or clients waiting on the same future.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._connection_lock:
            self.active_connections += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_slot()
            raise

    def _release_slot(self):
        with self._connection_lock:
            self.active_connections -= 1
        self._slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_slot()


def _logger(log_dir: Path) -> logging.Logger:
    logger = logging.getLogger("oj_rank.render")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_dir / "renderer.log", maxBytes=10 * 1024 * 1024, backupCount=2,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    return logger


def run() -> int:
    settings = Settings.from_env()
    settings.ensure_directories()
    logger = _logger(settings.log_dir)
    run_dir = Path(os.getenv("OJ_RENDER_RUN_DIR", str(PROJECT_ROOT / "run")))
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.chmod(0o755)
    socket_path = run_dir / "render.sock"
    if socket_path.exists() or socket_path.is_socket():
        socket_path.unlink()

    coordinator = SingleFlightQueue()
    stop_event = threading.Event()

    def stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    with sync_playwright() as playwright:
        context = LazyBrowserContext(playwright, settings)
        guard = PressureGuard()
        renderer = OnDemandRenderer(settings, context, logger)
        server = ThreadedUnixServer(
            str(socket_path), RenderSocketHandler,
            max_connections=int(os.getenv("RENDER_MAX_CONNECTIONS", "64")),
            socket_timeout=float(os.getenv("RENDER_SOCKET_TIMEOUT", "5")),
        )
        server.coordinator = coordinator  # type: ignore[attr-defined]
        server.request_timeout = float(os.getenv("RENDER_REQUEST_TIMEOUT", "90"))
        if not math.isfinite(server.request_timeout) or not 0 < server.request_timeout <= 300:
            raise ValueError("render request timeout must be finite and within 300 seconds")
        socket_path.chmod(0o666)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        logger.info("render service ready, socket=%s", socket_path)

        eager_mtimes: dict[tuple[str, str | None, int], int] = {}
        eager_pages = BackgroundQueue()
        last_status = 0.0
        poll_seconds = max(1.0, float(os.getenv("RENDER_POLL_SECONDS", "3")))
        next_poll = 0.0
        try:
            while not stop_event.is_set():
                was_paused = guard.paused
                pressured = guard.check()
                if was_paused != pressured:
                    logger.info("render pressure pause=%s", pressured)
                context.release_idle(pressured)
                if time.monotonic() - last_status >= 10:
                    write_status("renderer", phase="pressure_paused" if pressured else "ready",
                                 foreground_pending=coordinator.queue.qsize(), background_pending=len(eager_pages),
                                 browser_active=context.browser is not None, active_connections=server.active_connections)
                    last_status = time.monotonic()
                try:
                    request, _future = coordinator.queue.get(timeout=0.5)
                except queue.Empty:
                    request = None
                if request is not None:
                    try:
                        if time.monotonic() - _future.created_at >= server.request_timeout:
                            response = {"ok": False, "error": "图片请求已超时，请重新查询"}
                        elif pressured:
                            response = {"ok": False, "error": "服务器繁忙，暂缓生成图片"}
                        else:
                            write_status("renderer", phase="rendering", board=request.board, page=request.page,
                                         background_pending=len(eager_pages))
                            response = renderer.render(request)
                    except Exception as exc:
                        logger.exception(
                            "on-demand render failed: board=%s entity=%s page=%d",
                            request.board, request.entity, request.page,
                        )
                        response = {"ok": False, "error": str(exc) or "图片渲染失败"}
                    coordinator.complete(request, response)
                    continue

                if time.monotonic() < next_poll:
                    continue
                next_poll = time.monotonic() + poll_seconds
                for eager_request in OnDemandRenderer.EAGER_BOARDS:
                    try:
                        definition = renderer.definition(
                            eager_request.board, eager_request.entity
                        )
                        mtime = definition.source.stat().st_mtime_ns
                    except (OSError, ValueError):
                        continue
                    if eager_mtimes.get(eager_request.key) == mtime:
                        continue
                    try:
                        payload = renderer._load_payload(definition.source)
                        page_count = math.ceil(
                            int(payload["user_count"]) / definition.page_size
                        )
                    except Exception:
                        logger.exception("unable to queue eager render: %s", eager_request.board)
                        eager_mtimes[eager_request.key] = mtime
                        continue
                    eager_pages.replace_board(
                        eager_request.board, eager_request.entity,
                        min(renderer.pre_render_pages, page_count),
                    )
                    # Do not retry a failed background page in a tight poll loop. The
                    # next source update schedules a fresh, bounded batch instead.
                    eager_mtimes[eager_request.key] = mtime

                if pressured or not eager_pages:
                    continue
                eager_request = eager_pages.pop()
                try:
                    # Render exactly one background page before returning to the
                    # foreground queue, so a group request is never stuck behind a
                    # complete multi-page pre-render batch.
                    write_status("renderer", phase="background_rendering", board=eager_request.board,
                                 page=eager_request.page, background_pending=len(eager_pages))
                    renderer.render(eager_request)
                except Exception:
                    logger.exception(
                        "eager render failed: board=%s entity=%s page=%d",
                        eager_request.board,
                        eager_request.entity or "-",
                        eager_request.page,
                    )
        finally:
            server.shutdown()
            server.server_close()
            context.close()
            socket_path.unlink(missing_ok=True)
            logger.info("render service stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
