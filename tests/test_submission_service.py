from __future__ import annotations

import unittest

from app.submission_service import SubmissionCoordinator


class SubmissionCoordinatorTests(unittest.TestCase):
    def test_unavailable_queue_rejects_immediately(self) -> None:
        coordinator = SubmissionCoordinator()
        response = coordinator.submit("202600000001").wait(0)
        self.assertFalse(response["ok"])
        self.assertIn("启动", response["error"])

    def test_same_user_is_single_flight_and_success_is_cached(self) -> None:
        coordinator = SubmissionCoordinator(cache_seconds=60)
        coordinator.set_available(True)
        first = coordinator.submit("202600000001")
        second = coordinator.submit("202600000001")
        self.assertIs(first, second)
        queued_user, _ = coordinator.queue.get_nowait()
        response = {"ok": True, "records": []}
        coordinator.complete(queued_user, response)
        self.assertEqual(response, first.wait(0))
        self.assertEqual(response, coordinator.submit("202600000001").wait(0))
        self.assertTrue(coordinator.queue.empty())

    def test_queue_limit_is_bounded(self) -> None:
        coordinator = SubmissionCoordinator(limit=1)
        coordinator.set_available(True)
        coordinator.submit("202600000001")
        response = coordinator.submit("202600000002").wait(0)
        self.assertFalse(response["ok"])
        self.assertIn("较多", response["error"])

    def test_new_source_queries_have_a_global_minute_limit(self) -> None:
        coordinator = SubmissionCoordinator(limit=2, request_limit=1)
        coordinator.set_available(True)
        coordinator.submit("202600000001")
        response = coordinator.submit("202600000002").wait(0)
        self.assertFalse(response["ok"])
        self.assertIn("频繁", response["error"])
