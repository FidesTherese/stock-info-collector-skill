"""SQLite persistence: schema, daily check gates, observed revisions, and read-only queries.

The store never contacts the network. Every write happens in a short
transaction, and a failed transaction rolls back without touching older data.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .providers import (
    CacheState, CheckKey, CheckSummary, CodeLink, Coverage, DateRange, DiscoveryResult, FactRow, FetchResult,
    FilingResult, Group, Interval, Issue, JsonValue, MappingResult, OutputRecord, Pagination, PriceRow, QueryPage,
    QueryRequest, StatementRecord, StorageError, Symbol, compute_market_day, compute_sha256, convert_to_json,
    encode_canonical_json, format_utc, hash_json, make_issue, parse_utc,
)

SCHEMA_VERSION = 2
OUTPUT_BYTE_BUDGET = 8 * 1024 * 1024 - 64 * 1024
STALE_AFTER = timedelta(days=30)
VALID_STATES = ("succeeded", "partial")
SCHEMA_SQL = """
CREATE TABLE securities(symbol_id TEXT PRIMARY KEY, exchange TEXT NOT NULL,
 code TEXT NOT NULL, UNIQUE(exchange,code));
CREATE TABLE checks(id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
 symbol_id TEXT REFERENCES securities(symbol_id), provider TEXT NOT NULL,
 dataset TEXT NOT NULL, adjustment TEXT NOT NULL, resource TEXT NOT NULL DEFAULT '',
 gate_key TEXT UNIQUE, requested_start TEXT, requested_end TEXT, check_day TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,
 coverage_json TEXT NOT NULL, issues_json TEXT NOT NULL, metadata_json TEXT NOT NULL);
CREATE TABLE code_links(from_symbol TEXT NOT NULL REFERENCES securities(symbol_id),
 to_symbol TEXT NOT NULL REFERENCES securities(symbol_id), evidence_hash TEXT NOT NULL,
 evidence_url TEXT NOT NULL, effective_date TEXT, evidence_json TEXT NOT NULL,
 first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, check_id INTEGER NOT NULL REFERENCES checks(id),
 PRIMARY KEY(from_symbol,to_symbol,evidence_hash));
CREATE TABLE prices(check_id INTEGER NOT NULL REFERENCES checks(id), trade_date TEXT NOT NULL,
 content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(check_id,trade_date));
CREATE TABLE statements(check_id INTEGER NOT NULL REFERENCES checks(id), record_key TEXT NOT NULL,
 report_date TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
 content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(check_id,record_key));
CREATE TABLE filings(check_id INTEGER PRIMARY KEY REFERENCES checks(id), uri TEXT NOT NULL,
 format TEXT, body_sha256 TEXT, status TEXT NOT NULL, reference_json TEXT NOT NULL,
 metadata_json TEXT NOT NULL);
CREATE INDEX checks_lookup ON checks(symbol_id,provider,dataset,adjustment,resource,id);
CREATE INDEX prices_date ON prices(trade_date);
CREATE INDEX statements_date ON statements(report_date,kind,scope);
"""
# Schema version 2 adds the values that companies reported to the SEC. A version 1 database gets this
# table when a writable store opens it.
FACTS_SQL = """
CREATE TABLE facts(check_id INTEGER NOT NULL REFERENCES checks(id), fact_key TEXT NOT NULL,
 taxonomy TEXT NOT NULL, concept TEXT NOT NULL, label TEXT, unit TEXT NOT NULL, period_start TEXT,
 period_end TEXT NOT NULL, accession TEXT NOT NULL, form TEXT, filed TEXT,
 content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(check_id, fact_key));
CREATE INDEX facts_concept ON facts(concept, period_end);
CREATE INDEX facts_key ON facts(fact_key);
"""
_STATEMENT_DATASETS = ("income", "balance", "cash_flow")


def parse_symbol_id(symbol_id: str) -> Symbol:
    """Split a stored canonical identifier such as `SH:600519` into a Symbol and return it.

    :param symbol_id: str; canonical identifier.
    return symbol: Symbol; identity record.
    """
    exchange, code = symbol_id.split(":", 1)
    return Symbol(symbol_id=symbol_id, exchange=exchange, code=code)  # type: ignore[arg-type]


def load_coverage(value: Any) -> Coverage:
    """Rebuild a Coverage record from its stored JSON object and return it.

    :param value: Any; object produced by convert_to_json(Coverage).
    return coverage: Coverage; rebuilt record.
    """
    def to_date(text: Any) -> date | None:
        """Parse an ISO date or return None.

        :param text: Any; ISO date text or None.
        return day: date | None; parsed date.
        """
        return date.fromisoformat(text) if text else None

    requested = value.get("requested")
    return Coverage(
        requested=DateRange(date.fromisoformat(requested["start"]), date.fromisoformat(requested["end"]))
        if requested else None,
        fetched=tuple(Interval(date.fromisoformat(i["start"]), date.fromisoformat(i["end"]), i["outcome"],
                               i["row_count"]) for i in value.get("fetched", [])),
        unfetched=tuple(DateRange(date.fromisoformat(r["start"]), date.fromisoformat(r["end"]))
                        for r in value.get("unfetched", [])),
        observed_start=to_date(value.get("observed_start")), observed_end=to_date(value.get("observed_end")),
        row_count=int(value.get("row_count", 0)), traversal=value.get("traversal", "failed"))


def load_issue(value: Any) -> Issue:
    """Rebuild an Issue from its stored JSON object and return it.

    :param value: Any; object produced by convert_to_json(Issue).
    return issue: Issue; rebuilt record.
    """
    report_date = value.get("report_date")
    return Issue(code=value["code"], message=value["message"], severity=value["severity"], dataset=value["dataset"],
                 symbol_id=value.get("symbol_id"), provider=value.get("provider"), url=value.get("url"),
                 report_date=date.fromisoformat(report_date) if report_date else None,
                 retryable=bool(value.get("retryable")), cached_preserved=bool(value.get("cached_preserved", True)))


def compute_gate_key(key: CheckKey, day: date) -> str:
    """Hash a check identity and a market day into the daily gate key and return it.

    :param key: CheckKey; task identity.
    :param day: date; market calendar day, see providers.compute_market_day.
    return gate_key: str; SHA-256 hex digest.
    """
    return hash_json([key, day])


def compute_fact_key(record: FactRow) -> str:
    """Hash the business identity of a reported value and return the fact key.

    :param record: FactRow; reported value.
    return fact_key: str; SHA-256 over group, concept, unit, period start, period end, and filing identifier.
    """
    return hash_json([record.taxonomy, record.concept, record.unit, record.period_start, record.period_end,
                      record.accession])


def compute_record_key(record: StatementRecord) -> str:
    """Hash the business identity of a statement record and return the record key.

    :param record: StatementRecord; statement record.
    return record_key: str; SHA-256 over report_date, kind, scope, source_scope, and source_key.
    """
    return hash_json([record.report_date, record.kind, record.scope, record.source_scope, record.source_key])


def intersects(span: DateRange | None, other: DateRange | None) -> bool:
    """Report whether two inclusive ranges overlap; a missing range overlaps everything.

    :param span: DateRange | None; first range.
    :param other: DateRange | None; second range.
    return overlap: bool; True when the ranges overlap.
    """
    if span is None or other is None:
        return True
    return span.start <= other.end and other.start <= span.end


def encloses(outer: DateRange | None, inner: DateRange) -> bool:
    """Report whether `outer` contains every day of `inner`.

    :param outer: DateRange | None; candidate enclosing range.
    :param inner: DateRange; requested range.
    return enclosed: bool; True when outer covers inner.
    """
    return outer is not None and outer.start <= inner.start and outer.end >= inner.end


class Store:
    """Own one SQLite connection and provide gated checks, atomic finalization, and queries.

    A writable store creates and initializes a new empty database, opens a
    database whose `user_version` is 2, or upgrades a version 1 database by adding
    the `facts` table. Any other version raises StorageError
    `schema_version_unsupported`. A read-only store opens with `mode=ro`, never
    creates or upgrades a file (missing file raises `database_not_found`), accepts
    versions 1 and 2, and keeps one read transaction open until `close()` so that
    a show command sees one snapshot. `has_facts` is False only for a read-only
    version 1 database; queries for reported values then return nothing.
    Methods must not be called concurrently.

    :param path: Path; database file path.
    :param readonly: bool; True opens an existing database for reading only.
    return instance: Store; open store.
    """

    def __init__(self, path: Path, *, readonly: bool = False) -> None:
        """Open the database and verify or install the schema.

        :param path: Path; database file path.
        :param readonly: bool; True opens read-only.
        return None: None; no value is returned.
        """
        self.path = path
        self.readonly = readonly
        self.new_revisions = 0
        self.reused_revisions = 0
        self.has_facts = True
        self.conn: sqlite3.Connection = None  # type: ignore[assignment]
        try:
            if readonly:
                if not path.is_file():
                    raise StorageError(make_issue("database_not_found", "the database file does not exist", "prices"))
                self.conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None,
                                            timeout=5.0)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.conn = sqlite3.connect(str(path), isolation_level=None, timeout=5.0)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA busy_timeout=5000")
            if readonly:
                self.conn.execute("BEGIN")
            self._verify_schema()
        except sqlite3.Error as exc:
            self.close()
            raise self._wrap(exc) from None
        except BaseException:
            self.close()
            raise

    def _verify_schema(self) -> None:
        """Install the schema into an empty database, upgrade version 1, or reject an unknown schema version.

        return None: None; no value is returned.
        """
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        if version == 1 and self.readonly:
            self.has_facts = False
            return
        tables = self.conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        if not self.readonly and (version == 1 or (version == 0 and tables == 0)):
            script = FACTS_SQL if version == 1 else SCHEMA_SQL + FACTS_SQL
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in script.split(";"):
                    if statement.strip():
                        self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                self.conn.execute("COMMIT")
            except sqlite3.Error:
                self.conn.execute("ROLLBACK")
                raise
            return
        raise StorageError(make_issue("schema_version_unsupported",
                                      f"database schema version {version} is not supported", "prices"))

    @staticmethod
    def _wrap(exc: sqlite3.Error) -> StorageError:
        """Convert a sqlite3 error into a StorageError and return it.

        :param exc: sqlite3.Error; original error.
        return error: StorageError; `database_busy` for locks, otherwise `storage_error`.
        """
        text = str(exc)
        code = "database_busy" if "locked" in text or "busy" in text else "storage_error"
        return StorageError(make_issue(code, f"SQLite error: {text}", "prices", retryable=code == "database_busy"))

    def close(self) -> None:
        """End any open read transaction and close the connection.

        return None: None; no value is returned.
        """
        conn = getattr(self, "conn", None)
        if conn is None:
            return
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.close()
            self.conn = None  # type: ignore[assignment]

    # -- writes ------------------------------------------------------------

    def _ensure_security(self, symbol: Symbol) -> None:
        """Insert a securities row when it does not exist yet.

        :param symbol: Symbol; identity to insert.
        return None: None; no value is returned.
        """
        self.conn.execute("INSERT OR IGNORE INTO securities(symbol_id, exchange, code) VALUES (?,?,?)",
                          (symbol.symbol_id, symbol.exchange, symbol.code))

    def start_check(self, key: CheckKey, span: DateRange | None, run_id: str, now: datetime, *,
                    refresh: bool = False) -> int | None:
        """Reserve today's check for a task and return its id, or None when today's gate is already used.

        The gate key combines the task identity and the market day (the New York
        day for a US symbol or the SEC, otherwise the China day), so only one
        ordinary attempt per task and day can exist, whatever its outcome. With
        `refresh=True` the gate key is NULL and the reservation always succeeds. The
        row starts in state `running`; an interrupted run leaves it running, and such
        rows are never selected as data.

        :param key: CheckKey; task identity.
        :param span: DateRange | None; range that the task will visit.
        :param run_id: str; UUID4 of the current command.
        :param now: datetime; aware UTC time.
        :param refresh: bool; True bypasses the daily gate.
        return check_id: int | None; new check id, or None when gated.
        """
        if self.readonly:
            raise StorageError(make_issue("storage_error", "read-only store cannot start checks", key.dataset))
        day = compute_market_day(key.symbol_id, now, key.provider)
        gate = None if refresh else compute_gate_key(key, day)
        empty = encode_canonical_json(convert_to_json(Coverage(requested=span, traversal="running")))
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            if key.symbol_id is not None:
                self._ensure_security(parse_symbol_id(key.symbol_id))
            cursor = self.conn.execute(
                "INSERT INTO checks(run_id, symbol_id, provider, dataset, adjustment, resource, gate_key,"
                " requested_start, requested_end, check_day, started_at, finished_at, state, coverage_json,"
                " issues_json, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,'running',?,'[]','null')",
                (run_id, key.symbol_id, key.provider, key.dataset, key.adjustment, key.resource, gate,
                 span.start.isoformat() if span else None, span.end.isoformat() if span else None,
                 day.isoformat(), format_utc(now), empty))
            self.conn.execute("COMMIT")
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK")
            return None
        except sqlite3.Error as exc:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise self._wrap(exc) from None

    def finish_check(self, check_id: int, result: FetchResult | MappingResult | DiscoveryResult | FilingResult,
                     now: datetime) -> CheckSummary:
        """Store a task result and finalize its running check atomically, then return the summary.

        The result kind must match the check dataset (prices, statements, and facts
        take a FetchResult, mapping a MappingResult, discovery a DiscoveryResult,
        filing a FilingResult whose reference URI equals the check resource). Records
        are inserted with content hashes; a revision counts as reused when an earlier
        check of the same task stored the same content hash for the same business
        key. Unchanged code-link evidence only updates `last_seen` and `check_id`.
        Reported values (facts) are stored differently: only new and changed values
        are inserted, see _insert_facts. Any failure rolls back the whole transaction
        and raises StorageError.

        :param check_id: int; running check returned by start_check.
        :param result: FetchResult | MappingResult | DiscoveryResult | FilingResult; task outcome.
        :param now: datetime; aware UTC finish time.
        return summary: CheckSummary; finalized check.
        """
        if result.state == "running":
            raise StorageError(make_issue("storage_error", "a result cannot be stored as running", "prices"))
        dataset = "prices"
        try:
            row = self.conn.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
            if row is None or row["state"] != "running":
                raise StorageError(make_issue("storage_error", f"check {check_id} is not running", "prices"))
            key = self._row_key(row)
            dataset = key.dataset
            self._validate_result(key, result)
            self.conn.execute("BEGIN IMMEDIATE")
            counts = [0, 0]
            metadata: JsonValue = result.metadata
            issues = list(result.issues)
            if isinstance(result, FetchResult) and key.dataset == "prices":
                self._insert_prices(check_id, key, result.records, counts)
            elif isinstance(result, FetchResult) and key.dataset == "facts":
                summary = self._insert_facts(check_id, key, result.records, counts)
                base = result.metadata if isinstance(result.metadata, dict) else {}
                metadata = {**base, **summary}
                if summary["absent_keys"] and result.state == "succeeded":
                    issues.append(make_issue(
                        "values_missing_in_recheck", f"{summary['absent_keys']} stored values were not returned by"
                        " this check; they are kept", "facts", severity="warning", provider=key.provider))
            elif isinstance(result, FetchResult):
                self._insert_statements(check_id, key, result.records, counts)
            elif isinstance(result, MappingResult):
                self._insert_links(check_id, result.links, now)
            elif isinstance(result, DiscoveryResult):
                base = result.metadata if isinstance(result.metadata, dict) else {"source": result.metadata}
                metadata = {**base, "candidates": convert_to_json(result.candidates)}
            elif isinstance(result, FilingResult):
                self._insert_statements(check_id, key, result.statements, counts)
                filing_meta = convert_to_json(result.metadata)
                self.conn.execute(
                    "INSERT INTO filings(check_id, uri, format, body_sha256, status, reference_json, metadata_json)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (check_id, key.resource, result.format, result.body_sha256, result.status,
                     encode_canonical_json(convert_to_json(result.reference)), encode_canonical_json(filing_meta)))
            self.conn.execute(
                "UPDATE checks SET finished_at=?, state=?, coverage_json=?, issues_json=?, metadata_json=?"
                " WHERE id=? AND state='running'",
                (format_utc(now), result.state, encode_canonical_json(convert_to_json(result.coverage)),
                 encode_canonical_json(convert_to_json(issues)),
                 encode_canonical_json(convert_to_json(metadata)), check_id))
            self.conn.execute("COMMIT")
            self.new_revisions += counts[0]
            self.reused_revisions += counts[1]
        except StorageError:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        except (sqlite3.Error, ValueError, TypeError) as exc:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise self._wrap(exc) from None
            raise StorageError(make_issue("storage_error", f"invalid result: {exc}", dataset)) from None
        return self._summarize(self.conn.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone())

    @staticmethod
    def _validate_result(key: CheckKey, result: Any) -> None:
        """Check that the result kind and records match the check dataset, or raise StorageError.

        :param key: CheckKey; check identity.
        :param result: Any; task outcome.
        return None: None; no value is returned.
        """
        expected: dict[str, type] = {"prices": FetchResult, "income": FetchResult, "balance": FetchResult,
                                     "cash_flow": FetchResult, "facts": FetchResult, "mapping": MappingResult,
                                     "discovery": DiscoveryResult, "filing": FilingResult}
        ok = key.dataset in expected and isinstance(result, expected[key.dataset])
        if ok and key.dataset == "prices":
            ok = all(isinstance(r, PriceRow) for r in result.records)
        elif ok and key.dataset == "facts":
            ok = all(isinstance(r, FactRow) for r in result.records)
        elif ok and key.dataset in _STATEMENT_DATASETS:
            ok = all(isinstance(r, StatementRecord) and r.kind == key.dataset for r in result.records)
        elif ok and key.dataset == "filing":
            ok = result.reference is not None and result.reference.uri == key.resource
        if not ok:
            raise StorageError(make_issue("storage_error", f"result does not match dataset {key.dataset}",
                                          key.dataset))

    def _previous_hash_exists(self, table: str, column: str, value: str, key: CheckKey, content_hash: str,
                              check_id: int) -> bool:
        """Report whether an earlier check of the same task stored this content hash for this business key.

        :param table: str; `prices` or `statements`.
        :param column: str; business-key column, `trade_date` or `record_key`.
        :param value: str; business-key value.
        :param key: CheckKey; task identity.
        :param content_hash: str; content hash.
        :param check_id: int; current check, excluded from the search.
        return found: bool; True when the revision was already stored.
        """
        sql = (f"SELECT 1 FROM {table} t JOIN checks c ON c.id=t.check_id WHERE t.{column}=? AND t.content_hash=?"
               " AND c.id<>? AND c.symbol_id IS ? AND c.provider=? AND c.dataset=? AND c.adjustment=? LIMIT 1")
        return self.conn.execute(sql, (value, content_hash, check_id, key.symbol_id, key.provider, key.dataset,
                                       key.adjustment)).fetchone() is not None

    def _insert_prices(self, check_id: int, key: CheckKey, records: Sequence[PriceRow], counts: list[int]) -> None:
        """Insert price rows for a check and update the revision counters.

        :param check_id: int; check id.
        :param key: CheckKey; task identity.
        :param records: Sequence[PriceRow]; validated rows.
        :param counts: list[int]; [new, reused] counters to update.
        return None: None; no value is returned.
        """
        for row in records:
            payload = convert_to_json(row)
            day = row.trade_date.isoformat()
            content_hash = hash_json([key.symbol_id, key.provider, key.dataset, key.adjustment, day, payload])
            reused = self._previous_hash_exists("prices", "trade_date", day, key, content_hash, check_id)
            counts[1 if reused else 0] += 1
            self.conn.execute("INSERT INTO prices(check_id, trade_date, content_hash, payload_json) VALUES (?,?,?,?)",
                              (check_id, day, content_hash, encode_canonical_json(payload)))

    def _insert_facts(self, check_id: int, key: CheckKey, records: Sequence[FactRow],
                      counts: list[int]) -> dict[str, JsonValue]:
        """Insert the reported values that are new or changed for the symbol and return a storage summary.

        A company has tens of thousands of reported values and they rarely change,
        so a check stores only the values whose content differs from the newest
        stored version of the same fact key. The newest stored row of a key is its
        latest version. A stored value that this check did not return stays stored
        and is counted. Two different values with the same key in one check both
        stay: the later one gets a key that also covers its content.

        Output summary: `stored_rows: int` (new and changed values inserted),
        `unchanged_rows: int` (values equal to the stored version), and
        `absent_keys: int` (stored keys that this check did not return).

        :param check_id: int; check id.
        :param key: CheckKey; task identity.
        :param records: Sequence[FactRow]; validated values of one response.
        :param counts: list[int]; [new, reused] counters to update.
        return summary: dict[str, JsonValue]; counts following the output summary above.
        """
        known: dict[str, str] = {}
        sql = ("SELECT f.fact_key, f.content_hash FROM facts f JOIN checks c ON c.id=f.check_id"
               " WHERE c.symbol_id IS ? AND c.provider=? AND c.dataset='facts' ORDER BY f.check_id")
        for row in self.conn.execute(sql, (key.symbol_id, key.provider)):
            known[row["fact_key"]] = row["content_hash"]
        seen: set[str] = set()
        inserts: list[tuple[Any, ...]] = []
        unchanged = 0
        for record in records:
            payload = convert_to_json(record)
            fact_key = compute_fact_key(record)
            content_hash = hash_json([key.symbol_id, key.provider, fact_key, payload])
            if fact_key in seen:
                fact_key = hash_json([fact_key, content_hash])
                if fact_key in seen:
                    continue
            seen.add(fact_key)
            if known.get(fact_key) == content_hash:
                unchanged += 1
                continue
            inserts.append((check_id, fact_key, record.taxonomy, record.concept, record.label, record.unit,
                            record.period_start.isoformat() if record.period_start else None,
                            record.period_end.isoformat(), record.accession, record.form,
                            record.filed.isoformat() if record.filed else None, content_hash,
                            encode_canonical_json(payload)))
        self.conn.executemany(
            "INSERT INTO facts(check_id, fact_key, taxonomy, concept, label, unit, period_start, period_end,"
            " accession, form, filed, content_hash, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", inserts)
        counts[0] += len(inserts)
        counts[1] += unchanged
        return {"stored_rows": len(inserts), "unchanged_rows": unchanged, "absent_keys": len(set(known) - seen)}

    def list_fact_filings(self, symbol: Symbol) -> list[tuple[str, str | None, str | None, str, str, int]]:
        """List the filings named by the stored reported values of a symbol and return one row per period and group.

        The rows feed sec.plan_sec_filings, which derives the periodic reports of
        the company and estimates each report's period end.

        :param symbol: Symbol; US symbol.
        return rows: list[tuple[str, str | None, str | None, str, str, int]]; (filing identifier, form, filing
            date as ISO text, period end as ISO text, group, number of values); empty when nothing is stored.
        """
        if not self.has_facts:
            return []
        sql = ("SELECT f.accession, f.form, f.filed, f.period_end, f.taxonomy, COUNT(DISTINCT f.fact_key) AS n"
               " FROM facts f JOIN checks c ON c.id=f.check_id WHERE c.symbol_id=? AND c.provider='sec'"
               " AND c.dataset='facts' GROUP BY f.accession, f.form, f.filed, f.period_end, f.taxonomy")
        try:
            return [(row["accession"], row["form"], row["filed"], row["period_end"], row["taxonomy"], row["n"])
                    for row in self.conn.execute(sql, (symbol.symbol_id,))]
        except sqlite3.Error as exc:
            raise self._wrap(exc) from None

    def _insert_statements(self, check_id: int, key: CheckKey, records: Sequence[StatementRecord],
                           counts: list[int]) -> None:
        """Insert statement records for a check and update the revision counters.

        :param check_id: int; check id.
        :param key: CheckKey; task identity.
        :param records: Sequence[StatementRecord]; statement records.
        :param counts: list[int]; [new, reused] counters to update.
        return None: None; no value is returned.
        """
        for record in records:
            payload = convert_to_json(record)
            record_key = compute_record_key(record)
            content_hash = hash_json([key.symbol_id, key.provider, key.dataset, key.adjustment, record_key, payload])
            reused = self._previous_hash_exists("statements", "record_key", record_key, key, content_hash, check_id)
            counts[1 if reused else 0] += 1
            self.conn.execute(
                "INSERT INTO statements(check_id, record_key, report_date, kind, scope, content_hash, payload_json)"
                " VALUES (?,?,?,?,?,?,?)",
                (check_id, record_key, record.report_date.isoformat(), record.kind, record.scope, content_hash,
                 encode_canonical_json(payload)))

    def _insert_links(self, check_id: int, links: Sequence[CodeLink], now: datetime) -> None:
        """Insert new code-link evidence or refresh `last_seen` for unchanged evidence.

        :param check_id: int; mapping check id.
        :param links: Sequence[CodeLink]; verified links.
        :param now: datetime; aware UTC observation time.
        return None: None; no value is returned.
        """
        for link in links:
            self._ensure_security(link.from_symbol)
            self._ensure_security(link.to_symbol)
            params = (link.from_symbol.symbol_id, link.to_symbol.symbol_id, link.evidence_hash)
            exists = self.conn.execute("SELECT 1 FROM code_links WHERE from_symbol=? AND to_symbol=? AND"
                                       " evidence_hash=?", params).fetchone()
            if exists:
                self.conn.execute("UPDATE code_links SET last_seen=?, check_id=? WHERE from_symbol=? AND to_symbol=?"
                                  " AND evidence_hash=?", (format_utc(now), check_id, *params))
                continue
            evidence = {"headers": list(link.headers), "cells": list(link.cells)}
            self.conn.execute(
                "INSERT INTO code_links(from_symbol, to_symbol, evidence_hash, evidence_url, effective_date,"
                " evidence_json, first_seen, last_seen, check_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (*params, link.evidence_url, None, encode_canonical_json(evidence), format_utc(now),
                 format_utc(now), check_id))

    # -- reads -------------------------------------------------------------

    @staticmethod
    def _row_key(row: sqlite3.Row) -> CheckKey:
        """Build the CheckKey of a stored check row and return it.

        :param row: sqlite3.Row; checks row.
        return key: CheckKey; task identity.
        """
        return CheckKey(symbol_id=row["symbol_id"], provider=row["provider"], dataset=row["dataset"],
                        adjustment=row["adjustment"], resource=row["resource"])

    def _summarize(self, row: sqlite3.Row) -> CheckSummary:
        """Convert a stored check row into a CheckSummary and return it.

        :param row: sqlite3.Row; checks row.
        return summary: CheckSummary; check summary.
        """
        span = None
        if row["requested_start"]:
            span = DateRange(date.fromisoformat(row["requested_start"]), date.fromisoformat(row["requested_end"]))
        return CheckSummary(
            check_id=row["id"], key=self._row_key(row), span=span, state=row["state"],
            started_at=parse_utc(row["started_at"]),
            finished_at=parse_utc(row["finished_at"]) if row["finished_at"] else None,
            coverage=load_coverage(json.loads(row["coverage_json"])),
            issues=tuple(load_issue(i) for i in json.loads(row["issues_json"])),
            metadata=json.loads(row["metadata_json"]))

    def read_links(self) -> tuple[CodeLink, ...]:
        """Read every stored code link and return them sorted by old and new code.

        return links: tuple[CodeLink, ...]; stored links.
        """
        links = []
        for row in self.conn.execute("SELECT * FROM code_links ORDER BY from_symbol, to_symbol, evidence_hash"):
            evidence = json.loads(row["evidence_json"])
            links.append(CodeLink(
                from_symbol=parse_symbol_id(row["from_symbol"]), to_symbol=parse_symbol_id(row["to_symbol"]),
                evidence_url=row["evidence_url"], headers=tuple(evidence["headers"]), cells=tuple(evidence["cells"]),
                effective_date=None, evidence_hash=row["evidence_hash"], first_seen=parse_utc(row["first_seen"]),
                last_seen=parse_utc(row["last_seen"])))
        return tuple(links)

    def read_state(self, symbols: Sequence[Symbol]) -> CacheState:
        """Read stored links and the latest attempt and success of each task for the symbols and return them.

        The returned checks contain, per CheckKey, the latest attempt (any state) and
        the latest succeeded check when it differs. Discovery checks carry their
        candidate list in `metadata["candidates"]`.

        :param symbols: Sequence[Symbol]; symbols of interest.
        return state: CacheState; stored links and selected check summaries ordered by id.
        """
        try:
            ids = [s.symbol_id for s in symbols]
            marks = ",".join("?" for _ in ids) or "NULL"
            latest: dict[CheckKey, sqlite3.Row] = {}
            success: dict[CheckKey, sqlite3.Row] = {}
            sql = f"SELECT * FROM checks WHERE symbol_id IN ({marks}) OR symbol_id IS NULL ORDER BY id"
            for row in self.conn.execute(sql, ids):
                key = self._row_key(row)
                latest[key] = row
                if row["state"] == "succeeded":
                    success[key] = row
            rows = {row["id"]: row for row in list(latest.values()) + list(success.values())}
            checks = tuple(self._summarize(rows[i]) for i in sorted(rows))
            return CacheState(links=self.read_links(), checks=checks)
        except sqlite3.Error as exc:
            raise self._wrap(exc) from None

    def query(self, request: QueryRequest, now: datetime, *, symbols: Sequence[Symbol]) -> QueryPage:
        """Select stored records for a request and return one page of groups without changing the database.

        Records are streamed from SQL one row at a time. `offset` and `limit` count
        records across all groups in output order; `next_offset` is the first whole
        record that was not returned. A record that alone exceeds the output budget
        is skipped with an `output_size_limit` issue. Empty results are never proof
        that data does not exist. See the DTD "CLI and JSON" section for the
        selection rules per dataset.

        :param request: QueryRequest; validated query.
        :param now: datetime; aware UTC time used for freshness and provisional rows.
        :param symbols: Sequence[Symbol]; requested symbol followed by verified aliases.
        return page: QueryPage; groups, pagination, and issues.
        """
        try:
            builder = _PageBuilder(request.limit, request.offset)
            groups: list[tuple[Group, Callable[[], Iterator[OutputRecord]] | None]] = []
            for symbol in symbols:
                if request.dataset == "prices":
                    groups.extend(self._plan_price_groups(symbol, request, now, builder.issues))
                elif request.dataset == "statements":
                    groups.extend(self._plan_statement_groups(symbol, request, now))
                elif request.dataset == "filings":
                    groups.append(self._plan_filing_group(symbol, request, now, builder.issues))
                elif request.dataset == "facts":
                    groups.extend(self._plan_fact_groups(symbol, request, now))
                else:
                    groups.extend(self._plan_coverage_groups(symbol, request, now))
            if request.dataset == "coverage" and request.provider in ("auto", "bse") \
                    and request.symbol.exchange != "US":
                groups.extend(self._plan_coverage_groups(None, request, now))
            for group, produce in groups:
                if produce is None or builder.done:
                    continue
                for record in produce():
                    if not builder.offer(group, record):
                        break
            pagination = Pagination(limit=request.limit, offset=request.offset, returned=builder.returned,
                                    next_offset=builder.next_offset)
            return QueryPage(groups=[g for g, _ in groups], pagination=pagination,
                             issues=[convert_to_json(i) for i in builder.issues])  # type: ignore[misc]
        except sqlite3.Error as exc:
            raise self._wrap(exc) from None

    # -- query helpers -------------------------------------------------------

    def _load_checks(self, symbol_id: str | None, provider: str | None, dataset: str | Sequence[str],
                     adjustment: str | None = None) -> list[CheckSummary]:
        """Load every check that matches the filters, ordered by id, and return the summaries.

        :param symbol_id: str | None; symbol, or None for global checks.
        :param provider: str | None; provider filter, or None for every provider.
        :param dataset: str | Sequence[str]; dataset or datasets.
        :param adjustment: str | None; adjustment filter, or None.
        return checks: list[CheckSummary]; matching checks.
        """
        datasets = [dataset] if isinstance(dataset, str) else list(dataset)
        sql = f"SELECT * FROM checks WHERE symbol_id IS ? AND dataset IN ({','.join('?' for _ in datasets)})"
        params: list[Any] = [symbol_id, *datasets]
        if provider is not None:
            sql += " AND provider=?"
            params.append(provider)
        if adjustment is not None:
            sql += " AND adjustment=?"
            params.append(adjustment)
        return [self._summarize(row) for row in self.conn.execute(sql + " ORDER BY id", params)]

    def _describe_group(self, symbol_id: str | None, provider: str, dataset: str, adjustment: str, resource: str,
                        checks: list[CheckSummary], selected: CheckSummary | None, reason: str,
                        span: DateRange, now: datetime) -> Group:
        """Build a group header from its checks and selection and return it.

        Freshness is `never_fetched` without checks; `fresh` when a succeeded check
        from today's market day covers the span and no later attempt failed; otherwise
        `stale`. A last success older than 30 days is always stale. The market day is
        the New York day for a US symbol or the SEC, otherwise the China day.

        :param symbol_id: str | None; group symbol.
        :param provider: str; group provider.
        :param dataset: str; group dataset.
        :param adjustment: str; group adjustment.
        :param resource: str; group resource.
        :param checks: list[CheckSummary]; every check of the group, ordered by id.
        :param selected: CheckSummary | None; selected check.
        :param reason: str; selection reason.
        :param span: DateRange; requested range.
        :param now: datetime; aware UTC time.
        return group: Group; group header with an empty record list.
        """
        finished = [c for c in checks if c.finished_at is not None]
        successes = [c for c in finished if c.state == "succeeded"]
        last_attempt = checks[-1] if checks else None
        last_success = successes[-1] if successes else None
        if not checks:
            freshness = "never_fetched"
        else:
            today = compute_market_day(symbol_id, now, provider)
            fresh = (last_success is not None and last_success.finished_at is not None
                     and compute_market_day(symbol_id, last_success.finished_at, provider) == today
                     and (last_success.span is None or encloses(last_success.span, span) or dataset == "filing")
                     and all(c.state in VALID_STATES or c.state == "running"
                             for c in finished if c.check_id > last_success.check_id)
                     and now - last_success.finished_at <= STALE_AFTER)
            freshness = "fresh" if fresh else "stale"
        state = selected.state if selected else (last_attempt.state if last_attempt else "blocked")
        coverage = selected.coverage if selected else Coverage(requested=span, traversal="blocked")
        attempt_time = None
        if last_attempt is not None:
            attempt_time = last_attempt.finished_at or last_attempt.started_at
        return Group(
            symbol_id=symbol_id, provider=provider, dataset=dataset, adjustment=adjustment, resource=resource,
            selected_check_id=selected.check_id if selected else None, selection_reason=reason, state=state,
            freshness=freshness, coverage=convert_to_json(coverage),  # type: ignore[typeddict-item]
            market_completeness="unverified", absent_dates_reason="unclassified",
            last_attempt_at=format_utc(attempt_time) if attempt_time else None,
            last_success_at=format_utc(last_success.finished_at) if last_success and last_success.finished_at
            else None,
            last_full_recheck_at=format_utc(last_success.finished_at) if last_success and last_success.finished_at
            else None,
            records=[])

    def _plan_price_groups(self, symbol: Symbol, request: QueryRequest, now: datetime,
                           issues: list[Issue]) -> list[tuple[Group, Callable[[], Iterator[OutputRecord]] | None]]:
        """Select the price provider and check for one symbol and return the planned group.

        Raw prices select the latest valid (succeeded or partial) non-empty check.
        Forward prices require one succeeded non-empty snapshot that encloses the
        requested range. `auto` prefers a fresh covering Eastmoney check, then a fresh
        covering Tencent check, then the latest successful covering check (Eastmoney
        wins ties), then for raw prices the latest partial check.

        :param symbol: Symbol; symbol.
        :param request: QueryRequest; validated query.
        :param now: datetime; aware UTC time.
        :param issues: list[Issue]; page issue list.
        return groups: list[tuple[Group, Callable | None]]; one planned group.
        """
        span, adjustment = request.span, request.adjustment
        providers = ["eastmoney", "tencent"] if request.provider == "auto" else [request.provider]
        by_provider = {p: self._load_checks(symbol.symbol_id, p, "prices", adjustment) for p in providers}
        today = compute_market_day(symbol.symbol_id, now)

        def usable(check: CheckSummary) -> bool:
            """Report whether a check is a finished, valid, non-empty price snapshot for this request.

            :param check: CheckSummary; candidate check.
            return ok: bool; True when selectable.
            """
            if check.finished_at is None or check.coverage.row_count == 0:
                return False
            if adjustment == "forward":
                return check.state == "succeeded" and encloses(check.span, span)
            return check.state in VALID_STATES

        selected: CheckSummary | None = None
        reason = "no_usable_check"
        chosen = providers[0]
        candidates = {p: [c for c in checks if usable(c)] for p, checks in by_provider.items()}
        for provider in providers:
            fresh = [c for c in candidates[provider] if c.state == "succeeded" and encloses(c.span, span)
                     and c.finished_at and compute_market_day(symbol.symbol_id, c.finished_at) == today]
            if fresh:
                selected, reason, chosen = fresh[-1], "fresh_covering", provider
                break
        if selected is None:
            covering = [c for p in providers for c in candidates[p] if c.state == "succeeded"
                        and encloses(c.span, span)]
            if covering:
                selected = max(covering, key=lambda c: (c.finished_at, c.key.provider == "eastmoney", c.check_id))
                reason, chosen = "latest_successful_covering", selected.key.provider
        if selected is None and adjustment == "raw":
            partial = [c for p in providers for c in candidates[p]]
            if partial:
                selected = max(partial, key=lambda c: (c.finished_at, c.key.provider == "eastmoney", c.check_id))
                reason, chosen = "latest_partial", selected.key.provider
        if selected is None and adjustment == "forward":
            issues.append(make_issue("no_enclosing_forward_snapshot",
                                     "no succeeded forward-adjusted snapshot covers the requested range", "prices",
                                     severity="warning", symbol=symbol))
        if request.provider != "auto" and selected is not None:
            reason = f"explicit_provider:{reason}"
        group = self._describe_group(symbol.symbol_id, chosen, "prices", adjustment, "", by_provider[chosen],
                                     selected, reason, span, now)
        if selected is None:
            return [(group, None)]
        chosen_check = selected

        def produce() -> Iterator[OutputRecord]:
            """Yield the output price records of the selected provider in date order.

            return records: Iterator[OutputRecord]; price records.
            """
            if adjustment == "forward":
                yield from self._iter_forward_prices(symbol, chosen, chosen_check, request, today)
            else:
                yield from self._iter_raw_prices(symbol, chosen, chosen_check, request, today, issues)

        return [(group, produce)]

    def _iter_forward_prices(self, symbol: Symbol, provider: str, selected: CheckSummary, request: QueryRequest,
                             today: date) -> Iterator[OutputRecord]:
        """Yield forward-adjusted rows from one snapshot (latest) or from every succeeded snapshot (all).

        Snapshots are never spliced: each record comes from exactly one check.

        :param symbol: Symbol; symbol.
        :param provider: str; provider.
        :param selected: CheckSummary; selected enclosing snapshot.
        :param request: QueryRequest; validated query.
        :param today: date; market day for the provisional flag.
        return records: Iterator[OutputRecord]; price records.
        """
        if request.versions == "latest":
            check_filter, params = "c.id=?", [selected.check_id]
        else:
            check_filter, params = "c.symbol_id=? AND c.provider=? AND c.dataset='prices' AND c.adjustment='forward'" \
                                   " AND c.state='succeeded'", [symbol.symbol_id, provider]
        sql = ("SELECT p.check_id, c.finished_at, p.trade_date, p.content_hash, p.payload_json FROM prices p"
               f" JOIN checks c ON c.id=p.check_id WHERE {check_filter} AND p.trade_date BETWEEN ? AND ?"
               " ORDER BY p.trade_date, p.check_id")
        for row in self.conn.execute(sql, [*params, request.span.start.isoformat(), request.span.end.isoformat()]):
            data = json.loads(row["payload_json"])
            data["provisional"] = row["trade_date"] == today.isoformat()
            yield OutputRecord(type="price", check_id=row["check_id"], symbol_id=symbol.symbol_id, provider=provider,
                               record_key=row["trade_date"], content_hash=row["content_hash"],
                               first_observed_at=row["finished_at"], last_observed_at=row["finished_at"],
                               observation_count=1, data=data)

    def _iter_raw_prices(self, symbol: Symbol, provider: str, selected: CheckSummary, request: QueryRequest,
                         today: date, issues: list[Issue]) -> Iterator[OutputRecord]:
        """Yield raw price records aggregated over every valid check of the provider.

        For each trade date, `latest` returns the row from the newest valid check
        (so A then B then A returns A again) with the first and last observation of
        that content hash. `all` returns each distinct content hash once. Dates that
        the selected check visited but no longer returned keep their older value and
        are counted in a `missing_in_recheck` warning.

        :param symbol: Symbol; symbol.
        :param provider: str; provider.
        :param selected: CheckSummary; selected check.
        :param request: QueryRequest; validated query.
        :param today: date; market day for the provisional flag.
        :param issues: list[Issue]; page issue list for missing_in_recheck.
        return records: Iterator[OutputRecord]; price records.
        """
        sql = ("SELECT p.check_id, c.finished_at, p.trade_date AS bkey, p.content_hash, p.payload_json FROM prices p"
               " JOIN checks c ON c.id=p.check_id WHERE c.symbol_id=? AND c.provider=? AND c.dataset='prices'"
               " AND c.adjustment='raw' AND c.state IN ('succeeded','partial') AND c.finished_at IS NOT NULL"
               " AND c.id<=? AND p.trade_date BETWEEN ? AND ? ORDER BY p.trade_date, p.check_id DESC")
        rows = self.conn.execute(sql, (symbol.symbol_id, provider, selected.check_id, request.span.start.isoformat(),
                                       request.span.end.isoformat()))
        missing: list[str] = []
        for bkey, versions in _group_versions(rows):
            if versions[0]["check_id"] != selected.check_id and _visited(selected.coverage, bkey):
                missing.append(bkey)
            for version in (versions[:1] if request.versions == "latest" else versions):
                data = json.loads(version["payload_json"])
                data["provisional"] = bkey == today.isoformat()
                yield OutputRecord(type="price", check_id=version["check_id"], symbol_id=symbol.symbol_id,
                                   provider=provider, record_key=bkey, content_hash=version["content_hash"],
                                   first_observed_at=version["first"], last_observed_at=version["last"],
                                   observation_count=version["count"], data=data)
        if missing:
            issues.append(make_issue("missing_in_recheck",
                                     f"{len(missing)} stored dates were not returned by the latest check, first"
                                     f" {missing[0]}; older values are kept", "prices", severity="warning",
                                     symbol=symbol, provider=provider))  # type: ignore[arg-type]

    def _plan_statement_groups(self, symbol: Symbol, request: QueryRequest,
                               now: datetime) -> list[tuple[Group, Callable[[], Iterator[OutputRecord]] | None]]:
        """Plan one group per statement kind and provider for one symbol.

        An A-share has the providers `sina` and `filing`; a US symbol has the
        provider `sec`, whose statements come from SEC report pages. Sina groups
        select the latest valid check. Document groups (`filing` and `sec`)
        aggregate filing documents and have no single selected check: `latest` uses
        only the latest valid read of each document URI (so a re-read supersedes
        records whose locators changed), while `all` returns every observed record.

        :param symbol: Symbol; symbol.
        :param request: QueryRequest; validated query.
        :param now: datetime; aware UTC time.
        return groups: list[tuple[Group, Callable | None]]; planned groups.
        """
        kinds = list(_STATEMENT_DATASETS) if request.kind == "all" else [request.kind]
        if request.provider == "auto":
            providers = ["sec"] if symbol.exchange == "US" else ["sina", "filing"]
        else:
            providers = [request.provider]
        planned: list[tuple[Group, Callable[[], Iterator[OutputRecord]] | None]] = []
        for kind in kinds:
            for provider in providers:
                if provider == "sina":
                    checks = self._load_checks(symbol.symbol_id, "sina", kind)
                    valid = [c for c in checks if c.finished_at and c.state in VALID_STATES]
                    selected = valid[-1] if valid else None
                    reason = "latest_valid" if selected else "no_usable_check"
                    check_sql = ("c.symbol_id=? AND c.provider='sina' AND c.dataset=? AND c.id<=?",
                                 [symbol.symbol_id, kind, selected.check_id if selected else 0])
                else:
                    checks = self._load_checks(symbol.symbol_id, provider, "filing")
                    valid = [c for c in checks if c.finished_at and c.state in VALID_STATES]
                    selected, reason = None, "all_filing_documents" if valid else "no_usable_check"
                    check_sql = ("c.symbol_id=? AND c.provider=? AND c.dataset='filing' AND s.kind=?",
                                 [symbol.symbol_id, provider, kind])
                    if request.versions == "latest":
                        latest_ids = sorted({max(c.check_id for c in valid if c.key.resource == uri)
                                             for uri in {c.key.resource for c in valid}})
                        marks = ",".join("?" for _ in latest_ids) or "NULL"
                        check_sql = (check_sql[0] + f" AND c.id IN ({marks})", [*check_sql[1], *latest_ids])
                group = self._describe_group(symbol.symbol_id, provider, kind, "none", "", checks, selected, reason,
                                             request.span, now)
                if provider != "sina" and valid:
                    group["state"] = "partial" if any(c.state != "succeeded" for c in valid) else "succeeded"
                if not valid:
                    planned.append((group, None))
                    continue
                planned.append((group, self._make_statement_producer(symbol, provider, check_sql, request)))
        return planned

    def _make_statement_producer(self, symbol: Symbol, provider: str, check_sql: tuple[str, list[Any]],
                                 request: QueryRequest) -> Callable[[], Iterator[OutputRecord]]:
        """Create a generator function that yields aggregated statement records and return it.

        Records are ordered by report date, scope, source key, then observation.

        :param symbol: Symbol; symbol.
        :param provider: str; `sina`, `filing`, or `sec`.
        :param check_sql: tuple[str, list[Any]]; SQL condition on checks and its parameters.
        :param request: QueryRequest; validated query.
        return producer: Callable[[], Iterator[OutputRecord]]; generator function.
        """
        def produce() -> Iterator[OutputRecord]:
            """Yield statement records for the planned group.

            return records: Iterator[OutputRecord]; statement records.
            """
            condition, params = check_sql
            sql = ("SELECT s.check_id, c.finished_at, s.record_key AS bkey, s.content_hash, s.payload_json,"
                   " s.report_date, s.scope FROM statements s JOIN checks c ON c.id=s.check_id"
                   f" WHERE {condition} AND c.state IN ('succeeded','partial') AND c.finished_at IS NOT NULL"
                   " AND s.report_date BETWEEN ? AND ?")
            args = [*params, request.span.start.isoformat(), request.span.end.isoformat()]
            if request.scope != "all":
                sql += " AND s.scope=?"
                args.append(request.scope)
            sql += " ORDER BY s.report_date, s.scope, s.record_key, s.check_id DESC"
            bucket: list[tuple[str, dict[str, Any]]] = []
            bucket_key: tuple[str, str] | None = None
            for bkey, versions in _group_versions(self.conn.execute(sql, args)):
                head = versions[0]
                group_key = (head["report_date"], head["scope"])
                if bucket_key is not None and group_key != bucket_key:
                    yield from _emit_statement_bucket(bucket, symbol, provider, request.versions)
                    bucket = []
                bucket_key = group_key
                for version in (versions[:1] if request.versions == "latest" else versions):
                    bucket.append((bkey, version))
            yield from _emit_statement_bucket(bucket, symbol, provider, request.versions)

        return produce

    def _plan_filing_group(self, symbol: Symbol, request: QueryRequest, now: datetime,
                           issues: list[Issue]) -> tuple[Group, Callable[[], Iterator[OutputRecord]] | None]:
        """Plan the filing-document group for one symbol and return it.

        The documents of an A-share have the provider `filing`; those of a US symbol
        have the provider `sec`. `latest` returns the last attempt per URI; `all`
        returns every attempt. The date filter uses the reference report date;
        documents with an unknown date are kept and counted in a warning.

        :param symbol: Symbol; symbol.
        :param request: QueryRequest; validated query.
        :param now: datetime; aware UTC time.
        :param issues: list[Issue]; page issue list.
        return group: tuple[Group, Callable | None]; planned group.
        """
        provider = request.provider if request.provider != "auto" else ("sec" if symbol.exchange == "US" else "filing")
        checks = [c for c in self._load_checks(symbol.symbol_id, provider, "filing") if c.finished_at]
        group = self._describe_group(symbol.symbol_id, provider, "filing", "none", "", checks, None,
                                     "all_filing_documents" if checks else "never_fetched", request.span, now)
        if not checks:
            return group, None

        def produce() -> Iterator[OutputRecord]:
            """Yield filing records ordered by report date, URI, and observation.

            return records: Iterator[OutputRecord]; filing records.
            """
            sql = ("SELECT f.*, c.id AS cid, c.state, c.coverage_json, c.issues_json, c.metadata_json AS cmeta,"
                   " c.finished_at FROM filings f JOIN checks c ON c.id=f.check_id WHERE c.symbol_id=?"
                   " AND c.provider=? AND c.dataset='filing' AND c.finished_at IS NOT NULL ORDER BY f.uri,"
                   " c.id DESC")
            selected: list[dict[str, Any]] = []
            unknown = 0
            current_uri = None
            for row in self.conn.execute(sql, (symbol.symbol_id, provider)):
                if request.versions == "latest" and row["uri"] == current_uri:
                    continue
                current_uri = row["uri"]
                reference = json.loads(row["reference_json"])
                report_date = reference.get("report_date") if isinstance(reference, dict) else None
                if report_date is None:
                    unknown += 1
                elif not (request.span.start.isoformat() <= report_date <= request.span.end.isoformat()):
                    continue
                selected.append({
                    "sort": (report_date or "", row["uri"], row["cid"]),
                    "record": OutputRecord(
                        type="filing", check_id=row["cid"], symbol_id=symbol.symbol_id, provider=provider,
                        record_key=str(row["cid"]), content_hash=row["body_sha256"],
                        first_observed_at=row["finished_at"], last_observed_at=row["finished_at"],
                        observation_count=1,
                        data={"state": row["state"], "coverage": json.loads(row["coverage_json"]),
                              "issues": json.loads(row["issues_json"]), "metadata": json.loads(row["metadata_json"]),
                              "reference": reference, "format": row["format"], "body_sha256": row["body_sha256"],
                              "status": row["status"]})})
            if unknown:
                issues.append(make_issue("filing_report_date_unknown",
                                         f"{unknown} filing documents have no report date and are included", "filing",
                                         severity="warning", symbol=symbol,
                                         provider=provider))  # type: ignore[arg-type]
            for item in sorted(selected, key=lambda i: i["sort"]):
                yield item["record"]

        return group, produce

    def _plan_fact_groups(self, symbol: Symbol, request: QueryRequest,
                          now: datetime) -> list[tuple[Group, Callable[[], Iterator[OutputRecord]] | None]]:
        """Plan the reported-values group of one symbol and return it.

        Without concepts in the request, the group yields one `concept` record per
        concept and unit. Its data: `taxonomy`, `concept`, `label` (str or null),
        `unit`, `value_count` (int), `first_period_end`, `last_period_end`, and
        `last_filed` (ISO dates; `last_filed` may be null).

        With concepts, the group yields one `fact` record per value of those
        concepts; its data is the stored FactRow as JSON. Values are ordered by
        concept, period end, filing date, and fact key. `latest` returns the newest
        stored version of each value; `all` returns every stored version.

        Both forms keep only values whose period end lies in the requested range
        and, when `filed_until` is set, whose filing date is not later than it.

        :param symbol: Symbol; symbol.
        :param request: QueryRequest; validated query with `concepts` and `filed_until`.
        :param now: datetime; aware UTC time.
        return groups: list[tuple[Group, Callable | None]]; one planned group.
        """
        checks = self._load_checks(symbol.symbol_id, "sec", "facts")
        valid = [c for c in checks if c.finished_at and c.state in VALID_STATES]
        selected = valid[-1] if valid else None
        group = self._describe_group(symbol.symbol_id, "sec", "facts", "none", "", checks, selected,
                                     "latest_valid" if selected else "no_usable_check", request.span, now)
        if selected is None or selected.finished_at is None or not self.has_facts:
            return [(group, None)]
        scope = ("c.symbol_id=? AND c.provider='sec' AND c.dataset='facts' AND c.state IN ('succeeded','partial')"
                 " AND c.finished_at IS NOT NULL AND f.period_end BETWEEN ? AND ?")
        params: list[Any] = [symbol.symbol_id, request.span.start.isoformat(), request.span.end.isoformat()]
        if request.filed_until is not None:
            scope += " AND f.filed<=?"
            params.append(request.filed_until.isoformat())
        check_id, stamp = selected.check_id, format_utc(selected.finished_at)

        def produce_concepts() -> Iterator[OutputRecord]:
            """Yield one summary record per concept and unit.

            return records: Iterator[OutputRecord]; concept records.
            """
            sql = ("SELECT f.taxonomy, f.concept, f.unit, MAX(f.label) AS label, COUNT(DISTINCT f.fact_key) AS n,"
                   " MIN(f.period_end) AS first_end, MAX(f.period_end) AS last_end, MAX(f.filed) AS last_filed"
                   f" FROM facts f JOIN checks c ON c.id=f.check_id WHERE {scope}"
                   " GROUP BY f.taxonomy, f.concept, f.unit ORDER BY f.taxonomy, f.concept, f.unit")
            for row in self.conn.execute(sql, params):
                data: dict[str, JsonValue] = {
                    "taxonomy": row["taxonomy"], "concept": row["concept"], "label": row["label"],
                    "unit": row["unit"], "value_count": row["n"], "first_period_end": row["first_end"],
                    "last_period_end": row["last_end"], "last_filed": row["last_filed"]}
                yield OutputRecord(type="concept", check_id=check_id, symbol_id=symbol.symbol_id, provider="sec",
                                   record_key=f"{row['taxonomy']}:{row['concept']}:{row['unit']}", content_hash=None,
                                   first_observed_at=stamp, last_observed_at=stamp, observation_count=1, data=data)

        def produce_values() -> Iterator[OutputRecord]:
            """Yield the stored values of the requested concepts.

            return records: Iterator[OutputRecord]; fact records.
            """
            marks = ",".join("?" for _ in request.concepts)
            sql = ("SELECT f.check_id, c.finished_at, f.fact_key AS bkey, f.content_hash, f.payload_json FROM facts f"
                   f" JOIN checks c ON c.id=f.check_id WHERE {scope} AND f.concept IN ({marks})"
                   " ORDER BY f.concept, f.period_end, f.filed, f.fact_key, f.check_id DESC")
            for bkey, versions in _group_versions(self.conn.execute(sql, [*params, *request.concepts])):
                for version in (versions[:1] if request.versions == "latest" else versions):
                    yield OutputRecord(type="fact", check_id=version["check_id"], symbol_id=symbol.symbol_id,
                                       provider="sec", record_key=bkey, content_hash=version["content_hash"],
                                       first_observed_at=version["first"], last_observed_at=version["last"],
                                       observation_count=version["count"],
                                       data=json.loads(version["payload_json"]))

        return [(group, produce_values if request.concepts else produce_concepts)]

    def _plan_coverage_groups(self, symbol: Symbol | None, request: QueryRequest,
                              now: datetime) -> list[tuple[Group, Callable[[], Iterator[OutputRecord]] | None]]:
        """Plan one coverage group per stored task of a symbol (or the global tasks) and return them.

        Records are CheckSummary objects whose range intersects the requested range;
        global checks and checks without a range are always included.

        :param symbol: Symbol | None; symbol, or None for global tasks.
        :param request: QueryRequest; validated query.
        :param now: datetime; aware UTC time.
        return groups: list[tuple[Group, Callable | None]]; planned groups sorted by task identity.
        """
        provider = None if request.provider == "auto" else request.provider
        datasets = ["prices", "income", "balance", "cash_flow", "discovery", "filing", "mapping", "facts"]
        symbol_id = symbol.symbol_id if symbol else None
        checks = self._load_checks(symbol_id, provider, datasets)
        by_key: dict[CheckKey, list[CheckSummary]] = {}
        for check in checks:
            by_key.setdefault(check.key, []).append(check)
        planned: list[tuple[Group, Callable[[], Iterator[OutputRecord]] | None]] = []
        for key in sorted(by_key, key=lambda k: (k.dataset, k.adjustment, k.provider, k.resource)):
            key_checks = by_key[key]
            finished = [c for c in key_checks if c.finished_at]
            group = self._describe_group(symbol_id, key.provider, key.dataset, key.adjustment, key.resource,
                                         key_checks, finished[-1] if finished else None, "check_history",
                                         request.span, now)
            chosen = [c for c in key_checks if symbol is None or intersects(c.span, request.span)]

            def produce(items: list[CheckSummary] = chosen) -> Iterator[OutputRecord]:
                """Yield check records in id order.

                :param items: list[CheckSummary]; checks of the group.
                return records: Iterator[OutputRecord]; check records.
                """
                for check in items:
                    stamp = format_utc(check.finished_at or check.started_at)
                    yield OutputRecord(type="check", check_id=check.check_id, symbol_id=check.key.symbol_id,
                                       provider=check.key.provider, record_key=str(check.check_id),
                                       content_hash=None, first_observed_at=stamp, last_observed_at=stamp,
                                       observation_count=1, data=convert_to_json(check))  # type: ignore[typeddict-item]

            planned.append((group, produce))
        return planned


def _visited(coverage: Coverage, day_text: str) -> bool:
    """Report whether a coverage record visited a given ISO day.

    :param coverage: Coverage; coverage of a check.
    :param day_text: str; ISO date.
    return visited: bool; True when a fetched interval contains the day.
    """
    day = date.fromisoformat(day_text)
    return any(i.start <= day <= i.end for i in coverage.fetched)


def _group_versions(rows: Iterator[sqlite3.Row]) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Group consecutive rows by business key and aggregate observations per content hash.

    Rows must be ordered by business key (`bkey`) and then by check id descending.
    For each key the function yields the distinct versions, newest first. Each
    version dict contains the newest row's columns plus `first` and `last` (UTC
    observation times) and `count` (number of checks that observed the hash).

    :param rows: Iterator[sqlite3.Row]; ordered rows with check_id, finished_at, bkey, content_hash, payload_json.
    return groups: Iterator[tuple[str, list[dict[str, Any]]]]; (business key, versions newest first).
    """
    current: str | None = None
    versions: dict[str, dict[str, Any]] = {}
    for row in rows:
        bkey = row["bkey"]
        if current is not None and bkey != current:
            yield current, list(versions.values())
            versions = {}
        current = bkey
        version = versions.get(row["content_hash"])
        if version is None:
            version = dict(row)
            version.update(first=row["finished_at"], last=row["finished_at"], count=0)
            versions[row["content_hash"]] = version
        version["count"] += 1
        version["first"] = min(version["first"], row["finished_at"])
        version["last"] = max(version["last"], row["finished_at"])
    if current is not None:
        yield current, list(versions.values())


def _emit_statement_bucket(bucket: list[tuple[str, dict[str, Any]]], symbol: Symbol, provider: str,
                           versions: str) -> Iterator[OutputRecord]:
    """Yield statement records of one report date and scope sorted by source key and observation.

    :param bucket: list[tuple[str, dict[str, Any]]]; (record key, version) pairs.
    :param symbol: Symbol; symbol.
    :param provider: str; provider.
    :param versions: str; `latest` or `all`.
    return records: Iterator[OutputRecord]; statement records.
    """
    prepared = []
    for bkey, version in bucket:
        data = json.loads(version["payload_json"])
        prepared.append(((data.get("source_key", ""), version["first"], version["check_id"]), bkey, version, data))
    for _sort, bkey, version, data in sorted(prepared, key=lambda item: item[0]):
        yield OutputRecord(type="statement", check_id=version["check_id"], symbol_id=symbol.symbol_id,
                           provider=provider, record_key=bkey, content_hash=version["content_hash"],
                           first_observed_at=version["first"], last_observed_at=version["last"],
                           observation_count=version["count"], data=data)


class _PageBuilder:
    """Apply a global record offset, limit, and output byte budget across groups.

    :param limit: int; maximum records to return; 0 returns none.
    :param offset: int; records to skip.
    return instance: _PageBuilder; empty page builder.
    """

    def __init__(self, limit: int, offset: int) -> None:
        """Initialize counters.

        :param limit: int; maximum records.
        :param offset: int; records to skip.
        return None: None; no value is returned.
        """
        self.limit = limit
        self.offset = offset
        self.position = 0
        self.returned = 0
        self.bytes_used = 0
        self.next_offset: int | None = None
        self.done = False
        self.issues: list[Issue] = []

    def offer(self, group: Group, record: OutputRecord) -> bool:
        """Place one record on the page when it belongs there and return False once the page is complete.

        :param group: Group; group that receives the record.
        :param record: OutputRecord; next record in output order.
        return more: bool; True when the caller should keep offering records.
        """
        if self.done:
            return False
        if self.position < self.offset:
            self.position += 1
            return True
        if self.returned >= self.limit:
            self.next_offset = self.position
            self.done = True
            return False
        size = len(encode_canonical_json(record).encode("utf-8")) + 1  # type: ignore[arg-type]
        if size > OUTPUT_BYTE_BUDGET:
            self.issues.append(make_issue("output_size_limit", f"record {record['record_key']} of check"
                                          f" {record['check_id']} exceeds the output size limit and was skipped",
                                          "prices", severity="warning", url=None))
            self.position += 1
            return True
        if self.bytes_used + size > OUTPUT_BYTE_BUDGET:
            self.next_offset = self.position
            self.done = True
            return False
        group["records"].append(record)
        self.bytes_used += size
        self.returned += 1
        self.position += 1
        return True


def summarize_horizon(checks: Sequence[CheckSummary], key: CheckKey, span: DateRange) -> DateRange:
    """Combine the requested range with the ranges of the latest stored attempt and success for a task.

    Every due check re-fetches this union, which also satisfies the 30-day recheck.

    :param checks: Sequence[CheckSummary]; checks from read_state.
    :param key: CheckKey; task identity.
    :param span: DateRange; requested range.
    return horizon: DateRange; union range.
    """
    start, end = span.start, span.end
    for check in checks:
        if check.key == key and check.span is not None:
            start, end = min(start, check.span.start), max(end, check.span.end)
    return DateRange(start, end)


def replace_coverage(result: Any, coverage: Coverage) -> Any:
    """Return a copy of a result record with a different coverage.

    :param result: Any; FetchResult, MappingResult, DiscoveryResult, or FilingResult.
    :param coverage: Coverage; new coverage.
    return result: Any; copied result.
    """
    return replace(result, coverage=coverage)


__all__ = ["Store", "summarize_horizon", "replace_coverage", "compute_record_key", "compute_gate_key",
           "parse_symbol_id", "compute_sha256"]
