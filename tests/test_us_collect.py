"""Tests for a whole US collection and the US command-line selectors, with a fake world.

The fake world serves the SEC ticker list, reported values, two reports, and
Eastmoney prices. No test contacts a live service; the contact details are made up.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_data  # noqa: E402
from stock_info.collector import collect, show  # noqa: E402
from stock_info.providers import CollectRequest, DateRange, HttpClient, QueryRequest, SecAccess  # noqa: E402
from stock_info.storage import Store  # noqa: E402
from test_providers import FakeSession  # noqa: E402
from test_sec import (  # noqa: E402
    BALANCE_PAGE, CASH_PAGE, INCOME_PAGE, LINE, NEW_SUMMARY, OLD_SUMMARY, TICKERS, as_json,
)
from test_us import AAPL, ROWS, us_eastmoney  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)
SPAN = DateRange(date(2024, 1, 1), date(2026, 11, 13))
ANNUAL = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/"
QUARTER = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/"


def value(end: str, val: int, accession: str, form: str, filed: str, start: str | None = None) -> dict[str, Any]:
    """Build one reported value of the fake SEC answer and return it.

    :param end: str; period end as ISO text.
    :param val: int; value.
    :param accession: str; filing identifier.
    :param form: str; form type.
    :param filed: str; filing date as ISO text.
    :param start: str | None; period start as ISO text, or None.
    return value: dict[str, Any]; JSON object of one value.
    """
    item: dict[str, Any] = {"end": end, "val": val, "accn": accession, "fy": 2026, "fp": "FY", "form": form,
                            "filed": filed}
    if start:
        item["start"] = start
    return item


WORLD_FACTS = {"cik": 320193, "entityName": "Apple Inc.", "facts": {"us-gaap": {
    "NetIncomeLoss": {"label": "Net Income (Loss)", "description": "d", "units": {"USD": [
        value("2026-06-27", 101464000000, "0000320193-26-000020", "10-Q", "2026-07-31", "2025-09-28"),
        value("2026-09-26", 132175000000, "0000320193-26-000026", "10-K", "2026-11-03", "2025-09-28")]}},
    "Assets": {"label": "Assets", "description": "d", "units": {"USD": [
        value("2026-06-27", 383266000000, "0000320193-26-000020", "10-Q", "2026-07-31"),
        value("2026-09-26", 404548000000, "0000320193-26-000026", "10-K", "2026-11-03")]}}}}}


def build_us_world(*, prices: bool = True) -> FakeSession:
    """Build the fake world for `US:AAPL` and return its session.

    The annual report has only old-format pages; the quarterly report has three
    readable statement pages. With `prices=False`, Eastmoney knows the code but
    returns no rows, and Tencent serves two unadjusted rows.

    :param prices: bool; whether Eastmoney returns price rows.
    return session: FakeSession; session that answers every source of the world.
    """
    session = FakeSession()
    fixed = {
        "https://www.sec.gov/files/company_tickers_exchange.json": as_json(TICKERS),
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json": as_json(WORLD_FACTS),
        ANNUAL + "FilingSummary.xml": OLD_SUMMARY,
        QUARTER + "FilingSummary.xml": NEW_SUMMARY,
        QUARTER + "R2.htm": INCOME_PAGE.encode(), QUARTER + "R4.htm": BALANCE_PAGE.encode(),
        QUARTER + "R7.htm": CASH_PAGE.encode(),
    }
    for url, body in fixed.items():
        session.add(lambda u, expected=url: u == expected, lambda _u, answer=body: (200, answer, {}))
    session.add(lambda u: "eastmoney" in u, us_eastmoney("AAPL", "105", ROWS if prices else []))
    days = [["2024-01-02", "187.15", "185.64", "188.44", "183.89", "82488700.00", {}],
            ["2024-01-03", "184.22", "184.25", "185.88", "183.43", "58414500.00", {}]]

    def tencent(url: str) -> Any:
        """Serve the unadjusted rows on or before the cursor date of the request.

        :param url: str; request URL whose `param` holds the cursor as its fourth part.
        return reply: Any; (status, body, headers).
        """
        cursor = parse_qs(urlsplit(url).query)["param"][0].split(",")[3]
        rows = [row for row in days if row[0] <= cursor]
        return 200, as_json({"code": 0, "msg": "", "data": {"usAAPL.OQ": {"day": rows, "qt": {}}}}), {}

    session.add(lambda u: "proxy.finance.qq.com" in u, tencent)
    return session


def build_client(session: FakeSession, *, with_contact: bool = True, **options: Any) -> HttpClient:
    """Create a client whose browser-like and SEC sessions are both the fake session and return it.

    :param session: FakeSession; fake world.
    :param with_contact: bool; False leaves the SEC contact unset.
    :param options: Any; extra HttpClient options such as `max_requests`.
    return client: HttpClient; client that cannot reach a live service.
    """
    options.setdefault("sec_access", (lambda: SecAccess(user_agent=LINE)) if with_contact else None)
    return HttpClient(session_factory=lambda: session, sec_session_factory=lambda: session, sleep=lambda _s: None,
                      **options)


class UsCollectTests(unittest.TestCase):
    """Check the US task order, reuse on the same day, missing contact, the request budget, and the fallback."""

    def setUp(self) -> None:
        """Create a temporary database.

        return None: None; no value is returned.
        """
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")
        self.addCleanup(lambda: self.store.close())

    def run_collect(self, session: FakeSession, now: datetime = NOW, **options: Any) -> dict[str, Any]:
        """Run one collect for `US:AAPL` and return the envelope.

        :param session: FakeSession; fake world.
        :param now: datetime; fixed UTC time of the run.
        :param options: Any; options for build_client, or `refresh`.
        return envelope: dict[str, Any]; command envelope.
        """
        request = CollectRequest(symbol=AAPL, span=SPAN, refresh=options.pop("refresh", False))
        client = build_client(session, **options)
        try:
            return collect(self.store, client, request, lambda: now)  # type: ignore[return-value]
        finally:
            client.close()

    def records(self, dataset: str, **options: Any) -> list[dict[str, Any]]:
        """Run one show query and return the records of every group.

        :param dataset: str; dataset name.
        :param options: Any; extra QueryRequest fields.
        return records: list[dict[str, Any]]; output records.
        """
        request = QueryRequest(symbol=AAPL, span=DateRange(date(1970, 1, 1), date(2026, 11, 13)), dataset=dataset,
                               **options)
        envelope = show(self.store, request, NOW)
        return [record for group in envelope["datasets"] for record in group["records"]]

    def test_full_run_then_reuse(self) -> None:
        """One run collects the company, values, prices, and reports; a second run on the same day sends nothing."""
        session = build_us_world()
        envelope = self.run_collect(session)
        self.assertEqual(envelope["status"], "ok", envelope["issues"])
        self.assertEqual(sorted(i["code"] for i in envelope["issues"]),
                         ["code_mapping_unresolved", "unsupported_report_format"])
        self.assertTrue(all(i["severity"] == "warning" for i in envelope["issues"]))
        self.assertEqual((envelope["requested_symbol"], envelope["source_conditions_status"]), ("US:AAPL", "unresolved"))
        self.assertEqual([call.rsplit("/", 1)[1].split("?")[0] for call in session.calls],
                         ["company_tickers_exchange.json", "CIK0000320193.json", "get", "get", "get", "get", "get",
                          "get", "get", "FilingSummary.xml", "FilingSummary.xml", "R2.htm", "R4.htm", "R7.htm"])
        self.assertEqual(session.calls[9], ANNUAL + "FilingSummary.xml")
        groups = {(g["provider"], g["dataset"], g["adjustment"]): g for g in envelope["datasets"]}
        self.assertEqual(sorted(groups), [("eastmoney", "prices", "forward"), ("eastmoney", "prices", "raw"),
                                          ("sec", "balance", "none"), ("sec", "cash_flow", "none"),
                                          ("sec", "facts", "none"), ("sec", "filing", "none"),
                                          ("sec", "income", "none")])
        self.assertTrue(all(g["freshness"] == "fresh" for g in groups.values()))
        self.assertEqual(groups[("sec", "facts", "none")]["coverage"]["row_count"], 4)

        income = self.records("statements", kind="income")
        self.assertEqual([(r["provider"], r["data"]["report_date"], r["data"]["period_basis"]) for r in income],
                         [("sec", "2025-06-28", "quarter"), ("sec", "2025-06-28", "year_to_date"),
                          ("sec", "2026-06-27", "quarter"), ("sec", "2026-06-27", "year_to_date")])
        self.assertEqual(income[2]["data"]["publication_date"], "2026-07-31")
        filings = self.records("filings")
        self.assertEqual([(r["provider"], r["data"]["status"], r["data"]["reference"]["uri"],
                           r["data"]["reference"]["report_date"]) for r in filings],
                         [("sec", "parsed", QUARTER, "2026-06-27"), ("sec", "unparsed", ANNUAL, "2026-09-26")])
        prices = self.records("prices")
        self.assertEqual([(r["provider"], r["data"]["trade_date"], r["data"]["close"]) for r in prices],
                         [("eastmoney", "2024-01-02", "185.640"), ("eastmoney", "2024-01-03", "184.250")])
        concepts = self.records("facts")
        self.assertEqual([(r["type"], r["data"]["concept"], r["data"]["value_count"]) for r in concepts],
                         [("concept", "Assets", 2), ("concept", "NetIncomeLoss", 2)])

        calls = len(session.calls)
        again = self.run_collect(session)
        self.assertEqual((again["status"], len(session.calls)), ("ok", calls))
        self.assertEqual([i["code"] for i in again["issues"]], ["code_mapping_unresolved"])
        next_day = self.run_collect(session, NOW + timedelta(days=1))
        self.assertEqual(next_day["status"], "ok")
        self.assertEqual(len(session.calls) - calls, 9)
        self.assertFalse(any("Archives" in call for call in session.calls[calls:]))

    def test_missing_contact_blocks_the_sec_and_keeps_prices(self) -> None:
        """Without a contact, no SEC request is sent, prices are collected, and the run is partial."""
        session = build_us_world()
        envelope = self.run_collect(session, with_contact=False)
        self.assertEqual(envelope["status"], "partial")
        self.assertIn("sec_contact_missing", [i["code"] for i in envelope["issues"]])
        self.assertFalse(any("sec.gov" in call for call in session.calls))
        self.assertEqual(len(self.records("prices")), 2)
        self.assertEqual(self.records("facts"), [])
        calls = len(session.calls)
        again = self.run_collect(session, with_contact=True)
        self.assertEqual((again["status"], len(session.calls)), ("partial", calls))
        self.assertIn("sec_lookup_failed_today", [i["code"] for i in again["issues"]])
        refreshed = self.run_collect(session, refresh=True)
        self.assertEqual(refreshed["status"], "ok", refreshed["issues"])

    def test_request_budget_leaves_reports_pending(self) -> None:
        """Reports that do not fit the request budget are pending; a later run reads them."""
        session = build_us_world()
        envelope = self.run_collect(session, max_requests=11)
        self.assertEqual(envelope["status"], "partial")
        issue = next(i for i in envelope["issues"] if i["code"] == "work_limit")
        self.assertIn("2 reports remain unread", issue["message"])
        self.assertFalse(any("Archives" in call for call in session.calls))
        self.assertEqual(self.records("filings"), [])
        again = self.run_collect(session)
        self.assertEqual(again["status"], "ok", again["issues"])
        self.assertEqual(len(self.records("filings")), 2)

    def test_tencent_fallback_for_unadjusted_prices_only(self) -> None:
        """When Eastmoney has no rows, Tencent serves unadjusted prices, and forward prices stay missing."""
        session = build_us_world(prices=False)
        envelope = self.run_collect(session)
        codes = [i["code"] for i in envelope["issues"]]
        self.assertIn("price_fallback_used", codes)
        self.assertIn("forward_fallback_unavailable", codes)
        self.assertEqual(envelope["status"], "partial")
        tencent = [call for call in session.calls if "proxy.finance.qq.com" in call]
        self.assertEqual(len(tencent), 2)
        self.assertTrue(all("usAAPL.OQ" in call for call in tencent))
        prices = self.records("prices")
        self.assertEqual([(r["provider"], r["data"]["close"]) for r in prices],
                         [("tencent", "185.64"), ("tencent", "184.25")])
        self.assertEqual(self.records("prices", adjustment="forward"), [])


class UsCliTests(unittest.TestCase):
    """Check the US selectors of the command line."""

    def setUp(self) -> None:
        """Point the command line at a temporary root with made-up settings and the fake world.

        return None: None; no value is returned.
        """
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config.toml").write_text(
            '[sec]\ncontact_name = "Test Operator"\ncontact_email = "operator@collector.test"\n', encoding="utf-8")
        self.session = build_us_world()
        for patcher in (mock.patch.object(market_data, "ROOT", self.root),
                        mock.patch.object(market_data, "HttpClient",
                                          side_effect=lambda **kw: build_client(self.session, **kw))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *argv: str) -> tuple[int, dict[str, Any]]:
        """Run main() with arguments and return the exit code and parsed JSON envelope.

        :param argv: str; command-line arguments.
        return outcome: tuple[int, dict[str, Any]]; exit code and envelope.
        """
        buffer = io.BytesIO()
        fake_stdout = mock.Mock()
        fake_stdout.buffer = buffer
        with mock.patch.object(sys, "stdout", fake_stdout), mock.patch.object(sys, "stderr", io.StringIO()):
            code = market_data.main(list(argv))
        return code, json.loads(buffer.getvalue().decode("utf-8"))

    def test_collect_and_show_with_us_selectors(self) -> None:
        """Collect reads the contact from the settings file; show serves values, statements, and conditions."""
        code, collected = self.run_main("collect", "aapl", "--json", "--start", "2024-01-01")
        self.assertEqual((code, collected["status"], collected["requested_symbol"]), (0, "ok", "US:AAPL"))
        self.assertEqual(self.session.options[0]["headers"], {"User-Agent": "Test Operator operator@collector.test"})
        self.assertNotIn("operator@collector.test", json.dumps(collected))
        code, values = self.run_main("show", "AAPL", "--json", "--dataset", "facts", "--concept", "NetIncomeLoss",
                                     "--filed-until", "2026-08-01")
        self.assertEqual((code, values["source_conditions_status"]), (0, "sec_free_to_access_and_reuse"))
        records = [r for g in values["datasets"] for r in g["records"]]
        self.assertEqual([(r["type"], r["data"]["value"], r["data"]["form"]) for r in records],
                         [("fact", "101464000000", "10-Q")])
        code, concepts = self.run_main("show", "US:AAPL", "--json", "--dataset", "facts", "--limit", "1")
        self.assertEqual((code, concepts["datasets"][0]["records"][0]["type"], concepts["pagination"]["next_offset"]),
                         (0, "concept", 1))
        code, statements = self.run_main("show", "AAPL.US", "--json", "--dataset", "statements", "--kind", "balance",
                                         "--provider", "sec")
        self.assertEqual(code, 0)
        self.assertEqual({r["data"]["kind"] for g in statements["datasets"] for r in g["records"]}, {"balance"})
        code, prices = self.run_main("show", "AAPL", "--json", "--dataset", "prices")
        self.assertEqual((code, prices["source_conditions_status"]), (0, "unresolved"))

    def test_invalid_us_input(self) -> None:
        """Selectors of other datasets, bad concept names and dates, and explicit filings are rejected."""
        cases = [
            (["show", "AAPL", "--json", "--dataset", "prices", "--concept", "Assets"], "invalid_selector"),
            (["show", "AAPL", "--json", "--dataset", "statements", "--filed-until", "2026-01-01"], "invalid_selector"),
            (["show", "AAPL", "--json", "--dataset", "facts", "--provider", "sina"], "invalid_selector"),
            (["show", "AAPL", "--json", "--dataset", "facts", "--concept", "Net Income"], "invalid_selector"),
            (["show", "AAPL", "--json", "--dataset", "facts", "--filed-until", "soon"], "invalid_range"),
            (["collect", "AAPL", "--json", "--filing", "report.pdf"], "invalid_path"),
            (["collect", "AAPL", "--json", "--end", "2999-01-01"], "invalid_range"),
        ]
        for argv, expected in cases:
            code, envelope = self.run_main(*argv)
            self.assertEqual((code, envelope["status"], envelope["issues"][0]["code"]), (2, "error", expected), argv)
        self.assertEqual(self.session.calls, [])


if __name__ == "__main__":
    unittest.main()
