import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from app.crawler import RankCrawler, CrawlError
from app.database import RankDatabase
from app.parser import WebVPNPageKind


class WorkerLifecycleTests(unittest.TestCase):
    def crawler(self):
        crawler = RankCrawler.__new__(RankCrawler)
        crawler.settings = SimpleNamespace(page_timeout_ms=30000)
        crawler.logger = logging.getLogger('memory-test')
        crawler.context = MagicMock()
        crawler.context.new_cdp_session.return_value.send.return_value = {"identifier": "fixture-seed"}
        crawler._navigation_count = 100
        crawler.page = MagicMock()
        crawler.page.is_closed.return_value = False
        crawler.page.evaluate.return_value = {"origin": "https://fixture.example", "entries": []}
        crawler.current_page_kind = MagicMock(return_value=WebVPNPageKind.RANKLIST)
        return crawler

    def test_rotate_only_worker_preserves_context(self):
        crawler = self.crawler()
        context, old = crawler.context, crawler.page
        new = crawler.context.new_page.return_value
        crawler.prepare_cycle()
        self.assertIs(crawler.context, context)
        context.close.assert_not_called()
        old.expect_popup.assert_not_called()
        old.close.assert_called_once()
        self.assertIs(crawler.page, new)
        self.assertEqual(crawler._navigation_count, 0)
        new.set_default_timeout.assert_called_once_with(30000)

    def test_auth_page_and_unknown_page_are_never_recycled(self):
        for kind in (WebVPNPageKind.LOGIN, WebVPNPageKind.UNKNOWN):
            crawler = self.crawler()
            crawler.current_page_kind.return_value = kind
            old = crawler.page
            crawler.prepare_cycle()
            old.close.assert_not_called()
            crawler.context.new_page.assert_not_called()
            self.assertIs(crawler.page, old)

    def test_failed_replacement_keeps_old_worker(self):
        crawler = self.crawler()
        old = crawler.page
        new = crawler.context.new_page.return_value
        new.set_default_timeout.side_effect = RuntimeError('fixture')
        with self.assertRaises(CrawlError):
            crawler.prepare_cycle()
        self.assertIs(crawler.page, old)
        old.close.assert_not_called()
        new.close.assert_called_once()

    def test_storage_seed_removed_before_later_navigations(self):
        crawler = self.crawler()
        crawler.prepare_cycle()
        session = crawler.context.new_cdp_session.return_value
        crawler._clear_storage_seed()
        session.send.assert_called_with("Page.removeScriptToEvaluateOnNewDocument", {"identifier": "fixture-seed"})
        session.detach.assert_called_once()
        crawler._clear_storage_seed()
        session.detach.assert_called_once()

    def test_first_cycle_does_not_rotate(self):
        crawler = self.crawler()
        crawler._navigation_count = 0
        crawler.prepare_cycle()
        crawler.context.new_page.assert_not_called()


class ConnectionLifecycleTests(unittest.TestCase):
    def test_commit_rollback_and_close(self):
        with tempfile.TemporaryDirectory() as directory:
            db = RankDatabase(Path(directory) / 'fixture.db')
            with db._connect() as connection:
                connection.execute('CREATE TABLE fixture (v INTEGER)')
                connection.execute('INSERT INTO fixture VALUES (1)')
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute('SELECT 1')
            with self.assertRaises(RuntimeError):
                with db._connect() as failed:
                    failed.execute('INSERT INTO fixture VALUES (2)')
                    raise RuntimeError('rollback')
            with self.assertRaises(sqlite3.ProgrammingError):
                failed.execute('SELECT 1')
            with db._connect() as check:
                self.assertEqual(check.execute('SELECT * FROM fixture').fetchall(), [(1,)])

    def test_setup_failure_closes_connection(self):
        connection = MagicMock()
        connection.execute.side_effect = sqlite3.OperationalError('fixture')
        with patch('app.database.sqlite3.connect', return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                with RankDatabase(Path('unused'))._connect():
                    self.fail('setup should fail')
        connection.close.assert_called_once()
