"""Tests for US share collection: symbols, market days, US prices, and the storage of reported values.

The tests use the fake HTTP session from test_providers. No test contacts a live service.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_info.providers import (  # noqa: E402
    CheckKey, Coverage, DateRange, FactRow, FetchResult, HttpClient, InputError, QueryRequest, SecAccess, Symbol,
    compute_market_day, compute_new_york_day, fetch_prices,
)
from stock_info.securities import parse_symbol, resolve_symbols  # noqa: E402
from stock_info.storage import SCHEMA_SQL, Store  # noqa: E402
from test_providers import FakeSession, make_client  # noqa: E402

UTC = timezone.utc
AAPL = Symbol("US:AAPL", "US", "AAPL")
BRK = Symbol("US:BRK-B", "US", "BRK-B")
YEAR = DateRange(date(2024, 1, 1), date(2024, 12, 31))
TODAY = date(2026, 11, 13)
ROWS = [("2024-01-02", "187.150", "185.640", "188.440", "183.890"),
        ("2024-01-03", "184.220", "184.250", "185.880", "183.430")]


def us_eastmoney(code: str, venue: str, rows: list[tuple[str, str, str, str, str]]) -> Callable[[str], Any]:
    """Return a handler that imitates Eastmoney for one US code that exists in exactly one venue.

    A request for another venue or code gets the "not found" answer `rc=100` with
    `data: null`. A request for the right venue gets the rows inside its window.

    :param code: str; Eastmoney code such as `AAPL` or `BRK_B`.
    :param venue: str; venue number in which the code exists, such as `105`.
    :param rows: list[tuple[str, str, str, str, str]]; (date, open, close, high, low) rows.
    return handler: Callable[[str], Any]; route handler.
    """
    def handler(url: str) -> Any:
        """Serve one K-line request.

        :param url: str; request URL.
        return reply: Any; (status, body, headers).
        """
        query = parse_qs(urlsplit(url).query)
        if query["secid"][0] != f"{venue}.{code}":
            return 200, json.dumps({"rc": 100, "data": None}).encode(), {}
        beg, end = query["beg"][0], query["end"][0]
        lines = [f"{d},{o},{c},{h},{lo},1000,0.000" for d, o, c, h, lo in rows if beg <= d.replace("-", "") <= end]
        data = {"code": code, "market": int(venue), "name": "test", "decimal": 3, "klines": lines}
        return 200, json.dumps({"rc": 0, "data": data}).encode(), {}

    return handler


def secids(session: FakeSession) -> list[str]:
    """Return the `secid` value of every recorded Eastmoney request in order.

    :param session: FakeSession; fake session with recorded calls.
    return secids: list[str]; requested security identifiers.
    """
    return [parse_qs(urlsplit(url).query)["secid"][0] for url in session.calls if "eastmoney" in url]


class SymbolTests(unittest.TestCase):
    """Check US symbol forms and that A-share forms are unchanged."""

    def test_us_forms(self) -> None:
        """Bare tickers, the US prefix, the US suffix, and class shares give the canonical symbol."""
        for text in ("AAPL", "aapl", " AAPL ", "US:AAPL", "us:aapl", "AAPL.US", "aapl.us"):
            self.assertEqual(parse_symbol(text), AAPL, text)
        for text in ("BRK.B", "BRK-B", "brk.b", "US:BRK.B", "US:BRK-B", "BRK.B.US", "BRK-B.US"):
            self.assertEqual(parse_symbol(text), BRK, text)
        self.assertEqual(parse_symbol("F"), Symbol("US:F", "US", "F"))

    def test_rejections_and_a_shares(self) -> None:
        """Malformed tickers are rejected, and six-digit codes are still A-shares."""
        for text in ("", "AAPL1", "TOOLONG", "A.BCD", "12345", "AAPL.SH", "US:", "US:600519", "BRK..B", "-B"):
            with self.assertRaises(InputError, msg=text) as ctx:
                parse_symbol(text)
            self.assertEqual(ctx.exception.issue.code, "invalid_symbol")
        self.assertEqual(parse_symbol("600519").symbol_id, "SH:600519")
        self.assertEqual(resolve_symbols(AAPL, ()), (AAPL,))


class DayTests(unittest.TestCase):
    """Check the New York calendar day around midnight and around both clock changes."""

    def test_new_york_day(self) -> None:
        """New York is five hours behind UTC in winter and four in summer; 2026 changes on 8 March and 1 November."""
        cases = [
            (datetime(2026, 1, 15, 4, 59, tzinfo=UTC), date(2026, 1, 14)),
            (datetime(2026, 1, 15, 5, 0, tzinfo=UTC), date(2026, 1, 15)),
            (datetime(2026, 7, 1, 3, 59, tzinfo=UTC), date(2026, 6, 30)),
            (datetime(2026, 7, 1, 4, 0, tzinfo=UTC), date(2026, 7, 1)),
            (datetime(2026, 3, 8, 4, 59, tzinfo=UTC), date(2026, 3, 7)),
            (datetime(2026, 3, 8, 5, 0, tzinfo=UTC), date(2026, 3, 8)),
            (datetime(2026, 3, 9, 3, 59, tzinfo=UTC), date(2026, 3, 8)),
            (datetime(2026, 3, 9, 4, 0, tzinfo=UTC), date(2026, 3, 9)),
            (datetime(2026, 11, 1, 3, 59, tzinfo=UTC), date(2026, 10, 31)),
            (datetime(2026, 11, 1, 4, 0, tzinfo=UTC), date(2026, 11, 1)),
            (datetime(2026, 11, 2, 4, 59, tzinfo=UTC), date(2026, 11, 1)),
            (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), date(2026, 11, 2)),
        ]
        for moment, expected in cases:
            self.assertEqual(compute_new_york_day(moment), expected, moment)

    def test_market_day_follows_the_symbol_or_the_sec(self) -> None:
        """A US symbol and the SEC use the New York day; everything else uses the China day."""
        moment = datetime(2026, 7, 1, 2, 0, tzinfo=UTC)
        self.assertEqual(compute_market_day("US:AAPL", moment), date(2026, 6, 30))
        self.assertEqual(compute_market_day(None, moment, "sec"), date(2026, 6, 30))
        self.assertEqual(compute_market_day("SH:600519", moment), date(2026, 7, 1))
        self.assertEqual(compute_market_day(None, moment, "bse"), date(2026, 7, 1))


class UsPriceTests(unittest.TestCase):
    """Check the venue search for Eastmoney and the limits of the Tencent fallback."""

    def test_eastmoney_tries_venues_in_order(self) -> None:
        """A venue that answers "not found" is skipped; the next venue serves every window."""
        session = FakeSession([(lambda u: "eastmoney" in u, us_eastmoney("AAPL", "105", ROWS))])
        span = DateRange(date(2023, 6, 1), date(2024, 12, 31))
        result = fetch_prices(make_client(session), AAPL, span, "raw", "eastmoney", today=TODAY,
                              venues=("106", "105", "107"))
        self.assertEqual((result.state, result.issues), ("succeeded", ()))
        self.assertEqual(secids(session), ["106.AAPL", "105.AAPL", "105.AAPL"])
        self.assertEqual([r.trade_date.isoformat() for r in result.records], ["2024-01-02", "2024-01-03"])
        self.assertEqual((result.records[0].close, result.records[0].returned_symbol), ("185.640", "105.AAPL"))
        self.assertEqual(result.metadata["venue"], "105")
        self.assertEqual([(i.outcome, i.row_count) for i in result.coverage.fetched], [("empty", 0), ("nonempty", 2)])

    def test_unknown_ticker_fails_after_every_venue(self) -> None:
        """When every venue answers "not found", the task fails with symbol_not_found."""
        session = FakeSession([(lambda u: True, us_eastmoney("MSFT", "105", ROWS))])
        result = fetch_prices(make_client(session), AAPL, YEAR, "raw", "eastmoney", today=TODAY)
        self.assertEqual((result.state, [i.code for i in result.issues]), ("failed", ["symbol_not_found"]))
        self.assertEqual(secids(session), ["105.AAPL", "106.AAPL", "107.AAPL"])
        self.assertEqual(result.records, ())

    def test_class_share_uses_an_underscore_and_checks_identity(self) -> None:
        """A class share is requested as BRK_B, and a different echoed code is rejected."""
        session = FakeSession([(lambda u: True, us_eastmoney("BRK_B", "106", ROWS))])
        result = fetch_prices(make_client(session), BRK, YEAR, "forward", "eastmoney", today=TODAY, venues=("106",))
        self.assertEqual((result.state, result.records[0].returned_symbol), ("succeeded", "106.BRK_B"))
        wrong = FakeSession([(lambda u: True, lambda _u: (200, json.dumps(
            {"rc": 0, "data": {"code": "BRK_A", "market": 106, "klines": []}}).encode(), {}))])
        result = fetch_prices(make_client(wrong), BRK, YEAR, "raw", "eastmoney", today=TODAY, venues=("106",))
        self.assertEqual((result.state, result.issues[0].code), ("failed", "identity_mismatch"))

    def test_tencent_serves_unadjusted_prices_for_one_known_venue(self) -> None:
        """Tencent is asked with the exchange suffix, and its seven-cell rows are accepted."""
        def tencent(code: str) -> Callable[[str], Any]:
            """Return a handler that serves two rows under the given Tencent code.

            :param code: str; Tencent code such as `usAAPL.OQ`.
            return handler: Callable[[str], Any]; route handler.
            """
            rows = [["2024-01-02", "187.15", "185.64", "188.44", "183.89", "82488700.00", {}],
                    ["2024-01-03", "184.22", "184.25", "185.88", "183.43", "58414500.00", {}]]
            return lambda _u: (200, json.dumps({"code": 0, "msg": "", "data": {code: {"day": rows, "qt": {}}}}).encode(),
                               {"Content-Type": "text/html; charset=UTF-8"})

        session = FakeSession([(lambda u: True, tencent("usAAPL.OQ"))])
        span = DateRange(date(2024, 1, 2), date(2024, 1, 5))
        result = fetch_prices(make_client(session), AAPL, span, "raw", "tencent", today=TODAY, venues=("105",))
        self.assertEqual((result.state, len(result.records)), ("succeeded", 2))
        self.assertEqual(result.records[0].returned_symbol, "usAAPL.OQ")
        self.assertEqual(parse_qs(urlsplit(session.calls[0]).query)["param"][0], "usAAPL.OQ,day,,2024-01-05,640,")
        session = FakeSession([(lambda u: True, tencent("usBRK.B.N"))])
        result = fetch_prices(make_client(session), BRK, span, "raw", "tencent", today=TODAY, venues=("106",))
        self.assertEqual((result.state, result.metadata["venue"]), ("succeeded", "106"))
        self.assertIn("usBRK.B.N", session.calls[0].replace("%2C", ","))

    def test_tencent_refuses_forward_prices_and_an_unknown_venue(self) -> None:
        """Tencent has no forward-adjusted US prices and needs exactly one venue; neither case sends a request."""
        for adjustment, venues in (("forward", ("105",)), ("raw", ()), ("raw", ("105", "106"))):
            session = FakeSession()
            result = fetch_prices(make_client(session), AAPL, YEAR, adjustment, "tencent", today=TODAY,
                                  venues=venues)
            self.assertEqual((result.state, [i.code for i in result.issues]), ("failed", ["fallback_unavailable"]))
            self.assertEqual(session.calls, [])


class SecGapTests(unittest.TestCase):
    """Check that requests to SEC hosts wait a quarter of a second and other requests one second."""

    def test_request_gaps(self) -> None:
        """The wait before a request depends on the host of that request."""
        moment = [100.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            """Record the wait and advance the fake clock.

            :param seconds: float; requested wait.
            return None: None; no value is returned.
            """
            sleeps.append(round(seconds, 3))
            moment[0] += seconds

        session = FakeSession([(lambda u: True, lambda _u: (200, b"{}", {}))])
        client = HttpClient(session_factory=lambda: session, sec_session_factory=lambda: session,
                            sec_access=lambda: SecAccess(user_agent="Test Operator operator@collector.test"),
                            monotonic=lambda: moment[0], sleep=sleep)
        client.get("https://quotes.sina.cn/a", max_bytes=10)
        client.get("https://data.sec.gov/b", max_bytes=10)
        client.get("https://www.sec.gov/c", max_bytes=10)
        client.get("https://quotes.sina.cn/d", max_bytes=10)
        self.assertEqual(sleeps, [0.25, 0.25, 1.0])


def fact(concept: str, end: str, value: str, *, accession: str = "0000320193-26-000026", start: str | None = None,
         filed: str = "2026-11-03", form: str = "10-K", unit: str = "USD", taxonomy: str = "us-gaap") -> FactRow:
    """Build one reported value for tests and return it.

    :param concept: str; concept name.
    :param end: str; period end as ISO text.
    :param value: str; decimal text.
    :param accession: str; filing identifier.
    :param start: str | None; period start as ISO text, or None for a value at one moment.
    :param filed: str; filing date as ISO text.
    :param form: str; form type.
    :param unit: str; unit.
    :param taxonomy: str; group of the concept.
    return row: FactRow; reported value whose label is the concept name followed by ` label`.
    """
    return FactRow(taxonomy=taxonomy, concept=concept, label=f"{concept} label", unit=unit,
                   period_start=date.fromisoformat(start) if start else None, period_end=date.fromisoformat(end),
                   value=value, accession=accession, fiscal_year=2026, fiscal_period="FY", form=form,
                   filed=date.fromisoformat(filed), frame=None)


def facts_result(rows: list[FactRow]) -> FetchResult:
    """Wrap reported values in a succeeded fetch result and return it.

    :param rows: list[FactRow]; reported values.
    return result: FetchResult; succeeded result.
    """
    coverage = Coverage(requested=None, row_count=len(rows), traversal="succeeded")
    return FetchResult(state="succeeded", coverage=coverage, requests=(), issues=(), metadata={"cik": 320193},
                       records=tuple(rows))


FACTS_KEY = CheckKey("US:AAPL", "sec", "facts")
EVERYTHING = DateRange(date(1970, 1, 1), date(2026, 12, 31))


class UsStorageTests(unittest.TestCase):
    """Check the storage of reported values, their queries, the schema upgrade, and the New York gate."""

    def setUp(self) -> None:
        """Create a temporary database.

        return None: None; no value is returned.
        """
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())

    def save(self, rows: list[FactRow], day: int) -> Any:
        """Store one values check on the given day of November 2026 and return its summary.

        :param rows: list[FactRow]; values of the response.
        :param day: int; day of November 2026 (15:00 UTC).
        return summary: Any; CheckSummary of the finished check.
        """
        now = datetime(2026, 11, day, 15, 0, tzinfo=UTC)
        check_id = self.store.start_check(FACTS_KEY, None, f"run-{day}", now)
        assert check_id is not None
        return self.store.finish_check(check_id, facts_result(rows), now)

    def values(self, **options: Any) -> list[dict[str, Any]]:
        """Query reported values and return the records of the single group.

        :param options: Any; extra QueryRequest fields such as `concepts` or `versions`.
        return records: list[dict[str, Any]]; output records.
        """
        request = QueryRequest(symbol=AAPL, span=options.pop("span", EVERYTHING), dataset="facts", **options)
        page = self.store.query(request, datetime(2026, 11, 20, tzinfo=UTC), symbols=(AAPL,))
        self.assertEqual(len(page["groups"]), 1)
        return page["groups"][0]["records"]

    def test_values_are_stored_once_and_changes_are_versioned(self) -> None:
        """An unchanged value is not stored again; a changed value is a new version; a missing value is kept."""
        assets = fact("Assets", "2026-09-26", "404548000000")
        income = fact("NetIncomeLoss", "2026-09-26", "132175000000", start="2025-09-28")
        first = self.save([assets, income], 10)
        self.assertEqual((first.metadata["stored_rows"], first.metadata["unchanged_rows"], first.metadata["absent_keys"],
                          first.metadata["cik"]), (2, 0, 0, 320193))
        changed = fact("NetIncomeLoss", "2026-09-26", "132000000000", start="2025-09-28")
        second = self.save([assets, changed, fact("Liabilities", "2026-09-26", "300000000000")], 11)
        self.assertEqual((second.metadata["stored_rows"], second.metadata["unchanged_rows"], second.issues), (2, 1, ()))
        third = self.save([assets], 12)
        self.assertEqual((third.metadata["stored_rows"], third.metadata["unchanged_rows"], third.metadata["absent_keys"]),
                         (0, 1, 2))
        self.assertEqual([(i.code, i.severity) for i in third.issues], [("values_missing_in_recheck", "warning")])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM facts").fetchone()[0], 4)
        latest = self.values(concepts=("NetIncomeLoss",))
        self.assertEqual([(r["type"], r["data"]["value"], r["check_id"]) for r in latest],
                         [("fact", "132000000000", second.check_id)])
        every = self.values(concepts=("NetIncomeLoss",), versions="all")
        self.assertEqual([r["data"]["value"] for r in every], ["132000000000", "132175000000"])
        self.assertEqual(latest[0]["data"]["period_start"], "2025-09-28")
        self.assertEqual((self.store.new_revisions, self.store.reused_revisions), (4, 2))

    def test_concept_list_and_value_filters(self) -> None:
        """Without concepts the query lists concepts; with concepts it filters by period end and filing date."""
        rows = [
            fact("NetIncomeLoss", "2025-09-27", "112010000000", start="2024-09-29", accession="0000320193-25-000079",
                 filed="2025-10-31"),
            fact("NetIncomeLoss", "2025-09-27", "112010000000", start="2024-09-29"),
            fact("NetIncomeLoss", "2026-09-26", "132175000000", start="2025-09-28"),
            fact("Assets", "2026-09-26", "404548000000"),
            fact("EarningsPerShareBasic", "2026-09-26", "9.01", start="2025-09-28", unit="USD/shares"),
        ]
        check = self.save(rows, 10)
        concepts = self.values()
        self.assertEqual([(r["type"], r["record_key"], r["data"]["value_count"]) for r in concepts],
                         [("concept", "us-gaap:Assets:USD", 1), ("concept", "us-gaap:EarningsPerShareBasic:USD/shares", 1),
                          ("concept", "us-gaap:NetIncomeLoss:USD", 3)])
        self.assertEqual(concepts[2]["data"], {"taxonomy": "us-gaap", "concept": "NetIncomeLoss",
                                               "label": "NetIncomeLoss label", "unit": "USD", "value_count": 3,
                                               "first_period_end": "2025-09-27", "last_period_end": "2026-09-26",
                                               "last_filed": "2026-11-03"})
        self.assertEqual(concepts[0]["check_id"], check.check_id)
        income = self.values(concepts=("NetIncomeLoss",))
        self.assertEqual([(r["data"]["period_end"], r["data"]["filed"]) for r in income],
                         [("2025-09-27", "2025-10-31"), ("2025-09-27", "2026-11-03"), ("2026-09-26", "2026-11-03")])
        early = self.values(concepts=("NetIncomeLoss", "Assets"), filed_until=date(2026, 1, 1))
        self.assertEqual([(r["data"]["concept"], r["data"]["accession"]) for r in early],
                         [("NetIncomeLoss", "0000320193-25-000079")])
        year = self.values(concepts=("NetIncomeLoss",), span=DateRange(date(2026, 1, 1), date(2026, 12, 31)))
        self.assertEqual([r["data"]["value"] for r in year], ["132175000000"])
        self.assertEqual(self.values(concepts=("NetIncomeLoss",), limit=1)[0]["data"]["filed"], "2025-10-31")
        self.assertEqual(len(self.values(span=DateRange(date(2025, 1, 1), date(2025, 12, 31)))), 1)

    def test_filings_are_listed_from_the_values(self) -> None:
        """Each filing, period end, and group gives one row with the number of values."""
        self.save([fact("Assets", "2026-09-26", "1"), fact("Liabilities", "2026-09-26", "2"),
                   fact("Assets", "2025-09-27", "3"),
                   fact("EntityCommonStockSharesOutstanding", "2026-10-16", "4", taxonomy="dei", unit="shares")], 10)
        self.assertEqual(sorted(self.store.list_fact_filings(AAPL)),
                         [("0000320193-26-000026", "10-K", "2026-11-03", "2025-09-27", "us-gaap", 1),
                          ("0000320193-26-000026", "10-K", "2026-11-03", "2026-09-26", "us-gaap", 2),
                          ("0000320193-26-000026", "10-K", "2026-11-03", "2026-10-16", "dei", 1)])
        self.assertEqual(self.store.list_fact_filings(BRK), [])

    def test_version_1_database_is_read_without_values_and_upgraded_on_write(self) -> None:
        """A read-only store accepts version 1 and returns no values; a writable store adds the facts table."""
        old = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old)) as conn:
            for statement in SCHEMA_SQL.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("PRAGMA user_version=1")
            conn.commit()
        reader = Store(old, readonly=True)
        try:
            self.assertFalse(reader.has_facts)
            request = QueryRequest(symbol=AAPL, span=EVERYTHING, dataset="facts")
            page = reader.query(request, datetime(2026, 11, 20, tzinfo=UTC), symbols=(AAPL,))
            self.assertEqual([g["records"] for g in page["groups"]], [[]])
            self.assertEqual(reader.list_fact_filings(AAPL), [])
        finally:
            reader.close()
        Store(old).close()
        with closing(sqlite3.connect(old)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM facts").fetchone()[0], 0)

    def test_us_checks_are_gated_by_the_new_york_day(self) -> None:
        """A US check can start once per New York day, which changes at 04:00 UTC in summer."""
        key = CheckKey("US:AAPL", "eastmoney", "prices", "raw")
        first = self.store.start_check(key, YEAR, "r1", datetime(2026, 7, 1, 2, 0, tzinfo=UTC))
        again = self.store.start_check(key, YEAR, "r2", datetime(2026, 7, 1, 3, 30, tzinfo=UTC))
        later = self.store.start_check(key, YEAR, "r3", datetime(2026, 7, 1, 4, 30, tzinfo=UTC))
        self.assertIsNotNone(first)
        self.assertIsNone(again)
        self.assertIsNotNone(later)
        with closing(sqlite3.connect(self.path)) as conn:
            days = [r[0] for r in conn.execute("SELECT check_day FROM checks ORDER BY id")]
        self.assertEqual(days, ["2026-06-30", "2026-07-01"])


if __name__ == "__main__":
    unittest.main()
