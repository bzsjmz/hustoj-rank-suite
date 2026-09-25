from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.crawler import CrawlError, RankCrawler, WebVPNSessionExpired


def rank_html(start_rank: int, count: int) -> str:
    rows = []
    for offset in range(count):
        rank = start_rank + offset
        rows.append(
            "<tr><td>{rank}</td><td>2026{user:08d}</td><td>n{rank}</td>"
            "<td>{rank}</td><td>{rank}</td><td>100%</td><td>level</td></tr>".format(
                rank=rank, user=rank
            )
        )
    return "<table class='table'><thead><tr>" + "".join(f"<th>{v}</th>" for v in ("Rank", "User", "Nick", "Solved", "Submit", "Ratio", "Level")) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def joined_rank_html(*documents: str) -> str:
    rows = []
    for document in documents:
        rows.append(document.split("<tbody>", 1)[1].split("</tbody>", 1)[0])
    return "<table class='table'><thead><tr>" + "".join(f"<th>{v}</th>" for v in ("Rank", "User", "Nick", "Solved", "Submit", "Ratio", "Level")) + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


class FakeCrawler(RankCrawler):
    def __init__(self, settings: Settings, pages: list[str]):
        super().__init__(settings, logging.getLogger("test_crawler"))
        self.pages = iter(pages)
        self.urls: list[str] = []
        self.page = SimpleNamespace(url="https://webvpn.example/http/token/ranklist.php")

    def _load(self, url: str, *_args, **_kwargs) -> str:
        self.urls.append(url)
        return next(self.pages)


class FakeRequest:
    def __init__(self, url: str, redirected_from=None):
        self.url = url
        self.redirected_from = redirected_from


class FakeResponse:
    def __init__(self, status: int, request: FakeRequest):
        self.status = status
        self.request = request


class NavigationPage:
    def __init__(
        self,
        final_url: str,
        html: str,
        status: int = 200,
        redirected_from: FakeRequest | None = None,
    ):
        self.url = final_url
        self.html = html
        self.response = FakeResponse(
            status,
            FakeRequest(final_url, redirected_from=redirected_from),
        )

    def goto(self, _url: str, **_kwargs):
        return self.response

    def content(self) -> str:
        return self.html


class CookieContext:
    def __init__(self, cookies: list[dict]):
        self._cookies = cookies
        self.added: list[dict] = []

    def cookies(self) -> list[dict]:
        return self._cookies

    def add_cookies(self, cookies: list[dict]) -> None:
        self.added.extend(cookies)


class ClosedPage:
    def is_closed(self) -> bool:
        return True


class ReplacementPage:
    def __init__(self):
        self.timeout = None

    def is_closed(self) -> bool:
        return False

    def set_default_timeout(self, timeout: int) -> None:
        self.timeout = timeout


class ReplacementContext:
    def __init__(self):
        self.created: list[ReplacementPage] = []

    def new_page(self) -> ReplacementPage:
        page = ReplacementPage()
        self.created.append(page)
        return page


class CrawlerTests(unittest.TestCase):
    def settings(self, directory: str, max_pages: int = 100) -> Settings:
        root = Path(directory)
        return Settings(
            webvpn_origin="https://webvpn.example",
            oj_proxy_base="https://webvpn.example/http/token",
            prefix="2026",
            excluded_user_ids=frozenset(),
            data_dir=root,
            lookup_prefixes=("2023", "2024", "2025", "2026"),
            student_id_length=12,
            major_code_length=4,
            class_code_length=6,
            student_id_pattern=r"[0-9]+",
            log_dir=root,
            profile_dir=root / "profile",
            playwright_browsers_path=root / "browsers",
            share_dir=root / "share",
            scrape_interval_seconds=300,
            login_check_seconds=60,
            error_retry_seconds=60,
            page_size=50,
            max_pages=max_pages,
            page_timeout_ms=1000,
            browser_executable=None,
            headless=True,
        )

    def test_collects_all_pages_and_requires_empty_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            crawler = FakeCrawler(
                self.settings(directory), [rank_html(1, 50), rank_html(51, 3), rank_html(54, 0)]
            )
            entries = crawler.scrape_all()
            self.assertEqual(53, len(entries))
            self.assertEqual(53, entries[-1].rank)

    def test_changed_page_sizes_advance_by_actual_row_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            crawler = FakeCrawler(self.settings(directory),
                                  [rank_html(1, 3), rank_html(4, 2), rank_html(6, 0)])
            self.assertEqual(5, len(crawler.scrape_all()))
            from urllib.parse import parse_qs, urlparse
            self.assertEqual([0, 3, 5], [int(parse_qs(urlparse(u).query).get("start", ["0"])[0]) for u in crawler.urls])

    def test_bad_tail_never_completes_partial_ranklist(self) -> None:
        for tail in ("<html>upstream error</html>",
                     "<table class='table'><tbody></tbody></table>",
                     "<table class='table'><tr><td>maintenance</td></tr></table>",
                     rank_html(51, 1).replace("100%", "NaN%")):
            with self.subTest(tail=tail), tempfile.TemporaryDirectory() as directory:
                crawler = FakeCrawler(self.settings(directory), [rank_html(1, 50), tail])
                with self.assertRaises(CrawlError):
                    crawler.scrape_all()

    def test_max_pages_never_saves_truncated_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            crawler = FakeCrawler(self.settings(directory, max_pages=1), [rank_html(1, 50)])
            with self.assertRaises(CrawlError):
                crawler.scrape_all()

    def test_empty_first_page_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            crawler = FakeCrawler(
                self.settings(directory), ["<table class='table'><tbody></tbody></table>"]
            )
            with self.assertRaises(CrawlError):
                crawler.scrape_all()

    def test_identical_boundary_duplicate_is_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_page = rank_html(1, 50)
            repeated = rank_html(50, 1).replace(
                "<tr><td>50</td><td>202600000050",
                "<tr><td>51</td><td>202600000050",
            )
            second_page = joined_rank_html(repeated, rank_html(52, 2))
            crawler = FakeCrawler(self.settings(directory), [first_page, second_page, rank_html(54, 0)])
            entries = crawler.scrape_all()
            self.assertEqual(52, len(entries))
            self.assertEqual(52, entries[-1].rank)
            self.assertEqual(list(range(1, 53)), [entry.rank for entry in entries])

    def test_conflicting_boundary_duplicate_fails_the_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first_page = rank_html(1, 50)
            conflicting = rank_html(50, 1).replace("<td>50</td><td>50</td>", "<td>51</td><td>51</td>")
            second_page = joined_rank_html(conflicting, rank_html(51, 2))
            crawler = FakeCrawler(self.settings(directory), [first_page, second_page, rank_html(54, 0)])
            with self.assertRaises(CrawlError):
                crawler.scrape_all()

    def test_collects_a_requested_lookup_prefix_without_changing_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            crawler = FakeCrawler(
                self.settings(directory), [rank_html(1, 2).replace("2026", "2023"), rank_html(3, 0)]
            )
            entries = crawler.scrape_all(prefix="2023")

            self.assertEqual(["202300000001", "202300000002"], [entry.user_id for entry in entries])
            self.assertIn("prefix=2023", crawler.urls[0])

    def test_all_time_rejects_other_prefix_but_scoped_rank_allows_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mixed = rank_html(1, 2).replace("202600000001", "999900000001")
            with self.assertRaises(CrawlError):
                FakeCrawler(self.settings(directory), [mixed]).scrape_all()

            entries = FakeCrawler(self.settings(directory), [mixed, rank_html(3, 0)]).scrape_all(scope="w")
            self.assertEqual(2, len(entries))

    def test_load_rejects_login_even_if_it_contains_rank_style_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            crawler = RankCrawler(self.settings(directory), logging.getLogger("test"))
            crawler.page = NavigationPage(
                "https://webvpn.example/login",
                "<h1>WebVPN</h1><form action='/auth/login'>统一身份认证"
                + rank_html(1, 1)
                + "</form>",
            )
            with self.assertRaises(WebVPNSessionExpired):
                crawler._load("https://webvpn.example/http/token/ranklist.php")

    def test_load_rejects_http_403_and_unknown_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            crawler = RankCrawler(settings, logging.getLogger("test"))
            crawler.page = NavigationPage(
                "https://webvpn.example/http/token/ranklist.php",
                rank_html(1, 1),
                status=403,
            )
            with self.assertRaises(WebVPNSessionExpired):
                crawler._load(settings.ranklist_url())

            crawler.page = NavigationPage(
                "https://webvpn.example/",
                "<html><body>portal without ranklist</body></html>",
            )
            with self.assertRaises(CrawlError):
                crawler._load(settings.ranklist_url())

    def test_redirect_diagnostics_do_not_store_query_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            crawler = RankCrawler(settings, logging.getLogger("test"))
            previous = FakeRequest(
                "https://auth.example/login?ticket=SUPER_SECRET&service=PRIVATE"
            )
            crawler.page = NavigationPage(
                "https://webvpn.example/http/token/ranklist.php?prefix=2026",
                rank_html(1, 1),
                redirected_from=previous,
            )
            crawler._load(
                settings.ranklist_url(),
                "test_redirect",
                force_diagnostic=True,
            )
            events = crawler.diagnostics.events_path.read_text(encoding="utf-8")
            self.assertNotIn("SUPER_SECRET", events)
            self.assertNotIn("PRIVATE", events)
            self.assertIn("ticket", events)

    def test_redirect_to_unknown_page_is_treated_as_session_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            crawler = RankCrawler(settings, logging.getLogger("test"))
            previous = FakeRequest(settings.ranklist_url())
            crawler.page = NavigationPage(
                "https://webvpn.example/portal",
                "<html><body>unexpected portal</body></html>",
                redirected_from=previous,
            )
            with self.assertRaises(WebVPNSessionExpired):
                crawler._load(settings.ranklist_url())

    def test_restore_does_not_overwrite_newer_persistent_profile_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            stored = [
                {
                    "name": "vpn_ticket",
                    "value": "old-ticket",
                    "domain": ".webvpn.example",
                    "path": "/",
                },
                {
                    "name": "missing_preference",
                    "value": "1",
                    "domain": "webvpn.example",
                    "path": "/",
                },
            ]
            settings.browser_state_path.write_text(
                json.dumps({"cookies": stored}), encoding="utf-8"
            )
            context = CookieContext(
                [
                    {
                        "name": "vpn_ticket",
                        "value": "new-ticket",
                        "domain": ".webvpn.example",
                        "path": "/",
                    }
                ]
            )
            crawler = RankCrawler(settings, logging.getLogger("test"))
            crawler.context = context

            crawler._restore_session_cookies()

            self.assertEqual([stored[1]], context.added)

    def test_closed_worker_tab_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            crawler = RankCrawler(settings, logging.getLogger("test"))
            context = ReplacementContext()
            crawler.context = context
            crawler.page = ClosedPage()

            page = crawler._require_page()

            self.assertIs(page, crawler.page)
            self.assertEqual([page], context.created)
            self.assertEqual(settings.page_timeout_ms, page.timeout)


if __name__ == "__main__":
    unittest.main()
