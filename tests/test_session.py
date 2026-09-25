from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from app.session import SessionDiagnostics, sanitize_url


class SessionDiagnosticsTests(unittest.TestCase):
    def test_url_query_values_are_never_persisted(self) -> None:
        sanitized = sanitize_url(
            "https://auth.example/login?ticket=SUPER_SECRET&service=PRIVATE#token"
        )
        self.assertNotIn("SUPER_SECRET", sanitized)
        self.assertNotIn("PRIVATE", sanitized)
        self.assertNotIn("token", sanitized)
        self.assertIn("ticket", sanitized)
        self.assertIn("service", sanitized)

    def test_cookie_values_are_replaced_by_keyed_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            diagnostics = SessionDiagnostics(
                Path(directory),
                logging.getLogger("test_session"),
            )
            cookies = [
                {
                    "name": "vpn_ticket",
                    "value": "RAW_COOKIE_SECRET",
                    "domain": ".webvpn.example",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
            diagnostics.transition("READY", "test", cookies=cookies)
            diagnostics.transition("WAITING_FOR_AUTH", "expired", cookies=cookies)

            status_text = diagnostics.status_path.read_text(encoding="utf-8")
            events_text = diagnostics.events_path.read_text(encoding="utf-8")
            self.assertNotIn("RAW_COOKIE_SECRET", status_text)
            self.assertNotIn("RAW_COOKIE_SECRET", events_text)
            payload = json.loads(status_text)
            self.assertEqual("WAITING_FOR_AUTH", payload["state"])
            self.assertEqual("vpn_ticket", payload["cookies"][0]["name"])
            self.assertTrue(payload["cookies"][0]["session_cookie"])
            self.assertEqual(16, len(payload["cookies"][0]["value_fingerprint"]))
            self.assertEqual(0, payload["last_session_age_seconds"])
            self.assertEqual(0o600, diagnostics.status_path.stat().st_mode & 0o777)
            self.assertEqual(0o600, diagnostics.events_path.stat().st_mode & 0o777)


if __name__ == "__main__":
    unittest.main()
