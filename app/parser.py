from __future__ import annotations

import re
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup

from .models import RankEntry


class RanklistParseError(ValueError):
    pass


class SubmissionParseError(ValueError):
    pass


@dataclass(frozen=True)
class SubmissionRecord:
    submission_id: int
    user_id: str
    problem_id: str
    result: str
    memory_kb: int
    time_ms: int
    language: str
    code_length: str
    submitted_at: str
    judge: str


class WebVPNPageKind(str, Enum):
    RANKLIST = "ranklist"
    SUBMISSION = "submission"
    LOGIN = "login"
    UNKNOWN = "unknown"


def _parse_int(value: str, field: str) -> int:
    normalized = value.replace(",", "").strip()
    if not re.fullmatch(r"-?\d+", normalized):
        raise RanklistParseError(f"invalid {field} format")
    number = int(normalized)
    if number < (1 if field == "rank" else 0):
        raise RanklistParseError(f"invalid {field} domain")
    return number


def _parse_ratio(value: str) -> float:
    normalized = value.strip().removesuffix("%").strip()
    try:
        number = float(normalized)
        if not math.isfinite(number) or not 0 <= number <= 100:
            raise RanklistParseError("ratio must be finite and between 0 and 100")
        return number
    except ValueError as exc:
        raise RanklistParseError("invalid ratio format or domain") from None


# Headers are also required for an empty page: a generic empty table is not
# evidence that pagination completed. Nonempty legacy pages may omit headers.
_HEADER_ALIASES = (
    {"rank", "排名", "名次"},
    {"user", "userid", "用户", "用户id", "用户名", "学号"},
    {"nick", "nickname", "昵称"},
    {"solved", "accepted", "ac", "解决", "通过", "解题数", "正确"},
    {"submit", "submitted", "提交", "提交数"},
    {"ratio", "acratio", "正确率", "通过率", "比率"},
    {"level", "等级", "级别"},
)

_SUBMISSION_HEADER_ALIASES = (
    {"runid", "提交编号", "提交号"},
    {"user", "userid", "用户", "用户id", "用户名", "学号"},
    {"problem", "problemid", "问题", "题目", "题号"},
    {"result", "结果"},
    {"memory", "memory(kb)", "内存", "内存(kb)"},
    {"time", "time(ms)", "耗时", "耗时(ms)"},
    {"language", "语言"},
    {"codelength", "codelen", "代码长度"},
    {"submittime", "提交时间"},
    {"judger", "判题机", "判题者"},
)


def _cells(row):
    return row.find_all(["td", "th"], recursive=False)


def _is_header(row) -> bool:
    values = [re.sub(r"[\s%:：]+", "", c.get_text(" ", strip=True)).lower()
              for c in _cells(row)]
    return len(values) == 7 and all(v in names for v, names in zip(values, _HEADER_ALIASES))


def _normalized_headers(row) -> list[str]:
    return [re.sub(r"[\s:：]+", "", cell.get_text(" ", strip=True)).lower()
            for cell in _cells(row)]


def _is_submission_header(row) -> bool:
    values = _normalized_headers(row)
    return len(values) == 10 and all(
        value in aliases for value, aliases in zip(values, _SUBMISSION_HEADER_ALIASES)
    )


def _submission_table(soup):
    candidates = []
    for table in soup.select("table.table"):
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        if any(_is_submission_header(row) for row in rows):
            candidates.append(table)
    if len(candidates) != 1:
        raise SubmissionParseError("missing or ambiguous submission table")
    return candidates[0]


def has_submission_table(html: str) -> bool:
    try:
        _submission_table(BeautifulSoup(html, "html.parser"))
        return True
    except SubmissionParseError:
        return False


def _ranklist_table(soup):
    candidates = []
    for table in soup.select("table.table"):
        rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
        header = any(_is_header(row) for row in rows)
        shaped = any(len(_cells(row)) == 7
                     and re.fullmatch(r"[0-9,]+", _cells(row)[0].get_text(strip=True))
                     and _cells(row)[5].get_text(strip=True).endswith("%")
                     for row in rows)
        if header or shaped:
            candidates.append(table)
    if len(candidates) != 1:
        raise RanklistParseError("missing or ambiguous ranklist table")
    return candidates[0]


def has_ranklist_table(html: str) -> bool:
    try:
        _ranklist_table(BeautifulSoup(html, "html.parser"))
        return True
    except RanklistParseError:
        return False


def classify_webvpn_page(url: str, html: str) -> WebVPNPageKind:
    path = urlparse(url).path.lower().rstrip("/")
    login_path_markers = (
        "/login",
        "/authserver",
        "/cas/login",
        "/oauth/authorize",
        "/sso/login",
    )
    if any(marker in path for marker in login_path_markers):
        return WebVPNPageKind.LOGIN

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    source = html.lower()
    markers = (
        "webvpn",
        "统一身份认证",
        "扫码登录",
        "验证码",
        "cas login",
        "账号登录",
    )
    marker_count = sum(marker in text or marker in source for marker in markers)
    has_login_control = (
        soup.select_one('input[type="password"]') is not None
        or soup.select_one('form[action*="login"]') is not None
        or soup.select_one('form[action*="auth"]') is not None
        or "qrcode" in source
        or "qr-code" in source
    )
    if marker_count >= 1 and has_login_control:
        return WebVPNPageKind.LOGIN
    if has_login_control and not has_ranklist_table(html):
        return WebVPNPageKind.LOGIN
    if has_ranklist_table(html):
        return WebVPNPageKind.RANKLIST
    if has_submission_table(html):
        return WebVPNPageKind.SUBMISSION
    if marker_count >= 2:
        return WebVPNPageKind.LOGIN
    return WebVPNPageKind.UNKNOWN


def is_webvpn_login_page(url: str, html: str) -> bool:
    return classify_webvpn_page(url, html) == WebVPNPageKind.LOGIN


def parse_ranklist(html: str) -> list[RankEntry]:
    soup = BeautifulSoup(html, "html.parser")
    entries: list[RankEntry] = []
    errors: list[str] = []

    table = _ranklist_table(soup)
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    for row_index, row in enumerate(rows, start=1):
        if _is_header(row):
            continue
        cells = _cells(row)
        # HUSTOJ puts its prefix filter immediately before the column header.
        # Accept only this control row, never arbitrary short/malformed data.
        if (row_index == 1 and len(rows) > 1 and _is_header(rows[1])
                and len(cells) == 2
                and all(cell.get("colspan") == "3" for cell in cells)
                and row.select_one('form input[name="prefix"]') is not None
                and row.select_one('form input[type="submit"]') is not None):
            continue
        if not cells:
            continue
        if len(cells) != 7:
            errors.append(f"row {row_index}: expected 7 columns, got {len(cells)}")
            continue

        values = [cell.get_text(" ", strip=True) for cell in cells[:7]]
        try:
            if not values[1]:
                raise RanklistParseError("empty user_id")
            entries.append(
                RankEntry(
                    rank=_parse_int(values[0], "rank"),
                    user_id=values[1],
                    nickname=values[2],
                    accepted=_parse_int(values[3], "accepted"),
                    submitted=_parse_int(values[4], "submitted"),
                    ratio=_parse_ratio(values[5]),
                    level=values[6],
                )
            )
        except RanklistParseError as exc:
            errors.append(f"row {row_index}: {exc}")

    if errors:
        preview = "; ".join(errors[:5])
        if len(errors) > 5:
            preview += f"; and {len(errors) - 5} more"
        raise RanklistParseError(preview)
    return entries


def parse_submissions(html: str, expected_user_id: str) -> list[SubmissionRecord]:
    soup = BeautifulSoup(html, "html.parser")
    table = _submission_table(soup)
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    records: list[SubmissionRecord] = []
    errors: list[str] = []
    seen_ids: set[int] = set()
    for row_index, row in enumerate(rows, start=1):
        if _is_submission_header(row):
            continue
        cells = _cells(row)
        if not cells:
            continue
        if len(cells) != 10:
            errors.append(f"row {row_index}: expected 10 columns, got {len(cells)}")
            continue
        values = [cell.get_text(" ", strip=True) for cell in cells]
        try:
            submission_id = _parse_int(values[0], "submission_id")
            if submission_id <= 0:
                raise SubmissionParseError("invalid submission id")
            memory_kb = _parse_int(values[4], "memory_kb")
            time_ms = _parse_int(values[5], "time_ms")
            if values[1] != expected_user_id:
                raise SubmissionParseError("unexpected user id")
            if not re.fullmatch(r"[A-Za-z0-9_-]+", values[2]):
                raise SubmissionParseError("invalid problem id")
            if not values[3] or not values[6] or not values[7]:
                raise SubmissionParseError("required text field is empty")
            datetime.strptime(values[8], "%Y-%m-%d %H:%M:%S")
            if submission_id in seen_ids:
                raise SubmissionParseError("duplicate submission id")
            seen_ids.add(submission_id)
            records.append(SubmissionRecord(
                submission_id=submission_id, user_id=values[1], problem_id=values[2],
                result=values[3], memory_kb=memory_kb, time_ms=time_ms,
                language=values[6], code_length=values[7], submitted_at=values[8],
                judge=values[9],
            ))
        except (RanklistParseError, SubmissionParseError, ValueError) as exc:
            errors.append(f"row {row_index}: {exc}")
    if errors:
        preview = "; ".join(errors[:5])
        if len(errors) > 5:
            preview += f"; and {len(errors) - 5} more"
        raise SubmissionParseError(preview)
    return records


def has_authenticated_page(url: str, html: str, webvpn_origin: str) -> bool:
    """Positive passive evidence only; an unknown page is not proof of login."""
    kind = classify_webvpn_page(url, html)
    if kind == WebVPNPageKind.LOGIN:
        return False
    try:
        actual, expected = urlparse(url), urlparse(webvpn_origin)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            return False
    except ValueError:
        return False
    if kind in {WebVPNPageKind.RANKLIST, WebVPNPageKind.SUBMISSION}:
        return True
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("a[href], form[action]"):
        target = node.get("href", node.get("action", ""))
        try:
            resolved = urlparse(urljoin(url, target))
            if ((resolved.scheme, resolved.netloc) == (expected.scheme, expected.netloc)
                    and resolved.path.rstrip("/").lower().endswith("/logout")):
                return True
        except ValueError:
            continue
    return False
