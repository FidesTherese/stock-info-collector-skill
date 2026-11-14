"""Collection planning and the command result envelope for collect and show.

The collector decides which source tasks are due, runs them one at a time, saves
each outcome through storage.Store, and builds the JSON envelope. It never runs
SQL or parses provider bodies itself. A-shares and US shares have separate task
orders and share the ending that builds the envelope.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .filings import discover_filings, read_filing
from .providers import (
    US_VENUES, CacheState, CheckKey, CheckSummary, CodeLink, CollectRequest, CommandResult, Coverage, DateRange,
    FilingRef, FilingResult, Group, HttpClient, InputError, Interval, Issue, QueryRequest, StatementKind, Stats,
    Symbol, UtcClock, compute_china_day, compute_market_day, convert_to_json, fetch_prices, fetch_statements,
    make_issue,
)
from .sec import fetch_sec_facts, lookup_sec_company, plan_sec_filings, read_sec_filing
from .securities import lookup_code_links, resolve_symbols
from .storage import Store, summarize_horizon

LOGGER = logging.getLogger("stock_info")
MAX_DOCUMENTS = 24
# Reading one SEC report takes at most four requests: the summary and three statement pages.
SEC_REPORT_REQUESTS = 4
DOCUMENT_RECHECK = timedelta(days=30)
STATEMENT_KINDS: tuple[StatementKind, ...] = ("income", "balance", "cash_flow")
PARTIAL_CODES = frozenset({"missing_parent_statements", "filing_discovery_failed", "mapping_conflict",
                           "budget_exhausted", "work_limit", "interrupted_check", "parent_coverage_unverified",
                           "forward_fallback_unavailable", "price_fallback_unavailable"})


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
    """Collect prices, financial information, and mappings for one symbol and return the command envelope.

    An A-share runs the tasks of _collect_a_share; a US symbol runs the tasks of
    _collect_us. Each task is saved before the next starts. A source failure lets
    later tasks continue; a storage failure stops the run. Groups in the envelope
    summarize stored selections without records.

    :param store: Store; open writable store.
    :param http: HttpClient; bounded client.
    :param request: CollectRequest; validated request.
    :param clock: UtcClock; aware UTC clock called at each reservation and finalization.
    return result: CommandResult; envelope with status ok or partial.
    """
    started = time.monotonic()
    run = _Run(store, http, request, clock)
    symbol = request.symbol
    links: tuple[CodeLink, ...] = ()
    symbols: tuple[Symbol, ...] = (symbol,)
    if symbol.exchange == "US":
        _collect_us(run, symbol)
    else:
        links, symbols = _collect_a_share(run, symbol)
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


def _collect_a_share(run: _Run, symbol: Symbol) -> tuple[tuple[CodeLink, ...], tuple[Symbol, ...]]:
    """Run the A-share tasks for one symbol and return the stored code links and the resolved symbols.

    Order: Beijing code mapping (global daily check, Beijing symbols only), alias
    resolution from verified links, then for the requested symbol and each alias:
    raw and forward prices from Eastmoney (with a separate full-range Tencent task
    when the whole Eastmoney task failed or returned no rows), Sina income, balance,
    and cash-flow statements, filing discovery, and up to 24 filing documents
    including PDF attachments.

    :param run: _Run; run state.
    :param symbol: Symbol; requested A-share symbol.
    return outcome: tuple[tuple[CodeLink, ...], tuple[Symbol, ...]]; stored links, and the requested symbol
        followed by its verified aliases.
    """
    store, http, request, clock = run.store, run.http, run.request, run.clock
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
    return links, symbols


def _collect_us(run: _Run, symbol: Symbol) -> None:
    """Run the US tasks for one symbol.

    Order: the SEC ticker list (company number and exchange), the reported values,
    unadjusted and forward-adjusted prices, and the report pages named by the
    stored values. When the SEC lookup does not succeed, for example because the
    SEC contact is not configured, the other SEC tasks are skipped and the price
    tasks still run. Earlier tickers of a renamed company are not traced; the run
    says so in a warning.

    :param run: _Run; run state.
    :param symbol: Symbol; requested US symbol.
    return None: None; no value is returned.
    """
    state = run.store.read_state((symbol,))
    company = _collect_sec_company(run, symbol)
    cik = company.get("cik") if company.get("found") else None
    if isinstance(cik, int):
        run.run_task(CheckKey(symbol.symbol_id, "sec", "facts"), None,
                     lambda: fetch_sec_facts(run.http, symbol, cik))
    exchange = company.get("sec_exchange")
    _collect_us_prices(run, state, symbol, exchange if isinstance(exchange, str) else None)
    if isinstance(cik, int):
        _collect_sec_reports(run, symbol, cik)
    run.issues.append(make_issue("code_mapping_unresolved", "earlier tickers of this company are not traced, so"
                                 " history under a former ticker is not collected", "mapping", severity="warning",
                                 symbol=symbol))


def _collect_sec_company(run: _Run, symbol: Symbol) -> dict[str, Any]:
    """Look up the company of a US symbol in the SEC ticker list, or reuse today's lookup, and return its record.

    Output: the metadata of a succeeded lookup (`found: bool`, `ticker: str`, and
    when found `cik: int`, `name: str`, `sec_exchange: str | None`), or an empty
    object when no lookup succeeded today. A lookup that failed earlier today is
    not repeated without a refresh; the run then reports `sec_lookup_failed_today`.

    :param run: _Run; run state.
    :param symbol: Symbol; US symbol.
    return company: dict[str, Any]; company record, or an empty object.
    """
    key = CheckKey(symbol.symbol_id, "sec", "mapping")
    summary = run.run_task(key, None, lambda: lookup_sec_company(run.http, symbol))
    if summary is None:
        summary = _latest(run.store.read_state((symbol,)).checks, key)
        if summary is not None and summary.state != "succeeded":
            run.issues.append(make_issue("sec_lookup_failed_today", "the SEC company lookup failed earlier today, so"
                                         " SEC data is not collected in this run; use --refresh to try again",
                                         "mapping", symbol=symbol, provider="sec", retryable=True))
    if summary is None or summary.state != "succeeded" or not isinstance(summary.metadata, dict):
        return {}
    return summary.metadata


def _find_stored_venue(state: CacheState, symbol: Symbol) -> str | None:
    """Find the Eastmoney venue number that an earlier price check stored for a US symbol and return it, or None.

    :param state: CacheState; stored checks.
    :param symbol: Symbol; US symbol.
    return venue: str | None; venue number such as `105`, or None when no check stored one.
    """
    for check in sorted(state.checks, key=lambda c: c.check_id, reverse=True):
        if check.key.symbol_id == symbol.symbol_id and check.key.dataset == "prices" \
                and isinstance(check.metadata, dict) and check.metadata.get("venue"):
            return str(check.metadata["venue"])
    return None


def _collect_us_prices(run: _Run, state: CacheState, symbol: Symbol, sec_exchange: str | None) -> None:
    """Run the unadjusted and forward-adjusted price tasks for a US symbol, with the Tencent fallback when eligible.

    Eastmoney tries a venue stored by an earlier check first, then the venues in
    the order that the SEC exchange suggests. Tencent runs as a separate task only
    for unadjusted prices, only when the Eastmoney task failed completely or
    returned no rows, and only when the venue is known; it has no forward-adjusted
    US prices.

    :param run: _Run; run state.
    :param state: CacheState; stored checks.
    :param symbol: Symbol; US symbol.
    :param sec_exchange: str | None; exchange from the SEC ticker list, such as `Nasdaq` or `NYSE`, or None.
    return None: None; no value is returned.
    """
    today = compute_market_day(symbol.symbol_id, run.clock())
    known = _find_stored_venue(state, symbol)
    order = {"Nasdaq": ("105", "106", "107"), "NYSE": ("106", "107", "105")}.get(sec_exchange or "", US_VENUES)
    for adjustment in ("raw", "forward"):
        venues = tuple(dict.fromkeys(([known] if known else []) + list(order)))
        key = CheckKey(symbol.symbol_id, "eastmoney", "prices", adjustment)
        horizon = summarize_horizon(state.checks, key, run.request.span)
        summary = run.run_task(key, horizon, lambda h=horizon, a=adjustment, v=venues: fetch_prices(
            run.http, symbol, h, a, "eastmoney", today=today, venues=v))
        if summary is None:
            latest = _latest(run.store.read_state((symbol,)).checks, key)
            summary = latest if latest and compute_market_day(symbol.symbol_id, latest.started_at) == today else None
        if summary is None:
            continue
        if isinstance(summary.metadata, dict) and summary.metadata.get("venue"):
            known = str(summary.metadata["venue"])
        if summary.state != "failed" and summary.coverage.row_count > 0:
            continue
        if adjustment == "forward":
            run.issues.append(make_issue("forward_fallback_unavailable", "Eastmoney returned no forward-adjusted"
                                         " prices, and Tencent has none for US shares", "prices", severity="warning",
                                         symbol=symbol, provider="tencent"))
            continue
        if known is None:
            run.issues.append(make_issue("price_fallback_unavailable", "the Tencent fallback needs the venue that"
                                         " Eastmoney confirms, and no venue is known", "prices", severity="warning",
                                         symbol=symbol, provider="tencent"))
            continue
        tencent_key = CheckKey(symbol.symbol_id, "tencent", "prices", adjustment)
        tencent_horizon = summarize_horizon(state.checks, tencent_key, horizon)
        result = run.run_task(tencent_key, tencent_horizon, lambda h=tencent_horizon, v=known: fetch_prices(
            run.http, symbol, h, "raw", "tencent", today=today, venues=(v,)))
        if result is not None:
            run.issues.append(make_issue("price_fallback_used", "Eastmoney unadjusted prices were unavailable;"
                                         " Tencent was queried as a separate source", "prices", severity="warning",
                                         symbol=symbol, provider="tencent"))


def _collect_sec_reports(run: _Run, symbol: Symbol, cik: int) -> None:
    """Read the statement pages of the periodic reports that the stored values name, newest report first.

    A report is read when it was never read, when its last read failed, or on
    refresh; a filed report does not change, so there is no later recheck. Before
    each report the run must have at least four requests left. Reports beyond the
    budget are recorded as pending work with the warning `work_limit`, and a
    later run continues with them.

    :param run: _Run; run state.
    :param symbol: Symbol; US symbol.
    :param cik: int; SEC company number.
    return None: None; no value is returned.
    """
    planned = plan_sec_filings(run.store.list_fact_filings(symbol), symbol, cik, run.request.span)
    state = run.store.read_state((symbol,))
    pending: list[str] = []
    for filing in planned:
        key = CheckKey(symbol.symbol_id, "sec", "filing", "none", filing.reference.uri)
        latest = _latest(state.checks, key)
        if not run.request.refresh and latest is not None and latest.finished_at is not None \
                and latest.state != "failed":
            continue
        if run.http.max_requests - run.http.attempts < SEC_REPORT_REQUESTS:
            pending.append(filing.reference.uri)
            continue
        run.run_task(key, None, lambda f=filing: read_sec_filing(run.http, f))
    if pending:
        run.pending.extend(pending)
        run.issues.append(make_issue("work_limit", f"{len(pending)} reports remain unread because the request"
                                     " budget of this run is used up; run the command again to continue; first"
                                     f" {pending[0]}", "filing", severity="warning", symbol=symbol, provider="sec",
                                     retryable=True))


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
    """Read due filing documents and, when needed, their PDF attachments, at most 24 per run.

    A PDF attachment is read only when its HTML page yielded no statement. When
    the HTML page did yield statements, the PDF repeats the same report, so it is
    skipped and one `pdf_attachment_skipped` warning lists how many were skipped.
    Documents beyond the 24-document limit are recorded as pending work.

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
    skipped: list[str] = []
    while queue:
        reference = queue.pop(0)
        key = CheckKey(reference.symbol.symbol_id, "filing", "filing", "none", reference.uri)
        if not _is_document_due(state, key, run.clock(), run.request.refresh):
            continue
        if attempted >= MAX_DOCUMENTS:
            pending.append(reference.uri)
            continue
        attempted += 1
        summary = run.run_task(key, None, lambda r=reference: apply_sina_coverage(run, read_filing(run.http, r)))
        if summary is None or not isinstance(summary.metadata, dict):
            continue
        html_succeeded = summary.coverage.row_count > 0
        for item in summary.metadata.get("attachments") or []:
            uri = item.get("uri")
            if uri and uri not in seen:
                seen.add(uri)
                if html_succeeded:
                    skipped.append(uri)
                else:
                    queue.append(replace(reference, uri=uri))
    if skipped:
        run.issues.append(make_issue("pdf_attachment_skipped", f"{len(skipped)} PDF attachments were not read because"
                                     f" their HTML pages already yielded statements; first {skipped[0]}", "filing",
                                     severity="warning", provider="filing"))
    if pending:
        run.pending.extend(pending)
        run.issues.append(make_issue("work_limit", f"{len(pending)} filing documents remain pending after the"
                                     f" {MAX_DOCUMENTS}-document limit; first {pending[0]}", "filing",
                                     severity="warning", provider="filing", retryable=True))


def apply_sina_coverage(run: _Run, result: FilingResult) -> FilingResult:
    """Downgrade rejected consolidated filing tables that Sina already covers and return the result.

    A rejected table is covered when its scope is consolidated, its header has at
    least one explicit period, and a stored Sina consolidated statement of the same
    kind exists for every one of those periods. The matching
    `ambiguous_statement_table` issues (identified by `table at <locator> `) become
    warnings with an explanatory suffix. When every rejected table is covered and
    the document produced statements, the document state becomes `succeeded`; its
    extraction `status` and `unparsed_locators` stay unchanged. Parent-company
    tables are never covered, because Sina has no parent-company scope. Covered
    locators are listed in `metadata["sina_covered_locators"]`.

    :param run: _Run; run state with the store and clock.
    :param result: FilingResult; outcome of read_filing.
    return result: FilingResult; possibly adjusted outcome.
    """
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    rejected = metadata.get("rejected_tables") or []
    if not rejected or result.reference is None:
        return result
    covered: list[str] = []
    for table in rejected:
        dates = table.get("report_dates") or []
        if table.get("scope") != "consolidated" or not dates:
            continue
        span = DateRange(date.fromisoformat(dates[0]), date.fromisoformat(dates[-1]))
        query = QueryRequest(symbol=result.reference.symbol, span=span, dataset="statements", provider="sina",
                             scope="consolidated", kind=table["kind"], limit=2000)
        page = run.store.query(query, run.clock(), symbols=(result.reference.symbol,))
        found = {str(r["data"]["report_date"]) for g in page["groups"] for r in g["records"]}
        if set(dates) <= found:
            covered.append(table["locator"])
    if not covered:
        return result
    issues = []
    for issue in result.issues:
        if issue.code == "ambiguous_statement_table" and any(f"table at {loc} " in issue.message for loc in covered):
            issue = replace(issue, severity="warning", message=issue.message
                            + "; Sina's structured consolidated statement covers these periods")
        issues.append(issue)
    state = result.state
    if len(covered) == len(rejected) and result.statements and state == "partial":
        state = "succeeded"
    return replace(result, issues=tuple(issues), state=state, coverage=replace(result.coverage, traversal=state),
                   metadata={**metadata, "sina_covered_locators": covered})


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
    return queries: list[QueryRequest]; raw prices, forward prices, statements, and filings with limit 0,
        and for a US symbol also the reported values.
    """
    queries = [QueryRequest(symbol=symbol, span=span, dataset="prices", adjustment="raw", limit=0),
               QueryRequest(symbol=symbol, span=span, dataset="prices", adjustment="forward", limit=0),
               QueryRequest(symbol=symbol, span=span, dataset="statements", limit=0),
               QueryRequest(symbol=symbol, span=span, dataset="filings", limit=0)]
    if symbol.exchange == "US":
        queries.append(QueryRequest(symbol=symbol, span=span, dataset="facts", limit=0))
    return queries


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
    JSON". `source_conditions_status` is `sec_free_to_access_and_reuse` when every
    group of the envelope comes from the provider `sec`, because the SEC states
    that its filing content is free to access and reuse. It is `unresolved` in
    every other case, because the reuse conditions of the other providers were
    not established.

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
    providers = {group["provider"] for group in datasets}
    conditions = "sec_free_to_access_and_reuse" if providers and providers <= {"sec"} else "unresolved"
    return CommandResult(
        schema_version=1, command=command, status=status,
        requested_symbol=symbol.symbol_id if symbol else None,
        resolved_symbols=[convert_to_json(s) for s in symbols],  # type: ignore[misc]
        code_links=[convert_to_json(link) for link in links],  # type: ignore[misc]
        requested_range=convert_to_json(span) if span else None,  # type: ignore[typeddict-item]
        run_id=run_id, source_conditions_status=conditions, datasets=datasets,
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
