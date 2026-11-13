"""Tests for the command-line interface: JSON envelopes, selectors, exit codes, and read-only show."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_data  # noqa: E402
from test_collector import build_world  # noqa: E402
from test_providers import make_client  # noqa: E402

ENVELOPE_KEYS = {"schema_version", "command", "status", "requested_symbol", "resolved_symbols", "code_links",
                 "requested_range", "run_id", "source_conditions_status", "datasets", "issues", "stats",
                 "pagination"}
GROUP_KEYS = {"symbol_id", "provider", "dataset", "adjustment", "resource", "selected_check_id", "selection_reason",
              "state", "freshness", "coverage", "market_completeness", "absent_dates_reason", "last_attempt_at",
              "last_success_at", "last_full_recheck_at", "records"}
RANGE = ["--start", "2025-01-01", "--end", "2025-12-31"]


def count_checks(db: Path) -> int:
    """Count stored checks with a short-lived connection and return the count.

    :param db: Path; database path.
    return count: int; number of check rows.
    """
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT count(*) FROM checks").fetchone()[0]


class CliTestCase(unittest.TestCase):
    """Run main() against a temporary project root and capture standard output."""

    def setUp(self) -> None:
        """Point the CLI root at a temporary directory.

        return None: None; no value is returned.
        """
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patcher = mock.patch.object(market_data, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.session = build_world()
        client = mock.patch.object(market_data, "HttpClient",
                                   side_effect=lambda **kw: make_client(self.session, **kw))
        client.start()
        self.addCleanup(client.stop)

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


class ValidationTests(CliTestCase):
    """Check input validation and error envelopes."""

    def test_invalid_input_exits_2_with_envelope(self) -> None:
        """Bad symbols, ranges, selectors, and a missing --json flag give exit 2 and an error envelope."""
        cases = [
            (["collect", "AAPL", "--json"], "invalid_symbol"),
            (["collect", "600519", "--json", "--start", "2025-02-01", "--end", "2025-01-01"], "invalid_range"),
            (["collect", "600519", "--json", "--end", "2999-01-01"], "invalid_range"),
            (["collect", "600519"], "invalid_selector"),
            (["collect", "600519", "--json", "--max-requests", "0"], "invalid_selector"),
            (["collect", "600519", "--json", "--db", "../escape.sqlite3"], "invalid_path"),
            (["collect", "600519", "--json", "--filing", "../x.pdf"], "invalid_path"),
            (["show", "600519", "--json", "--dataset", "prices", "--scope", "all"], "invalid_selector"),
            (["show", "600519", "--json", "--dataset", "coverage", "--versions", "latest"], "invalid_selector"),
            (["show", "600519", "--json", "--dataset", "filings", "--provider", "sina"], "invalid_selector"),
            (["show", "600519", "--json", "--dataset", "prices", "--limit", "0"], "invalid_selector"),
        ]
        for argv, expected in cases:
            code, envelope = self.run_main(*argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(set(envelope), ENVELOPE_KEYS)
            self.assertEqual((envelope["status"], envelope["issues"][0]["code"]), ("error", expected), argv)
        self.assertFalse((self.root / "data").exists())

    def test_show_without_database_exits_1_and_creates_nothing(self) -> None:
        """Show reports database_not_found and does not create the database."""
        code, envelope = self.run_main("show", "600519", "--json", "--dataset", "prices")
        self.assertEqual((code, envelope["issues"][0]["code"]), (1, "database_not_found"))
        self.assertFalse((self.root / "data" / "market-data.sqlite3").exists())


class RoundTripTests(CliTestCase):
    """Check a collect followed by read-only show commands."""

    def test_collect_then_show(self) -> None:
        """Collect exits 0; show returns typed decimal strings, null keys, and pagination without writing."""
        code, collected = self.run_main("collect", "sh600519", "--json", *RANGE)
        self.assertEqual(code, 0, collected["issues"])
        self.assertEqual(collected["command"], "collect")
        self.assertIsNone(collected["pagination"])
        for group in collected["datasets"]:
            self.assertEqual(set(group), GROUP_KEYS)
        db = self.root / "data" / "market-data.sqlite3"
        before = count_checks(db)
        calls = len(self.session.calls)
        code, shown = self.run_main("show", "600519.SH", "--json", "--dataset", "prices", *RANGE, "--limit", "1")
        self.assertEqual(code, 0)
        self.assertIsNone(shown["run_id"])
        self.assertEqual(shown["stats"]["http_attempts"], 0)
        record = shown["datasets"][0]["records"][0]
        self.assertEqual(record["data"]["close"], "12")
        self.assertIn("amount_native", record["data"])
        self.assertIs(record["data"]["provisional"], False)
        self.assertEqual(shown["pagination"], {"limit": 1, "offset": 0, "returned": 1, "next_offset": 1})
        code, statements = self.run_main("show", "600519", "--json", "--dataset", "statements", "--kind", "balance",
                                         "--scope", "parent", *RANGE)
        self.assertEqual(code, 0)
        parent = [r for g in statements["datasets"] for r in g["records"]]
        self.assertTrue(parent and all(r["data"]["scope"] == "parent" for r in parent))
        self.assertIn("publication_date", parent[0]["data"])
        code, coverage = self.run_main("show", "600519", "--json", "--dataset", "coverage")
        self.assertEqual(code, 0)
        self.assertTrue(all(r["type"] == "check" for g in coverage["datasets"] for r in g["records"]))
        after = count_checks(db)
        self.assertEqual((before, len(self.session.calls)), (after, calls))


class SubprocessTests(unittest.TestCase):
    """Check the script entry point in a separate process."""

    def test_help_and_syntax_errors(self) -> None:
        """--help exits 0 and missing arguments exit 2 with usage text on standard error."""
        script = str(SCRIPTS / "market_data.py")
        ok = subprocess.run([sys.executable, "-B", script, "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(ok.returncode, 0)
        self.assertIn("collect", ok.stdout)
        bad = subprocess.run([sys.executable, "-B", script, "collect"], capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("usage", bad.stderr)


if __name__ == "__main__":
    unittest.main()
