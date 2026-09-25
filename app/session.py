from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sanitize_url(value: str) -> str:
    """Keep routing evidence without persisting tickets or query values."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    # Strip userinfo, fragments and path session IDs as well as query values.
    # Unknown query keys may themselves contain secrets, so bucket them.
    allowed = {"ticket", "service", "token", "code", "state", "prefix", "scope", "start", "keys"}
    query_keys = sorted({key if key in allowed else "other"
                         for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    safe_query = "keys=" + ",".join(query_keys) if query_keys else ""
    authority = parsed.netloc.rsplit("@", 1)[-1]
    path = re.sub(r";[^/]*", ";<redacted>", parsed.path)
    return urlunsplit((parsed.scheme, authority, path, safe_query, ""))


def sanitize_log_text(value: str) -> str:
    return re.sub(r"https?://[^\s<>\"']+", lambda match: sanitize_url(match.group()), value)



class SessionDiagnostics:
    EVENT_MAX_BYTES = 5 * 1024 * 1024
    EVENT_BACKUPS = 3

    def __init__(self, data_dir: Path, logger: logging.Logger):
        self.logger = logger
        self.status_path = data_dir / "webvpn-session-status.json"
        self.events_path = data_dir / "webvpn-session-events.jsonl"
        self.key_path = data_dir / ".webvpn-diagnostics.key"
        data_dir.mkdir(parents=True, exist_ok=True)
        data_dir.chmod(0o700)
        self._key = self._load_or_create_key()
        self.status = self._load_status()

    def _load_or_create_key(self) -> bytes:
        try:
            key = self.key_path.read_bytes()
            if len(key) >= 32:
                return key
        except FileNotFoundError:
            pass
        key = secrets.token_bytes(32)
        temporary = self.key_path.with_suffix(".tmp")
        temporary.write_bytes(key)
        temporary.chmod(0o600)
        temporary.replace(self.key_path)
        self.key_path.chmod(0o600)
        return key

    def _load_status(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)

    def record_event(self, event: str, **fields: Any) -> None:
        payload = {"timestamp": _timestamp(), "event": event, **fields}
        content = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        # The collector is the sole writer. Rotate by stat/rename, never by
        # reading or copying the existing event history into memory.
        try:
            rotate = self.events_path.stat().st_size + len(content) > self.EVENT_MAX_BYTES
        except FileNotFoundError:
            rotate = False
        if rotate:
            for index in range(self.EVENT_BACKUPS, 0, -1):
                source = self.events_path if index == 1 else Path(str(self.events_path) + f".{index - 1}")
                target = Path(str(self.events_path) + f".{index}")
                if source.exists():
                    source.replace(target)
                    target.chmod(0o600)
        descriptor = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                if not written:
                    raise OSError("session event write made no progress")
                remaining = remaining[written:]
        finally:
            os.close(descriptor)
        self.events_path.chmod(0o600)

    def service_started(self) -> None:
        self.record_event(
            "service_started",
            previous_state=self.status.get("state", "UNKNOWN"),
            authenticated_at=self.status.get("authenticated_at"),
        )

    def cookie_metadata(self, cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        metadata = []
        for cookie in cookies:
            raw_value = str(cookie.get("value", "")).encode()
            fingerprint = hmac.new(self._key, raw_value, hashlib.sha256).hexdigest()[:16]
            expires = float(cookie.get("expires", -1) or -1)
            expires_at = None
            if expires > 0:
                expires_at = datetime.fromtimestamp(
                    expires, tz=timezone.utc
                ).isoformat(timespec="seconds")
            metadata.append(
                {
                    "name": str(cookie.get("name", "")),
                    "domain": str(cookie.get("domain", "")),
                    "path": str(cookie.get("path", "")),
                    "session_cookie": expires <= 0,
                    "expires_at": expires_at,
                    "http_only": bool(cookie.get("httpOnly", False)),
                    "secure": bool(cookie.get("secure", False)),
                    "same_site": str(cookie.get("sameSite", "")),
                    "value_fingerprint": fingerprint,
                }
            )
        metadata.sort(key=lambda item: (item["domain"], item["name"], item["path"]))
        return metadata

    def record_cookies(self, cookies: list[dict[str, Any]], reason: str) -> None:
        metadata = self.cookie_metadata(cookies)
        self.status["cookie_count"] = len(metadata)
        self.status["cookies"] = metadata
        self.status["cookie_observed_at"] = _timestamp()
        self._atomic_json(self.status_path, self.status)
        self.record_event(
            "cookie_snapshot",
            reason=reason,
            cookie_count=len(metadata),
            cookies=metadata,
        )

    def record_navigation(
        self,
        purpose: str,
        probe: dict[str, Any],
        *,
        force: bool = False,
    ) -> None:
        self.status.update(
            {
                "last_probe_at": _timestamp(),
                "last_http_status": probe.get("status"),
                "last_final_url": probe.get("final_url"),
                "last_classification": probe.get("classification"),
                "last_redirect_count": probe.get("redirect_count", 0),
            }
        )
        self._atomic_json(self.status_path, self.status)
        if (
            force
            or probe.get("status") != 200
            or probe.get("redirect_count", 0)
            or probe.get("classification") != "ranklist"
        ):
            self.record_event("navigation", purpose=purpose, **probe)

    def transition(
        self,
        state: str,
        reason: str,
        *,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        now = _timestamp()
        previous = str(self.status.get("state", "UNKNOWN"))
        authenticated_at = self.status.get("authenticated_at")
        session_age_seconds = None
        if authenticated_at and state in {"RECOVERING", "WAITING_FOR_AUTH"}:
            try:
                start = datetime.fromisoformat(str(authenticated_at))
                session_age_seconds = max(
                    0,
                    int((datetime.now().astimezone() - start).total_seconds()),
                )
            except ValueError:
                session_age_seconds = None

        self.status.update(
            {
                "state": state,
                "state_changed_at": now,
                "state_reason": reason,
            }
        )
        if state == "READY":
            self.status["last_success_at"] = now
            self.status["last_failure_reason"] = None
            if previous != "READY":
                self.status["authenticated_at"] = now
                self.status["auth_generation"] = int(
                    self.status.get("auth_generation", 0)
                ) + 1
        elif state in {"RECOVERING", "WAITING_FOR_AUTH"}:
            self.status["last_failure_at"] = now
            self.status["last_failure_reason"] = reason
            self.status["last_session_age_seconds"] = session_age_seconds

        if cookies is not None:
            metadata = self.cookie_metadata(cookies)
            self.status["cookie_count"] = len(metadata)
            self.status["cookies"] = metadata
            self.status["cookie_observed_at"] = now

        self._atomic_json(self.status_path, self.status)
        self.record_event(
            "state_transition",
            previous_state=previous,
            state=state,
            reason=reason,
            authenticated_at=self.status.get("authenticated_at"),
            session_age_seconds=session_age_seconds,
            cookie_count=self.status.get("cookie_count"),
            cookies=self.status.get("cookies", []),
        )
