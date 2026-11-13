"""Tests for the SQLite store: schema, gates, atomic finalization, revisions, and queries."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from stock_info.providers import (  # noqa: E402
    CheckKey, Coverage, DateRange, FetchResult, Interval, MappingResult, PriceRow, QueryRequest, StatementItem,
    StatementRecord, StorageError, Symbol,
)
from stock_info.storage import Store  # noqa: E402

UTC = timezone.utc
SH = Symbol("SH:600519", "SH", "600519")
SPAN = DateRange(date(2025, 1, 1), date(2025, 1, 31))
NOW = datetime(2025, 2, 1, 2, 0, tzinfo=UTC)


def make_row(day: int, close: str = "10") -> PriceRow:
    """Build a valid January 2025 price row and return it.

    :param day: int; day of January 2025.
    :param close: str; closing price.
    return row: PriceRow; price row.
    """
    return PriceRow(trade_date=date(2025, 1, day), open="10", high="20", low="5", close=close, volume_native="1",
                    amount_native="2", source_row=[f"2025-01-{day:02d}"])


def price_result(rows: list[PriceRow], state: str = "succeeded", span: DateRange = SPAN) -> FetchResult:
    """Wrap rows in a FetchResult covering the span and return it.

    :param rows: list[PriceRow]; rows.
    :param state: str; result state.
    :param span: DateRange; visited range.
    return result: FetchResult; price result.
    """
    coverage = Coverage(requested=span, fetched=(Interval(span.start, span.end, "nonempty" if rows else "empty",
                                                          len(rows)),),
                        row_count=len(rows), traversal=state)  # type: ignore[arg-type]
    return FetchResult(state=state, coverage=coverage, requests=(), issues=(), metadata={},  # type: ignore[arg-type]
                       records=tuple(rows))


def statement(scope: str = "consolidated", value: str = "1") -> StatementRecord:
    """Build a balance-sheet record for 2024-12-31 and return it.

    :param scope: str; statement scope.
    :param value: str; value of the single item.
    return record: StatementRecord; statement record.
    """
    item = StatementItem(ordinal=0, item_field=None, item_title="货币资金", value_raw=value, value_kind="number",
                         value_decimal=value)
    return StatementRecord(report_date=date(2024, 12, 31), kind="balance", scope=scope, source_scope="合并期末",  # type: ignore[arg-type]
                           source_key="20241231", format="structured", period_basis="source_reported",
                           publication_date=None, publication_date_raw=None, source_update_at=None,
                           source_update_raw=None, currency=None, unit=None, items=(item,))


class StoreTestCase(unittest.TestCase):
    """Provide a temporary database path for each test."""

    def setUp(self) -> None:
        """Create a temporary directory and database path.

        return None: None; no value is returned.
        """
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "test.sqlite3"
        self.store = Store(self.path)

    def tearDown(self) -> None:
        """Close the store and delete the directory.

        return None: None; no value is returned.
        """
        self.store.close()
        self.tmp.cleanup()

    def record_prices(self, rows: list[PriceRow], now: datetime, *, adjustment: str = "raw",
                      state: str = "succeeded", span: DateRange = SPAN) -> int:
        """Start and finish one refresh-gated price check and return its id.

        :param rows: list[PriceRow]; rows.
        :param now: datetime; observation time.
        :param adjustment: str; raw or forward.
        :param state: str; result state.
        :param span: DateRange; check span.
        return check_id: int; check id.
        """
        key = CheckKey(SH.symbol_id, "eastmoney", "prices", adjustment)  # type: ignore[arg-type]
        check_id = self.store.start_check(key, span, "run", now, refresh=True)
        assert check_id is not None
        self.store.finish_check(check_id, price_result(rows, state, span), now)
        return check_id

    def query(self, **kwargs: object) -> dict:
        """Run a price query with defaults and return the page.

        :param kwargs: object; QueryRequest overrides.
        return page: dict; query page.
        """
        options = {"symbol": SH, "span": SPAN, "dataset": "prices", **kwargs}
        return self.store.query(QueryRequest(**options), NOW, symbols=[SH])  # type: ignore[arg-type]


class SchemaTests(StoreTestCase):
    """Check schema installation and version handling."""

    def test_schema_foreign_keys_and_version(self) -> None:
        """A new database has six tables, version 1, and enforced foreign keys."""
        conn = sqlite3.connect(self.path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"securities", "checks", "code_links", "prices", "statements", "filings"})
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.conn.execute("INSERT INTO prices VALUES (999, '2025-01-01', 'h', '{}')")

    def test_unknown_version_and_missing_read_only_file(self) -> None:
        """An unknown version is rejected; read-only opening never creates a file."""
        self.store.close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version=7")
        conn.commit()
        conn.close()
        with self.assertRaises(StorageError) as ctx:
            Store(self.path)
        self.assertEqual(ctx.exception.issue.code, "schema_version_unsupported")
        missing = Path(self.tmp.name) / "none.sqlite3"
        with self.assertRaises(StorageError) as ctx:
            Store(missing, readonly=True)
        self.assertEqual(ctx.exception.issue.code, "database_not_found")
        self.assertFalse(missing.exists())
        self.store = Store(Path(self.tmp.name) / "other.sqlite3")


class GateTests(StoreTestCase):
    """Check daily gates in the China time zone and refresh behaviour."""

    def test_one_attempt_per_china_day(self) -> None:
        """A second same-day reservation is refused; the next China day and refresh are allowed."""
        key = CheckKey(SH.symbol_id, "sina", "income")
        before_midnight = datetime(2025, 3, 1, 15, 59, tzinfo=UTC)  # 23:59 in China
        self.assertIsNotNone(self.store.start_check(key, SPAN, "a", before_midnight))
        self.assertIsNone(self.store.start_check(key, SPAN, "b", before_midnight - timedelta(hours=10)))
        self.assertIsNotNone(self.store.start_check(key, SPAN, "c", before_midnight + timedelta(minutes=1)))
        self.assertIsNotNone(self.store.start_check(key, SPAN, "d", before_midnight, refresh=True))

    def test_failed_finalization_rolls_back_and_keeps_running(self) -> None:
        """A mismatched result is refused atomically and older data stays intact."""
        first = self.record_prices([make_row(2)], NOW)
        key = CheckKey(SH.symbol_id, "eastmoney", "prices", "raw")
        check_id = self.store.start_check(key, SPAN, "run", NOW, refresh=True)
        with self.assertRaises(StorageError):
            self.store.finish_check(check_id, MappingResult(state="succeeded", coverage=Coverage(None), requests=(),
                                                            issues=(), metadata={}), NOW)
        state = self.store.conn.execute("SELECT state FROM checks WHERE id=?", (check_id,)).fetchone()[0]
        self.assertEqual(state, "running")
        page = self.query()
        self.assertEqual(page["groups"][0]["selected_check_id"], first)
        self.assertEqual(len(page["groups"][0]["records"]), 1)


class RevisionTests(StoreTestCase):
    """Check revision selection, retention, and pagination."""

    def test_latest_selection_after_a_b_a(self) -> None:
        """A then B then A returns A again; all versions aggregate observations per hash."""
        self.record_prices([make_row(2, "10")], NOW - timedelta(days=3))
        self.record_prices([make_row(2, "11")], NOW - timedelta(days=2))
        self.record_prices([make_row(2, "10")], NOW - timedelta(days=1))
        latest = self.query()["groups"][0]["records"]
        self.assertEqual([r["data"]["close"] for r in latest], ["10"])
        self.assertEqual(latest[0]["observation_count"], 2)
        every = self.query(versions="all")["groups"][0]["records"]
        self.assertEqual(sorted((r["data"]["close"], r["observation_count"]) for r in every), [("10", 2), ("11", 1)])
        self.assertEqual(self.store.reused_revisions, 1)

    def test_omitted_rows_are_kept_with_warning(self) -> None:
        """A date missing from the latest check keeps its older value and adds missing_in_recheck."""
        self.record_prices([make_row(2), make_row(3)], NOW - timedelta(days=2))
        self.record_prices([make_row(3)], NOW - timedelta(days=1))
        page = self.query()
        self.assertEqual([r["record_key"] for r in page["groups"][0]["records"]], ["2025-01-02", "2025-01-03"])
        self.assertIn("missing_in_recheck", [i["code"] for i in page["issues"]])

    def test_forward_snapshots_stay_separate(self) -> None:
        """Forward prices come from one enclosing succeeded snapshot and are never spliced."""
        self.record_prices([make_row(2, "10")], NOW - timedelta(days=2), adjustment="forward")
        self.record_prices([make_row(3, "12")], NOW - timedelta(days=1), adjustment="forward",
                           span=DateRange(date(2025, 1, 3), date(2025, 1, 31)))
        page = self.query(adjustment="forward")
        self.assertEqual([r["record_key"] for r in page["groups"][0]["records"]], ["2025-01-02"])
        narrow = self.query(adjustment="forward", span=DateRange(date(2025, 1, 3), date(2025, 1, 31)))
        self.assertEqual([r["data"]["close"] for r in narrow["groups"][0]["records"]], ["12"])
        partial = self.query(adjustment="forward", span=DateRange(date(2024, 12, 1), date(2025, 1, 31)))
        self.assertIsNone(partial["groups"][0]["selected_check_id"])
        self.assertIn("no_enclosing_forward_snapshot", [i["code"] for i in partial["issues"]])

    def test_pagination_and_statement_scope(self) -> None:
        """Offsets count whole records; statement queries filter by scope."""
        self.record_prices([make_row(d) for d in range(2, 7)], NOW - timedelta(hours=1))
        first = self.query(limit=2)
        self.assertEqual((first["pagination"]["returned"], first["pagination"]["next_offset"]), (2, 2))
        last = self.query(limit=2, offset=4)
        self.assertEqual((last["pagination"]["returned"], last["pagination"]["next_offset"]), (1, None))
        key = CheckKey(SH.symbol_id, "sina", "balance")
        check_id = self.store.start_check(key, SPAN, "run", NOW)
        result = FetchResult(state="succeeded", coverage=Coverage(requested=SPAN, row_count=2, traversal="succeeded"),
                             requests=(), issues=(), metadata={}, records=(statement(), statement("parent")))
        self.store.finish_check(check_id, result, NOW)
        span = DateRange(date(2024, 1, 1), date(2025, 1, 31))
        page = self.store.query(QueryRequest(symbol=SH, span=span, dataset="statements", scope="parent",
                                             kind="balance", provider="sina"), NOW, symbols=[SH])
        self.assertEqual([r["data"]["scope"] for r in page["groups"][0]["records"]], ["parent"])

    def test_read_only_store_sees_data_without_writing(self) -> None:
        """A read-only store queries existing data and refuses to start checks."""
        self.record_prices([make_row(2)], NOW)
        reader = Store(self.path, readonly=True)
        try:
            page = reader.query(QueryRequest(symbol=SH, span=SPAN, dataset="coverage"), NOW, symbols=[SH])
            self.assertEqual(page["groups"][0]["records"][0]["type"], "check")
            with self.assertRaises(StorageError):
                reader.start_check(CheckKey(SH.symbol_id, "sina", "income"), SPAN, "x", NOW)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
