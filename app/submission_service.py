from __future__ import annotations

import json
import math
import os
import queue
import socketserver
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .crawler import BrowserUnavailable, CrawlError, RankCrawler, WebVPNSessionExpired


class SubmissionFuture:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.response: dict[str, Any] | None = None

    def wait(self, timeout: float) -> dict[str, Any]:
        if not self.event.wait(timeout) or self.response is None:
            return {"ok": False, "error": "提交记录查询超时，请稍后重试"}
        return self.response


class SubmissionCoordinator:
    """Bounded single-flight queue; Playwright work stays on the collector thread."""

    def __init__(self, *, limit: int = 16, cache_seconds: float = 60.0,
                 request_limit: int = 20) -> None:
        if type(limit) is not int or not 1 <= limit <= 128:
            raise ValueError("submission queue limit must be between 1 and 128")
        if not math.isfinite(cache_seconds) or not 0 <= cache_seconds <= 3600:
            raise ValueError("submission cache duration must be between 0 and 3600 seconds")
        if type(request_limit) is not int or not 1 <= request_limit <= 120:
            raise ValueError("submission request limit must be between 1 and 120 per minute")
        self.limit = limit
        self.cache_seconds = cache_seconds
        self.request_limit = request_limit
        self.queue: queue.Queue[tuple[str, SubmissionFuture]] = queue.Queue()
        self.wake_event = threading.Event()
        self._lock = threading.Lock()
        self._active: dict[str, SubmissionFuture] = {}
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._recent_requests: list[float] = []
        self._unavailable_error = "提交记录服务正在启动，请稍后重试"

    def set_available(self, available: bool, error: str = "") -> None:
        with self._lock:
            self._unavailable_error = "" if available else (error or "提交记录服务暂不可用")

    def submit(self, user_id: str) -> SubmissionFuture:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(user_id)
            if cached is not None and now - cached[0] <= self.cache_seconds:
                future = SubmissionFuture()
                future.response = cached[1]
                future.event.set()
                return future
            self._cache.pop(user_id, None)
            if self._unavailable_error:
                future = SubmissionFuture()
                future.response = {"ok": False, "error": self._unavailable_error}
                future.event.set()
                return future
            existing = self._active.get(user_id)
            if existing is not None:
                return existing
            threshold = now - 60.0
            self._recent_requests = [stamp for stamp in self._recent_requests if stamp > threshold]
            if len(self._recent_requests) >= self.request_limit:
                future = SubmissionFuture()
                future.response = {"ok": False, "error": "提交记录查询频繁，请稍后重试"}
                future.event.set()
                return future
            future = SubmissionFuture()
            if len(self._active) >= self.limit:
                future.response = {"ok": False, "error": "提交记录查询较多，请稍后重试"}
                future.event.set()
                return future
            self._active[user_id] = future
            self._recent_requests.append(now)
            self.queue.put((user_id, future))
            self.wake_event.set()
            return future

    def complete(self, user_id: str, response: dict[str, Any]) -> None:
        with self._lock:
            future = self._active.pop(user_id, None)
            if response.get("ok"):
                self._cache[user_id] = (time.monotonic(), response)
            if not self._active:
                self.wake_event.clear()
        if future is not None:
            future.response = response
            future.event.set()

    def fail_all(self, error: str) -> None:
        with self._lock:
            active = list(self._active.values())
            self._active.clear()
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break
            self.wake_event.clear()
        for future in active:
            future.response = {"ok": False, "error": error}
            future.event.set()


class SubmissionSocketHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            deadline = time.monotonic() + self.server.socket_timeout
            raw = bytearray()
            while b"\n" not in raw and len(raw) < 513:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self.connection.settimeout(remaining)
                chunk = self.connection.recv(513 - len(raw))
                if not chunk:
                    return
                raw.extend(chunk)
            raw = bytes(raw).split(b"\n", 1)[0]
            if not raw or len(raw) > 512:
                raise ValueError("请求过长")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("请求格式无效")
            user_id = str(payload.get("user_id", ""))
            self.server.validate_user_id(user_id)
            future = self.server.coordinator.submit(user_id)
            response = future.wait(self.server.request_timeout)
        except OSError:
            return
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            response = {"ok": False, "error": str(exc) or "提交记录请求无效"}
        try:
            self.connection.settimeout(self.server.socket_timeout)
            self.wfile.write(
                (json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                .encode("utf-8")
            )
        except OSError:
            return


class BoundedUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, handler, *, max_connections: int = 32,
                 socket_timeout: float = 5.0) -> None:
        if type(max_connections) is not int or not 1 <= max_connections <= 128:
            raise ValueError("submission connection limit must be between 1 and 128")
        if not math.isfinite(socket_timeout) or not 0 < socket_timeout <= 30:
            raise ValueError("submission socket timeout must be finite and within 30 seconds")
        self.socket_timeout = socket_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class SubmissionService:
    def __init__(self, settings: Settings, logger) -> None:
        self.settings = settings
        self.logger = logger
        self.coordinator = SubmissionCoordinator(
            limit=int(os.getenv("SUBMISSION_QUEUE_LIMIT", "16")),
            cache_seconds=float(os.getenv("SUBMISSION_CACHE_SECONDS", "60")),
            request_limit=int(os.getenv("SUBMISSION_REQUESTS_PER_MINUTE", "20")),
        )
        run_dir = Path(os.getenv("OJ_RENDER_RUN_DIR", str(settings.share_dir.parent / "run")))
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir.chmod(0o755)
        self.socket_path = run_dir / "submission.sock"
        if self.socket_path.exists() or self.socket_path.is_socket():
            self.socket_path.unlink()
        self.server = BoundedUnixServer(
            str(self.socket_path), SubmissionSocketHandler,
            max_connections=int(os.getenv("SUBMISSION_MAX_CONNECTIONS", "32")),
            socket_timeout=float(os.getenv("SUBMISSION_SOCKET_TIMEOUT", "5")),
        )
        self.server.coordinator = self.coordinator
        self.server.request_timeout = float(os.getenv("SUBMISSION_REQUEST_TIMEOUT", "90"))
        if not math.isfinite(self.server.request_timeout) or not 0 < self.server.request_timeout <= 180:
            raise ValueError("submission request timeout must be finite and within 180 seconds")
        self.server.validate_user_id = self.validate_user_id
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def validate_user_id(self, user_id: str) -> None:
        self.settings.submission_url(user_id)

    def start(self) -> None:
        self.socket_path.chmod(0o666)
        self._thread.start()
        self.logger.info("submission service ready, socket=%s", self.socket_path)

    def set_available(self, available: bool, error: str = "") -> None:
        self.coordinator.set_available(available, error)
        if not available:
            self.coordinator.fail_all(error or "提交记录服务暂不可用")

    def process_pending(self, crawler: RankCrawler, *, maximum: int = 4) -> int:
        processed = 0
        while processed < maximum:
            try:
                user_id, _future = self.coordinator.queue.get_nowait()
            except queue.Empty:
                break
            try:
                records = crawler.scrape_submissions(user_id)
                response = {
                    "ok": True, "schema_version": 1, "user_id": user_id,
                    "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "records": [asdict(record) for record in records],
                }
            except (WebVPNSessionExpired, BrowserUnavailable):
                self.coordinator.complete(
                    user_id, {"ok": False, "error": "OJ 登录状态失效，等待管理员重新认证"}
                )
                raise
            except (CrawlError, ValueError) as exc:
                self.logger.warning(
                    "submission query failed, error=%s", type(exc).__name__
                )
                response = {"ok": False, "error": "提交记录暂时无法读取，请稍后重试"}
            self.coordinator.complete(user_id, response)
            processed += 1
        return processed

    def wait(self, timeout: float) -> bool:
        return self.coordinator.wake_event.wait(timeout)

    def close(self) -> None:
        self.coordinator.set_available(False, "提交记录服务已停止")
        self.coordinator.fail_all("提交记录服务已停止")
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        self.socket_path.unlink(missing_ok=True)
