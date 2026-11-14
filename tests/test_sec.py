"""Tests for the SEC sources: the ticker list, reported values, report planning, and report pages.

The fixtures are modelled on responses observed on 2026-11-13 and 2026-11-14 and
are shortened. No test contacts a live service; the contact line is made up.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_info.providers import DateRange, FilingRef, HttpClient, SecAccess, Symbol  # noqa: E402
from stock_info.sec import (  # noqa: E402
    SecFiling, build_page_statements, fetch_sec_facts, lookup_sec_company, parse_filing_summary, parse_report_page,
    plan_sec_filings, read_sec_filing, select_statement_pages,
)
from test_providers import FakeSession  # noqa: E402

AAPL = Symbol("US:AAPL", "US", "AAPL")
LINE = "Test Operator operator@collector.test"
ACCESSION = "0000320193-26-000020"
FOLDER = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/"
FILING = SecFiling(FilingRef(AAPL, FOLDER, date(2026, 6, 27), date(2026, 7, 31), "automatic"), "10-Q", ACCESSION)

TICKERS = {"fields": ["cik", "name", "ticker", "exchange"],
           "data": [[320193, "Apple Inc.", "AAPL", "Nasdaq"], [1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"],
                    [11, "First Holder", "DUP", "NYSE"], [12, "Second Holder", "DUP", None]]}

FACTS = {"cik": 320193, "entityName": "Apple Inc.", "facts": {
    "dei": {"EntityCommonStockSharesOutstanding": {"label": "Shares Outstanding", "description": "d", "units": {
        "shares": [{"end": "2026-10-16", "val": 14500000000, "accn": "0000320193-26-000026", "fy": 2026, "fp": "FY",
                    "form": "10-K", "filed": "2026-11-03"}]}}},
    "us-gaap": {
        "NetIncomeLoss": {"label": "Net Income (Loss)", "description": "d", "units": {"USD": [
            {"start": "2024-09-29", "end": "2025-09-27", "val": 112010000000, "accn": "0000320193-26-000026",
             "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-11-03"},
            {"start": "2025-09-28", "end": "2026-09-26", "val": 132175000000, "accn": "0000320193-26-000026",
             "fy": 2026, "fp": "FY", "form": "10-K", "filed": "2026-11-03", "frame": "CY2026"}]}},
        "EarningsPerShareBasic": {"label": None, "description": None, "units": {"USD/shares": [
            {"start": "2025-09-28", "end": "2026-09-26", "val": 9.01, "accn": "0000320193-26-000026", "fy": 2026,
             "fp": "FY", "form": "10-K", "filed": "2026-11-03"}]}},
        "Assets": {"label": "Assets", "description": "d", "units": {"USD": [
            {"end": "2026-09-26", "val": 404548000000, "accn": "0000320193-26-000026", "fy": None, "fp": None,
             "form": "10-K", "filed": "2026-11-03"}]}}}}}


def report(file_tag: str, name: str, long_name: str) -> str:
    """Build one Report element of a filing summary and return it.

    :param file_tag: str; `HtmlFileName` or `XmlFileName`.
    :param name: str; page file name such as `R2.htm`.
    :param long_name: str; long name such as `9952151 - Statement - BALANCE SHEETS`.
    return xml: str; Report element.
    """
    return (f"<Report><IsDefault>false</IsDefault><{file_tag}>{name}</{file_tag}><LongName>{long_name}</LongName>"
            f"<ReportType>Sheet</ReportType><ShortName>{long_name.split(' - ')[-1]}</ShortName></Report>")


def summary(*reports: str) -> bytes:
    """Build a filing summary from Report elements and return its bytes.

    :param reports: str; Report elements.
    return xml: bytes; UTF-8 filing summary with a closing "All Reports" entry.
    """
    book = "<Report><LongName>All Reports</LongName><ReportType>Book</ReportType><ShortName>All Reports</ShortName></Report>"
    return ('<?xml version="1.0" encoding="utf-8"?><FilingSummary><Version>3.26.3</Version><MyReports>'
            + "".join(reports) + book + "</MyReports></FilingSummary>").encode("utf-8")


NEW_SUMMARY = summary(
    report("HtmlFileName", "R1.htm", "0000001 - Document - Cover Page"),
    report("HtmlFileName", "R2.htm", "9952151 - Statement - CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (Unaudited)"),
    report("HtmlFileName", "R3.htm", "9952152 - Statement - CONDENSED CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME (Unaudited)"),
    report("HtmlFileName", "R4.htm", "9952153 - Statement - CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited)"),
    report("HtmlFileName", "R5.htm", "9952154 - Statement - CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited) (Parenthetical)"),
    report("HtmlFileName", "R6.htm", "9952155 - Statement - CONDENSED CONSOLIDATED STATEMENTS OF SHAREHOLDERS' EQUITY (Unaudited)"),
    report("HtmlFileName", "R7.htm", "9952156 - Statement - CONDENSED CONSOLIDATED STATEMENTS OF CASH FLOWS (Unaudited)"),
    report("HtmlFileName", "R8.htm", "9952157 - Disclosure - Summary of Significant Accounting Policies"))
OLD_SUMMARY = summary(report("XmlFileName", "R1.xml", "11 - Statement - Statement Of Income"),
                      report("XmlFileName", "R2.xml", "12 - Statement - Statement Of Financial Position Classified"),
                      report("XmlFileName", "R4.xml", "14 - Statement - Statement Of Cash Flows Indirect"))


def label(concept: str, text: str, css: str = "pl") -> str:
    """Build the label cell of a report row and return it.

    :param concept: str; concept such as `us-gaap_NetIncomeLoss`.
    :param text: str; row label.
    :param css: str; cell class, `pl` or `pl custom`.
    return html: str; label cell.
    """
    return (f'<td class="{css}" style="border-bottom: 0px;" valign="top"><a class="a" href="javascript:void(0);" '
            f"onclick=\"Show.showAR( this, 'defref_{concept}', window );\">{text}</a></td>")


def cells(*values: str) -> str:
    """Build value cells and return them; a value in parentheses gets the class `num`, an empty one `text`.

    :param values: str; displayed values.
    return html: str; value cells.
    """
    out = []
    for value in values:
        css = "text" if not value else ("num" if value.startswith("(") else "nump")
        out.append(f'<td class="{css}">{value or "&#160;"}<span></span></td>')
    return "".join(out)


DEFINITIONS = ('<div style="display: none;"><table border="0" cellpadding="0" cellspacing="0" class="authRefData" '
               'id="defref_us-gaap_NetIncomeLoss"><tr><td class="hide"><a>X</a></td></tr><tr><td><div class="body">'
               "<p>The portion of profit or loss 999</p></div></td></tr></table></div>")
INCOME_PAGE = (
    "<DOCUMENT>\n<TYPE>XML\n<FILENAME>R2.htm\n<TEXT>\n<html><head><script>var t = \"<table class='report'>\";</script>"
    '</head><body><span style="display: none;">v3.26.3</span><table class="report" border="0" cellspacing="2" id="i1">'
    '<tr><th class="tl" colspan="1" rowspan="2"><div style="width: 200px;"><strong>CONDENSED CONSOLIDATED STATEMENTS '
    "OF OPERATIONS (Unaudited) - USD ($)<br> shares in Thousands, $ in Millions</strong></div></th>"
    '<th class="th" colspan="2">3 Months Ended</th><th class="th" colspan="2">9 Months Ended</th></tr>'
    '<tr><th class="th"><div>Jun. 27, 2026</div></th><th class="th"><div>Jun. 28, 2025</div></th>'
    '<th class="th"><div>Jun. 27, 2026</div></th><th class="th"><div>Jun. 28, 2025</div></th></tr>'
    '<tr class="re">' + label("us-gaap_Revenues", "Net sales") + cells("$ 94,036", "$ 85,777", "$ 313,695", "$ 296,105") + "</tr>"
    '<tr class="ro">' + label("us-gaap_NonoperatingIncomeExpense", "Other income/(expense), net") + cells("(171)", "142", "(321)", "269") + "</tr>"
    '<tr class="re">' + label("us-gaap_EarningsPerShareAbstract", "Earnings per share:") + cells("", "", "", "") + "</tr>"
    '<tr class="ro">' + label("us-gaap_EarningsPerShareBasic", "Basic (in dollars per share)") + cells("$ 1.57", "$ 1.40", "$ 5.62", "$ 5.20") + "</tr>"
    '<tr class="rh">' + label("srt_ProductOrServiceAxis", "<strong>Products</strong>") + cells("", "", "", "") + "</tr>"
    '<tr class="re">' + label("aapl_ProductSales", "Net sales", "pl custom") + cells("$ 66,613", "$ 61,564", "$ 230,000 [1]", "$ 220,000") + "</tr>"
    "</table>" + DEFINITIONS + "</body></html>")
BALANCE_PAGE = (
    '<html><body><table class="report" border="0" cellspacing="2" id="i2">'
    '<tr><th class="tl" colspan="2" rowspan="1"><div><strong>CONDENSED CONSOLIDATED BALANCE SHEETS (Unaudited) - '
    'USD ($)<br> $ in Millions</strong></div></th><th class="th"><div>Jun. 27, 2026</div></th>'
    '<th class="th"><div>Sep. 27, 2025</div></th></tr>'
    '<tr class="re">' + label("us-gaap_CashAndCashEquivalentsAtCarryingValue", "Cash and cash equivalents")
    + '<td class="th" style="border-bottom: 0px;"></td>' + cells("$ 36,269", "$ 35,934") + "</tr>"
    '<tr class="rou">' + label("us-gaap_Assets", "Total assets")
    + '<td class="th" style="border-bottom: 0px;"><sup>[1]</sup></td>' + cells("383,266", "359,241") + "</tr>"
    '<tr><td colspan="4"></td></tr><tr><td colspan="4"><table class="outerFootnotes" width="100%">'
    '<tr class="outerFootnote"><td class="outerFootnote" valign="top">[1]</td><td>Includes amounts shown in '
    "<table><tr><td>27,777</td><td>nested</td></tr></table></td></tr></table></td></tr></table>" + DEFINITIONS
    + "</body></html>")
CASH_PAGE = (
    '<html><body><table class="report" id="i3"><tr><th class="tl" colspan="1" rowspan="2"><div><strong>CONDENSED '
    "CONSOLIDATED STATEMENTS OF CASH FLOWS (Unaudited) - USD ($)<br> $ in Millions</strong></div></th>"
    '<th class="th" colspan="2">9 Months Ended</th></tr><tr><th class="th"><div>Jun. 27, 2026</div></th>'
    '<th class="th"><div>Jun. 28, 2025</div></th></tr>'
    '<tr class="re">' + label("us-gaap_NetCashProvidedByUsedInOperatingActivities", "Cash generated by operating activities")
    + cells("81,754", "91,443") + "</tr></table></body></html>")


def make_http(routes: dict[str, Any], *, with_contact: bool = True) -> tuple[HttpClient, FakeSession]:
    """Create a client whose SEC session answers the given URLs and return the client and the fake session.

    A route value is bytes (HTTP 200), an int (that HTTP status with an empty body), or an exception to raise.

    :param routes: dict[str, Any]; exact URL to answer.
    :param with_contact: bool; False leaves the SEC contact unset, which blocks SEC requests.
    return pair: tuple[HttpClient, FakeSession]; client and its SEC session.
    """
    session = FakeSession()
    for url, answer in routes.items():
        def handler(_url: str, value: Any = answer) -> Any:
            """Serve one prepared answer.

            :param _url: str; ignored.
            :param value: Any; prepared answer.
            return reply: Any; (status, body, headers).
            """
            if isinstance(value, Exception):
                raise value
            return (value, b"", {}) if isinstance(value, int) else (200, value, {})

        session.add(lambda u, expected=url: u == expected, handler)
    access = (lambda: SecAccess(user_agent=LINE)) if with_contact else None
    client = HttpClient(session_factory=lambda: session, sec_session_factory=lambda: session, sec_access=access,
                        sleep=lambda _s: None)
    return client, session


def as_json(payload: Any) -> bytes:
    """Encode a payload as UTF-8 JSON and return the bytes.

    :param payload: Any; JSON payload.
    return body: bytes; encoded payload.
    """
    return json.dumps(payload).encode("utf-8")


class CompanyLookupTests(unittest.TestCase):
    """Check the search of the SEC ticker list."""

    URL = "https://www.sec.gov/files/company_tickers_exchange.json"

    def test_found_missing_and_ambiguous(self) -> None:
        """A ticker gives its company; a missing ticker is a warning; two companies are an error."""
        http, session = make_http({self.URL: as_json(TICKERS)})
        result = lookup_sec_company(http, AAPL)
        self.assertEqual((result.state, result.issues, result.links), ("succeeded", (), ()))
        self.assertEqual(result.metadata, {"found": True, "cik": 320193, "name": "Apple Inc.",
                                           "sec_exchange": "Nasdaq", "ticker": "AAPL"})
        self.assertEqual(session.options[0]["headers"], {"User-Agent": LINE})
        result = lookup_sec_company(http, Symbol("US:BRK-B", "US", "BRK-B"))
        self.assertEqual((result.metadata["cik"], result.metadata["sec_exchange"]), (1067983, "NYSE"))
        result = lookup_sec_company(http, Symbol("US:ZZZZ", "US", "ZZZZ"))
        self.assertEqual((result.state, result.metadata), ("succeeded", {"found": False, "ticker": "ZZZZ"}))
        self.assertEqual([(i.code, i.severity) for i in result.issues], [("sec_company_not_found", "warning")])
        result = lookup_sec_company(http, Symbol("US:DUP", "US", "DUP"))
        self.assertEqual((result.state, [i.code for i in result.issues]), ("failed", ["sec_company_ambiguous"]))

    def test_changed_shape_and_missing_contact(self) -> None:
        """An unexpected list shape fails with schema_changed; without a contact nothing is sent."""
        http, _session = make_http({self.URL: as_json({"fields": ["cik", "title"], "data": []})})
        result = lookup_sec_company(http, AAPL)
        self.assertEqual((result.state, result.issues[0].code), ("failed", "schema_changed"))
        http, session = make_http({self.URL: as_json(TICKERS)}, with_contact=False)
        result = lookup_sec_company(http, AAPL)
        self.assertEqual((result.state, result.issues[0].code, result.issues[0].provider),
                         ("failed", "sec_contact_missing", "sec"))
        self.assertEqual(session.calls, [])


class FactsTests(unittest.TestCase):
    """Check the parsing of reported values."""

    URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"

    def test_values_keep_exact_numbers_and_filing_details(self) -> None:
        """Each value becomes one row with exact decimal text, its period, and its filing."""
        http, _session = make_http({self.URL: as_json(FACTS)})
        result = fetch_sec_facts(http, AAPL, 320193)
        self.assertEqual((result.state, result.issues, len(result.records)), ("succeeded", (), 5))
        self.assertEqual(result.metadata, {"cik": 320193, "entity_name": "Apple Inc.", "concept_count": 4})
        by_concept = {(r.concept, r.period_end.isoformat()): r for r in result.records}
        income = by_concept[("NetIncomeLoss", "2026-09-26")]
        self.assertEqual((income.taxonomy, income.label, income.unit, income.value),
                         ("us-gaap", "Net Income (Loss)", "USD", "132175000000"))
        self.assertEqual((income.period_start, income.accession, income.fiscal_year, income.fiscal_period,
                          income.form, income.filed, income.frame),
                         (date(2025, 9, 28), "0000320193-26-000026", 2026, "FY", "10-K", date(2026, 11, 3), "CY2026"))
        eps = by_concept[("EarningsPerShareBasic", "2026-09-26")]
        self.assertEqual((eps.value, eps.label, eps.unit), ("9.01", None, "USD/shares"))
        assets = by_concept[("Assets", "2026-09-26")]
        self.assertEqual((assets.period_start, assets.fiscal_year, assets.fiscal_period, assets.frame),
                         (None, None, None, None))
        self.assertEqual([r.concept for r in result.records],
                         ["EntityCommonStockSharesOutstanding", "Assets", "EarningsPerShareBasic", "NetIncomeLoss",
                          "NetIncomeLoss"])
        self.assertEqual((result.coverage.row_count, result.coverage.observed_start, result.coverage.observed_end),
                         (5, date(2025, 9, 27), date(2026, 10, 16)))

    def test_missing_values_wrong_company_and_malformed_rows(self) -> None:
        """HTTP 404 is a warning without rows; another company or a malformed value rejects the whole answer."""
        http, _session = make_http({self.URL: 404})
        result = fetch_sec_facts(http, AAPL, 320193)
        self.assertEqual((result.state, result.records), ("succeeded", ()))
        self.assertEqual([(i.code, i.severity) for i in result.issues], [("sec_values_unavailable", "warning")])
        http, _session = make_http({self.URL: 503})
        self.assertEqual(fetch_sec_facts(http, AAPL, 320193).state, "failed")
        http, _session = make_http({self.URL: as_json({**FACTS, "cik": 99})})
        result = fetch_sec_facts(http, AAPL, 320193)
        self.assertEqual((result.state, result.issues[0].code, result.records), ("failed", "identity_mismatch", ()))
        for broken in ({"end": "2026-09-26", "val": 1}, {"end": "26 Sep 2026", "val": 1, "accn": "x"},
                       {"end": "2026-09-26", "val": "many", "accn": "x"}, {"end": "2026-09-26", "val": True, "accn": "x"}):
            payload = json.loads(json.dumps(FACTS))
            payload["facts"]["us-gaap"]["Assets"]["units"]["USD"].append(broken)
            http, _session = make_http({self.URL: as_json(payload)})
            result = fetch_sec_facts(http, AAPL, 320193)
            self.assertEqual((result.state, result.issues[0].code, result.records), ("failed", "schema_changed", ()),
                             broken)


class PlanTests(unittest.TestCase):
    """Check which reports are planned from the stored values and how their period end is estimated."""

    def test_period_end_estimate_and_filters(self) -> None:
        """Periodic reports with financial values are kept, newest first, with an estimated period end."""
        rows = [
            ("0000320193-26-000026", "10-K", "2026-11-03", "2026-09-26", "us-gaap", 100),
            ("0000320193-26-000026", "10-K", "2026-11-03", "2025-09-27", "us-gaap", 95),
            ("0000320193-26-000026", "10-K", "2026-11-03", "2024-09-28", "us-gaap", 40),
            ("0000320193-26-000026", "10-K", "2026-11-03", "2026-10-20", "us-gaap", 2),
            ("0000320193-26-000026", "10-K", "2026-11-03", "2026-10-16", "dei", 1),
            ("0000320193-26-000020", "10-Q", "2026-07-31", "2026-06-27", "us-gaap", 80),
            ("0000320193-26-000020", "10-Q", "2026-07-31", "2025-09-27", "us-gaap", 50),
            ("0000320193-25-000079", "10-K", "2025-10-31", "2025-09-27", "us-gaap", 90),
            ("0000320193-26-000005", "8-K", "2026-01-29", "2025-12-27", "us-gaap", 30),
            ("0000320193-26-000009", "10-K/A", "2026-02-10", "2026-01-30", "dei", 1),
            ("0001628280-26-025362", "20-F", "2026-04-16", "2025-12-31", "ifrs-full", 1),
            ("not-an-accession", "10-Q", "2026-05-01", "2026-03-28", "us-gaap", 70),
        ]
        span = DateRange(date(2025, 10, 1), date(2026, 12, 31))
        plan = plan_sec_filings(rows, AAPL, 320193, span)
        self.assertEqual([(f.accession, f.form, f.reference.report_date.isoformat()) for f in plan],
                         [("0000320193-26-000026", "10-K", "2026-09-26"),
                          ("0000320193-26-000020", "10-Q", "2026-06-27"),
                          ("0001628280-26-025362", "20-F", "2025-12-31")])
        first = plan[0].reference
        self.assertEqual((first.uri, first.publication_date, first.origin, first.symbol),
                         ("https://www.sec.gov/Archives/edgar/data/320193/000032019326000026/", date(2026, 11, 3),
                          "automatic", AAPL))
        everything = plan_sec_filings(rows, AAPL, 320193, DateRange(date(1970, 1, 1), date(2026, 12, 31)))
        self.assertEqual(len(everything), 4)


class SummaryTests(unittest.TestCase):
    """Check the filing summary and the choice of statement pages by title."""

    def test_new_and_old_summaries(self) -> None:
        """New summaries name HTML pages and old ones XML pages; only statement entries are statement pages."""
        pages = parse_filing_summary(NEW_SUMMARY)
        self.assertEqual(len(pages), 9)
        self.assertEqual((pages[1].title, pages[1].html_file, pages[1].xml_file, pages[1].is_statement),
                         ("CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (Unaudited)", "R2.htm", None, True))
        self.assertEqual((pages[0].is_statement, pages[7].is_statement, pages[8].is_statement), (False, False, False))
        chosen = select_statement_pages(pages)
        self.assertEqual({kind: page.html_file for kind, page in chosen.items()},
                         {"income": "R2.htm", "balance": "R4.htm", "cash_flow": "R7.htm"})
        old = parse_filing_summary(OLD_SUMMARY)
        self.assertEqual([(p.html_file, p.xml_file) for p in old[:3]],
                         [(None, "R1.xml"), (None, "R2.xml"), (None, "R4.xml")])

    def test_title_rules(self) -> None:
        """Titles decide the kind; equity, parenthetical, and a separate comprehensive-income page are skipped."""
        cases = [
            (["CONSOLIDATED STATEMENTS OF FINANCIAL POSITION",
              "CONSOLIDATED STATEMENTS OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME",
              "CONSOLIDATED STATEMENTS OF CHANGES IN EQUITY", "CONSOLIDATED STATEMENTS OF CASH FLOWS"],
             {"balance": 0, "income": 1, "cash_flow": 3}),
            (["Consolidated Statements of Comprehensive Income", "Consolidated Statements of Income",
              "Consolidated Balance Sheets (Parenthetical)", "Consolidated Balance Sheets",
              "Consolidated Statements of Changes in Stockholders' Equity", "Consolidated Statements of Cash Flows"],
             {"income": 1, "balance": 3, "cash_flow": 5}),
            (["Statement Of Income", "Statement Of Financial Position Classified", "Statement Of Cash Flows Indirect"],
             {"income": 0, "balance": 1, "cash_flow": 2}),
            (["Consolidated Statements of Earnings", "Consolidated Statements of Financial Condition"],
             {"income": 0, "balance": 1}),
            (["Consolidated Statements of Partners' Capital", "Notes"], {}),
        ]
        for titles, expected in cases:
            pages = parse_filing_summary(summary(*[report("HtmlFileName", f"R{i + 2}.htm", f"10{i} - Statement - {t}")
                                                   for i, t in enumerate(titles)]))
            chosen = select_statement_pages(pages)
            self.assertEqual({kind: titles.index(page.title) for kind, page in chosen.items()}, expected, titles)

    def test_unsafe_summaries_are_rejected(self) -> None:
        """A document type declaration, broken XML, and page names that are not plain R files are refused."""
        for body in (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><FilingSummary/>', b"<FilingSummary><MyReports>"):
            with self.assertRaises(ValueError):
                parse_filing_summary(body)
        pages = parse_filing_summary(summary(report("HtmlFileName", "../../evil.htm", "1 - Statement - Balance Sheets"),
                                             report("HtmlFileName", "https://evil.test/R2.htm", "2 - Statement - Cash Flows")))
        self.assertEqual([p.html_file for p in pages[:2]], [None, None])


class ReportPageTests(unittest.TestCase):
    """Check the reading of one report table and the statements built from it."""

    def test_income_page_with_two_header_rows_and_a_breakdown(self) -> None:
        """Each date column becomes one statement with its period, unit, items, and breakdown heading."""
        table = parse_report_page(INCOME_PAGE)
        self.assertEqual(len(table.rows), 6)
        page = select_statement_pages(parse_filing_summary(NEW_SUMMARY))["income"]
        records = build_page_statements(table, "income", FILING, page)
        self.assertEqual([(r.report_date.isoformat(), r.period_basis, r.source_metadata["column_role"]) for r in records],
                         [("2026-06-27", "quarter", "current"), ("2025-06-28", "quarter", "comparative"),
                          ("2026-06-27", "year_to_date", "current"), ("2025-06-28", "year_to_date", "comparative")])
        first = records[0]
        self.assertEqual((first.kind, first.scope, first.format, first.currency, first.unit, first.publication_date),
                         ("income", "consolidated", "html", "USD", "shares in Thousands, $ in Millions",
                          date(2026, 7, 31)))
        self.assertEqual(first.source_key, FOLDER + "#R2.htm#c1")
        self.assertEqual(first.source_metadata, {"form": "10-Q", "accession": ACCESSION, "page": "R2.htm",
                                                 "title": "CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS (Unaudited)",
                                                 "period_label": "3 Months Ended", "months": 3, "column_role": "current"})
        items = first.items
        self.assertEqual([(i.ordinal, i.item_field, i.item_title, i.value_raw, i.value_kind, i.value_decimal)
                          for i in items[:4]],
                         [(0, "us-gaap_Revenues", "Net sales", "$ 94,036", "number", "94036"),
                          (1, "us-gaap_NonoperatingIncomeExpense", "Other income/(expense), net", "(171)", "number", "-171"),
                          (2, "us-gaap_EarningsPerShareAbstract", "Earnings per share:", None, "missing", None),
                          (3, "us-gaap_EarningsPerShareBasic", "Basic (in dollars per share)", "$ 1.57", "number", "1.57")])
        self.assertEqual((items[3].source_metadata, items[4].item_title, items[4].value_kind),
                         ({"member": None, "custom": False}, "Products", "missing"))
        self.assertEqual((items[5].item_field, items[5].source_metadata), ("aapl_ProductSales",
                                                                            {"member": "Products", "custom": True}))
        self.assertEqual((records[2].items[5].value_raw, records[2].items[5].value_decimal), ("$ 230,000 [1]", "230000"))
        self.assertEqual(records[2].source_metadata["months"], 9)

    def test_balance_page_with_footnote_column_and_nested_tables(self) -> None:
        """Footnote cells, the footnote row, nested tables, and definition tables add no values."""
        table = parse_report_page(BALANCE_PAGE)
        self.assertEqual([(r.label, r.values) for r in table.rows],
                         [("Cash and cash equivalents", ("$ 36,269", "$ 35,934")), ("Total assets", ("383,266", "359,241"))])
        page = select_statement_pages(parse_filing_summary(NEW_SUMMARY))["balance"]
        records = build_page_statements(table, "balance", FILING, page)
        self.assertEqual([(r.report_date.isoformat(), r.period_basis, r.unit, r.source_metadata["column_role"])
                          for r in records],
                         [("2026-06-27", "instant", "$ in Millions", "current"),
                          ("2025-09-27", "instant", "$ in Millions", "comparative")])
        self.assertEqual([i.value_decimal for i in records[1].items], ["35934", "359241"])
        self.assertEqual(records[0].source_metadata["period_label"], None)

    def test_currency_columns_and_older_title_form(self) -> None:
        """A currency in a column heading wins over the title; the older title form gives currency and unit."""
        html = ('<table class="report"><tr><th class="tl"><div><strong>CONSOLIDATED STATEMENTS OF FINANCIAL POSITION'
                '<br> $ in Millions, $ in Millions</strong></div></th><th class="th"><div>Dec. 31, 2025</div><div>'
                'TWD ($)</div></th><th class="th"><div>Dec. 31, 2025</div><div>USD ($)</div></th></tr>'
                '<tr class="re">' + label("ifrs-full_Assets", "Total assets") + cells("$ 7,000,000", "$ 220,000")
                + "</tr></table>")
        pages = parse_filing_summary(summary(report("HtmlFileName", "R3.htm",
                                                    "1 - Statement - CONSOLIDATED STATEMENTS OF FINANCIAL POSITION")))
        records = build_page_statements(parse_report_page(html), "balance", FILING, pages[0])
        self.assertEqual([(r.currency, r.unit, r.source_key[-9:]) for r in records],
                         [("TWD", "$ in Millions, $ in Millions", "R3.htm#c1"), ("USD", "$ in Millions, $ in Millions", "R3.htm#c2")])
        old = ('<table class="report"><tr><th class="tl" rowspan="2"><div><strong>CONDENSED CONSOLIDATED STATEMENTS OF '
               "OPERATIONS (USD $)<br>In Millions, except Share data in Thousands, unless otherwise specified</strong>"
               '</div></th><th class="th" colspan="1">12 Months Ended</th></tr><tr><th class="th"><div>Sep. 29, 2012'
               '</div></th></tr><tr class="re">' + label("us-gaap_SalesRevenueNet", "Net sales") + cells("$ 156,508")
               + "</tr></table>")
        pages = parse_filing_summary(summary(report("HtmlFileName", "R2.htm",
                                                    "1 - Statement - CONDENSED CONSOLIDATED STATEMENTS OF OPERATIONS")))
        record = build_page_statements(parse_report_page(old), "income", FILING, pages[0])[0]
        self.assertEqual((record.currency, record.unit, record.period_basis, record.report_date),
                         ("USD", "In Millions, except Share data in Thousands, unless otherwise specified", "annual",
                          date(2012, 9, 29)))

    def test_ambiguous_pages_are_rejected(self) -> None:
        """A page without a report table, without a date column, or with a wrong cell count is rejected."""
        page = parse_filing_summary(NEW_SUMMARY)[1]
        with self.assertRaises(ValueError):
            parse_report_page("<html><body><table><tr><td>no report</td></tr></table></body></html>")
        no_dates = '<table class="report"><tr><th class="tl">TITLE</th><th class="th">Total</th></tr></table>'
        with self.assertRaises(ValueError):
            build_page_statements(parse_report_page(no_dates), "income", FILING, page)
        uneven = ('<table class="report"><tr><th class="tl">TITLE</th><th class="th">Jun. 27, 2026</th>'
                  '<th class="th">Jun. 28, 2025</th></tr><tr class="re">' + label("us-gaap_Revenues", "Net sales")
                  + cells("1") + "</tr></table>")
        with self.assertRaises(ValueError) as ctx:
            build_page_statements(parse_report_page(uneven), "income", FILING, page)
        self.assertIn("Net sales", str(ctx.exception))


class ReadFilingTests(unittest.TestCase):
    """Check the reading of one report: summary, three pages, and the stored outcome."""

    def test_complete_report(self) -> None:
        """Four requests give three statement kinds; the reference gets the exact period end."""
        http, session = make_http({FOLDER + "FilingSummary.xml": NEW_SUMMARY, FOLDER + "R2.htm": INCOME_PAGE.encode(),
                                   FOLDER + "R4.htm": BALANCE_PAGE.encode(), FOLDER + "R7.htm": CASH_PAGE.encode()})
        estimate = SecFiling(FilingRef(AAPL, FOLDER, date(2026, 6, 30), date(2026, 7, 31), "automatic"), "10-Q",
                             ACCESSION)
        result = read_sec_filing(http, estimate)
        self.assertEqual((result.state, result.status, result.format, result.issues),
                         ("succeeded", "parsed", "html", ()))
        self.assertEqual([call.rsplit("/", 1)[1] for call in session.calls],
                         ["FilingSummary.xml", "R2.htm", "R4.htm", "R7.htm"])
        self.assertEqual(sorted({s.kind for s in result.statements}), ["balance", "cash_flow", "income"])
        self.assertEqual(len(result.statements), 8)
        self.assertEqual((result.reference.uri, result.reference.report_date), (FOLDER, date(2026, 6, 27)))
        self.assertEqual((result.metadata["form"], result.metadata["accession"]), ("10-Q", ACCESSION))
        self.assertEqual([(p["kind"], p["file"]) for p in result.metadata["pages"]],
                         [("income", "R2.htm"), ("balance", "R4.htm"), ("cash_flow", "R7.htm")])
        self.assertEqual((result.coverage.row_count, result.coverage.observed_start, result.coverage.observed_end),
                         (8, date(2025, 6, 28), date(2026, 6, 27)))
        self.assertEqual(len(result.body_sha256 or ""), 64)

    def test_old_format_missing_page_and_failures(self) -> None:
        """Old XML pages are a warning; a missing or rejected page is an error; a failed summary fails the report."""
        http, session = make_http({FOLDER + "FilingSummary.xml": OLD_SUMMARY})
        result = read_sec_filing(http, FILING)
        self.assertEqual((result.state, result.status, result.statements), ("succeeded", "unparsed", ()))
        self.assertEqual([(i.code, i.severity) for i in result.issues], [("unsupported_report_format", "warning")])
        self.assertEqual(len(session.calls), 1)

        partial = summary(report("HtmlFileName", "R2.htm", "1 - Statement - CONSOLIDATED STATEMENTS OF OPERATIONS"),
                          report("HtmlFileName", "R4.htm", "2 - Statement - CONSOLIDATED BALANCE SHEETS"))
        http, _session = make_http({FOLDER + "FilingSummary.xml": partial, FOLDER + "R2.htm": INCOME_PAGE.encode(),
                                    FOLDER + "R4.htm": b"<html><body>no table</body></html>"})
        result = read_sec_filing(http, FILING)
        self.assertEqual((result.state, result.status), ("partial", "partial"))
        self.assertEqual(sorted((i.code, i.severity) for i in result.issues),
                         [("ambiguous_statement_table", "error"), ("statement_page_not_found", "error")])
        self.assertEqual({s.kind for s in result.statements}, {"income"})

        http, _session = make_http({FOLDER + "FilingSummary.xml": summary(report("HtmlFileName", "R1.htm",
                                                                                 "1 - Document - Cover Page"))})
        result = read_sec_filing(http, FILING)
        self.assertEqual((result.state, result.status, [i.code for i in result.issues]),
                         ("succeeded", "unparsed", ["statement_pages_absent"]))

        http, _session = make_http({FOLDER + "FilingSummary.xml": 404})
        result = read_sec_filing(http, FILING)
        self.assertEqual((result.state, result.status, result.reference.uri), ("failed", "fetch_failed", FOLDER))
        http, _session = make_http({FOLDER + "FilingSummary.xml": b"<FilingSummary><broken"})
        result = read_sec_filing(http, FILING)
        self.assertEqual((result.state, result.status, result.issues[0].code), ("failed", "unparsed", "schema_changed"))


if __name__ == "__main__":
    unittest.main()
