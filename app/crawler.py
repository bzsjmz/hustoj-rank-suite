from __future__ import annotations

import json
import gc
from collections import Counter
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace

from .config import Settings
from .models import RankEntry
from .parser import (
    RanklistParseError,
    WebVPNPageKind,
    classify_webvpn_page,
    has_ranklist_table,
    has_authenticated_page,
    has_submission_table,
    parse_ranklist,
    parse_submissions,
    SubmissionParseError,
)
from .session import SessionDiagnostics, sanitize_url


class WebVPNSessionExpired(RuntimeError):
    pass


class CrawlError(RuntimeError):
    pass


class BrowserUnavailable(CrawlError):
    """The context cannot be reused; let the supervisor relaunch the process."""


@dataclass(frozen=True)
class NavigationProbe:
    requested_url: str
    final_url: str
    status: int | None
    classification: str
    redirect_count: int
    redirect_chain: list[str]

    def as_dict(self) -> dict:
        return asdict(self)


class RankCrawler:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self._playwright = None
        self.context = None
        self.page = None
        self._browser_unavailable = False
        self._observed_auth_evidence = set()
        self._navigation_count = 0
        self._storage_seed = None
        self.diagnostics = SessionDiagnostics(settings.data_dir, logger)

    def start(self) -> None:
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH", str(self.settings.playwright_browsers_path)
        )
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        launch_options: dict[str, object] = {
            "user_data_dir": str(self.settings.profile_dir),
            "headless": self.settings.headless,
            "viewport": {"width": 1365, "height": 768},
            "args": ["--disable-dev-shm-usage"],
        }
        if self.settings.browser_executable:
            launch_options["executable_path"] = self.settings.browser_executable

        self.context = self._playwright.chromium.launch_persistent_context(**launch_options)
        self.context.on("close", self._mark_browser_unavailable)
        if self.context.browser is not None:
            self.context.browser.on("disconnected", self._mark_browser_unavailable)
        self._restore_session_cookies()
        visible_page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page = self._new_worker_page()
        self._show_browser_window(visible_page)
        self.diagnostics.service_started()
        self.logger.info("persistent Chromium started, profile=%s", self.settings.profile_dir)

    def _mark_browser_unavailable(self, *_args) -> None:
        self._browser_unavailable = True

    def ensure_browser_available(self) -> None:
        browser = getattr(self.context, "browser", None)
        connected = getattr(browser, "is_connected", None)
        if getattr(self, "_browser_unavailable", False) or (
            callable(connected) and not connected()
        ):
            raise BrowserUnavailable("browser/context unavailable; supervisor restart required") from None

    def prepare_cycle(self) -> None:
        """Dispose navigation protocol objects at a cycle boundary, keeping auth context."""
        self.ensure_browser_available()
        if not self._navigation_count or self.context is None:
            return
        old_page = self._require_page()
        # Never discard the login page or an unresolved authentication state.
        if self.current_page_kind() not in {
            WebVPNPageKind.RANKLIST, WebVPNPageKind.SUBMISSION,
        }:
            return
        before = self.protocol_object_counts()
        new_page = None
        try:
            # A standalone tab avoids a chain of retained Page.opener wrappers.
            # Cookie/localStorage remain in the persistent context. Seed sessionStorage
            # for this origin on the first navigation only, using an ephemeral CDP script.
            storage = old_page.evaluate(
                "() => ({origin: location.origin, entries: Object.entries(sessionStorage)})"
            )
            self._clear_storage_seed()
            new_page = self._new_worker_page()
            session = self.context.new_cdp_session(new_page)
            self._storage_seed = (session, None)
            session.send("Page.enable")
            identifier = session.send("Page.addScriptToEvaluateOnNewDocument", {"source":
                "(() => { const saved = " + json.dumps(storage) + "; "
                "if (location.origin === saved.origin) { "
                "for (const [k,v] of saved.entries) sessionStorage.setItem(k,v); "
                "} })();"
            })["identifier"]
            self._storage_seed = (session, identifier)
            old_page.close()
        except Exception as exc:
            try:
                self._clear_storage_seed()
            except Exception:
                pass
            self.page = old_page
            if new_page is not None:
                try:
                    new_page.close()
                except Exception:
                    pass
            self.ensure_browser_available()
            raise CrawlError("worker tab recycle failed: " + type(exc).__name__) from None
        self._navigation_count = 0
        # Python wrapper objects can contain cycles even after protocol disposal.
        gc.collect()
        self.logger.info("worker tab recycled; protocol counts before=%s after=%s",
                         before, self.protocol_object_counts())

    def _clear_storage_seed(self) -> None:
        seed, self._storage_seed = getattr(self, "_storage_seed", None), None
        if seed is None:
            return
        session, identifier = seed
        try:
            if identifier is not None:
                session.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})
        finally:
            session.detach()

    def protocol_object_counts(self) -> dict[str, int]:
        """Read-only diagnostics: object types/counts, never initializer payloads."""
        try:
            objects = self.context._impl_obj._connection._objects
            counts = Counter(obj._type for obj in objects.values())
            return {name: counts[name] for name in ("Request", "Response", "Page", "Frame")}
        except (AttributeError, TypeError):
            return {}

    def _new_worker_page(self):
        if self.context is None:
            raise RuntimeError("browser has not been started")
        self.ensure_browser_available()
        try:
            page = self.context.new_page()
        except Exception as exc:
            self.ensure_browser_available()
            raise CrawlError("worker tab creation failed: " + type(exc).__name__) from None
        try:
            page.set_default_timeout(self.settings.page_timeout_ms)
        except Exception:
            page.close()
            raise
        self.page = page
        self._worker_crashed = False
        on = getattr(page, "on", None)
        if callable(on):
            on("crash", lambda *_: self._mark_worker_crashed(page))
        self.logger.info("dedicated crawler tab created")
        return page

    @staticmethod
    def _page_is_closed(page) -> bool:
        is_closed = getattr(page, "is_closed", None)
        return bool(is_closed()) if callable(is_closed) else False

    def _show_browser_window(self, page=None) -> None:
        page = page or self.page
        if self.context is None or page is None or self.settings.headless:
            return
        try:
            page.bring_to_front()
            session = self.context.new_cdp_session(page)
            try:
                window = session.send("Browser.getWindowForTarget")
                session.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window["windowId"],
                        "bounds": {
                            "windowState": "normal",
                            "left": 0,
                            "top": 0,
                            "width": 1365,
                            "height": 768,
                        },
                    },
                )
            finally:
                session.detach()
        except Exception as exc:
            self.logger.warning("could not normalize Chromium window: %s", type(exc).__name__)

    def _restore_session_cookies(self) -> None:
        state_path = self.settings.browser_state_path
        if self.context is None or not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            stored_cookies = state.get("cookies", [])
            existing_keys = {
                self._cookie_key(cookie) for cookie in self.context.cookies()
            }
            missing_cookies = [
                cookie
                for cookie in stored_cookies
                if self._cookie_key(cookie) not in existing_keys
            ]
            if missing_cookies:
                self.context.add_cookies(missing_cookies)
                self.logger.info(
                    "restored %d missing browser cookies from protected storage state; "
                    "%d persistent-profile cookies kept",
                    len(missing_cookies),
                    len(existing_keys),
                )
            elif stored_cookies:
                self.logger.info(
                    "persistent browser profile already contains stored cookies; "
                    "profile values kept"
                )
        except Exception as exc:
            self.logger.warning(
                "could not restore browser storage state: %s", type(exc).__name__
            )

    @staticmethod
    def _cookie_key(cookie: dict) -> tuple[str, str, str]:
        return (
            str(cookie.get("name", "")),
            str(cookie.get("domain", "")),
            str(cookie.get("path", "/")),
        )

    def persist_session_state(self) -> None:
        if self.context is None:
            return
        state_path = self.settings.browser_state_path
        temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
        state = self.context.storage_state()
        temporary_path.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(state_path)
        state_path.chmod(0o600)
        self.diagnostics.record_cookies(state.get("cookies", []), "storage_state_saved")
        self.logger.info("browser storage state saved securely")

    def close(self) -> None:
        try:
            if self.context is not None:
                try:
                    self.ensure_browser_available()
                    if self.current_page_kind() in {
                        WebVPNPageKind.RANKLIST, WebVPNPageKind.SUBMISSION,
                    }:
                        self.persist_session_state()
                except Exception as exc:
                    self.logger.warning("browser state not saved on shutdown: %s", type(exc).__name__)
                self.context.close()
        finally:
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self.context = None
                self.page = None
                self._playwright = None

    def _mark_worker_crashed(self, page) -> None:
        if self.page is page:
            self._worker_crashed = True

    def _require_page(self):
        self.ensure_browser_available()
        if getattr(self, "_worker_crashed", False):
            try:
                self.page.close()
            except Exception as exc:
                self.ensure_browser_available()
                self.logger.warning("could not close crashed worker: %s", type(exc).__name__)
            self.page = None
        if self.page is None or self._page_is_closed(self.page):
            self.logger.warning("dedicated crawler tab was closed; creating a replacement")
            return self._new_worker_page()
        return self.page

    def current_page_kind(self) -> WebVPNPageKind:
        page = self._require_page()
        try:
            return classify_webvpn_page(page.url, page.content())
        except Exception as exc:
            self.logger.warning(
                "could not inspect current browser page: %s", type(exc).__name__
            )
            self.ensure_browser_available()
            return WebVPNPageKind.UNKNOWN

    def current_page_is_login(self) -> bool:
        return self.current_page_kind() == WebVPNPageKind.LOGIN

    def _cookies(self) -> list[dict]:
        if self.context is None:
            return []
        try:
            return self.context.cookies()
        except Exception as exc:
            self.logger.warning("could not inspect browser cookies: %s", type(exc).__name__)
            return []

    @staticmethod
    def _redirect_chain(response) -> list[str]:
        if response is None:
            return []
        chain = []
        request = response.request
        while request.redirected_from is not None:
            request = request.redirected_from
            chain.append(sanitize_url(request.url))
        chain.reverse()
        return chain

    def _navigate(
        self,
        url: str,
        purpose: str,
        *,
        force_diagnostic: bool = False,
    ) -> tuple[str, NavigationProbe]:
        page = self._require_page()
        self._navigation_count += 1
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=self.settings.page_timeout_ms,
            )
            self._clear_storage_seed()
            html = page.content()
        except Exception as exc:
            safe_url = sanitize_url(url)
            self.diagnostics.record_event(
                "navigation_error",
                purpose=purpose,
                requested_url=safe_url,
                error_type=type(exc).__name__,
            )
            self.ensure_browser_available()
            raise CrawlError(
                f"page load failed for {safe_url}: {type(exc).__name__}"
            ) from None

        redirects = self._redirect_chain(response)
        classification = classify_webvpn_page(page.url, html)
        probe = NavigationProbe(
            requested_url=sanitize_url(url),
            final_url=sanitize_url(page.url),
            status=response.status if response is not None else None,
            classification=classification.value,
            redirect_count=len(redirects),
            redirect_chain=redirects,
        )
        self.diagnostics.record_navigation(
            purpose,
            probe.as_dict(),
            force=force_diagnostic,
        )
        return html, probe

    def _load(
        self,
        url: str,
        purpose: str = "ranklist",
        *,
        force_diagnostic: bool = False,
    ) -> str:
        html, probe = self._navigate(
            url,
            purpose,
            force_diagnostic=force_diagnostic,
        )
        if probe.status in {401, 403}:
            raise WebVPNSessionExpired(
                f"WebVPN returned HTTP {probe.status} for {purpose}"
            )
        if probe.classification == WebVPNPageKind.LOGIN.value:
            raise WebVPNSessionExpired(
                f"WebVPN redirected to login during {purpose}"
            )
        if probe.status is None or probe.status >= 400:
            raise CrawlError(
                f"unexpected HTTP status {probe.status} during {purpose}; "
                f"final_url={probe.final_url}"
            )
        if (
            probe.classification == WebVPNPageKind.UNKNOWN.value
            and probe.redirect_count > 0
        ):
            raise WebVPNSessionExpired(
                f"WebVPN ranklist request redirected to an unknown page during {purpose}"
            )
        if probe.classification != WebVPNPageKind.RANKLIST.value:
            raise CrawlError(
                f"refusing non-ranklist page during {purpose}; "
                f"classification={probe.classification} final_url={probe.final_url}"
            )
        return html

    def mark_ready(self, reason: str) -> None:
        self.diagnostics.transition("READY", reason, cookies=self._cookies())

    def enter_waiting_for_auth(self, reason: str) -> None:
        self._observed_auth_evidence = self._authentication_evidence()
        self._show_browser_window()
        self.diagnostics.transition(
            "WAITING_FOR_AUTH",
            reason,
            cookies=self._cookies(),
        )

    def attempt_automatic_recovery(self, reason: str) -> bool:
        self.diagnostics.transition(
            "RECOVERING",
            reason,
            cookies=self._cookies(),
        )
        self._show_browser_window()
        self.logger.warning(
            "WebVPN session invalid; trying normal login entry once: %s",
            sanitize_url(self.settings.login_entry_url),
        )
        try:
            self._navigate(
                self.settings.login_entry_url,
                "automatic_recovery_entry",
                force_diagnostic=True,
            )
            page = self._require_page()
            page.wait_for_timeout(self.settings.auth_recovery_wait_ms)
            settled_html = page.content()
            settled_kind = classify_webvpn_page(page.url, settled_html)
            self.diagnostics.record_event(
                "automatic_recovery_settled",
                final_url=sanitize_url(page.url),
                classification=settled_kind.value,
            )
            if not has_authenticated_page(page.url, settled_html, self.settings.webvpn_origin):
                return False

            self._load(
                self.settings.ranklist_url(),
                "automatic_recovery_validation",
                force_diagnostic=True,
            )
            self.persist_session_state()
            self.mark_ready("automatic_recovery_succeeded")
            return True
        except BrowserUnavailable:
            raise
        except Exception as exc:
            self.ensure_browser_available()
            self.logger.warning(
                "automatic WebVPN recovery did not authenticate: %s",
                type(exc).__name__,
            )
            return False

    def _authentication_evidence(self) -> set[tuple]:
        self.ensure_browser_available()
        pages = list(getattr(self.context, "pages", []))
        if self.page is not None and self.page not in pages:
            pages.append(self.page)
        evidence = set()
        for page in pages:
            try:
                if self._page_is_closed(page):
                    continue
                html = page.content()
                if has_authenticated_page(page.url, html, self.settings.webvpn_origin):
                    evidence.add((id(page), page.url))
            except Exception as exc:
                self.ensure_browser_available()
                self.logger.debug("authentication observation unavailable: %s", type(exc).__name__)
        self.ensure_browser_available()
        return evidence

    def validate_after_user_authentication(self) -> bool:
        # No creation/navigation/closing of tabs until new positive evidence.
        # Stale authenticated tabs present before waiting do not count.
        evidence = self._authentication_evidence()
        changed = evidence - self._observed_auth_evidence
        self._observed_auth_evidence = evidence
        if not changed:
            return False
        self.logger.info("observed authenticated page; validating WebVPN session")
        try:
            self._load(self.settings.ranklist_url(), "manual_auth_validation", force_diagnostic=True)
            self.persist_session_state()
            self.mark_ready("manual_authentication_succeeded")
            return True
        except BrowserUnavailable:
            raise
        except Exception as exc:
            self.ensure_browser_available()
            self.logger.warning("manual authentication not ready: %s", type(exc).__name__)
            return False

    @staticmethod
    def _same_user_payload(left: RankEntry, right: RankEntry) -> bool:
        return (
            left.user_id == right.user_id
            and left.nickname == right.nickname
            and left.accepted == right.accepted
            and left.submitted == right.submitted
            and left.ratio == right.ratio
            and left.level == right.level
        )

    def _finalize_entries(self, entries: list[RankEntry]) -> list[RankEntry]:
        normalized = [replace(entry, rank=index) for index, entry in enumerate(entries, start=1)]
        changed = sum(before.rank != after.rank for before, after in zip(entries, normalized))
        if changed:
            self.logger.warning(
                "normalized %d source ranks after removing duplicate source rows",
                changed,
            )
        return normalized

    def scrape_all(
        self, scope: str | None = None, prefix: str | None = None
    ) -> Sequence[RankEntry]:
        between_pages = getattr(self, "between_pages_callback", None)
        target_prefix = prefix or self.settings.prefix
        self.logger.info(
            "ranklist scrape started, prefix=%s scope=%s",
            target_prefix,
            scope or "all-time",
        )
        collected: list[RankEntry] = []
        seen: dict[str, tuple[int, RankEntry]] = {}

        start = 0
        for page_index in range(self.settings.max_pages):
            html = self._load(
                self.settings.ranklist_url(start, scope, target_prefix),
                f"ranklist_{target_prefix}_{scope or 'all'}_page_{page_index}",
                force_diagnostic=page_index == 0,
            )
            try:
                entries = parse_ranklist(html)
            except RanklistParseError as exc:
                raise CrawlError(f"ranklist page {page_index} HTML parse failed: {exc}") from None

            if not entries:
                if page_index == 0:
                    structure = "table present" if has_ranklist_table(html) else "table missing"
                    raise CrawlError(f"ranklist page 0 has no users ({structure})")
                self.logger.info("ranklist page %d is empty; pagination complete", page_index)
                if between_pages is not None:
                    between_pages()
                return self._finalize_entries(collected)

            self.logger.info(
                "ranklist page %d loaded, %d rows, ranks %d..%d, final_url=%s",
                page_index,
                len(entries),
                entries[0].rank,
                entries[-1].rank,
                sanitize_url(self._require_page().url),
            )

            new_entries: list[RankEntry] = []
            for entry in entries:
                if scope is None and not entry.user_id.startswith(target_prefix):
                    raise CrawlError(
                        f"unexpected account prefix on page {page_index}"
                    )
                if entry.user_id in seen:
                    previous_page, previous_entry = seen[entry.user_id]
                    if self._same_user_payload(entry, previous_entry):
                        self.logger.warning(
                            "ranklist source repeated an account on pages %d and %d "
                            "with source ranks %d and %d; payload is identical, deduplicated safely",
                            previous_page,
                            page_index,
                            previous_entry.rank,
                            entry.rank,
                        )
                        continue
                    raise CrawlError(
                        f"conflicting duplicate account on pages {previous_page} and {page_index} "
                        f"("
                        f"final_url={sanitize_url(self._require_page().url)})"
                    )
                seen[entry.user_id] = (page_index, entry)
                new_entries.append(entry)
            collected.extend(new_entries)
            self.logger.info(
                "ranklist page %d OK, %d new users, %d unique total",
                page_index,
                len(new_entries),
                len(collected),
            )

            # Source page sizes can change independently of our configuration.
            # Advance by raw rows (including compatible duplicates), and require
            # a structurally valid empty page before publishing a complete result.
            start += len(entries)
            if between_pages is not None:
                between_pages()

        raise CrawlError(
            f"reached MAX_PAGES={self.settings.max_pages} before a validated empty page; "
            "refusing to save a possibly truncated ranklist"
        )

    def scrape_submissions(self, user_id: str):
        url = self.settings.submission_url(user_id)
        html, probe = self._navigate(url, "submission_query")
        if probe.status in {401, 403} or probe.classification == WebVPNPageKind.LOGIN.value:
            raise WebVPNSessionExpired("WebVPN authentication expired during submission query")
        if probe.status is None or probe.status >= 400:
            raise CrawlError(f"unexpected HTTP status {probe.status} during submission query")
        if not has_submission_table(html):
            if probe.redirect_count > 0:
                raise WebVPNSessionExpired("submission query redirected away from OJ")
            raise CrawlError("submission page does not contain the expected table")
        try:
            records = parse_submissions(html, user_id)
        except SubmissionParseError as exc:
            raise CrawlError(f"submission HTML parse failed: {exc}") from None
        if len(records) > 50:
            raise CrawlError("submission page exceeds the safe record limit")
        self.logger.info("submission query completed, records=%d", len(records))
        return records
