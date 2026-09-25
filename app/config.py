from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from .student_ids import StudentIdLayout

try:
    from dotenv import load_dotenv
except ImportError:  # Tests can run before dependencies are installed.
    load_dotenv = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if load_dotenv is not None:
    load_dotenv(PROJECT_ROOT / ".env")


def _integer(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _string_set(name: str, default: str = "") -> frozenset[str]:
    value = os.getenv(name, default)
    return frozenset(item for item in re.split(r"[\s,]+", value.strip()) if item)


@dataclass(frozen=True)
class Settings:
    webvpn_origin: str
    oj_proxy_base: str
    prefix: str
    lookup_prefixes: tuple[str, ...]
    student_id_length: int
    major_code_length: int
    class_code_length: int
    student_id_pattern: str
    excluded_user_ids: frozenset[str]
    data_dir: Path
    log_dir: Path
    profile_dir: Path
    playwright_browsers_path: Path
    share_dir: Path
    scrape_interval_seconds: int
    login_check_seconds: int
    error_retry_seconds: int
    page_size: int
    max_pages: int
    page_timeout_ms: int
    browser_executable: str | None
    headless: bool
    webvpn_login_url: str = ""
    auth_recovery_wait_ms: int = 5000
    college_split_snapshot_id: int = 1

    def __post_init__(self) -> None:
        StudentIdLayout(self.prefix, self.student_id_length, self.major_code_length, self.class_code_length, self.student_id_pattern)
        if not self.lookup_prefixes:
            raise ValueError("OJ_LOOKUP_PREFIXES must contain at least one prefix")
        if self.prefix not in self.lookup_prefixes:
            raise ValueError("OJ_LOOKUP_PREFIXES must include OJ_PREFIX")
        if any(
            not re.fullmatch(r"[A-Za-z0-9_-]+", item)
            or not re.fullmatch(self.student_id_pattern, item)
            or len(item) != len(self.prefix)
            for item in self.lookup_prefixes
        ):
            raise ValueError("lookup prefixes must match the configured pattern and prefix length")

    @property
    def student_id_layout(self) -> StudentIdLayout:
        return StudentIdLayout(self.prefix, self.student_id_length, self.major_code_length, self.class_code_length, self.student_id_pattern)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "rank.db"

    @property
    def browser_state_path(self) -> Path:
        return self.data_dir / "webvpn-storage-state.json"

    @property
    def login_entry_url(self) -> str:
        return self.webvpn_login_url or self.webvpn_origin.rstrip("/") + "/"

    def ranklist_url(
        self, start: int = 0, scope: str | None = None, prefix: str | None = None
    ) -> str:
        query: dict[str, str | int] = {"prefix": prefix or self.prefix}
        if scope:
            query["scope"] = scope
        if start:
            query["start"] = start
        return f"{self.oj_proxy_base.rstrip('/')}/ranklist.php?{urlencode(query)}"

    def submission_url(self, user_id: str) -> str:
        if (
            len(user_id) != self.student_id_length
            or not re.fullmatch(r"[A-Za-z0-9_-]+", user_id)
            or not re.fullmatch(self.student_id_pattern, user_id)
        ):
            raise ValueError("invalid student id")
        if not any(user_id.startswith(prefix) for prefix in self.lookup_prefixes):
            raise ValueError("student id prefix is outside the configured lookup range")
        query = {"user_id": user_id, "language": -1, "jresult": -1}
        return f"{self.oj_proxy_base.rstrip('/')}/status.php?{urlencode(query)}"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("OJ_DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser()
        prefix = os.getenv("OJ_PREFIX", "2026").strip()
        lookup_prefixes = tuple(
            item
            for item in re.split(
                r"[\s,]+", os.getenv("OJ_LOOKUP_PREFIXES", prefix).strip()
            )
            if item
        )
        return cls(
            webvpn_origin=os.getenv("WEBVPN_ORIGIN", "https://webvpn.example.edu"),
            oj_proxy_base=os.getenv(
                "OJ_PROXY_BASE",
                "https://webvpn.example.edu/http/replace-with-proxy-token",
            ),
            prefix=prefix,
            lookup_prefixes=lookup_prefixes,
            student_id_length=_integer("STUDENT_ID_LENGTH", 12),
            major_code_length=_integer("MAJOR_CODE_LENGTH", 4),
            class_code_length=_integer("CLASS_CODE_LENGTH", 6),
            student_id_pattern=os.getenv("STUDENT_ID_PATTERN", r"[0-9]+").strip(),
            excluded_user_ids=_string_set("OJ_EXCLUDED_USER_IDS"),
            data_dir=data_dir,
            log_dir=Path(os.getenv("OJ_LOG_DIR", str(PROJECT_ROOT / "logs"))).expanduser(),
            profile_dir=Path(
                os.getenv("WEBVPN_PROFILE_DIR", str(data_dir / "webvpn-profile"))
            ).expanduser(),
            playwright_browsers_path=Path(
                os.getenv("PLAYWRIGHT_BROWSERS_PATH", str(data_dir / "ms-playwright"))
            ).expanduser(),
            share_dir=Path(
                os.getenv("OJ_SHARE_DIR", str(PROJECT_ROOT / "share"))
            ).expanduser(),
            scrape_interval_seconds=_integer("SCRAPE_INTERVAL_SECONDS", 300),
            login_check_seconds=_integer("LOGIN_CHECK_SECONDS", 60),
            error_retry_seconds=_integer("ERROR_RETRY_SECONDS", 60),
            page_size=_integer("RANK_PAGE_SIZE", 50),
            max_pages=_integer("MAX_PAGES", 100),
            page_timeout_ms=_integer("PAGE_TIMEOUT_MS", 45_000),
            browser_executable=os.getenv("BROWSER_EXECUTABLE") or None,
            headless=_boolean("BROWSER_HEADLESS", False),
            webvpn_login_url=os.getenv("WEBVPN_LOGIN_URL", "").strip(),
            auth_recovery_wait_ms=_integer("AUTH_RECOVERY_WAIT_MS", 5000),
            college_split_snapshot_id=_integer("COLLEGE_SPLIT_SNAPSHOT_ID", 1),
        )

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.log_dir,
            self.profile_dir,
            self.playwright_browsers_path,
        ):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)
        self.share_dir.mkdir(parents=True, exist_ok=True)
        self.share_dir.chmod(0o755)
