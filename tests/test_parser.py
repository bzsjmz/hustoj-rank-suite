from __future__ import annotations

import unittest

from app.parser import (
    RanklistParseError,
    SubmissionParseError,
    WebVPNPageKind,
    classify_webvpn_page,
    is_webvpn_login_page,
    parse_ranklist,
    parse_submissions,
)


RANK_HTML = """
<html><body><table class="table"><tbody>
<tr class="evenrow">
  <td>1</td><td><a>202600000101</a></td><td>示例同学甲</td>
  <td>34</td><td>36</td><td>94.444%</td><td>初学乍练</td>
</tr>
<tr class="oddrow">
  <td>2</td><td>202600000002</td><td></td>
  <td>1</td><td>4</td><td>25%</td><td>初学乍练</td>
</tr>
</tbody></table></body></html>
"""

SUBMISSION_HTML = """
<table class="table"><thead><tr>
<th>提交编号</th><th>用户</th><th>问题</th><th>结果</th><th>内存(KB)</th>
<th>耗时(MS)</th><th>语言</th><th>代码长度</th><th>提交时间</th><th>判题机</th>
</tr></thead><tbody>
<tr><td>9000002</td><td><a>202610010199</a></td><td>1087</td><td>正确</td>
<td>1044</td><td>1</td><td>C</td><td>471 B</td><td>2026-01-02 12:00:00</td><td>LOCAL</td></tr>
<tr><td>9000001</td><td>202610010199</td><td>1085</td><td>编译错误</td>
<td>0</td><td>0</td><td>C++</td><td>322 B</td><td>2026-01-02 11:55:00</td><td></td></tr>
</tbody></table>
"""


class ParserTests(unittest.TestCase):
    def test_parses_submission_rows_from_status_page(self) -> None:
        records = parse_submissions(SUBMISSION_HTML, "202610010199")
        self.assertEqual(2, len(records))
        self.assertEqual(9000002, records[0].submission_id)
        self.assertEqual("1087", records[0].problem_id)
        self.assertEqual("正确", records[0].result)
        self.assertEqual("", records[1].judge)
        self.assertEqual(
            WebVPNPageKind.SUBMISSION,
            classify_webvpn_page("https://webvpn.example/http/token/status.php", SUBMISSION_HTML),
        )

    def test_submission_page_requires_exact_structure_and_user(self) -> None:
        for html, user_id in (
            (SUBMISSION_HTML, "202600000001"),
            (SUBMISSION_HTML.replace("1044", "-1", 1), "202610010199"),
            (SUBMISSION_HTML.replace("9000002", "0", 1), "202610010199"),
            (SUBMISSION_HTML.replace("提交时间", "未知字段"), "202610010199"),
        ):
            with self.subTest(user_id=user_id), self.assertRaises(SubmissionParseError):
                parse_submissions(html, user_id)

    def test_empty_submission_table_is_valid(self) -> None:
        empty = SUBMISSION_HTML.split("<tbody>", 1)[0] + "<tbody></tbody></table>"
        self.assertEqual([], parse_submissions(empty, "202610010199"))

    def test_parses_rank_rows_and_empty_nickname(self) -> None:
        entries = parse_ranklist(RANK_HTML)
        self.assertEqual(2, len(entries))
        self.assertEqual("202600000101", entries[0].user_id)
        self.assertEqual("示例同学甲", entries[0].nickname)
        self.assertEqual(34, entries[0].accepted)
        self.assertAlmostEqual(94.444, entries[0].ratio)
        self.assertEqual("", entries[1].nickname)

    def test_numeric_domains(self) -> None:
        for old, new in (("<td>1</td>", "<td>0</td>"),
                         ("<td>34</td>", "<td>-1</td>"),
                         ("<td>36</td>", "<td>-2</td>"),
                         ("94.444%", "NaN%"), ("94.444%", "Infinity%"),
                         ("94.444%", "-1%"), ("94.444%", "100.1%")):
            with self.subTest(new=new), self.assertRaises(RanklistParseError):
                parse_ranklist(RANK_HTML.replace(old, new, 1))

    def test_generic_table_is_not_an_empty_ranklist(self) -> None:
        for html in ("<p>error</p>", "<table class='table'><tbody></tbody></table>"):
            with self.subTest(html=html):
                self.assertEqual(WebVPNPageKind.UNKNOWN, classify_webvpn_page("https://example.invalid/ranklist.php", html))
                with self.assertRaises(RanklistParseError):
                    parse_ranklist(html)

    def test_recognized_empty_header_and_no_tbody(self) -> None:
        header = "<tr>" + "".join(f"<td>{v}</td>" for v in ("排名", "用户", "昵称", "解决", "提交", "正确率", "等级")) + "</tr>"
        self.assertEqual([], parse_ranklist("<table class='table'>" + header + "</table>"))
        self.assertEqual(2, len(parse_ranklist(RANK_HTML.replace("<tbody>", "").replace("</tbody>", ""))))

    def test_source_filter_and_chinese_header(self) -> None:
        header = "<tr>" + "".join(f"<td>{v}</td>" for v in
            ("名次", "用户", "昵称", "正确", "提交", "比率", "级别")) + "</tr>"
        controls = ('<tr><td colspan="3"><form><input name="prefix">'
                    '<input type="submit"></form></td><td colspan="3">'
                    '<a>日</a><a>周</a><a>月</a><a>总</a></td></tr>')
        empty = '<table class="table">' + controls + header + '</table>'
        self.assertEqual([], parse_ranklist(empty))
        full = RANK_HTML.replace('<tbody>', '<tbody>' + controls + header)
        self.assertEqual(2, len(parse_ranklist(full)))
        with self.assertRaises(RanklistParseError):
            parse_ranklist(full.replace('name="prefix"', 'name="other"'))
        with self.assertRaises(RanklistParseError):
            parse_ranklist(full.replace('<td>34</td>', '<td>broken</td>', 1))
        with self.assertRaises(RanklistParseError):
            parse_ranklist('<table class="table">' + controls + '</table>')

    def test_login_url_is_detected(self) -> None:
        self.assertTrue(
            is_webvpn_login_page(
                "https://webvpn.example.invalid/login", "<html>anything</html>"
            )
        )

    def test_login_html_is_detected_without_login_url(self) -> None:
        html = "<form action='/auth/login'><h1>WebVPN</h1><p>统一身份认证</p></form>"
        self.assertTrue(is_webvpn_login_page("https://webvpn.example.invalid/", html))

    def test_rank_page_is_not_mistaken_for_login(self) -> None:
        self.assertFalse(is_webvpn_login_page("https://webvpn.example.invalid/http/x", RANK_HTML))

    def test_login_controls_win_over_rank_style_rows(self) -> None:
        html = (
            "<form action='/auth/login'><h1>WebVPN</h1><p>统一身份认证</p>"
            + RANK_HTML
            + "</form>"
        )
        self.assertEqual(
            WebVPNPageKind.LOGIN,
            classify_webvpn_page("https://webvpn.example.invalid/", html),
        )

    def test_unknown_page_is_neither_login_nor_ranklist(self) -> None:
        self.assertEqual(
            WebVPNPageKind.UNKNOWN,
            classify_webvpn_page(
                "https://webvpn.example.invalid/",
                "<html><body>unexpected portal</body></html>",
            ),
        )

    def test_malformed_row_fails_the_cycle(self) -> None:
        html = RANK_HTML.replace("<td>34</td>", "<td>not-a-number</td>", 1)
        with self.assertRaises(RanklistParseError):
            parse_ranklist(html)


if __name__ == "__main__":
    unittest.main()
