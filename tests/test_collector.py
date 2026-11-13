"""Tests for collection planning: gates, fallback, mappings, parent gaps, interruption, and storage stops."""

from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_info import collector  # noqa: E402
from stock_info.collector import collect, show  # noqa: E402
from stock_info.providers import (  # noqa: E402
    CheckKey, CodeLink, CollectRequest, Coverage, DateRange, MappingResult, QueryRequest, StorageError, Symbol,
)
from stock_info.securities import parse_symbol  # noqa: E402
from stock_info.storage import Store  # noqa: E402
from test_filings import DETAIL_HTML  # noqa: E402
from test_providers import (  # noqa: E402
    BSE_PAGE, FakeSession, eastmoney_payload, html_reply, json_reply, make_client, sina_pages, sina_report,
)

UTC = timezone.utc
SH = Symbol("SH:600519", "SH", "600519")
SPAN = DateRange(date(2025, 1, 1), date(2025, 12, 31))
START = datetime(2026, 1, 10, 2, 0, tzinfo=UTC)
ROWS = [("2025-01-02", "11", "12", "13", "10"), ("2025-06-30", "12", "12.5", "13", "11.5")]
INDEX = """<html><head><meta charset="gbk"></head><body><ul>
2026-03-28&nbsp;<a href="/corp/view/vCB_AllBulletinDetail.php?stockid=600519&id=111">贵州茅台2025年年度报告</a><br>
</ul></body></html>"""
DETAIL = DETAIL_HTML.replace("300059", "600519").split("<p><a href=")[0] + "</body></html>"


class Clock:
    """Provide a controllable aware UTC clock.

    :param now: datetime; starting time.
    return instance: Clock; callable clock.
    """

    def __init__(self, now: datetime) -> None:
        """Store the starting time.

        :param now: datetime; starting time.
        return None: None; no value is returned.
        """
        self.now = now

    def __call__(self) -> datetime:
        """Return the current fake time.

        return now: datetime; current time.
        """
        return self.now


def build_world(eastmoney_ok: bool = True) -> FakeSession:
    """Build a fake session that serves every source used by one Shanghai collect run.

    :param eastmoney_ok: bool; False makes every Eastmoney request fail with rc=102.
    return session: FakeSession; configured session.
    """
    eastmoney = eastmoney_payload(SH, ROWS) if eastmoney_ok else json_reply({"rc": 102, "data": None})
    tencent = json_reply({"code": 0, "data": {"sh600519": {"day": [["2025-03-03", "1", "1", "1", "1", "5"]],
                                                           "qfqday": [["2025-03-03", "1", "1", "1", "1", "5"]]}}})
    periods = {"20251231": sina_report()}
    return FakeSession([
        (lambda u: "eastmoney" in u, eastmoney),
        (lambda u: "qq.com" in u and "%2C2025-12-31%2C" in u, tencent),
        (lambda u: "qq.com" in u, json_reply({"code": 0, "data": {"sh600519": {"day": [], "qfqday": []}}})),
        (lambda u: "source=lrb" in u, sina_pages(SH, "lrb", periods)),
        (lambda u: "source=fzb" in u, sina_pages(SH, "fzb", periods)),
        (lambda u: "source=llb" in u, sina_pages(SH, "llb", periods)),
        (lambda u: "ndbg.phtml" in u, html_reply(INDEX, "gbk")),
        (lambda u: "page_type" in u, html_reply("<html><body></body></html>")),
        (lambda u: "vCB_AllBulletinDetail" in u, html_reply(DETAIL)),
        (lambda u: "bse.cn" in u, html_reply(BSE_PAGE)),
    ])


class CollectorTestCase(unittest.TestCase):
    """Provide a temporary store and a fake clock."""

    def setUp(self) -> None:
        """Create the store and clock.

        return None: None; no value is returned.
        """
        self.tmp = TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "db.sqlite3")
        self.clock = Clock(START)

    def tearDown(self) -> None:
        """Close the store and remove the directory.

        return None: None; no value is returned.
        """
        self.store.close()
        self.tmp.cleanup()

    def run_collect(self, session: FakeSession, symbol: Symbol = SH, **kwargs: Any) -> dict:
        """Run one collect command against the fake session and return the envelope.

        :param session: FakeSession; fake session.
        :param symbol: Symbol; requested symbol.
        :param kwargs: Any; CollectRequest overrides.
        return envelope: dict; command result.
        """
        request = CollectRequest(symbol=symbol, span=SPAN, **kwargs)
        return collect(self.store, make_client(session), request, self.clock)  # type: ignore[return-value]


class CollectFlowTests(CollectorTestCase):
    """Check a complete run and the daily gate behaviour."""

    def test_complete_run_then_same_day_reuse_then_next_day_recheck(self) -> None:
        """A satisfied run is ok; the same day reuses data without HTTP; the next day rechecks."""
        session = build_world()
        first = self.run_collect(session)
        self.assertEqual(first["status"], "ok", first["issues"])
        self.assertTrue(first["run_id"])
        self.assertEqual(first["source_conditions_status"], "unresolved")
        self.assertIn("code_mapping_unresolved", [i["code"] for i in first["issues"]])
        prices = [g for g in first["datasets"] if g["dataset"] == "prices"]
        self.assertEqual({(g["provider"], g["adjustment"], g["freshness"]) for g in prices},
                         {("eastmoney", "raw", "fresh"), ("eastmoney", "forward", "fresh")})
        self.assertTrue(all(g["records"] == [] for g in first["datasets"]))
        self.assertGreater(first["stats"]["new_revisions"], 0)
        calls = len(session.calls)
        self.clock.now = START + timedelta(hours=1)
        second = self.run_collect(session)
        self.assertEqual(len(session.calls), calls)
        self.assertEqual(second["status"], "ok")
        self.assertEqual(second["stats"]["http_attempts"], 0)
        self.clock.now = START + timedelta(days=1)
        third = self.run_collect(session)
        self.assertGreater(len(session.calls), calls)
        self.assertGreater(third["stats"]["reused_revisions"], 0)

    def test_refresh_bypasses_gate_and_parent_statements_are_stored(self) -> None:
        """Refresh fetches again on the same day; parent statements from the filing are queryable."""
        session = build_world()
        self.run_collect(session)
        calls = len(session.calls)
        self.run_collect(session, refresh=True)
        self.assertGreater(len(session.calls), calls)
        page = show(self.store, QueryRequest(symbol=SH, span=SPAN, dataset="statements", scope="parent",
                                             provider="filing"), self.clock())
        kinds = {r["data"]["kind"] for g in page["datasets"] for r in g["records"]}
        self.assertEqual(kinds, {"balance", "income", "cash_flow"})

    def test_missing_parent_statements_make_run_partial(self) -> None:
        """Without parent tables in the annual report the run reports missing_parent_statements."""
        session = build_world()
        session.routes = [r for r in session.routes if not r[0]("https://x/vCB_AllBulletinDetail")]
        session.add(lambda u: "vCB_AllBulletinDetail" in u,
                    html_reply(DETAIL.replace("母公司", "合并")))
        result = self.run_collect(session)
        self.assertEqual(result["status"], "partial")
        self.assertIn("missing_parent_statements", [i["code"] for i in result["issues"]])


class AttachmentTests(CollectorTestCase):
    """Check when PDF attachments of HTML reports are read."""

    ATTACHMENT = '<p><a href="http://file.finance.sina.com.cn/MRGG/2026/111.PDF">下载公告</a></p></body></html>'

    def test_pdf_skipped_when_html_yields_statements(self) -> None:
        """A parsed HTML report does not trigger a download of its PDF attachment."""
        session = build_world()
        session.routes = [r for r in session.routes if not r[0]("https://x/vCB_AllBulletinDetail")]
        session.add(lambda u: "vCB_AllBulletinDetail" in u,
                    html_reply(DETAIL.replace("</body></html>", self.ATTACHMENT)))
        result = self.run_collect(session)
        self.assertFalse(any(u.endswith(".PDF") for u in session.calls))
        self.assertIn("pdf_attachment_skipped", [i["code"] for i in result["issues"]])
        self.assertEqual(result["status"], "ok", result["issues"])

    def test_pdf_read_when_html_has_no_statements(self) -> None:
        """An HTML page without statements makes the collector read its PDF attachment."""
        session = build_world()
        session.routes = [r for r in session.routes if not r[0]("https://x/vCB_AllBulletinDetail")]
        empty = "<html><body><p>证券代码：600519</p>" + self.ATTACHMENT
        session.add(lambda u: "vCB_AllBulletinDetail" in u, html_reply(empty))
        self.run_collect(session)
        self.assertTrue(any(u.endswith(".PDF") for u in session.calls))


class FallbackTests(CollectorTestCase):
    """Check the separate Tencent task after a whole Eastmoney failure."""

    def test_tencent_is_a_separate_source(self) -> None:
        """Eastmoney failure triggers Tencent; show selects Tencent without mixing rows."""
        result = self.run_collect(build_world(eastmoney_ok=False))
        codes = [i["code"] for i in result["issues"]]
        self.assertIn("price_fallback_used", codes)
        self.assertIn("provider_error", codes)
        self.assertEqual(result["status"], "ok", result["issues"])
        page = show(self.store, QueryRequest(symbol=SH, span=SPAN, dataset="prices"), self.clock())
        group = page["datasets"][0]
        self.assertEqual(group["provider"], "tencent")
        self.assertEqual([r["record_key"] for r in group["records"]], ["2025-03-03"])


class MappingFlowTests(CollectorTestCase):
    """Check Beijing alias resolution and conflicts."""

    def test_bj_aliases_are_collected(self) -> None:
        """A verified BSE pair adds the old code as an alias and collects it too."""
        session = build_world()
        result = self.run_collect(session, symbol=parse_symbol("920000"))
        self.assertEqual([s["symbol_id"] for s in result["resolved_symbols"]], ["BJ:920000", "BJ:832000"])
        self.assertEqual(len(result["code_links"]), 1)
        self.assertTrue(any("secid=0.832000" in u for u in session.calls))

    def test_conflicting_links_block_aliases(self) -> None:
        """Conflicting stored evidence blocks aliases and the requested code is still collected."""
        other = parse_symbol("920001")
        old = parse_symbol("832000")
        check_id = self.store.start_check(CheckKey(None, "bse", "mapping"), None, "seed", START - timedelta(days=3))
        fork = CodeLink(old, other, "https://www.bse.cn/service/code_mapping.html", ("旧代码", "新代码"),
                        ("832000", "920001"), None, "fork", START, START)
        self.store.finish_check(check_id, MappingResult(state="succeeded", coverage=Coverage(None), requests=(),
                                                        issues=(), metadata={}, links=(fork,)), START)
        session = build_world()
        result = self.run_collect(session, symbol=parse_symbol("920000"))
        self.assertIn("mapping_conflict", [i["code"] for i in result["issues"]])
        self.assertEqual([s["symbol_id"] for s in result["resolved_symbols"]], ["BJ:920000"])
        self.assertEqual(result["status"], "partial")
        self.assertFalse(any("832000" in u and "eastmoney" in u for u in session.calls))


class FailureTests(CollectorTestCase):
    """Check interruption and storage failures."""

    def test_interruption_finalizes_check_as_failed(self) -> None:
        """KeyboardInterrupt stops the run and leaves no running check behind."""
        with mock.patch.object(collector, "fetch_prices", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_collect(build_world())
        rows = self.store.conn.execute("SELECT state, issues_json FROM checks").fetchall()
        self.assertEqual([r["state"] for r in rows], ["failed"])
        self.assertIn("interrupted_check", rows[0]["issues_json"])

    def test_storage_failure_stops_further_requests(self) -> None:
        """A storage error propagates and no later source task starts."""
        session = build_world()
        original = self.store.finish_check
        calls = {"n": 0}

        def failing(*args: Any, **kwargs: Any) -> Any:
            """Fail on the second finalization.

            :param args: Any; positional arguments.
            :param kwargs: Any; keyword arguments.
            return summary: Any; check summary from the original method.
            """
            calls["n"] += 1
            if calls["n"] == 2:
                from stock_info.providers import make_issue

                raise StorageError(make_issue("storage_error", "disk full", "prices"))
            return original(*args, **kwargs)

        with mock.patch.object(self.store, "finish_check", side_effect=failing):
            with self.assertRaises(StorageError):
                self.run_collect(session)
        self.assertFalse(any("sina" in u for u in session.calls))


if __name__ == "__main__":
    unittest.main()
