"""Tests for shared records, the bounded HTTP client, price and statement adapters, and BSE mappings.

This module also defines the fake HTTP session and inline source fixtures that the
other test modules import. No test contacts a live service.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stock_info import providers  # noqa: E402
from stock_info.providers import (  # noqa: E402
    CURL_WRITE_ABORT, DateRange, HttpClient, InputError, SourceError, Symbol, compute_sha256, convert_to_json,
    encode_canonical_json, fetch_prices, fetch_statements, parse_decimal_text,
)
from stock_info.securities import lookup_code_links, parse_symbol, resolve_symbols  # noqa: E402

UTC = timezone.utc
SH = Symbol("SH:600519", "SH", "600519")
SZ = Symbol("SZ:300059", "SZ", "300059")


class FakeResponse:
    """Imitate the status and headers of a curl_cffi response.

    :param status_code: int; HTTP status.
    :param headers: dict[str, str]; response headers.
    return instance: FakeResponse; fake response.
    """

    def __init__(self, status_code: int, headers: dict[str, str]) -> None:
        """Store status and headers.

        :param status_code: int; HTTP status.
        :param headers: dict[str, str]; response headers.
        return None: None; no value is returned.
        """
        self.status_code = status_code
        self.headers = headers


class FakeSession:
    """Serve canned responses by URL matching and record every request URL.

    Each route is (predicate, handler). A handler returns (status, body, headers)
    or raises an exception to simulate a transport failure. The body is delivered
    through `content_callback` in 1 KiB chunks; an abort return raises an error
    like libcurl does.

    :param routes: list[tuple[Callable[[str], bool], Callable[[str], Any]]]; ordered routes.
    return instance: FakeSession; fake session.
    """

    def __init__(self, routes: list[tuple[Callable[[str], bool], Callable[[str], Any]]] | None = None) -> None:
        """Store routes and an empty call log.

        :param routes: list; ordered routes.
        return None: None; no value is returned.
        """
        self.routes = list(routes or [])
        self.calls: list[str] = []
        self.closed = False

    def add(self, predicate: Callable[[str], bool], handler: Callable[[str], Any]) -> None:
        """Append one route.

        :param predicate: Callable[[str], bool]; URL matcher.
        :param handler: Callable[[str], Any]; response producer.
        return None: None; no value is returned.
        """
        self.routes.append((predicate, handler))

    def get(self, url: str, *, timeout: float, allow_redirects: bool, proxy: str,
            content_callback: Callable[[bytes], int]) -> FakeResponse:
        """Serve one request through the first matching route and return the fake response.

        :param url: str; requested URL.
        :param timeout: float; ignored timeout.
        :param allow_redirects: bool; must be False.
        :param proxy: str; must be an empty string.
        :param content_callback: Callable[[bytes], int]; body sink.
        return response: FakeResponse; status and headers.
        """
        assert allow_redirects is False and proxy == ""
        self.calls.append(url)
        for predicate, handler in self.routes:
            if predicate(url):
                status, body, headers = handler(url)
                for start in range(0, len(body), 1024):
                    if content_callback(body[start:start + 1024]) == CURL_WRITE_ABORT:
                        raise RuntimeError("Failed writing received data to disk/application")
                return FakeResponse(status, headers)
        return FakeResponse(404, {})

    def close(self) -> None:
        """Mark the session closed.

        return None: None; no value is returned.
        """
        self.closed = True


def make_client(session: FakeSession, **kwargs: Any) -> HttpClient:
    """Create an HttpClient that uses the fake session and never sleeps, and return it.

    :param session: FakeSession; fake session.
    :param kwargs: Any; extra HttpClient options.
    return client: HttpClient; client for tests.
    """
    return HttpClient(session_factory=lambda: session, sleep=lambda _s: None, **kwargs)


def json_reply(payload: Any, content_type: str = "application/json") -> Callable[[str], Any]:
    """Return a handler that answers with a JSON payload.

    :param payload: Any; JSON payload.
    :param content_type: str; Content-Type header.
    return handler: Callable[[str], Any]; route handler.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return lambda _url: (200, body, {"Content-Type": content_type})


def html_reply(text: str, charset: str = "utf-8") -> Callable[[str], Any]:
    """Return a handler that answers with HTML in the given charset.

    :param text: str; HTML text.
    :param charset: str; charset name used for encoding and the header.
    return handler: Callable[[str], Any]; route handler.
    """
    body = text.encode("gb18030" if charset.lower() in ("gbk", "gb2312", "gb18030") else charset)
    return lambda _url: (200, body, {"Content-Type": f"text/html; charset={charset}"})


def eastmoney_payload(symbol: Symbol, rows: list[tuple[str, str, str, str, str]]) -> Callable[[str], Any]:
    """Return a handler that serves Eastmoney K-lines filtered to the requested window.

    Each row is (date, open, close, high, low); volume and amount are fixed.

    :param symbol: Symbol; security to echo.
    :param rows: list[tuple[str, str, str, str, str]]; daily rows.
    return handler: Callable[[str], Any]; route handler.
    """
    def handler(url: str) -> Any:
        """Serve the rows inside the beg/end window.

        :param url: str; request URL.
        return reply: Any; (status, body, headers).
        """
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(url).query)
        beg, end = query["beg"][0], query["end"][0]
        lines = [f"{d},{o},{c},{h},{lo},100,1000.5" for d, o, c, h, lo in rows
                 if beg <= d.replace("-", "") <= end]
        data = {"code": symbol.code, "market": 1 if symbol.exchange == "SH" else 0, "name": "测试", "klines": lines}
        return 200, json.dumps({"rc": 0, "data": data}).encode(), {"Content-Type": "application/json"}

    return handler


def sina_pages(symbol: Symbol, kind_source: str, periods: dict[str, dict[str, Any]],
               page_size: int = 50) -> Callable[[str], Any]:
    """Return a handler that serves Sina report pages followed by a terminal page.

    :param symbol: Symbol; security (unused by the handler but documents intent).
    :param kind_source: str; `lrb`, `fzb`, or `llb`.
    :param periods: dict[str, dict[str, Any]]; period key to report object.
    :param page_size: int; periods per page.
    return handler: Callable[[str], Any]; route handler.
    """
    keys = sorted(periods, reverse=True)

    def handler(url: str) -> Any:
        """Serve one page by its page parameter.

        :param url: str; request URL.
        return reply: Any; (status, body, headers).
        """
        from urllib.parse import parse_qs, urlsplit

        page = int(parse_qs(urlsplit(url).query)["page"][0])
        chunk = keys[(page - 1) * page_size: page * page_size]
        if not chunk:
            payload = {"result": {"status": {"code": 0}, "data": None}}
        else:
            payload = {"result": {"status": {"code": 0}, "data": {
                "report_count": str(len(keys)), "report_date": [{"date_value": k} for k in chunk],
                "report_list": {k: periods[k] for k in chunk}}}}
        return 200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), {"Content-Type": "application/json"}

    return handler


def sina_report(scope: str = "合并期末", value: Any = "1234.5") -> dict[str, Any]:
    """Build one Sina report object with two items and return it.

    :param scope: str; rType label.
    :param value: Any; value of the first item.
    return report: dict[str, Any]; report object.
    """
    return {"rType": scope, "rCurrency": "CNY", "publish_date": "20260328", "update_time": 1774656000,
            "is_audit": "是", "data": [{"item_field": "F1", "item_title": "货币资金", "item_value": value},
                                     {"item_title": "存货"}]}


BSE_PAGE = """<html><head><meta charset="utf-8"></head><body><table>
<tr><th>序号</th><th>证券简称</th><th>上市日期</th><th>旧代码</th><th>新代码</th></tr>
<tr><td>1</td><td>五新隧装</td><td>2021-11-15</td><td>832000</td><td>920000</td></tr>
<tr><td>2</td><td>某公司</td><td>2022-01-01</td><td>832982</td><td>920982</td></tr>
<tr><td>注：上表中上市日期字段为其在原精选层的挂牌日期。</td><td></td><td></td><td></td><td></td></tr>
</table></body></html>"""


class SharedRecordTests(unittest.TestCase):
    """Check canonical JSON, hashing, decimals, and symbol parsing."""

    def test_canonical_json_is_sorted_compact_and_unicode(self) -> None:
        """Canonical JSON sorts keys, keeps Chinese text, and hashes deterministically."""
        text = encode_canonical_json({"b": 1, "a": "货币"})
        self.assertEqual(text, '{"a":"货币","b":1}')
        self.assertEqual(compute_sha256(text.encode()), compute_sha256('{"a":"货币","b":1}'.encode()))
        self.assertEqual(convert_to_json(datetime(2026, 1, 1, tzinfo=UTC)), "2026-01-01T00:00:00Z")
        with self.assertRaises(ValueError):
            encode_canonical_json({"x": float("nan")})

    def test_decimal_classification(self) -> None:
        """Grouped and accounting numbers parse; markers stay text; unclear digits are ambiguous."""
        self.assertEqual(parse_decimal_text("12,345.00")[2], "12345.00")
        self.assertEqual(parse_decimal_text("(1,234.50)")[2], "-1234.50")
        self.assertEqual(parse_decimal_text("--")[:3], ("string", "--", None))
        self.assertEqual(parse_decimal_text("1,23"), ("string", "1,23", None, True))
        self.assertEqual(parse_decimal_text(None)[0], "null")
        self.assertEqual(parse_decimal_text(...)[0], "missing")
        self.assertEqual(parse_decimal_text("NaN")[2], None)
        self.assertEqual(parse_decimal_text(5)[2], "5")

    def test_symbol_forms_and_rejections(self) -> None:
        """All accepted forms give one canonical symbol; mismatched markets are rejected."""
        for text in ("sh600519", "600519.SH", "SH:600519", "600519", " SH600519 "):
            self.assertEqual(parse_symbol(text), SH)
        self.assertEqual(parse_symbol("920000").symbol_id, "BJ:920000")
        for bad in ("sz600519", "900000", "60051", "AAPL", ""):
            with self.assertRaises(InputError) as ctx:
                parse_symbol(bad)
            self.assertEqual(ctx.exception.issue.code, "invalid_symbol")


class HttpClientTests(unittest.TestCase):
    """Check redirects, retries, rate limits, byte limits, and budgets."""

    def test_follows_validated_redirects_and_limits_them(self) -> None:
        """Up to three redirects are followed; a fourth raises redirect_limit."""
        session = FakeSession()
        base = "https://www.bse.cn/service/code_mapping.html"
        session.add(lambda u: u == base, lambda _u: (302, b"", {"Location": base + "?a=1"}))
        session.add(lambda u: u.endswith("?a=1"), lambda _u: (200, b"ok", {"Content-Type": "text/html"}))
        response = make_client(session).get(base, max_bytes=100)
        self.assertEqual(response.body, b"ok")
        self.assertEqual([m.outcome for m in response.requests], ["redirect", "ok"])
        loop = FakeSession([(lambda u: True, lambda u: (302, b"", {"Location": u}))])
        with self.assertRaises(SourceError) as ctx:
            make_client(loop).get(base, max_bytes=100)
        self.assertEqual(ctx.exception.issue.code, "redirect_limit")
        self.assertEqual(len(ctx.exception.requests), 4)

    def test_rejects_disallowed_redirect_and_hosts(self) -> None:
        """A redirect to an unknown host and a plain HTTP URL are refused."""
        session = FakeSession([(lambda u: True, lambda _u: (302, b"", {"Location": "https://evil.example/x"}))])
        with self.assertRaises(SourceError) as ctx:
            make_client(session).get("https://quotes.sina.cn/x", max_bytes=10)
        self.assertEqual(ctx.exception.issue.code, "source_access_error")
        with self.assertRaises(SourceError):
            make_client(FakeSession()).get("http://quotes.sina.cn/x", max_bytes=10)

    def test_retries_once_on_503_and_transport_errors(self) -> None:
        """One retry follows a 503 or a transport error; a second failure is reported."""
        replies = iter([(503, b"", {}), (200, b"done", {})])
        session = FakeSession([(lambda u: True, lambda _u: next(replies))])
        self.assertEqual(make_client(session).get("https://quotes.sina.cn/x", max_bytes=10).body, b"done")

        def broken(_url: str) -> Any:
            """Raise a transport failure.

            :param _url: str; ignored.
            return reply: Any; never returns.
            """
            raise OSError("connection reset")

        session = FakeSession([(lambda u: True, broken)])
        with self.assertRaises(SourceError) as ctx:
            make_client(session).get("https://quotes.sina.cn/x", max_bytes=10)
        self.assertEqual(ctx.exception.issue.code, "transport_error")
        self.assertEqual(len(session.calls), 2)

    def test_rate_limit_cools_host(self) -> None:
        """HTTP 429 raises rate_limited and later requests to the host fail without a call."""
        session = FakeSession([(lambda u: True, lambda _u: (429, b"", {"Retry-After": "60"}))])
        client = make_client(session)
        with self.assertRaises(SourceError) as ctx:
            client.get("https://quotes.sina.cn/x", max_bytes=10)
        self.assertEqual(ctx.exception.issue.code, "rate_limited")
        with self.assertRaises(SourceError):
            client.get("https://quotes.sina.cn/y", max_bytes=10)
        self.assertEqual(len(session.calls), 1)

    def test_byte_limit_aborts_transfer_and_budget_is_enforced(self) -> None:
        """A body over the limit aborts through the callback; the attempt budget stops requests."""
        session = FakeSession([(lambda u: True, lambda _u: (200, b"x" * 5000, {}))])
        with self.assertRaises(SourceError) as ctx:
            make_client(session).get("https://quotes.sina.cn/x", max_bytes=2048)
        self.assertEqual(ctx.exception.issue.code, "response_too_large")
        client = make_client(FakeSession([(lambda u: True, lambda _u: (200, b"x", {}))]), max_requests=1)
        client.get("https://quotes.sina.cn/x", max_bytes=10)
        with self.assertRaises(SourceError) as ctx:
            client.get("https://quotes.sina.cn/x", max_bytes=10)
        self.assertEqual(ctx.exception.issue.code, "budget_exhausted")


class PriceAdapterTests(unittest.TestCase):
    """Check Eastmoney windows and Tencent cursors."""

    ROWS = [("2024-12-30", "10", "11", "12", "9"), ("2025-01-02", "11", "12", "13", "10"),
            ("2025-06-30", "12", "12.5", "13", "11.5")]

    def test_eastmoney_calendar_windows_and_identity(self) -> None:
        """Each calendar year is one request; rows keep native units and source cells."""
        session = FakeSession([(lambda u: "eastmoney" in u, eastmoney_payload(SH, self.ROWS))])
        result = fetch_prices(make_client(session), SH, DateRange(date(2024, 12, 1), date(2025, 12, 31)), "raw",
                              "eastmoney", today=date(2026, 1, 5))
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(len(session.calls), 2)
        self.assertEqual([r.trade_date.isoformat() for r in result.records], [r[0] for r in self.ROWS])
        self.assertEqual(result.records[0].volume_unit, "source_native")
        self.assertEqual(result.coverage.row_count, 3)
        wrong = FakeSession([(lambda u: True, eastmoney_payload(SZ, self.ROWS))])
        bad = fetch_prices(make_client(wrong), SH, DateRange(date(2025, 1, 1), date(2025, 2, 1)), "raw",
                           "eastmoney", today=date(2026, 1, 5))
        self.assertEqual((bad.state, bad.issues[0].code), ("failed", "identity_mismatch"))

    def test_eastmoney_keeps_accepted_windows_after_failure(self) -> None:
        """A failing later window leaves earlier rows and marks the rest unfetched."""
        good = eastmoney_payload(SH, self.ROWS)
        session = FakeSession([(lambda u: "beg=2025" in u, json_reply({"rc": 102, "data": None})),
                               (lambda u: True, good)])
        result = fetch_prices(make_client(session), SH, DateRange(date(2024, 12, 1), date(2025, 12, 31)), "raw",
                              "eastmoney", today=date(2026, 1, 5))
        self.assertEqual(result.state, "partial")
        self.assertEqual(len(result.records), 1)
        self.assertEqual(result.issues[0].code, "provider_error")
        self.assertEqual(result.coverage.unfetched[0].start, date(2025, 1, 1))

    def test_eastmoney_null_data_is_empty_interval(self) -> None:
        """A null data object is an empty visit, not an error and not proof of no trading."""
        session = FakeSession([(lambda u: True, json_reply({"rc": 0, "data": None}))])
        result = fetch_prices(make_client(session), SH, DateRange(date(1990, 1, 1), date(1990, 12, 31)), "raw",
                              "eastmoney", today=date(2026, 1, 5))
        self.assertEqual((result.state, result.coverage.fetched[0].outcome), ("succeeded", "empty"))

    def test_forward_anchor_change_blocks_promotion(self) -> None:
        """A changed earliest forward-adjusted bar makes the traversal partial."""
        calls = {"n": 0}
        first = eastmoney_payload(SH, self.ROWS)
        changed = eastmoney_payload(SH, [("2024-12-30", "9", "10", "11", "8")] + self.ROWS[1:])

        def handler(url: str) -> Any:
            """Serve the original rows, then changed rows for the anchor recheck.

            :param url: str; request URL.
            return reply: Any; (status, body, headers).
            """
            calls["n"] += 1
            return (changed if calls["n"] > 2 else first)(url)

        session = FakeSession([(lambda u: True, handler)])
        result = fetch_prices(make_client(session), SH, DateRange(date(2024, 12, 1), date(2025, 12, 31)), "forward",
                              "eastmoney", today=date(2026, 1, 5))
        self.assertEqual(result.state, "partial")
        self.assertIn("adjustment_mismatch", [i.code for i in result.issues])

    def test_tencent_pages_backwards_and_never_substitutes_adjustment(self) -> None:
        """The cursor moves before the earliest row; a raw series is not accepted for forward."""
        pages = {
            "2025-12-31": [["2025-06-30", "12", "12.5", "13", "11.5", "100"], ["2025-01-02", "11", "12", "13", "10",
                                                                               "90", {}, "0", "2000"]],
            "2025-01-01": [["2024-12-30", "10", "11", "12", "9", "80"]],
            "2024-12-29": [],
        }

        def handler(url: str) -> Any:
            """Serve the page for the cursor in the param value.

            :param url: str; request URL.
            return reply: Any; (status, body, headers).
            """
            from urllib.parse import parse_qs, urlsplit

            cursor = parse_qs(urlsplit(url).query)["param"][0].split(",")[3]
            key = "qfqday" if url.endswith("qfq") else "day"
            payload = {"code": 0, "data": {"sh600519": {key: pages[cursor]}}}
            return 200, json.dumps(payload).encode(), {"Content-Type": "text/html"}

        session = FakeSession([(lambda u: True, handler)])
        result = fetch_prices(make_client(session), SH, DateRange(date(2024, 1, 1), date(2025, 12, 31)), "raw",
                              "tencent", today=date(2026, 1, 5))
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(result.records[1].amount_native, "2000")
        mismatch = FakeSession([(lambda u: True, json_reply({"code": 0, "data": {"sh600519": {"day": [
            ["2025-01-02", "1", "1", "1", "1", "1"]]}}}))])
        bad = fetch_prices(make_client(mismatch), SH, DateRange(date(2025, 1, 1), date(2025, 1, 31)), "forward",
                           "tencent", today=date(2026, 1, 5))
        self.assertEqual(bad.issues[0].code, "adjustment_mismatch")


class SinaStatementTests(unittest.TestCase):
    """Check Sina pagination, scopes, values, and dates."""

    def test_walks_pages_until_terminal(self) -> None:
        """Every period is stored across pages, with scope and missing values preserved."""
        periods = {f"20{y:02d}1231": sina_report() for y in range(10, 13)}
        periods["20130630"] = sina_report("母公司期末", "--")
        session = FakeSession([(lambda u: "fzb" in u, sina_pages(SH, "fzb", periods, page_size=2))])
        result = fetch_statements(make_client(session), SH, "balance")
        self.assertEqual(result.state, "succeeded")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(len(result.records), 4)
        record = result.records[0]
        self.assertEqual((record.report_date, record.scope, record.unit), (date(2013, 6, 30), "parent", None))
        self.assertEqual(record.items[0].value_kind, "string")
        self.assertEqual(record.items[1].value_kind, "missing")
        self.assertEqual(result.records[1].items[0].value_decimal, "1234.5")
        self.assertEqual(result.records[1].publication_date, date(2026, 3, 28))
        self.assertIn("unit_unknown", [i.code for i in result.issues])

    def test_early_terminal_and_invalid_dates(self) -> None:
        """A terminal page before report_count is an error; invalid optional dates become warnings."""
        report = sina_report()
        report["publish_date"] = "2026-03-28"
        body = {"result": {"status": {"code": 0}, "data": {"report_count": 3, "report_date": [
            {"date_value": "20251231"}], "report_list": {"20251231": report}}}}
        replies = iter([body, {"result": {"status": {"code": 0}, "data": None}}])
        session = FakeSession([(lambda u: True, lambda _u: (200, json.dumps(next(replies)).encode(), {}))])
        result = fetch_statements(make_client(session), SH, "income")
        self.assertEqual(result.state, "partial")
        codes = [i.code for i in result.issues]
        self.assertIn("schema_changed", codes)
        self.assertIn("invalid_optional_date", codes)
        self.assertIsNone(result.records[0].publication_date)
        self.assertEqual(result.records[0].publication_date_raw, "2026-03-28")


class MappingTests(unittest.TestCase):
    """Check the official BSE mapping parser and alias resolution."""

    def test_parses_real_columns_without_inferring_dates(self) -> None:
        """Rows bind exact old and new codes; the listing date stays in cells only."""
        session = FakeSession([(lambda u: "bse.cn" in u, html_reply(BSE_PAGE))])
        result = lookup_code_links(make_client(session), now=datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(result.state, "succeeded")
        self.assertEqual([(link.from_symbol.code, link.to_symbol.code) for link in result.links],
                         [("832000", "920000"), ("832982", "920982")])
        self.assertIsNone(result.links[0].effective_date)
        self.assertIn("2021-11-15", result.links[0].cells)
        self.assertEqual(result.metadata["table_headers"], ["序号", "证券简称", "上市日期", "旧代码", "新代码"])

    def test_rejects_malformed_and_conflicting_tables(self) -> None:
        """Non-six-digit codes and conflicting rows produce a failed lookup with no links."""
        footnote_with_code = BSE_PAGE.replace("<td></td><td></td><td></td><td></td></tr>",
                                              "<td></td><td></td><td>832001</td><td></td></tr>")
        for bad in (BSE_PAGE.replace("920982", "92098"), BSE_PAGE.replace("832982", "832000"), footnote_with_code):
            session = FakeSession([(lambda u: True, html_reply(bad))])
            result = lookup_code_links(make_client(session))
            self.assertEqual((result.state, result.links), ("failed", ()))

    def test_resolves_chains_and_rejects_forks(self) -> None:
        """A verified pair resolves both ways; a fork raises mapping_conflict."""
        session = FakeSession([(lambda u: True, html_reply(BSE_PAGE))])
        links = lookup_code_links(make_client(session)).links
        new = parse_symbol("920000")
        self.assertEqual([s.symbol_id for s in resolve_symbols(new, links)], ["BJ:920000", "BJ:832000"])
        self.assertEqual(resolve_symbols(parse_symbol("430001"), links), (parse_symbol("430001"),))
        fork = providers.CodeLink(links[0].from_symbol, parse_symbol("920001"), "u", (), (), None, "h",
                                  datetime.now(UTC), datetime.now(UTC))
        with self.assertRaises(InputError) as ctx:
            resolve_symbols(new, [*links, fork])
        self.assertEqual(ctx.exception.issue.code, "mapping_conflict")


if __name__ == "__main__":
    unittest.main()

