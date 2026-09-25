from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.render_service import RenderSocketHandler, ThreadedUnixServer, RenderFuture


class ImmediateQueue:
    def __init__(self):
        self.calls = 0

    def submit(self, request):
        self.calls += 1
        future = RenderFuture()
        future.response = {"ok": True}
        future.event.set()
        return future, True


class SocketLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "render.sock")
        self.server = ThreadedUnixServer(self.path, RenderSocketHandler,
                                        max_connections=2, socket_timeout=0.35)
        self.server.coordinator = ImmediateQueue()
        self.server.request_timeout = 0.1
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def connect(self):
        client = socket.socket(socket.AF_UNIX)
        client.settimeout(2)
        client.connect(self.path)
        self.clients.append(client)
        return client

    def wait_count(self, count):
        deadline = time.monotonic() + 2
        while self.server.active_connections != count and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(count, self.server.active_connections)

    def test_idle_connections_are_capped_then_slots_recover(self):
        self.connect()
        self.connect()
        self.wait_count(2)
        excess = self.connect()
        self.assertEqual(b"", excess.recv(1))
        self.assertEqual(0, self.server.coordinator.calls)
        self.wait_count(0)
        client = self.connect()
        client.sendall(b'{"board":"total","page":1}\n')
        self.assertTrue(json.loads(client.recv(1024))["ok"])
        self.wait_count(0)

    def test_trickle_input_cannot_extend_request_deadline(self):
        client = self.connect()
        start = time.monotonic()
        for _ in range(12):
            try:
                client.sendall(b" ")
            except OSError:
                break
            time.sleep(0.07)
        self.wait_count(0)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertEqual(0, self.server.coordinator.calls)

    def test_oversized_and_disconnected_requests_release_slots(self):
        client = self.connect()
        client.sendall(b"x" * 8193)
        self.assertFalse(json.loads(client.recv(1024))["ok"])
        self.wait_count(0)
        self.connect().close()
        self.wait_count(0)
        self.assertEqual(0, self.server.coordinator.calls)

    def test_thread_start_failure_releases_slot(self):
        left, right = socket.socketpair()
        try:
            with patch("threading.Thread.start", side_effect=RuntimeError("start failed")):
                with self.assertRaises(RuntimeError):
                    self.server.process_request(left, "")
            self.assertEqual(0, self.server.active_connections)
        finally:
            left.close()
            right.close()

    def test_invalid_limits_are_rejected(self):
        for kwargs in ({"max_connections": 0}, {"max_connections": 257},
                       {"socket_timeout": float("nan")}, {"socket_timeout": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ThreadedUnixServer(self.path + "-bad", RenderSocketHandler, **kwargs)
