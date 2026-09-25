from __future__ import annotations

import io
import json
import logging
import tempfile
import traceback
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import main
from app.crawler import BrowserUnavailable, CrawlError, RankCrawler
from app.logging_config import SafeFormatter
from app.session import SessionDiagnostics, sanitize_url
from tests import test_crawler


class AuthRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = test_crawler.CrawlerTests().settings(self.temp.name)
        self.crawler = RankCrawler(self.settings, logging.getLogger('auth-test'))
        self.page = Mock(url='https://webvpn.example/login')
        self.page.is_closed.return_value = False
        self.page.content.return_value = '<form><input type="password"></form>'
        self.context = Mock(browser=None, pages=[self.page])
        self.context.cookies.return_value = []
        self.crawler.page, self.crawler.context = self.page, self.context

    def test_login_and_unknown_pages_are_observed_without_navigation(self):
        for url, html in [('https://webvpn.example/login', '<input type="password">'),
                          ('https://webvpn.example/portal', '<p>loading</p>'),
                          ('https://other.example/', '<a href="/logout">logout</a>'),
                          ('https://webvpn.example/', '<a href="https://other.example/logout">logout</a>')]:
            self.page.url = url
            self.page.content.return_value = html
            for _ in range(3):
                self.assertFalse(self.crawler.validate_after_user_authentication())
        self.page.goto.assert_not_called()
        self.page.close.assert_not_called()
        self.context.new_page.assert_not_called()

    def test_authenticated_popup_allows_one_validation(self):
        popup = Mock(url='https://webvpn.example/portal')
        popup.is_closed.return_value = False
        popup.content.return_value = '<a href="/logout">logout</a>'
        self.context.pages.append(popup)
        with patch.object(self.crawler, '_load') as load, \
             patch.object(self.crawler, 'persist_session_state'), \
             patch.object(self.crawler, 'mark_ready') as ready:
            self.assertTrue(self.crawler.validate_after_user_authentication())
            load.assert_called_once()
            ready.assert_called_once_with('manual_authentication_succeeded')
        popup.goto.assert_not_called()

    def test_failed_validation_does_not_repeat_for_same_evidence(self):
        self.page.url = 'https://webvpn.example/portal'
        self.page.content.return_value = '<a href="/logout">logout</a>'
        with patch.object(self.crawler, '_load', side_effect=CrawlError('fixture')) as load:
            for _ in range(3):
                self.assertFalse(self.crawler.validate_after_user_authentication())
            self.assertEqual(1, load.call_count)
            self.page.url = 'https://webvpn.example/login'
            self.page.content.return_value = '<input type="password">'
            self.assertFalse(self.crawler.validate_after_user_authentication())
            self.page.url = 'https://webvpn.example/portal'
            self.page.content.return_value = '<a href="/logout">logout</a>'
            self.assertFalse(self.crawler.validate_after_user_authentication())
            self.assertEqual(2, load.call_count)

    def test_stale_authenticated_tab_does_not_interrupt_login(self):
        stale = Mock(url='https://webvpn.example/portal')
        stale.is_closed.return_value = False
        stale.content.return_value = '<a href="/logout">logout</a>'
        self.context.pages.append(stale)
        self.crawler.enter_waiting_for_auth('fixture')
        with patch.object(self.crawler, '_load') as load:
            for _ in range(3):
                self.assertFalse(self.crawler.validate_after_user_authentication())
            load.assert_not_called()
        self.page.goto.assert_not_called()

    def test_automatic_recovery_keeps_unknown_login_flow_untouched(self):
        self.page.url = 'https://webvpn.example/portal'
        self.page.content.return_value = '<p>loading</p>'
        with patch.object(self.crawler, '_navigate') as navigate, patch.object(self.crawler, '_load') as load:
            self.assertFalse(self.crawler.attempt_automatic_recovery('fixture'))
            navigate.assert_called_once()
            load.assert_not_called()

    def test_context_closed_event_is_fatal_before_retry(self):
        self.crawler._mark_browser_unavailable()
        with self.assertRaises(BrowserUnavailable):
            self.crawler.prepare_cycle()
        with self.assertRaises(BrowserUnavailable):
            self.crawler.validate_after_user_authentication()
        self.context.new_page.assert_not_called()

    def test_crashed_worker_is_replaced_without_closing_context(self):
        replacement = Mock()
        self.context.new_page.return_value = replacement
        self.crawler._mark_worker_crashed(self.page)
        self.assertIs(replacement, self.crawler._require_page())
        self.page.close.assert_called_once()
        self.context.close.assert_not_called()
        replacement.on.assert_called_once()
        self.assertFalse(self.crawler._worker_crashed)

    def test_disconnect_during_navigation_is_fatal(self):
        def crash(*args, **kwargs):
            self.crawler._mark_browser_unavailable()
            raise RuntimeError('cookie=DO_NOT_LOG')
        self.page.goto.side_effect = crash
        with self.assertRaises(BrowserUnavailable):
            self.crawler._navigate(self.settings.ranklist_url(), 'fixture')

    def test_network_failure_keeps_context_and_hides_original_chain(self):
        self.page.goto.side_effect = RuntimeError('cookie=DO_NOT_LOG https://example.invalid/?ticket=SECRET')
        url = 'https://example.invalid/?ticket=SECRET'
        try:
            self.crawler._navigate(url, 'fixture')
        except CrawlError:
            rendered = traceback.format_exc()
        else:
            self.fail('expected navigation failure')
        self.assertNotIn('SECRET', rendered)
        self.assertNotIn('DO_NOT_LOG', rendered)
        self.crawler.ensure_browser_available()
        self.context.close.assert_not_called()

    def test_driver_stops_even_if_context_close_fails(self):
        driver = Mock()
        self.crawler._playwright = driver
        self.context.close.side_effect = RuntimeError('fixture')
        with self.assertRaises(RuntimeError):
            self.crawler.close()
        driver.stop.assert_called_once()
        self.assertIsNone(self.crawler.context)

    def test_supervisor_exit_on_browser_failure(self):
        event = Mock()
        event.is_set.side_effect = [False, True]
        crawler = Mock()
        crawler.prepare_cycle.side_effect = BrowserUnavailable('fixture')
        guard = Mock(paused=False)
        guard.check.return_value = False
        settings = replace(self.settings, lookup_prefixes=('2026',))
        submission_service = Mock()
        with patch.object(main.Settings, 'from_env', return_value=settings), \
             patch.object(main, 'RankCrawler', return_value=crawler), \
             patch.object(main, 'SubmissionService', return_value=submission_service), \
             patch.object(main, 'PressureGuard', return_value=guard), \
             patch.object(main, 'configure_logging', return_value=Mock()), \
             patch.object(main, 'write_status'), patch.object(main.signal, 'signal'), \
             patch.object(main.threading, 'Event', return_value=event):
            self.assertEqual(1, main.run())
        event.wait.assert_not_called()
        crawler.close.assert_called_once()
        submission_service.close.assert_called_once()


class DiagnosticSafetyTests(unittest.TestCase):
    def test_url_userinfo_query_keys_and_session_path_are_redacted(self):
        result = sanitize_url('https://USER:PASSWORD@example.invalid/path;jsessionid=SESSION?RAW_SECRET=value&ticket=TICKET#FRAGMENT')
        for secret in ('USER', 'PASSWORD', 'SESSION', 'RAW_SECRET', 'TICKET', 'FRAGMENT'):
            self.assertNotIn(secret, result)
        self.assertIn('example.invalid', result)
        self.assertIn('ticket', result)

    def test_exception_formatter_never_prints_payload_or_cached_traceback(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(SafeFormatter('%(message)s'))
        logger = logging.Logger('isolated-safe-test')
        logger.addHandler(handler)
        try:
            try:
                raise RuntimeError('Cookie: SESSION_SECRET')
            except RuntimeError as exc:
                raise ValueError('password=PASSWORD_SECRET') from exc
        except ValueError:
            logger.exception('failed at https://example.invalid/?ticket=TICKET_SECRET')
        record = logging.LogRecord('test', logging.ERROR, __file__, 1, 'safe', (), None)
        record.exc_text = 'CACHED_SECRET'
        handler.handle(record)
        result = output.getvalue()
        for secret in ('SESSION_SECRET', 'PASSWORD_SECRET', 'TICKET_SECRET', 'CACHED_SECRET'):
            self.assertNotIn(secret, result)
        self.assertIn('ValueError', result)
        self.assertIn('test_auth_recovery.py', result)

    def test_event_history_rotates_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = SessionDiagnostics(Path(directory), logging.getLogger('rotation-test'))
            diagnostics.EVENT_MAX_BYTES = 256
            for i in range(30):
                diagnostics.record_event('fixture', sequence=i)
            files = list(Path(directory).glob('webvpn-session-events.jsonl*'))
            self.assertEqual(4, len(files))
            for file in files:
                self.assertLessEqual(file.stat().st_size, 256)
                self.assertEqual(0o600, file.stat().st_mode & 0o777)
                for line in file.read_text().splitlines():
                    self.assertEqual('fixture', json.loads(line)['event'])
