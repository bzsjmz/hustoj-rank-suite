import unittest
from unittest.mock import MagicMock, patch
from app.resource_guard import PressureGuard, RetryBackoff
from app.render_service import BackgroundQueue, LazyBrowserContext, RenderRequest, SingleFlightQueue
from app.resource_monitor import Sampler


def pressure(mib, memory=0, io=0):
    return {'available_kib': mib * 1024, 'memory': {'some': {'avg10': memory}}, 'io': {'full': {'avg10': io}}}


class ResourceControlTests(unittest.TestCase):
    def test_memory_hysteresis_prevents_flapping(self):
        guard = PressureGuard()
        self.assertFalse(guard.check(pressure(700)))
        self.assertTrue(guard.check(pressure(400)))
        self.assertTrue(guard.check(pressure(600)))
        self.assertFalse(guard.check(pressure(800)))

    def test_pressure_without_low_memory_and_missing_sample(self):
        guard = PressureGuard()
        self.assertTrue(guard.check(pressure(1200, io=30)))
        self.assertTrue(guard.check({}))
        self.assertTrue(guard.check(pressure(1200, memory=3)))
        self.assertFalse(guard.check(pressure(1200)))

    def test_backoff_caps_and_resets(self):
        retry = RetryBackoff()
        self.assertEqual([retry.failed() for _ in range(7)], [60, 120, 240, 480, 600, 600, 600])
        retry.reset()
        self.assertEqual(retry.failed(), 60)

    def test_foreground_limit_still_merges_duplicates(self):
        queue = SingleFlightQueue(limit=1)
        request = RenderRequest('total', None, 1)
        first, admitted = queue.submit(request)
        self.assertTrue(admitted)
        same, admitted = queue.submit(request)
        self.assertIs(first, same)
        self.assertFalse(admitted)
        rejected, admitted = queue.submit(RenderRequest('total', None, 2))
        self.assertFalse(admitted)
        self.assertFalse(rejected.wait(0)['ok'])
        self.assertEqual(queue.queue.qsize(), 1)
        queue.queue.get_nowait()
        queue.complete(request, {'ok': True})
        self.assertTrue(first.wait(0)['ok'])
        self.assertTrue(queue.submit(RenderRequest('total', None, 2))[1])

    def test_background_replaces_obsolete_pages_and_is_bounded(self):
        queue = BackgroundQueue()
        queue.replace_board('total', None, 3)
        queue.replace_board('computer', None, 3)
        for _ in range(100):
            queue.replace_board('total', None, 3)
        self.assertEqual(len(queue), 6)
        queue.replace_board('total', None, 1)
        self.assertEqual(len(queue), 4)
        requests = [queue.pop() for _ in range(4)]
        self.assertEqual([r.page for r in requests if r.board == 'total'], [1])

    def test_lazy_browser_releases_and_reopens(self):
        playwright, settings = MagicMock(), MagicMock()
        context = LazyBrowserContext(playwright, settings)
        playwright.chromium.launch.assert_not_called()
        context.new_page()
        first_browser = context.browser
        context.release_idle(pressured=True)
        first_browser.close.assert_called_once()
        self.assertIsNone(context.browser)
        context.new_page()
        self.assertEqual(playwright.chromium.launch.call_count, 2)
        with patch('app.render_service.time.monotonic', return_value=context.last_used + 121):
            context.release_idle()
        self.assertIsNone(context.browser)

    def test_counter_identity_reset_has_no_false_spike(self):
        sampler = Sampler()
        sampler.previous = {'pid:1:100': {'read_bytes': 1000}}
        next_previous = {}
        self.assertIsNone(sampler.delta('pid:1:200', {'read_bytes': 20}, next_previous))
        self.assertEqual(sampler.delta('pid:1:100', {'read_bytes': 1300}, next_previous), {'read_bytes': 300})
