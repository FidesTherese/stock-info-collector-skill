"""Collection planning and the command result envelope for collect and show.

The collector decides which source tasks are due, runs them one at a time, saves
each outcome through storage.Store, and builds the JSON envelope. It never runs
SQL or parses provider bodies itself.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable

from .filings import discover_filings, read_filing
from .providers import (
    CacheState, CheckKey, CheckSummary, CollectRequest, CommandResult, Coverage, DateRange, FilingRef, Group,
    HttpClient, InputError, Interval, Issue, QueryRequest, StatementKind, Stats, Symbol, UtcClock,
    compute_china_day, convert_to_json, fetch_prices, fetch_statements, make_issue,
)
from .securities import lookup_code_links, resolve_symbols
from .storage import Store, summarize_horizon

LOGGER = logging.getLogger("stock_info")
MAX_DOCUMENTS = 24
DOCUMENT_RECHECK = timedelta(days=30)
STATEMENT_KINDS: tuple[StatementKind, ...] = ("income", "balance", "cash_flow")
PARTIAL_CODES = frozenset({"missing_parent_statements", "filing_discovery_failed", "mapping_conflict",
                           "budget_exhausted", "work_limit", "interrupted_check", "parent_coverage_unverified"})


def log_event(level: int, event: str, **fields: Any) -> None:
    """Write one JSON log line to the `stock_info` logger without bodies or cookies.

    :param level: int; logging level such as logging.INFO.
    :param event: str; event name such as `check_finished`.
    :param fields: Any; extra JSON-serializable fields (run_id, check_id, provider, bytes, issue_code).
    return None: None; no value is returned.
    """
    if LOGGER.isEnabledFor(level):
        payload = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "level": logging.getLevelName(level), "event": event, **fields}
        LOGGER.log(level, json.dumps(payload, ensure_ascii=False, default=str))


class _Run:
    """Hold the mutable state of one collect run: issues, summaries, and pending work.

    :param store: Store; open writable store.
    :param http: HttpClient; bounded client.
    :param request: CollectRequest; validated request.
    :param clock: UtcClock; aware UTC clock.
    return instance: _Run; run state.
    """

    def __init__(self, store: Store, http: HttpClient, request: CollectRequest, clock: UtcClock) -> None:
        """Initialize the run with a new UUID4 run id.

        return None: None; no value is returned.
        """
        self.store = store
        self.http = http
        self.request = request
        self.clock = clock
        self.run_id = str(uuid.uuid4())
        self.issues: list[Issue] = []
        self.summaries: list[CheckSummary] = []
        self.pending: list[str] = []

    def run_task(self, key: CheckKey, span: DateRange | None, fetch: Callable[[], Any]) -> CheckSummary | None:
        """Reserve today's check, run the source operation, and save its result atomically.

        Returns None when today's gate for this task is already used. A storage
        failure propagates and stops the run. An interruption or unexpected error
        marks the check failed before re-raising, so no check is left running when
        Python can still react.

        :param key: CheckKey; task identity.
        :param span: DateRange | None; range the task visits.
        :param fetch: Callable[[], Any]; operation that returns a result record.
        return summary: CheckSummary | None; finalized check, or None when gated.
        """
        check_id = self.store.start_check(key, span, self.run_id, self.clock(), refresh=self.request.refresh)
        if check_id is None:
            log_event(logging.DEBUG, "check_gated", run_id=self.run_id, provider=key.provider, dataset=key.dataset)
            return None
        try:
            result = fetch()
        except BaseException as exc:
            code = "interrupted_check" if isinstance(exc, KeyboardInterrupt) else "internal_error"
            failed = _failed_result(key, span, make_issue(code, f"{type(exc).__name__} during {key.dataset}",
                                                          key.dataset, provider=key.provider))
            try:
                self.store.finish_check(check_id, failed, self.clock())
            except Exception:  # the original error is more important
                pass
            raise
        summary = self.store.finish_check(check_id, result, self.clock())
        self.summaries.append(summary)
        self.issues.extend(summary.issues)
        level = logging.INFO if summary.state == "succeeded" else logging.WARNING
        log_event(level, "check_finished", run_id=self.run_id, check_id=check_id, provider=key.provider,
                  dataset=key.dataset, state=summary.state, bytes=self.http.received_bytes,
                  issue_code=summary.issues[0].code if summary.issues else None)
        return summary


def _failed_result(key: CheckKey, span: DateRange | None, issue: Issue) -> Any:
    """Build a failed result of the right kind for a check that could not finish and return it.

    :param key: CheckKey; task identity.
    :param span: DateRange | None; requested range.
    :param issue: Issue; failure reason.
    return result: Any; FetchResult, MappingResult, DiscoveryResult, or FilingResult in state failed.
    """
    from .providers import DiscoveryResult, FetchResult, FilingResult, MappingResult

    coverage = Coverage(requested=span, unfetched=(span,) if span else (), traversal="failed")
    common = dict(state="failed", coverage=coverage, requests=(), issues=(issue,), metadata={})
    if key.dataset == "mapping":
        return MappingResult(**common)  # type: ignore[arg-type]
    if key.dataset == "discovery":
        return DiscoveryResult(**common)  # type: ignore[arg-type]
    if key.dataset == "filing":
        symbol = Symbol(key.symbol_id or "", key.symbol_id.split(":")[0] if key.symbol_id else "SH",  # type: ignore[arg-type]
                        key.symbol_id.split(":")[1] if key.symbol_id else "")
        reference = FilingRef(symbol=symbol, uri=key.resource, report_date=None, publication_date=None,
                              origin="explicit")
        return FilingResult(**common, reference=reference, status="fetch_failed")  # type: ignore[arg-type]
    return FetchResult(**common)  # type: ignore[arg-type]


def _latest(checks: tuple[CheckSummary, ...] | list[CheckSummary], key: CheckKey) -> CheckSummary | None:
    """Return the most recent check for a key, or None.

    :param checks: tuple[CheckSummary, ...] | list[CheckSummary]; checks to search.
    :param key: CheckKey; task identity.
    return check: CheckSummary | None; latest check.
    """
    matching = [c for c in checks if c.key == key]
    return max(matching, key=lambda c: c.check_id) if matching else None


def collect(store: Store, http: HttpClient, request: CollectRequest, clock: UtcClock) -> CommandResult:
    """Collect prices, statements, filings, and mappings for one symbol and return the command envelope.

    Order: Beijing code mapping (global daily check, Beijing symbols only), alias
    resolution from verified links, then for the requested symbol and each alias:
    raw and forward prices from Eastmoney (with a separate full-range Tencent task
    when the whole Eastmoney task failed or returned no rows), Sina income, balance,
    and cash-flow statements, filing discovery, and up to 24 filing documents
    including PDF attachments. Each task is saved before the next starts. A source
    failure lets later tasks continue; a storage failure stops the run. Groups in the
    envelope summarize stored selections without records.

    :param store: Store; open writable store.
    :param http: HttpClient; bounded client.
    :param request: CollectRequest; validated request.
    :param clock: UtcClock; aware UTC clock called at each reservation and finalization.
    return result: CommandResult; envelope with status ok or partial.
    """
    started = time.monotonic()
    run = _Run(store, http, request, clock)
    symbol = request.symbol
    if symbol.exchange == "BJ":
        run.run_task(CheckKey(None, "bse", "mapping"), None, lambda: lookup_code_links(http, now=clock()))
    links = store.read_links()
    symbols: tuple[Symbol, ...] = (symbol,)
    try:
        symbols = resolve_symbols(symbol, links)
    except InputError as exc:
        run.issues.append(exc.issue)
    if len(symbols) == 1 and not any(i.code in ("mapping_conflict", "work_limit") for i in run.issues):
        reason = ("no official code-change source is configured for Shanghai or Shenzhen"
                  if symbol.exchange != "BJ" else "no verified BSE code change involves this code")
        run.issues.append(make_issue("code_mapping_unresolved", reason, "mapping", severity="warning", symbol=symbol))
    if symbol.exchange == "BJ":
        mapping = _latest(store.read_state(()).checks, CheckKey(None, "bse", "mapping"))
        if mapping is None or mapping.state != "succeeded":
            run.issues.append(make_issue("mapping_unavailable", "the BSE code mapping could not be verified", "mapping",
                                         symbol=symbol, provider="bse", retryable=True))
    state = store.read_state(symbols)
    candidates: list[FilingRef] = []
    for current in symbols:
        aliases = tuple(s for s in symbols if s != current)
        _collect_prices(run, state, current)
        _collect_statements(run, state, current)
        candidates.extend(_collect_discovery(run, state, current, aliases))
    explicit = [replace(ref, verified_aliases=tuple(s for s in symbols if s != ref.symbol)) for ref in request.filings]
    _collect_documents(run, store.read_state(symbols), explicit + candidates)
    _report_parent_gaps(run, symbols, candidates)
    now = clock()
    datasets: list[Group] = []
    for query in _summary_queries(symbol, request.span):
        page = store.query(query, now, symbols=symbols)
        datasets.extend(page["groups"])
    status = "partial" if _is_partial(run, datasets) else "ok"
    component = {s.symbol_id for s in symbols}
    used_links = [link for link in links if link.from_symbol.symbol_id in component]
    stats = Stats(http_attempts=http.attempts, received_bytes=http.received_bytes, new_revisions=store.new_revisions,
                  reused_revisions=store.reused_revisions, elapsed_seconds=f"{time.monotonic() - started:.3f}")
    log_event(logging.INFO if status == "ok" else logging.WARNING, "collect_finished", run_id=run.run_id,
              bytes=http.received_bytes)
    return build_envelope("collect", status, symbol, symbols, used_links, request.span, run.run_id, datasets,
                          _deduplicate(run.issues), stats, None)


def _collect_prices(run: _Run, state: CacheState, symbol: Symbol) -> None:
    """Run the raw and forward price tasks for one symbol, with the Tencent fallback when eligible.

    :param run: _Run; run state.
    :param state: CacheState; stored checks.
    :param symbol: Symbol; symbol to collect.
    return None: None; no value is returned.
    """
    today = compute_china_day(run.clock())
    for adjustment in ("raw", "forward"):
        key = CheckKey(symbol.symbol_id, "eastmoney", "prices", adjustment)
        horizon = summarize_horizon(state.checks, key, run.request.span)
        summary = run.run_task(key, horizon, lambda h=horizon, a=adjustment: fetch_prices(
            run.http, symbol, h, a, "eastmoney", today=today))
        if summary is None:
            latest = _latest(run.store.read_state((symbol,)).checks, key)
            summary = latest if latest and compute_china_day(latest.started_at) == today else None
        if summary is None or (summary.state != "failed" and summary.coverage.row_count > 0):
            continue
        tencent_key = CheckKey(symbol.symbol_id, "tencent", "prices", adjustment)
        tencent_horizon = summarize_horizon(state.checks, tencent_key, horizon)
        result = run.run_task(tencent_key, tencent_horizon, lambda h=tencent_horizon, a=adjustment: fetch_prices(
            run.http, symbol, h, a, "tencent", today=today))
        if result is not None:
            run.issues.append(make_issue("price_fallback_used", f"Eastmoney {adjustment} prices were unavailable;"
                                         " Tencent was queried as a separate source", "prices", severity="warning",
                                         symbol=symbol, provider="tencent"))


def _collect_statements(run: _Run, state: CacheState, symbol: Symbol) -> None:
    """Run the three Sina statement tasks for one symbol.

    A complete traversal records the requested horizon as one visited interval,
    because Sina returns every period without a date filter; a partial traversal
    keeps point intervals for the periods it observed.

    :param run: _Run; run state.
    :param state: CacheState; stored checks.
    :param symbol: Symbol; symbol to collect.
    return None: None; no value is returned.
    """
    for kind in STATEMENT_KINDS:
        key = CheckKey(symbol.symbol_id, "sina", kind)
        horizon = summarize_horizon(state.checks, key, run.request.span)

        def fetch(k: StatementKind = kind, h: DateRange = horizon) -> Any:
            """Fetch one statement kind and attach the horizon to its coverage.

            :param k: StatementKind; statement kind.
            :param h: DateRange; horizon.
            return result: FetchResult; statement result.
            """
            result = fetch_statements(run.http, symbol, k)
            coverage = result.coverage
            if result.state == "succeeded":
                outcome = "nonempty" if result.records else "empty"
                coverage = replace(coverage, requested=h, fetched=(Interval(h.start, h.end, outcome,
                                                                            len(result.records)),))
            else:
                coverage = replace(coverage, requested=h, unfetched=(h,))
            return replace(result, coverage=coverage)

        run.run_task(key, horizon, fetch)


def _collect_discovery(run: _Run, state: CacheState, symbol: Symbol,
                       aliases: tuple[Symbol, ...]) -> list[FilingRef]:
    """Run filing discovery for one symbol, or reuse today's cached candidates, and return candidates in range.

    :param run: _Run; run state.
    :param state: CacheState; stored checks.
    :param symbol: Symbol; symbol.
    :param aliases: tuple[Symbol, ...]; verified aliases of the symbol.
    return candidates: list[FilingRef]; candidates whose period lies in the requested range.
    """
    key = CheckKey(symbol.symbol_id, "filing", "discovery")
    horizon = summarize_horizon(state.checks, key, run.request.span)
    summary = run.run_task(key, horizon, lambda: discover_filings(run.http, symbol, horizon))
    if summary is None:
        summary = _latest(run.store.read_state((symbol,)).checks, key)
    if summary is None or not isinstance(summary.metadata, dict):
        return []
    span = run.request.span
    found = []
    for item in summary.metadata.get("candidates") or []:
        report_date = item.get("report_date")
        if not report_date or not (span.start.isoformat() <= report_date <= span.end.isoformat()):
            continue
        publication = item.get("publication_date")
        found.append(FilingRef(symbol=symbol, uri=item["uri"], report_date=datetime.fromisoformat(report_date).date(),
                               publication_date=datetime.fromisoformat(publication).date() if publication else None,
                               origin="automatic", verified_aliases=aliases))
    return found


def _is_document_due(state: CacheState, key: CheckKey, now: datetime, refresh: bool) -> bool:
    """Decide whether a filing document needs reading and return the decision.

    A document is due when it was never read, when its last attempt failed to
    fetch, when its last read is older than 30 days, or on explicit refresh. The
    daily gate still prevents a second attempt on the same China day.

    :param state: CacheState; stored checks.
    :param key: CheckKey; document task identity.
    :param now: datetime; aware UTC time.
    :param refresh: bool; explicit refresh.
    return due: bool; True when the document should be read.
    """
    if refresh:
        return True
    latest = _latest(state.checks, key)
    if latest is None or latest.finished_at is None or latest.state == "failed":
        return True
    return now - latest.finished_at > DOCUMENT_RECHECK


def _collect_documents(run: _Run, state: CacheState, references: list[FilingRef]) -> None:
    """Read due filing documents and their PDF attachments, at most 24 per run, and record pending work.

    :param run: _Run; run state.
    :param state: CacheState; stored checks.
    :param references: list[FilingRef]; explicit references first, then discovered candidates.
    return None: None; no value is returned.
    """
    queue: list[FilingRef] = []
    seen: set[str] = set()
    for reference in references:
        if reference.uri not in seen:
            seen.add(reference.uri)
            queue.append(reference)
    attempted = 0
    pending: list[str] = []
    while queue:
        reference = queue.pop(0)
        key = CheckKey(reference.symbol.symbol_id, "filing", "filing", "none", reference.uri)
        if not _is_document_due(state, key, run.clock(), run.request.refresh):
            continue
        if attempted >= MAX_DOCUMENTS:
            pending.append(reference.uri)
            continue
        attempted += 1
        summary = run.run_task(key, None, lambda r=reference: read_filing(run.http, r))
        if summary is None or not isinstance(summary.metadata, dict):
            continue
        for item in summary.metadata.get("attachments") or []:
            uri = item.get("uri")
            if uri and uri not in seen:
                seen.add(uri)
                queue.append(replace(reference, uri=uri))
    if pending:
        run.pending.extend(pending)
        run.issues.append(make_issue("work_limit", f"{len(pending)} filing documents remain pending after the"
                                     f" {MAX_DOCUMENTS}-document limit; first {pending[0]}", "filing",
                                     severity="warning", provider="filing", retryable=True))


def _report_parent_gaps(run: _Run, symbols: tuple[Symbol, ...], candidates: list[FilingRef]) -> None:
    """Report annual and half-year periods for which no parent-company statement is stored.

    Only periods of discovered annual and half-year reports are checked; no
    quarterly obligation is invented. Without any discovered report the parent
    coverage is reported as unverified.

    :param run: _Run; run state.
    :param symbols: tuple[Symbol, ...]; requested symbol and aliases.
    :param candidates: list[FilingRef]; discovered candidates in range.
    return None: None; no value is returned.
    """
    periods = sorted({c.report_date for c in candidates if c.report_date and c.report_date.month in (6, 12)},
                     reverse=True)
    if not periods:
        run.issues.append(make_issue("parent_coverage_unverified", "no annual or half-year report was discovered, so"
                                     " parent-company coverage is unverified", "filing", severity="warning",
                                     symbol=symbols[0], provider="filing"))
        return
    found: set[tuple[str, str]] = set()
    offset: int | None = 0
    query = QueryRequest(symbol=symbols[0], span=run.request.span, dataset="statements", scope="parent",
                         limit=2000, offset=0)
    while offset is not None:
        page = run.store.query(replace(query, offset=offset), run.clock(), symbols=symbols)
        for group in page["groups"]:
            for record in group["records"]:
                data = record["data"]
                found.add((str(data["report_date"]), str(data["kind"])))
        offset = page["pagination"]["next_offset"]
    gaps = [f"{p.isoformat()}:{k}" for p in periods for k in STATEMENT_KINDS if (p.isoformat(), k) not in found]
    if gaps:
        run.issues.append(make_issue("missing_parent_statements", f"{len(gaps)} parent-company statements are"
                                     f" missing, for example {', '.join(gaps[:6])}", "filing", severity="warning",
                                     symbol=symbols[0], provider="filing"))


def _summary_queries(symbol: Symbol, span: DateRange) -> list[QueryRequest]:
    """Build the record-free queries that summarize stored groups for a collect envelope.

    :param symbol: Symbol; requested symbol.
    :param span: DateRange; requested range.
    return queries: list[QueryRequest]; raw prices, forward prices, statements, and filings with limit 0.
    """
    return [QueryRequest(symbol=symbol, span=span, dataset="prices", adjustment="raw", limit=0),
            QueryRequest(symbol=symbol, span=span, dataset="prices", adjustment="forward", limit=0),
            QueryRequest(symbol=symbol, span=span, dataset="statements", limit=0),
            QueryRequest(symbol=symbol, span=span, dataset="filings", limit=0)]


def _is_partial(run: _Run, datasets: list[Group]) -> bool:
    """Decide whether a collect run is partial and return the decision.

    The run is partial when any task finished in a state other than succeeded, any
    error issue or partial-making issue exists, filing work is pending, or a price
    or Sina statement group is not fresh. A failed Eastmoney price task whose
    separate Tencent task succeeded is covered by the fallback: it keeps its issues
    in the envelope but does not make the run partial by itself.

    :param run: _Run; run state.
    :param datasets: list[Group]; summary groups.
    return partial: bool; True for exit code 3.
    """
    covered = {(s.key.symbol_id, s.key.adjustment) for s in run.summaries
               if s.key.provider == "tencent" and s.state == "succeeded" and s.coverage.row_count > 0}
    failed = [s for s in run.summaries if s.state != "succeeded"
              and not (s.key.provider == "eastmoney" and (s.key.symbol_id, s.key.adjustment) in covered)]
    if failed:
        return True
    ignored = {id(i) for s in run.summaries if s.key.provider == "eastmoney"
               and (s.key.symbol_id, s.key.adjustment) in covered for i in s.issues}
    if any((i.severity == "error" or i.code in PARTIAL_CODES) and id(i) not in ignored for i in run.issues):
        return True
    if run.pending:
        return True
    for group in datasets:
        if group["provider"] in ("eastmoney", "tencent", "sina") and group["freshness"] != "fresh":
            return True
    return False


def _deduplicate(issues: list[Issue]) -> list[Issue]:
    """Remove repeated identical issues while keeping their first order.

    :param issues: list[Issue]; issues in discovery order.
    return issues: list[Issue]; unique issues.
    """
    seen: set[Issue] = set()
    unique = []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            unique.append(issue)
    return unique


def show(store: Store, request: QueryRequest, now: datetime) -> CommandResult:
    """Read stored data for one symbol and its verified aliases and return the command envelope.

    The function resolves aliases from stored links inside the same read snapshot,
    then runs one query page. It never writes, fills gaps, or contacts the network.
    The status is partial when any group lacks a usable selection, is not fresh, or
    an error issue exists; a show still exits 0 because the query was executed.

    :param store: Store; read-only store holding one snapshot.
    :param request: QueryRequest; validated query.
    :param now: datetime; aware UTC time.
    return result: CommandResult; envelope with records and pagination.
    """
    issues: list[Issue] = []
    links = store.read_links()
    symbols: tuple[Symbol, ...] = (request.symbol,)
    try:
        symbols = resolve_symbols(request.symbol, links)
    except InputError as exc:
        issues.append(exc.issue)
    page = store.query(request, now, symbols=symbols)
    groups = page["groups"]
    partial = bool(issues) or any(i.get("severity") == "error" for i in page["issues"]) or any(
        g["selected_check_id"] is None and g["selection_reason"] != "all_filing_documents"
        or g["state"] != "succeeded" or g["freshness"] != "fresh" for g in groups) or not groups
    component = {s.symbol_id for s in symbols}
    used_links = [link for link in links if link.from_symbol.symbol_id in component]
    stats = Stats(http_attempts=0, received_bytes=0, new_revisions=0, reused_revisions=0, elapsed_seconds="0.000")
    envelope = build_envelope("show", "partial" if partial else "ok", request.symbol, symbols, used_links,
                              request.span, None, groups, issues, stats, page["pagination"])
    envelope["issues"].extend(page["issues"])
    return envelope


def build_envelope(command: str, status: str, symbol: Symbol | None, symbols: tuple[Symbol, ...], links: list[Any],
                   span: DateRange | None, run_id: str | None, datasets: list[Group], issues: list[Issue],
                   stats: Stats, pagination: Any) -> CommandResult:
    """Assemble the JSON command envelope from its parts and return it.

    Output schema: see `CommandResult` in providers.py and the DTD section "CLI and
    JSON". `source_conditions_status` is always `unresolved` because provider reuse
    conditions were not established.

    :param command: str; `collect` or `show`.
    :param status: str; `ok`, `partial`, or `error`.
    :param symbol: Symbol | None; requested symbol, or None before validation.
    :param symbols: tuple[Symbol, ...]; resolved symbols.
    :param links: list[Any]; code links of the resolved component.
    :param span: DateRange | None; requested range, or None before validation.
    :param run_id: str | None; UUID4 for collect, None for show.
    :param datasets: list[Group]; dataset groups.
    :param issues: list[Issue]; issues.
    :param stats: Stats; statistics.
    :param pagination: Any; Pagination for show, None for collect.
    return envelope: CommandResult; JSON-ready envelope.
    """
    return CommandResult(
        schema_version=1, command=command, status=status,
        requested_symbol=symbol.symbol_id if symbol else None,
        resolved_symbols=[convert_to_json(s) for s in symbols],  # type: ignore[misc]
        code_links=[convert_to_json(link) for link in links],  # type: ignore[misc]
        requested_range=convert_to_json(span) if span else None,  # type: ignore[typeddict-item]
        run_id=run_id, source_conditions_status="unresolved", datasets=datasets,
        issues=[convert_to_json(i) if isinstance(i, Issue) else i for i in issues],  # type: ignore[misc]
        stats=stats, pagination=pagination)


def build_error_envelope(command: str, issue: Issue, symbol: Symbol | None = None,
                         span: DateRange | None = None) -> CommandResult:
    """Build the envelope for a command that stopped with an error and return it.

    :param command: str; `collect` or `show`.
    :param issue: Issue; the error.
    :param symbol: Symbol | None; requested symbol when validated.
    :param span: DateRange | None; requested range when validated.
    return envelope: CommandResult; error envelope.
    """
    stats = Stats(http_attempts=0, received_bytes=0, new_revisions=0, reused_revisions=0, elapsed_seconds="0.000")
    return build_envelope(command, "error", symbol, (symbol,) if symbol else (), [], span, None, [], [issue],
                          stats, None)


__all__ = ["collect", "show", "build_envelope", "build_error_envelope", "log_event"]
