"""Shared records, errors, bounded HTTP access, and the price and statement adapters.

Every other module imports its shared types from this module. The module never
performs input or output at import time. The price adapters serve A-shares and
US shares; the statement adapter in this module serves A-shares only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Callable, Literal, Sequence, TypeAlias, TypedDict
from urllib.parse import urlencode, urljoin, urlsplit

Exchange: TypeAlias = Literal["SH", "SZ", "BJ", "US"]
Adjustment: TypeAlias = Literal["raw", "forward", "none"]
PriceProvider: TypeAlias = Literal["eastmoney", "tencent"]
Provider: TypeAlias = Literal["eastmoney", "tencent", "sina", "filing", "bse", "sec"]
StatementKind: TypeAlias = Literal["income", "balance", "cash_flow"]
Scope: TypeAlias = Literal["consolidated", "parent", "unknown"]
Dataset: TypeAlias = Literal["prices", "income", "balance", "cash_flow", "discovery", "filing", "mapping", "facts",
                             "settings"]
State: TypeAlias = Literal["running", "succeeded", "partial", "failed", "blocked"]
JsonValue: TypeAlias = None | bool | int | str | list["JsonValue"] | dict[str, "JsonValue"]
UtcClock: TypeAlias = Callable[[], datetime]

CHINA_TZ = timezone(timedelta(hours=8))
MIB = 1024 * 1024
API_MAX_BYTES = 4 * MIB
MAPPING_MAX_BYTES = 2 * MIB
HTML_MAX_BYTES = 8 * MIB
PDF_MAX_BYTES = 16 * MIB
TOTAL_MAX_BYTES = 128 * MIB
SEC_FACTS_MAX_BYTES = 64 * MIB
FIELD_MAX_BYTES = 64 * 1024
REPORT_MAX_ITEMS = 1000
TENCENT_MAX_PAGES = 64
TENCENT_PAGE_SIZE = 640
SINA_MAX_PAGES = 20
SINA_PAGE_SIZE = 50
SINA_MAX_PERIODS = 1000
CURL_WRITE_ABORT = 0xFFFFFFFF

API_HOSTS = frozenset({"push2his.eastmoney.com", "proxy.finance.qq.com", "quotes.sina.cn"})
FILING_HOSTS = frozenset({
    "vip.stock.finance.sina.com.cn", "money.finance.sina.com.cn", "file.finance.sina.com.cn",
    "www.sse.com.cn", "www.szse.cn", "www.bse.cn", "www.cninfo.com.cn", "static.cninfo.com.cn",
})
SEC_HOSTS = frozenset({"www.sec.gov", "data.sec.gov"})
ALLOWED_HOSTS = API_HOSTS | FILING_HOSTS | SEC_HOSTS
SINA_HOSTS = frozenset({"vip.stock.finance.sina.com.cn", "money.finance.sina.com.cn", "file.finance.sina.com.cn"})
RETRY_STATUSES = frozenset({408, 500, 502, 503, 504})

EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TENCENT_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
SINA_REPORT_URL = "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
SINA_SOURCES: dict[str, str] = {"income": "lrb", "balance": "fzb", "cash_flow": "llb"}
# Eastmoney serves US securities under three venue numbers; Tencent needs a matching code suffix.
US_VENUES: tuple[str, ...] = ("105", "106", "107")
TENCENT_US_SUFFIX: dict[str, str] = {"105": "OQ", "106": "N", "107": "AM"}


# ---------------------------------------------------------------------------
# Shared records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """Identify one security by market and code and return the identity record.

    The canonical identifier is `EXCHANGE:CODE`, for example `SH:600519` or
    `US:AAPL`. An A-share code has six digits. A US code is the ticker in upper
    case, with `-` before a share class as the SEC writes it, for example `BRK-B`.
    The record does not prove that the security exists or trades.

    :param symbol_id: str; canonical identifier such as `SH:600519` or `US:BRK-B`.
    :param exchange: Exchange; `SH`, `SZ`, `BJ`, or `US`.
    :param code: str; six-digit A-share code, or US ticker.
    return instance: Symbol; immutable security identity.
    """

    symbol_id: str
    exchange: Exchange
    code: str

    @property
    def wire_code(self) -> str:
        """Build the lowercase prefixed code that Tencent and Sina expect for A-shares and return it.

        The wire code joins the lowercase exchange and the code, for example
        `sh600519`. US requests do not use it: they need a venue, see
        build_eastmoney_secid and build_tencent_code.

        return wire_code: str; provider-facing prefixed code.
        """
        return f"{self.exchange.lower()}{self.code}"


@dataclass(frozen=True)
class DateRange:
    """Describe an inclusive calendar date range and return the range record.

    Both ends are inclusive. Callers validate that `start <= end`.

    :param start: date; first included day.
    :param end: date; last included day.
    return instance: DateRange; immutable inclusive range.
    """

    start: date
    end: date


@dataclass(frozen=True)
class CheckKey:
    """Identify one source task that the storage layer gates once per China day.

    A mapping check uses `symbol_id=None`. A filing document check stores the
    document URI in `resource`. Only price checks use `raw` or `forward`.

    :param symbol_id: str | None; canonical symbol or None for the global mapping check.
    :param provider: Provider; source that the check contacts.
    :param dataset: Dataset; kind of data that the check collects.
    :param adjustment: Adjustment; price adjustment, or `none` for other datasets.
    :param resource: str; filing URI for document checks, otherwise an empty string.
    return instance: CheckKey; immutable task identity.
    """

    symbol_id: str | None
    provider: Provider
    dataset: Dataset
    adjustment: Adjustment = "none"
    resource: str = ""


@dataclass(frozen=True)
class Issue:
    """Describe one warning or error that the caller must see and return the issue record.

    `cached_preserved` states that previously stored data remains untouched.

    :param code: str; stable machine-readable issue code such as `schema_changed`.
    :param message: str; human-readable explanation without response bodies.
    :param severity: Literal["warning", "error"]; impact of the issue.
    :param dataset: Dataset; dataset that the issue affects.
    :param symbol_id: str | None; affected symbol or None for global issues.
    :param provider: Provider | None; affected source or None.
    :param url: str | None; affected URL or URI when one exists.
    :param report_date: date | None; affected report period when one exists.
    :param retryable: bool; True when a later attempt may succeed.
    :param cached_preserved: bool; True when stored data was not changed by the failure.
    return instance: Issue; immutable issue record.
    """

    code: str
    message: str
    severity: Literal["warning", "error"]
    dataset: Dataset
    symbol_id: str | None = None
    provider: Provider | None = None
    url: str | None = None
    report_date: date | None = None
    retryable: bool = False
    cached_preserved: bool = True


@dataclass(frozen=True)
class RequestMeta:
    """Record one HTTP attempt without headers or bodies and return the attempt record.

    :param url: str; requested URL.
    :param status: int | None; HTTP status, or None when no response arrived.
    :param received_bytes: int; body bytes received during this attempt.
    :param body_sha256: str | None; lowercase SHA-256 of the complete body, or None.
    :param checked_at: datetime; aware UTC time when the attempt started.
    :param outcome: str; `ok`, `redirect`, `retry`, or an issue code.
    return instance: RequestMeta; immutable attempt record.
    """

    url: str
    status: int | None
    received_bytes: int
    body_sha256: str | None
    checked_at: datetime
    outcome: str


@dataclass(frozen=True, repr=False)
class SecAccess:
    """Say whether requests to the SEC may be sent, and with which identification line.

    The SEC asks every automated tool to state who operates it. `user_agent` holds
    that line (`NAME EMAIL`) and is personal data: `repr()` shows only whether the
    record is configured or blocked, and convert_to_json leaves the line out. When
    `user_agent` is None, SEC requests are blocked, and the two block fields give
    the reason; the block fields have no meaning for a configured record.

    :param user_agent: str | None; line for the `User-Agent` header, or None when blocked.
    :param block_code: str; issue code that explains the block, such as `sec_contact_missing`.
    :param block_message: str; explanation that tells the person what to do next.
    return instance: SecAccess; immutable access record.
    """

    user_agent: str | None = field(default=None, repr=False)
    block_code: str = "sec_contact_missing"
    block_message: str = ("No SEC contact is configured. Copy config.example.toml to config.toml and fill in"
                          " contact_name and contact_email.")

    def __repr__(self) -> str:
        """Describe the record without the contact line and return the text.

        return text: str; `SecAccess(configured)` or `SecAccess(blocked: CODE)`.
        """
        state = "configured" if self.user_agent is not None else f"blocked: {self.block_code}"
        return f"SecAccess({state})"


@dataclass(frozen=True)
class ResponseData:
    """Hold one complete successful HTTP response body and return the response record.

    :param url: str; final URL after validated redirects.
    :param body: bytes; complete response body within the caller's byte limit.
    :param content_type: str; Content-Type header value, or an empty string.
    :param status: int; final HTTP status, always 200.
    :param requests: tuple[RequestMeta, ...]; every attempt and redirect in order.
    return instance: ResponseData; immutable response record.
    """

    url: str
    body: bytes
    content_type: str
    status: int
    requests: tuple[RequestMeta, ...]


@dataclass(frozen=True)
class Interval:
    """Describe one validated source visit over an inclusive date range.

    `outcome=empty` means the source returned no rows for the range; it does not
    prove that no trading or reporting happened.

    :param start: date; first day that the visit covered.
    :param end: date; last day that the visit covered.
    :param outcome: Literal["nonempty", "empty"]; whether rows were returned.
    :param row_count: int; number of accepted rows inside the range.
    return instance: Interval; immutable visit record.
    """

    start: date
    end: date
    outcome: Literal["nonempty", "empty"]
    row_count: int


@dataclass(frozen=True)
class Coverage:
    """Summarize which dates a check visited, which it did not, and what it observed.

    :param requested: DateRange | None; range that the check intended to visit.
    :param fetched: tuple[Interval, ...]; validated visits in date order.
    :param unfetched: tuple[DateRange, ...]; requested ranges that were not visited.
    :param observed_start: date | None; earliest accepted record date.
    :param observed_end: date | None; latest accepted record date.
    :param row_count: int; number of accepted records.
    :param traversal: State; whether the source traversal finished.
    return instance: Coverage; immutable coverage summary.
    """

    requested: DateRange | None
    fetched: tuple[Interval, ...] = ()
    unfetched: tuple[DateRange, ...] = ()
    observed_start: date | None = None
    observed_end: date | None = None
    row_count: int = 0
    traversal: State = "failed"


@dataclass(frozen=True)
class PriceRow:
    """Hold one daily bar exactly as the provider reported it, with decimal strings.

    Volume and amount stay in the provider's native units.

    :param trade_date: date; trading day.
    :param open: str; opening price as a decimal string.
    :param high: str; highest price as a decimal string.
    :param low: str; lowest price as a decimal string.
    :param close: str; closing price as a decimal string.
    :param volume_native: str | None; volume in provider units, or None.
    :param amount_native: str | None; turnover in provider units, or None.
    :param volume_unit: str; always `source_native`.
    :param amount_unit: str; always `source_native`.
    :param returned_symbol: str | None; identity echoed by the provider, or None.
    :param source_row: JsonValue; original row cells.
    return instance: PriceRow; immutable price record.
    """

    trade_date: date
    open: str
    high: str
    low: str
    close: str
    volume_native: str | None
    amount_native: str | None
    volume_unit: str = "source_native"
    amount_unit: str = "source_native"
    returned_symbol: str | None = None
    source_row: JsonValue = None


@dataclass(frozen=True)
class FactRow:
    """Hold one financial value that a company reported to the SEC, with the filing that reported it.

    The same period can appear in several filings, for example as a comparative
    figure one year later. Each appearance is its own row with its own filing date,
    so a reader can select only the values that were public on a chosen day.

    :param taxonomy: str; group of the concept, such as `us-gaap`, `ifrs-full`, or `dei`.
    :param concept: str; concept name, such as `NetIncomeLoss`.
    :param label: str | None; SEC label of the concept, or None when the SEC gives none.
    :param unit: str; unit of the value, such as `USD`, `shares`, or `USD/shares`.
    :param period_start: date | None; first day of the period, or None for a value at one moment.
    :param period_end: date; last day of the period, or the moment of the value.
    :param value: str; exact decimal text of the value, never rounded.
    :param accession: str; SEC identifier of the filing, such as `0000320193-26-000026`.
    :param fiscal_year: int | None; fiscal year of the filing, or None.
    :param fiscal_period: str | None; fiscal period of the filing, such as `FY` or `Q2`, or None.
    :param form: str | None; form type of the filing, such as `10-K`, or None.
    :param filed: date | None; day on which the filing became public, or None.
    :param frame: str | None; SEC calendar-frame label such as `CY2026Q1`, or None.
    return instance: FactRow; immutable reported value.
    """

    taxonomy: str
    concept: str
    label: str | None
    unit: str
    period_start: date | None
    period_end: date
    value: str
    accession: str
    fiscal_year: int | None
    fiscal_period: str | None
    form: str | None
    filed: date | None
    frame: str | None


@dataclass(frozen=True)
class StatementItem:
    """Hold one statement line item without zero-filling missing values.

    `value_kind` is `number` for numeric values, `string` for text such as `--`,
    `null` for an explicit null, and `missing` when the value was absent.

    :param ordinal: int; zero-based position in the source report.
    :param item_field: str | None; provider field code, or None.
    :param item_title: str; source line-item title.
    :param value_raw: str | None; original value text, or None.
    :param value_kind: Literal["string", "number", "null", "missing"]; value classification.
    :param value_decimal: str | None; parsed decimal string, or None when unclear.
    :param source_metadata: JsonValue; additional source fields or original cells.
    return instance: StatementItem; immutable line item.
    """

    ordinal: int
    item_field: str | None
    item_title: str
    value_raw: str | None
    value_kind: Literal["string", "number", "null", "missing"]
    value_decimal: str | None
    source_metadata: JsonValue = None


@dataclass(frozen=True)
class StatementRecord:
    """Hold one financial statement for one period, scope, and source column.

    :param report_date: date; balance-sheet date or period end.
    :param kind: StatementKind; income, balance, or cash_flow.
    :param scope: Scope; consolidated, parent, or unknown.
    :param source_scope: str; raw scope label from the source, possibly empty.
    :param source_key: str; Sina period key or `URI#locator` for filings.
    :param format: Literal["structured", "html", "pdf"]; source format.
    :param period_basis: str; `source_reported`, `instant`, `annual`, or `year_to_date`.
    :param publication_date: date | None; source publication date, or None.
    :param publication_date_raw: str | None; raw publication value, or None.
    :param source_update_at: datetime | None; source update time in UTC, or None.
    :param source_update_raw: str | None; raw update value, or None.
    :param currency: str | None; explicit currency, or None.
    :param unit: str | None; explicit unit such as `元`, or None.
    :param items: tuple[StatementItem, ...]; line items in source order.
    :param source_metadata: JsonValue; bounded additional source fields.
    :param locators: tuple[str, ...]; table, page, row, and column references.
    return instance: StatementRecord; immutable statement record.
    """

    report_date: date
    kind: StatementKind
    scope: Scope
    source_scope: str
    source_key: str
    format: Literal["structured", "html", "pdf"]
    period_basis: str
    publication_date: date | None
    publication_date_raw: str | None
    source_update_at: datetime | None
    source_update_raw: str | None
    currency: str | None
    unit: str | None
    items: tuple[StatementItem, ...]
    source_metadata: JsonValue = None
    locators: tuple[str, ...] = ()


@dataclass(frozen=True)
class CodeLink:
    """Record one official old-code to new-code relationship and its evidence.

    `effective_date` stays None because the source table does not state one.

    :param from_symbol: Symbol; old code.
    :param to_symbol: Symbol; new code.
    :param evidence_url: str; page that contained the row.
    :param headers: tuple[str, ...]; table header cells.
    :param cells: tuple[str, ...]; original row cells.
    :param effective_date: date | None; always None for BSE evidence.
    :param evidence_hash: str; SHA-256 over headers and cells.
    :param first_seen: datetime; first UTC observation.
    :param last_seen: datetime; latest UTC observation.
    return instance: CodeLink; immutable mapping evidence.
    """

    from_symbol: Symbol
    to_symbol: Symbol
    evidence_url: str
    headers: tuple[str, ...]
    cells: tuple[str, ...]
    effective_date: date | None
    evidence_hash: str
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True)
class FilingRef:
    """Identify one filing document to read and the identities it may legitimately name.

    :param symbol: Symbol; symbol whose statements the document should contain.
    :param uri: str; HTTPS URL or project-relative local path.
    :param report_date: date | None; candidate period from a clear title, or None.
    :param publication_date: date | None; adjacent source date, or None.
    :param origin: Literal["automatic", "explicit"]; discovery or user input.
    :param verified_aliases: tuple[Symbol, ...]; other codes proven by official mappings.
    return instance: FilingRef; immutable document reference.
    """

    symbol: Symbol
    uri: str
    report_date: date | None
    publication_date: date | None
    origin: Literal["automatic", "explicit"]
    verified_aliases: tuple[Symbol, ...] = ()


@dataclass(frozen=True)
class FetchResult:
    """Return the outcome of one price, statement, or reported-values traversal.

    `records` is homogeneous: only PriceRow, only StatementRecord, or only FactRow values.

    :param state: State; succeeded, partial, failed, or blocked.
    :param coverage: Coverage; visited and unvisited ranges.
    :param requests: tuple[RequestMeta, ...]; HTTP attempts.
    :param issues: tuple[Issue, ...]; warnings and errors.
    :param metadata: JsonValue; bounded provider metadata.
    :param records: tuple[PriceRow | StatementRecord | FactRow, ...]; accepted records.
    return instance: FetchResult; immutable traversal outcome.
    """

    state: State
    coverage: Coverage
    requests: tuple[RequestMeta, ...]
    issues: tuple[Issue, ...]
    metadata: JsonValue
    records: tuple[Any, ...] = ()


@dataclass(frozen=True)
class MappingResult:
    """Return the outcome of one official code-mapping lookup.

    Metadata contains `table_headers: list[str]` and `row_count: int`.

    :param state: State; lookup state.
    :param coverage: Coverage; requested is None for mappings.
    :param requests: tuple[RequestMeta, ...]; HTTP attempts.
    :param issues: tuple[Issue, ...]; warnings and errors.
    :param metadata: JsonValue; table summary.
    :param links: tuple[CodeLink, ...]; accepted links.
    return instance: MappingResult; immutable lookup outcome.
    """

    state: State
    coverage: Coverage
    requests: tuple[RequestMeta, ...]
    issues: tuple[Issue, ...]
    metadata: JsonValue
    links: tuple[CodeLink, ...] = ()


@dataclass(frozen=True)
class DiscoveryResult:
    """Return the filings found on index pages; discovery never proves exhaustion.

    Metadata contains `visited_urls`, `pending_urls`, and `coverage_verified=False`.

    :param state: State; discovery state.
    :param coverage: Coverage; candidate period points.
    :param requests: tuple[RequestMeta, ...]; HTTP attempts.
    :param issues: tuple[Issue, ...]; warnings and errors.
    :param metadata: JsonValue; navigation summary.
    :param candidates: tuple[FilingRef, ...]; selected periodic reports.
    return instance: DiscoveryResult; immutable discovery outcome.
    """

    state: State
    coverage: Coverage
    requests: tuple[RequestMeta, ...]
    issues: tuple[Issue, ...]
    metadata: JsonValue
    candidates: tuple[FilingRef, ...] = ()


@dataclass(frozen=True)
class FilingResult:
    """Return the outcome of reading one filing document.

    Metadata contains `headings`, `unparsed_locators`, and `attachments`.

    :param state: State; document state.
    :param coverage: Coverage; statement period points.
    :param requests: tuple[RequestMeta, ...]; HTTP attempts, empty for local files.
    :param issues: tuple[Issue, ...]; warnings and errors.
    :param metadata: JsonValue; extraction summary.
    :param reference: FilingRef | None; document that was read.
    :param format: Literal["html", "pdf"] | None; detected format.
    :param body_sha256: str | None; SHA-256 of the document bytes.
    :param status: Literal["parsed", "partial", "unparsed", "fetch_failed"]; extraction outcome.
    :param statements: tuple[StatementRecord, ...]; extracted statements.
    return instance: FilingResult; immutable document outcome.
    """

    state: State
    coverage: Coverage
    requests: tuple[RequestMeta, ...]
    issues: tuple[Issue, ...]
    metadata: JsonValue
    reference: FilingRef | None = None
    format: Literal["html", "pdf"] | None = None
    body_sha256: str | None = None
    status: Literal["parsed", "partial", "unparsed", "fetch_failed"] = "fetch_failed"
    statements: tuple[StatementRecord, ...] = ()


@dataclass(frozen=True)
class CheckSummary:
    """Describe one stored check row and return the summary record.

    A running check has no `finished_at`.

    :param check_id: int; storage identifier.
    :param key: CheckKey; task identity.
    :param span: DateRange | None; requested range.
    :param state: State; stored state.
    :param started_at: datetime; UTC start.
    :param finished_at: datetime | None; UTC finish, or None while running.
    :param coverage: Coverage; stored coverage.
    :param issues: tuple[Issue, ...]; stored issues.
    :param metadata: JsonValue; stored metadata.
    return instance: CheckSummary; immutable check summary.
    """

    check_id: int
    key: CheckKey
    span: DateRange | None
    state: State
    started_at: datetime
    finished_at: datetime | None
    coverage: Coverage
    issues: tuple[Issue, ...]
    metadata: JsonValue


@dataclass(frozen=True)
class CacheState:
    """Hold the stored links and check summaries relevant to one command.

    :param links: tuple[CodeLink, ...]; every stored code link.
    :param checks: tuple[CheckSummary, ...]; latest attempts and successes per key.
    return instance: CacheState; immutable cache snapshot.
    """

    links: tuple[CodeLink, ...]
    checks: tuple[CheckSummary, ...]


@dataclass(frozen=True)
class CollectRequest:
    """Describe one collect command after validation.

    :param symbol: Symbol; requested symbol.
    :param span: DateRange; inclusive requested range.
    :param refresh: bool; True bypasses daily gates.
    :param filings: tuple[FilingRef, ...]; explicit documents, at most five.
    return instance: CollectRequest; immutable collect request.
    """

    symbol: Symbol
    span: DateRange
    refresh: bool = False
    filings: tuple[FilingRef, ...] = ()


@dataclass(frozen=True)
class QueryRequest:
    """Describe one show command after validation.

    :param symbol: Symbol; requested symbol.
    :param span: DateRange; inclusive query range.
    :param dataset: Literal["prices", "statements", "filings", "coverage", "facts"]; data to return.
    :param provider: str; `auto` or a provider name allowed for the dataset.
    :param adjustment: Literal["raw", "forward"]; price adjustment.
    :param scope: Literal["all", "consolidated", "parent", "unknown"]; statement scope filter.
    :param kind: Literal["all", "income", "balance", "cash_flow"]; statement kind filter.
    :param versions: Literal["latest", "all"]; latest values or every observed version.
    :param limit: int; maximum records to return; 0 returns group summaries only.
    :param offset: int; number of records to skip.
    :param concepts: tuple[str, ...]; for the dataset `facts`: concept names whose values to return; empty
        returns one summary record per concept and unit instead of values.
    :param filed_until: date | None; for the dataset `facts`: return only values filed on or before this day.
    return instance: QueryRequest; immutable query request.
    """

    symbol: Symbol
    span: DateRange
    dataset: Literal["prices", "statements", "filings", "coverage", "facts"]
    provider: str = "auto"
    adjustment: Literal["raw", "forward"] = "raw"
    scope: Literal["all", "consolidated", "parent", "unknown"] = "all"
    kind: Literal["all", "income", "balance", "cash_flow"] = "all"
    versions: Literal["latest", "all"] = "latest"
    limit: int = 200
    offset: int = 0
    concepts: tuple[str, ...] = ()
    filed_until: date | None = None


class Stats(TypedDict):
    """Describe command statistics: request count, received bytes, revisions, and time."""

    http_attempts: int
    received_bytes: int
    new_revisions: int
    reused_revisions: int
    elapsed_seconds: str


class Pagination(TypedDict):
    """Describe one record page: limit, offset, returned count, and the next offset or None."""

    limit: int
    offset: int
    returned: int
    next_offset: int | None


class OutputRecord(TypedDict):
    """Describe one output record: type, check, symbol, provider, keys, observations, and data."""

    type: str
    check_id: int
    symbol_id: str | None
    provider: str
    record_key: str
    content_hash: str | None
    first_observed_at: str
    last_observed_at: str
    observation_count: int
    data: dict[str, JsonValue]


class Group(TypedDict):
    """Describe one dataset group with its selection, freshness, coverage, and records."""

    symbol_id: str | None
    provider: str
    dataset: str
    adjustment: str
    resource: str
    selected_check_id: int | None
    selection_reason: str
    state: str
    freshness: str
    coverage: dict[str, JsonValue]
    market_completeness: str
    absent_dates_reason: str
    last_attempt_at: str | None
    last_success_at: str | None
    last_full_recheck_at: str | None
    records: list[OutputRecord]


class QueryPage(TypedDict):
    """Describe one query result page: groups, pagination, and issues as JSON-ready objects."""

    groups: list[Group]
    pagination: Pagination
    issues: list[dict[str, JsonValue]]


class CommandResult(TypedDict):
    """Describe the command envelope written to standard output as JSON."""

    schema_version: int
    command: str
    status: str
    requested_symbol: str | None
    resolved_symbols: list[dict[str, JsonValue]]
    code_links: list[dict[str, JsonValue]]
    requested_range: dict[str, JsonValue] | None
    run_id: str | None
    source_conditions_status: str
    datasets: list[Group]
    issues: list[dict[str, JsonValue]]
    stats: Stats
    pagination: Pagination | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CollectorError(Exception):
    """Carry one Issue across a module boundary and return the exception instance.

    :param issue: Issue; the reason for the failure.
    return instance: CollectorError; raisable error.
    """

    def __init__(self, issue: Issue) -> None:
        """Store the issue and use its message as the exception text.

        :param issue: Issue; the reason for the failure.
        return None: None; no value is returned.
        """
        super().__init__(issue.message)
        self.issue = issue


class InputError(CollectorError):
    """Report invalid user input before any affected I/O starts."""


class StorageError(CollectorError):
    """Report a storage failure after rolling back the current transaction."""


class SourceError(CollectorError):
    """Report a source failure together with the HTTP attempts that led to it.

    :param issue: Issue; the reason for the failure.
    :param requests: tuple[RequestMeta, ...]; attempts made before the failure.
    return instance: SourceError; raisable error.
    """

    def __init__(self, issue: Issue, requests: tuple[RequestMeta, ...] = ()) -> None:
        """Store the issue and the attempts.

        :param issue: Issue; the reason for the failure.
        :param requests: tuple[RequestMeta, ...]; attempts made before the failure.
        return None: None; no value is returned.
        """
        super().__init__(issue)
        self.requests = requests


def make_issue(code: str, message: str, dataset: Dataset, *, severity: Literal["warning", "error"] = "error",
               symbol: Symbol | None = None, provider: Provider | None = None, url: str | None = None,
               report_date: date | None = None, retryable: bool = False) -> Issue:
    """Build an Issue from keyword values and return it.

    This helper keeps issue construction short and consistent across modules.

    :param code: str; issue code.
    :param message: str; human-readable message.
    :param dataset: Dataset; affected dataset.
    :param severity: Literal["warning", "error"]; issue impact, default error.
    :param symbol: Symbol | None; affected symbol.
    :param provider: Provider | None; affected provider.
    :param url: str | None; affected URL.
    :param report_date: date | None; affected period.
    :param retryable: bool; whether a later attempt may succeed.
    return issue: Issue; constructed issue.
    """
    return Issue(code=code, message=message, severity=severity, dataset=dataset,
                 symbol_id=symbol.symbol_id if symbol else None, provider=provider, url=url,
                 report_date=report_date, retryable=retryable)


# ---------------------------------------------------------------------------
# JSON, hashes, time, and decimals
# ---------------------------------------------------------------------------


def format_utc(value: datetime) -> str:
    """Format an aware datetime as an ISO UTC string ending in `Z` and return it.

    Naive datetimes raise ValueError because their zone is unknown.

    :param value: datetime; aware timestamp.
    return text: str; for example `2026-11-13T01:02:03Z`.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetime cannot be serialized")
    text = value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    return text.replace("+00:00", "Z").replace(".000000Z", "Z")


def parse_utc(text: str) -> datetime:
    """Parse a stored `...Z` timestamp into an aware UTC datetime and return it.

    :param text: str; ISO timestamp ending in `Z`.
    return value: datetime; aware UTC timestamp.
    """
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def compute_china_day(value: datetime) -> date:
    """Convert an aware timestamp to the calendar day in China (fixed UTC+08:00) and return it.

    :param value: datetime; aware timestamp.
    return day: date; China calendar day.
    """
    return value.astimezone(CHINA_TZ).date()


def _find_sunday(year: int, month: int, ordinal: int) -> int:
    """Find the day of the month of the first, second, or later Sunday and return it.

    :param year: int; calendar year.
    :param month: int; calendar month, 1 to 12.
    :param ordinal: int; 1 for the first Sunday, 2 for the second, and so on.
    return day: int; day of the month.
    """
    first_weekday = date(year, month, 1).weekday()  # Monday is 0, Sunday is 6
    return 1 + (6 - first_weekday) % 7 + 7 * (ordinal - 1)


def compute_new_york_day(value: datetime) -> date:
    """Convert an aware timestamp to the calendar day in New York and return it.

    New York is four hours behind UTC from 07:00 UTC on the second Sunday of March
    until 06:00 UTC on the first Sunday of November, and five hours behind UTC
    otherwise. This is the rule in force since 2007. The function is meant for
    current dates; it does not know older rules. It needs no time-zone database.

    :param value: datetime; aware timestamp.
    return day: date; New York calendar day.
    """
    moment = value.astimezone(timezone.utc)
    summer_start = datetime(moment.year, 3, _find_sunday(moment.year, 3, 2), 7, tzinfo=timezone.utc)
    summer_end = datetime(moment.year, 11, _find_sunday(moment.year, 11, 1), 6, tzinfo=timezone.utc)
    hours = 4 if summer_start <= moment < summer_end else 5
    return (moment - timedelta(hours=hours)).date()


def compute_market_day(symbol_id: str | None, value: datetime, provider: str = "") -> date:
    """Convert an aware timestamp to the calendar day of the market that a check belongs to and return it.

    A US symbol and the provider `sec` use the New York day. Every other check,
    including the global Beijing code-mapping check, uses the China day. The market
    day gates the once-a-day check, decides freshness, and marks today's price row.

    :param symbol_id: str | None; canonical symbol such as `US:AAPL`, or None for a global check.
    :param value: datetime; aware timestamp.
    :param provider: str; provider of the check; only `sec` changes the result.
    return day: date; New York or China calendar day.
    """
    if provider == "sec" or (symbol_id or "").startswith("US:"):
        return compute_new_york_day(value)
    return compute_china_day(value)


def convert_to_json(value: Any) -> JsonValue:
    """Convert records, dates, decimals, and containers into plain JSON values and return them.

    Dataclasses become objects with every field (None values stay as null keys),
    except fields declared with `repr=False`: those hold private values, such as
    the SEC contact line, and are left out. Dates become ISO strings, aware
    datetimes become UTC strings ending in `Z`, decimals become strings, and
    tuples become lists. Unsupported types raise TypeError.

    :param value: Any; record or JSON-compatible value.
    return json_value: JsonValue; plain JSON value.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        raise TypeError("floats are not allowed; use decimal strings")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal")
        return format(value, "f")
    if isinstance(value, datetime):
        return format_utc(value)
    if isinstance(value, date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: convert_to_json(getattr(value, f.name)) for f in dataclasses.fields(value) if f.repr}
    if isinstance(value, (list, tuple)):
        return [convert_to_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): convert_to_json(v) for k, v in value.items()}
    raise TypeError(f"unsupported JSON type: {type(value).__name__}")


def encode_canonical_json(value: JsonValue) -> str:
    """Serialize a JSON value with sorted keys and compact separators and return the text.

    The output keeps non-ASCII characters and rejects NaN or infinity, so equal
    values always produce equal text and equal hashes.

    :param value: JsonValue; plain JSON value (convert records with convert_to_json first).
    return text: str; canonical JSON text.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def compute_sha256(value: bytes) -> str:
    """Hash bytes with SHA-256 and return the lowercase hexadecimal digest.

    :param value: bytes; exact bytes to hash.
    return digest: str; 64 lowercase hexadecimal characters.
    """
    return hashlib.sha256(value).hexdigest()


def hash_json(value: Any) -> str:
    """Hash the canonical UTF-8 JSON form of a value and return the digest.

    :param value: Any; record or JSON value accepted by convert_to_json.
    return digest: str; lowercase SHA-256 hex digest.
    """
    return compute_sha256(encode_canonical_json(convert_to_json(value)).encode("utf-8"))


_PLAIN_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?")
_GROUPED_NUMBER = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_MISSING_MARKERS = frozenset({"", "-", "--", "---", "—", "——", "/", "不适用", "N/A", "n/a", "null", "None"})


def parse_decimal_text(raw: Any) -> tuple[Literal["string", "number", "null", "missing"], str | None, str | None, bool]:
    """Classify one source value and parse it as an exact decimal when it is unambiguous.

    JSON numbers and decimal strings become `number`. Grouped thousands such as
    `12,345.00` and accounting negatives such as `(1,234.00)` are accepted. Missing
    markers such as `--` stay `string` without a decimal. Any other text that contains
    digits is treated as ambiguous: the decimal is None and the fourth value is True.
    NaN and infinity are never accepted.

    :param raw: Any; JSON scalar or table-cell text; the sentinel `...` means the value was absent.
    return parsed: tuple[str, str | None, str | None, bool]; (value_kind, value_raw, value_decimal, ambiguous).
    """
    if raw is Ellipsis:
        return "missing", None, None, False
    if raw is None:
        return "null", None, None, False
    if isinstance(raw, bool):
        return "string", str(raw).lower(), None, True
    if isinstance(raw, (int, Decimal)):
        dec = Decimal(raw)
        if not dec.is_finite():
            return "string", str(raw), None, True
        return "number", format(dec, "f"), format(dec, "f"), False
    if isinstance(raw, float):
        return "string", repr(raw), None, True
    text = str(raw)
    stripped = text.strip().replace(" ", "")
    if stripped in _MISSING_MARKERS:
        return "string", text, None, False
    negative = False
    candidate = stripped
    if candidate.startswith("(") and candidate.endswith(")"):
        negative = True
        candidate = candidate[1:-1].strip()
    if _PLAIN_NUMBER.fullmatch(candidate) or _GROUPED_NUMBER.fullmatch(candidate):
        try:
            dec = Decimal(candidate.replace(",", ""))
        except InvalidOperation:
            return "string", text, None, True
        if not dec.is_finite() or (negative and candidate[:1] in "+-"):
            return "string", text, None, True
        if negative:
            dec = -dec
        return "number", text, format(dec, "f"), False
    if any(ch.isdigit() for ch in stripped):
        return "string", text, None, True
    return "string", text, None, False


def parse_strict_json(body: bytes) -> Any:
    """Decode a UTF-8 JSON body without evaluating JavaScript and return the parsed value.

    Fractions become Decimal values so no binary rounding happens. NaN and infinity
    are rejected. A decoding failure raises ValueError.

    :param body: bytes; response body.
    return value: Any; parsed JSON with Decimal fractions.
    """
    text = body.decode("utf-8")
    if text.startswith("﻿"):
        text = text[1:]

    def reject_constant(name: str) -> Any:
        """Reject non-finite JSON constants by raising ValueError.

        :param name: str; constant name such as `NaN`.
        return value: Any; never returns.
        """
        raise ValueError(f"non-finite JSON constant {name}")

    return json.loads(text, parse_float=Decimal, parse_constant=reject_constant)


def convert_source_json(value: Any) -> JsonValue:
    """Convert a parsed source JSON value into a storable JSON value and return it.

    Decimal fractions become strings so they keep their exact digits.

    :param value: Any; value from parse_strict_json.
    return json_value: JsonValue; storable JSON value.
    """
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {str(k): convert_source_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [convert_source_json(v) for v in value]
    return value


def bound_metadata(value: JsonValue, limit: int = FIELD_MAX_BYTES) -> JsonValue:
    """Return the value unchanged when its canonical JSON fits the limit, otherwise a marker.

    Oversized metadata becomes `{"omitted": "field_size_limit", "bytes": N}`.

    :param value: JsonValue; metadata to bound.
    :param limit: int; maximum UTF-8 size in bytes.
    return bounded: JsonValue; original or marker object.
    """
    size = len(encode_canonical_json(value).encode("utf-8"))
    if size <= limit:
        return value
    return {"omitted": "field_size_limit", "bytes": size}


# ---------------------------------------------------------------------------
# URL and HTML helpers
# ---------------------------------------------------------------------------


def normalize_source_url(url: str) -> str:
    """Upgrade an HTTP Sina link to HTTPS and return the URL; other URLs are returned unchanged.

    :param url: str; absolute URL.
    return url: str; possibly upgraded URL.
    """
    parts = urlsplit(url)
    if parts.scheme == "http" and (parts.hostname or "") in SINA_HOSTS:
        return "https" + url[4:]
    return url


def validate_remote_url(url: str) -> str | None:
    """Check that a URL is an allowed HTTPS source URL and return None, or return the reason it fails.

    The URL must use HTTPS on port 443, name an allowed host exactly, contain no
    user information or fragment, and be at most 2,048 characters long.

    :param url: str; absolute URL to check.
    return reason: str | None; None when allowed, otherwise a short reason.
    """
    if len(url) > 2048:
        return "URL is longer than 2,048 characters"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "URL cannot be parsed"
    if parts.scheme != "https":
        return "URL does not use HTTPS"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "URL contains user information"
    if parts.fragment or "#" in url:
        return "URL contains a fragment"
    if port not in (None, 443):
        return "URL uses a port other than 443"
    if (parts.hostname or "") not in ALLOWED_HOSTS:
        return "URL host is not an allowed source host"
    return None


_CHARSET_PATTERN = re.compile(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9_\-]+)", re.IGNORECASE)


def decode_html(body: bytes, content_type: str) -> str:
    """Decode an HTML body using its declared charset and return the text.

    GBK, GB2312, and GB18030 decode strictly as GB18030. UTF-8 (with or without a
    byte-order mark) is supported. Without a declaration the body must be strict
    UTF-8. Any other charset or decoding failure raises ValueError with the code
    `unsupported_charset` in the message.

    :param body: bytes; HTML bytes.
    :param content_type: str; Content-Type header value, possibly empty.
    return text: str; decoded HTML text.
    """
    declared = None
    match = _CHARSET_PATTERN.search(content_type.encode("ascii", "ignore"))
    if match is None:
        match = _CHARSET_PATTERN.search(body[:4096])
    if match is not None:
        declared = match.group(1).decode("ascii").lower()
    if body.startswith(b"\xef\xbb\xbf"):
        declared = "utf-8"
        body = body[3:]
    try:
        if declared in ("gbk", "gb2312", "gb18030", "x-gbk"):
            return body.decode("gb18030")
        if declared in (None, "utf-8", "utf8"):
            return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"unsupported_charset: body is not valid {declared or 'utf-8'}") from exc
    raise ValueError(f"unsupported_charset: {declared}")


@dataclass
class HtmlBlock:
    """Hold one visible text block or one data table extracted from HTML.

    A table is `simple=False` when it uses row spans, or column spans other than a
    full-width single-cell label row; such tables are never converted into
    statements. A full-width label row (for example `流动资产：` spanning every
    column) is expanded to the title followed by empty cells.

    :param kind: Literal["text", "table"]; block type.
    :param text: str; visible text for text blocks, empty for tables.
    :param rows: list[list[str]]; table rows of cell texts, empty for text blocks.
    :param simple: bool; False when the table geometry is unsupported.
    :param links: list[tuple[str, str]]; (href, text) pairs found in a text block.
    :param index: int; zero-based table index among all tables.
    return instance: HtmlBlock; mutable block record used during parsing.
    """

    kind: Literal["text", "table"]
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    simple: bool = True
    links: list[tuple[str, str]] = field(default_factory=list)
    index: int = -1


@dataclass
class _TableFrame:
    """Track one open table while parsing.

    A frame becomes `layout=True` when another table opens inside it. Layout tables
    are page scaffolding: their cell text flows into ordinary text blocks and they
    never become table blocks.

    :param block: HtmlBlock; table block being built.
    :param layout: bool; True for page-layout tables.
    :param row: list[str] | None; cells of the open row.
    :param row_spans: list[int]; column spans of the open row's cells.
    :param cell: list[str] | None; text pieces of the open cell.
    :param label_rows: list[int]; indexes of rows that are single-cell spanned labels.
    return instance: _TableFrame; mutable frame.
    """

    block: HtmlBlock
    layout: bool = False
    row: list[str] | None = None
    row_spans: list[int] = field(default_factory=list)
    cell: list[str] | None = None
    label_rows: list[int] = field(default_factory=list)


_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "section",
                         "article", "body", "html", "center", "hr", "dd", "dt", "dl", "header", "footer"})
_SPACES = re.compile(r"[ \t\r\f\v　 ]+")


class HtmlBlockParser(HTMLParser):
    """Split HTML into visible text blocks and data tables while ignoring scripts and styles.

    Tables that contain other tables are treated as page layout: their text flows
    into text blocks and only the innermost tables become table blocks. Every link,
    at any depth, is recorded in `link_contexts` as (href, anchor text, up to 200
    characters of visible text before the link), so callers can find dates printed
    next to links. Links outside data tables are also attached to their text block.
    Call `feed()` then `close()` and read `blocks`.

    return instance: HtmlBlockParser; parser with an empty block list.
    """

    def __init__(self) -> None:
        """Initialize empty parser state.

        return None: None; no value is returned.
        """
        super().__init__(convert_charrefs=True)
        self.blocks: list[HtmlBlock] = []
        self.link_contexts: list[tuple[str, str, str]] = []
        self._text: list[str] = []
        self._links: list[tuple[str, str]] = []
        self._recent = ""
        self._skip_depth = 0
        self._stack: list[_TableFrame] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self._pre_anchor = ""
        self._table_count = 0

    def _data_frame(self) -> _TableFrame | None:
        """Return the innermost open table when it is a data table, otherwise None.

        return frame: _TableFrame | None; open data table.
        """
        return self._stack[-1] if self._stack and not self._stack[-1].layout else None

    def _flush_text(self) -> None:
        """Close the current text block when it contains visible text.

        return None: None; no value is returned.
        """
        text = _SPACES.sub(" ", "".join(self._text)).strip()
        if text or self._links:
            self.blocks.append(HtmlBlock(kind="text", text=text, links=list(self._links)))
        self._text = []
        self._links = []

    def _convert_to_layout(self, frame: _TableFrame) -> None:
        """Turn a table that turned out to contain another table into layout and keep its text.

        Rows collected so far become text blocks, and the open cell's text continues
        as ordinary text.

        :param frame: _TableFrame; table to convert.
        return None: None; no value is returned.
        """
        frame.layout = True
        for row in frame.block.rows:
            self._text.append(" ".join(c for c in row if c))
            self._flush_text()
        frame.block.rows = []
        if frame.row:
            self._text.append(" ".join(c for c in frame.row if c) + " ")
        if frame.cell:
            self._text.append("".join(frame.cell))
        frame.row, frame.cell, frame.row_spans = None, None, []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track scripts, tables, rows, cells, links, and block boundaries.

        :param tag: str; lowercase tag name.
        :param attrs: list[tuple[str, str | None]]; tag attributes.
        return None: None; no value is returned.
        """
        if tag in ("script", "style", "noscript", "template"):
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        attr = dict(attrs)
        frame = self._data_frame()
        if tag == "table":
            if frame is not None:
                self._convert_to_layout(frame)
            self._flush_text()
            self._stack.append(_TableFrame(block=HtmlBlock(kind="table", index=self._table_count)))
            self._table_count += 1
        elif tag == "tr" and frame is not None:
            frame.row, frame.row_spans = [], []
        elif tag in ("td", "th") and frame is not None:
            rowspan = (attr.get("rowspan") or "1").strip() or "1"
            colspan = (attr.get("colspan") or "1").strip() or "1"
            if rowspan != "1" or not colspan.isdigit() or int(colspan) < 1:
                frame.block.simple = False
            frame.row_spans.append(int(colspan) if colspan.isdigit() else 1)
            frame.cell = []
        elif tag in ("td", "th", "tr") and self._stack:
            self._flush_text()
        elif tag == "a":
            self._href = attr.get("href")
            self._anchor = []
            self._pre_anchor = self._recent[-200:]
        elif tag in _BLOCK_TAGS:
            if frame is None:
                self._flush_text()
            elif frame.cell is not None:
                frame.cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        """Close cells, rows, tables, links, and block boundaries.

        :param tag: str; lowercase tag name.
        return None: None; no value is returned.
        """
        if tag in ("script", "style", "noscript", "template"):
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        frame = self._data_frame()
        if tag in ("td", "th") and frame is not None and frame.cell is not None:
            if frame.row is None:
                frame.row = []
            frame.row.append(re.sub(r"\s+", " ", "".join(frame.cell)).strip())
            frame.cell = None
        elif tag == "tr" and frame is not None and frame.row is not None:
            if frame.row:
                if any(span != 1 for span in frame.row_spans):
                    if len(frame.row) == 1:
                        frame.label_rows.append(len(frame.block.rows))
                    else:
                        frame.block.simple = False
                frame.block.rows.append(frame.row)
            frame.row, frame.row_spans = None, []
        elif tag == "table" and self._stack:
            closed = self._stack.pop()
            if closed.layout:
                self._flush_text()
            else:
                self._finish_table(closed)
        elif tag == "a" and self._href is not None:
            text = re.sub(r"\s+", " ", "".join(self._anchor)).strip()
            if self._data_frame() is None:
                self._links.append((self._href, text))
            self.link_contexts.append((self._href, text, self._pre_anchor))
            self._href = None
            self._anchor = []
        elif tag in _BLOCK_TAGS and frame is None:
            self._flush_text()
        elif tag in ("td", "th", "tr") and self._stack:
            self._flush_text()

    def _finish_table(self, frame: _TableFrame) -> None:
        """Expand full-width label rows, check geometry, and append the finished data table.

        A spanned single-cell row is accepted only when the table's other rows give
        a width of at least two columns; it becomes the label followed by empty cells.

        :param frame: _TableFrame; closed data table.
        return None: None; no value is returned.
        """
        block = frame.block
        widths = [len(r) for i, r in enumerate(block.rows) if i not in frame.label_rows]
        width = max(widths) if widths else 0
        for index in frame.label_rows:
            if width < 2:
                block.simple = False
                continue
            block.rows[index] = block.rows[index] + [""] * (width - 1)
        self.blocks.append(block)

    def handle_data(self, data: str) -> None:
        """Append visible text to the current cell, anchor, or text block.

        :param data: str; decoded text.
        return None: None; no value is returned.
        """
        if self._skip_depth:
            return
        self._recent = (self._recent + data)[-400:]
        if self._href is not None:
            self._anchor.append(data)
        frame = self._data_frame()
        if frame is None:
            self._text.append(data)
        elif frame.cell is not None:
            frame.cell.append(data)

    def close(self) -> None:
        """Finish parsing, close unterminated tables as non-simple, and flush the final text block.

        return None: None; no value is returned.
        """
        super().close()
        while self._stack:
            frame = self._stack.pop()
            if not frame.layout:
                frame.block.simple = False
                self._finish_table(frame)
        self._flush_text()


def parse_html_blocks(text: str) -> HtmlBlockParser:
    """Parse decoded HTML into blocks and return the finished parser.

    :param text: str; decoded HTML.
    return parser: HtmlBlockParser; parser whose `blocks` and `link_contexts` are complete.
    """
    parser = HtmlBlockParser()
    parser.feed(text)
    parser.close()
    return parser


# ---------------------------------------------------------------------------
# Bounded HTTP client
# ---------------------------------------------------------------------------


def create_default_session() -> Any:
    """Create a curl_cffi session with Chrome impersonation, TLS verification, and no proxy.

    The import happens here so that modules can be imported without the dependency
    during tests that inject a fake session.

    return session: curl_cffi.requests.Session; configured session.
    """
    from curl_cffi import requests as curl_requests

    return curl_requests.Session(impersonate="chrome", verify=True, trust_env=False)


def create_sec_session() -> Any:
    """Create a plain curl_cffi session for SEC hosts, with TLS verification and no proxy.

    The session does not imitate a browser. The SEC asks automated tools to
    identify themselves, so SEC requests carry only the headers that the client
    sets: the declared contact line and the accepted encodings.

    return session: curl_cffi.requests.Session; configured session.
    """
    from curl_cffi import requests as curl_requests

    return curl_requests.Session(verify=True, trust_env=False)


class HttpClient:
    """Perform bounded, sequential HTTPS GET requests and return complete bodies.

    The client counts every attempt, redirect, and retry against `max_requests`,
    enforces a run deadline, a per-request timeout, a gap between request starts
    (one second, or a quarter of a second before a request to an SEC host, which
    stays below the SEC's limit of ten requests per second), per-response and total
    byte limits, and at most three validated redirects. Connection failures, timeouts, and HTTP 408/500/502/503/504 are retried
    once after two seconds. HTTP 429 cools the host for the rest of the run. Failures
    raise SourceError carrying the attempts. The client is not thread-safe.

    Requests to SEC hosts (`SEC_HOSTS`) use a separate plain session and carry the
    contact line from `sec_access` as their `User-Agent` header. The client calls
    `sec_access` only before its first SEC request and keeps the answer. Without a
    contact line, an SEC request raises SourceError with the block code before any
    network traffic, and no attempt is counted. Requests to every other host use
    the browser-like session and never carry the contact line.

    :param max_requests: int; total attempt budget, 1 to 512.
    :param run_seconds: float; run deadline in seconds.
    :param request_timeout: float; timeout per attempt in seconds.
    :param start_gap: float; minimum seconds between request starts.
    :param sec_start_gap: float; minimum seconds before the start of a request to an SEC host.
    :param retry_delay: float; seconds before the single retry.
    :param total_bytes: int; total body bytes allowed for the run.
    :param session_factory: Callable[[], Any] | None; creates the HTTP session; tests inject fakes.
    :param sec_access: Callable[[], SecAccess] | None; loads the SEC contact on demand; None blocks SEC requests.
    :param sec_session_factory: Callable[[], Any] | None; creates the plain SEC session; tests inject fakes.
    :param monotonic: Callable[[], float]; monotonic clock.
    :param sleep: Callable[[float], None]; sleep function.
    :param clock: UtcClock; aware UTC clock for attempt timestamps.
    return instance: HttpClient; ready client that owns its session.
    """

    def __init__(self, *, max_requests: int = 256, run_seconds: float = 900.0, request_timeout: float = 12.0,
                 start_gap: float = 1.0, sec_start_gap: float = 0.25, retry_delay: float = 2.0,
                 total_bytes: int = TOTAL_MAX_BYTES,
                 session_factory: Callable[[], Any] | None = None,
                 sec_access: Callable[[], SecAccess] | None = None,
                 sec_session_factory: Callable[[], Any] | None = None,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
                 clock: UtcClock = lambda: datetime.now(timezone.utc)) -> None:
        """Store limits and create the session lazily on the first request.

        return None: None; no value is returned.
        """
        self.max_requests = max_requests
        self.request_timeout = request_timeout
        self.start_gap = start_gap
        self.sec_start_gap = sec_start_gap
        self.retry_delay = retry_delay
        self.total_bytes = total_bytes
        self._session_factory = session_factory or create_default_session
        self._session: Any = None
        self._sec_access_loader = sec_access
        self._sec_access: SecAccess | None = None
        self._sec_session_factory = sec_session_factory or create_sec_session
        self._sec_session: Any = None
        self._monotonic = monotonic
        self._sleep = sleep
        self._clock = clock
        self._deadline = monotonic() + run_seconds
        self._last_start: float | None = None
        self._cooldowns: dict[str, float] = {}
        self.attempts = 0
        self.received_bytes = 0

    def close(self) -> None:
        """Close the browser-like session and the SEC session when they were created.

        return None: None; no value is returned.
        """
        try:
            if self._session is not None:
                self._session.close()
        finally:
            self._session = None
            if self._sec_session is not None:
                try:
                    self._sec_session.close()
                finally:
                    self._sec_session = None

    def _resolve_sec_access(self) -> SecAccess:
        """Load the SEC access record on the first call, keep it, and return it.

        Without a loader the record is blocked with the code `sec_contact_missing`.

        return access: SecAccess; contact line for SEC requests, or the reason why they are blocked.
        """
        if self._sec_access is None:
            self._sec_access = self._sec_access_loader() if self._sec_access_loader is not None else SecAccess()
        return self._sec_access

    def get(self, url: str, *, max_bytes: int) -> ResponseData:
        """Fetch one URL with validated redirects and return the complete HTTP 200 body.

        Raises SourceError with code `budget_exhausted`, `work_limit`,
        `response_too_large`, `transport_error`, `rate_limited`, `redirect_limit`, or
        `source_access_error`. Intermediate redirect bodies are discarded.

        :param url: str; allowed HTTPS URL.
        :param max_bytes: int; maximum body size for each attempt.
        return response: ResponseData; final response and every attempt.
        """
        metas: list[RequestMeta] = []
        current = normalize_source_url(url)
        for _hop in range(4):
            reason = validate_remote_url(current)
            if reason is not None:
                raise SourceError(self._issue("source_access_error", f"{reason}: {current}", current), tuple(metas))
            status, body, headers = self._attempt_with_retry(current, max_bytes, metas)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("location") or headers.get("Location")
                if not location:
                    raise SourceError(self._issue("source_access_error", "redirect without Location", current),
                                      tuple(metas))
                current = normalize_source_url(urljoin(current, location))
                continue
            if status == 200:
                content_type = headers.get("content-type") or headers.get("Content-Type") or ""
                return ResponseData(url=current, body=body, content_type=content_type, status=200,
                                    requests=tuple(metas))
            raise SourceError(self._issue("source_access_error", f"HTTP status {status}", current), tuple(metas))
        raise SourceError(self._issue("redirect_limit", "more than three redirects", url), tuple(metas))

    def _issue(self, code: str, message: str, url: str, *, retryable: bool = False) -> Issue:
        """Build an HTTP issue for the given URL and return it.

        The dataset is set to `prices` as a neutral placeholder; adapters re-label it.

        :param code: str; issue code.
        :param message: str; explanation.
        :param url: str; affected URL.
        :param retryable: bool; whether a later attempt may succeed.
        return issue: Issue; HTTP issue.
        """
        return Issue(code=code, message=message, severity="error", dataset="prices", url=url, retryable=retryable)

    def _attempt_with_retry(self, url: str, max_bytes: int, metas: list[RequestMeta]) -> tuple[int, bytes, dict]:
        """Perform one attempt and at most one retry for transient failures and return the response.

        :param url: str; validated URL.
        :param max_bytes: int; per-attempt byte limit.
        :param metas: list[RequestMeta]; attempt log that this method appends to.
        return response: tuple[int, bytes, dict]; (status, body, headers).
        """
        for attempt in (0, 1):
            try:
                status, body, headers = self._attempt_once(url, max_bytes, metas)
            except _TransientError as exc:
                if attempt == 0 and self._monotonic() + self.retry_delay < self._deadline:
                    self._sleep(self.retry_delay)
                    continue
                raise SourceError(self._issue("transport_error", str(exc), url, retryable=True), tuple(metas)) from None
            if status in RETRY_STATUSES and attempt == 0 and self._monotonic() + self.retry_delay < self._deadline:
                self._sleep(self.retry_delay)
                continue
            if status == 429:
                host = urlsplit(url).hostname or ""
                retry_after = headers.get("retry-after") or headers.get("Retry-After") or ""
                self._cooldowns[host] = self._deadline
                raise SourceError(self._issue("rate_limited", f"HTTP 429; Retry-After={retry_after or 'absent'}",
                                              url, retryable=True), tuple(metas))
            return status, body, headers
        raise AssertionError("unreachable")

    def _attempt_once(self, url: str, max_bytes: int, metas: list[RequestMeta]) -> tuple[int, bytes, dict]:
        """Perform exactly one bounded GET attempt and return its status, body, and headers.

        :param url: str; validated URL.
        :param max_bytes: int; byte limit for this body.
        :param metas: list[RequestMeta]; attempt log that this method appends to.
        return response: tuple[int, bytes, dict]; (status, body, lowercase-key headers).
        """
        host = urlsplit(url).hostname or ""
        if host in self._cooldowns:
            raise SourceError(self._issue("rate_limited", "host is cooling down after HTTP 429", url, retryable=True),
                              tuple(metas))
        sec_line: str | None = None
        if host in SEC_HOSTS:
            access = self._resolve_sec_access()
            if access.user_agent is None:
                raise SourceError(self._issue(access.block_code, access.block_message, url), tuple(metas))
            sec_line = access.user_agent
        if self.attempts >= self.max_requests:
            raise SourceError(self._issue("budget_exhausted", "request budget is exhausted", url, retryable=True),
                              tuple(metas))
        gap = self.sec_start_gap if sec_line is not None else self.start_gap
        now = self._monotonic()
        if self._last_start is not None and now < self._last_start + gap:
            self._sleep(self._last_start + gap - now)
        if self._monotonic() >= self._deadline:
            raise SourceError(self._issue("work_limit", "run deadline reached", url, retryable=True), tuple(metas))
        self.attempts += 1
        self._last_start = self._monotonic()
        started_at = self._clock()
        buffer = bytearray()
        abort: list[str] = []

        def collect_chunk(chunk: bytes) -> int:
            """Append one received chunk unless a byte or time limit is exceeded.

            Returning CURL_WRITE_ABORT makes libcurl stop the transfer.

            :param chunk: bytes; received bytes.
            return written: int; chunk length, or CURL_WRITE_ABORT to stop.
            """
            if len(buffer) + len(chunk) > max_bytes:
                abort.append("response_too_large")
                return CURL_WRITE_ABORT
            if self.received_bytes + len(buffer) + len(chunk) > self.total_bytes:
                abort.append("response_too_large")
                return CURL_WRITE_ABORT
            if self._monotonic() >= self._deadline:
                abort.append("work_limit")
                return CURL_WRITE_ABORT
            buffer.extend(chunk)
            return len(chunk)

        if sec_line is not None and self._sec_session is None:
            self._sec_session = self._sec_session_factory()
        if sec_line is None and self._session is None:
            self._session = self._session_factory()
        timeout = max(0.1, min(self.request_timeout, self._deadline - self._monotonic()))
        try:
            if sec_line is not None:
                response = self._sec_session.get(url, timeout=timeout, allow_redirects=False, proxy="",
                                                 content_callback=collect_chunk, headers={"User-Agent": sec_line},
                                                 accept_encoding="gzip, deflate")
            else:
                response = self._session.get(url, timeout=timeout, allow_redirects=False, proxy="",
                                             content_callback=collect_chunk)
        except Exception as exc:  # curl_cffi raises RequestException subclasses
            self.received_bytes += len(buffer)
            if abort:
                metas.append(RequestMeta(url, None, len(buffer), None, started_at, abort[0]))
                raise SourceError(self._issue(abort[0], f"{abort[0]} while reading {url}", url), tuple(metas)) from None
            metas.append(RequestMeta(url, None, len(buffer), None, started_at, "transport_error"))
            raise _TransientError(type(exc).__name__) from None
        self.received_bytes += len(buffer)
        if abort:
            metas.append(RequestMeta(url, response.status_code, len(buffer), None, started_at, abort[0]))
            raise SourceError(self._issue(abort[0], f"{abort[0]} while reading {url}", url), tuple(metas))
        body = bytes(buffer)
        status = int(response.status_code)
        headers = {str(k).lower(): str(v) for k, v in dict(response.headers).items()}
        outcome = "ok" if status == 200 else ("redirect" if 300 <= status < 400 else f"http_{status}")
        metas.append(RequestMeta(url, status, len(body), compute_sha256(body), started_at, outcome))
        return status, body, headers


class _TransientError(Exception):
    """Signal a connection failure or timeout that may be retried once."""


# ---------------------------------------------------------------------------
# Price adapters
# ---------------------------------------------------------------------------


def split_calendar_years(span: DateRange) -> list[DateRange]:
    """Split an inclusive range into calendar-year windows and return them in date order.

    :param span: DateRange; inclusive range.
    return windows: list[DateRange]; non-empty windows clipped to the range.
    """
    windows = []
    for year in range(span.start.year, span.end.year + 1):
        start = max(span.start, date(year, 1, 1))
        end = min(span.end, date(year, 12, 31))
        windows.append(DateRange(start, end))
    return windows


def build_price_row(cells: Sequence[Any], trade_date: date, open_: Any, close: Any, high: Any, low: Any,
                    volume: Any, amount: Any, returned_symbol: str | None, adjustment: Adjustment) -> PriceRow:
    """Validate one daily bar and return it as a PriceRow, or raise ValueError.

    Prices must be finite decimals with `low <= open, close <= high`. Raw prices,
    volume, and amount must not be negative. Forward-adjusted prices may be negative.

    :param cells: Sequence[Any]; original row cells kept as source_row.
    :param trade_date: date; trading day.
    :param open_: Any; opening price value.
    :param close: Any; closing price value.
    :param high: Any; highest price value.
    :param low: Any; lowest price value.
    :param volume: Any; volume value or None.
    :param amount: Any; amount value or None.
    :param returned_symbol: str | None; provider-echoed identity.
    :param adjustment: Adjustment; `raw` or `forward`.
    return row: PriceRow; validated row.
    """
    values = []
    for value in (open_, close, high, low):
        kind, _raw, dec, _amb = parse_decimal_text(value)
        if kind != "number" or dec is None:
            raise ValueError(f"price value is not a clear decimal: {value!r}")
        values.append(Decimal(dec))
    o, c, h, lo = values
    if not (lo <= o <= h and lo <= c <= h):
        raise ValueError("OHLC values are not ordered")
    if adjustment == "raw" and lo < 0:
        raise ValueError("raw price is negative")
    extras: list[str | None] = []
    for value in (volume, amount):
        if value is None:
            extras.append(None)
            continue
        kind, _raw, dec, _amb = parse_decimal_text(value)
        if kind != "number" or dec is None or Decimal(dec) < 0:
            raise ValueError(f"volume or amount is not a nonnegative decimal: {value!r}")
        extras.append(dec)
    return PriceRow(trade_date=trade_date, open=format(o, "f"), high=format(h, "f"), low=format(lo, "f"),
                    close=format(c, "f"), volume_native=extras[0], amount_native=extras[1],
                    returned_symbol=returned_symbol, source_row=convert_source_json(list(cells)))


def summarize_coverage(requested: DateRange | None, fetched: list[Interval], unfetched: list[DateRange],
                       dates: list[date], traversal: State) -> Coverage:
    """Build a Coverage record from visited intervals and observed dates and return it.

    :param requested: DateRange | None; intended range.
    :param fetched: list[Interval]; validated visits.
    :param unfetched: list[DateRange]; ranges not visited.
    :param dates: list[date]; dates of accepted records.
    :param traversal: State; traversal state.
    return coverage: Coverage; immutable coverage summary.
    """
    return Coverage(requested=requested, fetched=tuple(sorted(fetched, key=lambda i: (i.start, i.end))),
                    unfetched=tuple(sorted(unfetched, key=lambda r: r.start)),
                    observed_start=min(dates) if dates else None, observed_end=max(dates) if dates else None,
                    row_count=len(dates), traversal=traversal)


def relabel_issue(issue: Issue, dataset: Dataset, symbol: Symbol | None, provider: Provider) -> Issue:
    """Return a copy of an HTTP issue labelled with the adapter's dataset, symbol, and provider.

    :param issue: Issue; issue raised by HttpClient.
    :param dataset: Dataset; adapter dataset.
    :param symbol: Symbol | None; adapter symbol.
    :param provider: Provider; adapter provider.
    return issue: Issue; relabelled issue.
    """
    return dataclasses.replace(issue, dataset=dataset, symbol_id=symbol.symbol_id if symbol else None,
                               provider=provider)


def decide_state(accepted: int, had_error: bool, complete: bool) -> State:
    """Choose the traversal state from accepted work, errors, and completion and return it.

    :param accepted: int; number of accepted pages or windows.
    :param had_error: bool; True when any page failed.
    :param complete: bool; True when every planned page was visited successfully.
    return state: State; succeeded, partial, or failed.
    """
    if complete and not had_error:
        return "succeeded"
    return "partial" if accepted else "failed"


def fetch_prices(http: HttpClient, symbol: Symbol, span: DateRange, adjustment: Adjustment,
                 provider: PriceProvider, *, today: date | None = None, venues: Sequence[str] = ()) -> FetchResult:
    """Collect daily bars for one symbol, range, adjustment, and provider and return the traversal result.

    Eastmoney is requested in calendar-year windows; Tencent is paged backwards with
    a date cursor. Accepted pages are kept when a later page fails. Forward-adjusted
    traversals re-request the page with the earliest row and compare its OHLC values;
    a mismatch adds `adjustment_mismatch` and makes the result partial so it is never
    promoted. Rows after the market-local `today` are rejected.

    US symbols need a venue. Eastmoney serves a US security under one of the venue
    numbers in `US_VENUES`; the adapter tries `venues` in order (all three when
    `venues` is empty) and keeps the first that knows the code. Tencent needs
    exactly one venue, because a wrong one returns misleading rows, and it serves
    only unadjusted US prices. A Tencent task that breaks either rule returns a
    failed result with the issue `fallback_unavailable` and makes no request. The
    metadata of a US result holds the venue in use under the key `venue`.

    :param http: HttpClient; bounded HTTP client.
    :param symbol: Symbol; security to collect.
    :param span: DateRange; inclusive range.
    :param adjustment: Adjustment; `raw` or `forward`.
    :param provider: PriceProvider; `eastmoney` or `tencent`.
    :param today: date | None; market-local day used to reject future rows; defaults to now.
    :param venues: Sequence[str]; for a US symbol: venue numbers to try, such as `("105", "106")`.
    return result: FetchResult; records are PriceRow values sorted by trade_date.
    """
    if adjustment not in ("raw", "forward"):
        raise ValueError("price adjustment must be raw or forward")
    today = today or compute_market_day(symbol.symbol_id, datetime.now(timezone.utc))
    is_us = symbol.exchange == "US"
    if provider == "eastmoney":
        return _fetch_eastmoney(http, symbol, span, adjustment, today, tuple(venues or US_VENUES) if is_us else ())
    if provider == "tencent":
        if not is_us:
            return _fetch_tencent(http, symbol, span, adjustment, today)
        if adjustment != "raw" or len(venues) != 1 or venues[0] not in TENCENT_US_SUFFIX:
            reason = ("Tencent has no forward-adjusted US prices" if adjustment != "raw"
                      else "the Tencent fallback needs the one venue that Eastmoney confirmed")
            issue = make_issue("fallback_unavailable", reason, "prices", symbol=symbol, provider="tencent")
            coverage = Coverage(requested=span, unfetched=(span,), traversal="failed")
            return FetchResult(state="failed", coverage=coverage, requests=(), issues=(issue,), metadata={})
        return _fetch_tencent(http, symbol, span, adjustment, today, venues[0])
    raise ValueError(f"unknown price provider {provider}")


def build_eastmoney_secid(symbol: Symbol, venue: str | None = None) -> str:
    """Build the Eastmoney security identifier `MARKET.CODE` and return it.

    Shanghai uses market 1; Shenzhen and Beijing use market 0. A US symbol uses the
    given venue number, and its code has `_` before a share class (`106.BRK_B`).

    :param symbol: Symbol; security.
    :param venue: str | None; US venue number such as `105`; ignored for A-shares.
    return secid: str; identifier such as `1.600519` or `105.AAPL`.
    """
    if symbol.exchange == "US":
        return f"{venue}.{symbol.code.replace('-', '_')}"
    return f"{'1' if symbol.exchange == 'SH' else '0'}.{symbol.code}"


def build_tencent_code(symbol: Symbol, venue: str | None = None) -> str:
    """Build the code that Tencent expects and return it.

    An A-share uses its wire code (`sh600519`). A US symbol uses `us`, the ticker
    with `.` before a share class, and the suffix of its venue (`usBRK.B.N`).

    :param symbol: Symbol; security.
    :param venue: str | None; US venue number, a key of TENCENT_US_SUFFIX; ignored for A-shares.
    return code: str; Tencent code.
    """
    if symbol.exchange == "US":
        return f"us{symbol.code.replace('-', '.')}.{TENCENT_US_SUFFIX[venue or '']}"
    return symbol.wire_code


def _build_eastmoney_url(symbol: Symbol, window: DateRange, adjustment: Adjustment, venue: str | None = None) -> str:
    """Build the Eastmoney daily K-line URL for one window and return it.

    :param symbol: Symbol; security.
    :param window: DateRange; inclusive window.
    :param adjustment: Adjustment; raw (fqt=0) or forward (fqt=1).
    :param venue: str | None; US venue number; None for A-shares.
    return url: str; request URL.
    """
    params = {"secid": build_eastmoney_secid(symbol, venue), "klt": "101",
              "fqt": "1" if adjustment == "forward" else "0",
              "beg": window.start.strftime("%Y%m%d"), "end": window.end.strftime("%Y%m%d"),
              "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57"}
    return f"{EASTMONEY_URL}?{urlencode(params)}"


def _parse_eastmoney_page(body: bytes, symbol: Symbol, window: DateRange, adjustment: Adjustment,
                          today: date, venue: str | None = None) -> tuple[list[PriceRow], JsonValue]:
    """Validate one Eastmoney response and return its rows and metadata, or raise _PageError.

    Eastmoney answers `rc=100` with `data: null` when it does not know the code in
    the requested market; this raises _PageError with the code `symbol_not_found`.

    :param body: bytes; response body.
    :param symbol: Symbol; requested security.
    :param window: DateRange; requested window.
    :param adjustment: Adjustment; requested adjustment.
    :param today: date; market-local day for the future-row check.
    :param venue: str | None; US venue number that was requested; None for A-shares.
    return page: tuple[list[PriceRow], JsonValue]; rows and metadata without klines.
    """
    try:
        payload = parse_strict_json(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _PageError("schema_changed", f"Eastmoney body is not strict JSON: {exc}") from None
    if not isinstance(payload, dict) or "rc" not in payload:
        raise _PageError("schema_changed", "Eastmoney body has no rc field")
    secid = build_eastmoney_secid(symbol, venue)
    if payload.get("rc") == 100 and payload.get("data") is None:
        raise _PageError("symbol_not_found", f"Eastmoney does not know {secid}")
    if payload.get("rc") != 0:
        raise _PageError("provider_error", f"Eastmoney rc={payload.get('rc')!r}")
    data = payload.get("data")
    if data is None:
        return [], {"data": None}
    if not isinstance(data, dict) or not isinstance(data.get("klines"), list) or not isinstance(data.get("code"), str):
        raise _PageError("schema_changed", "Eastmoney data lacks code or klines")
    if isinstance(data.get("market"), bool) or f"{data.get('market')}.{data.get('code')}" != secid:
        raise _PageError("identity_mismatch", f"Eastmoney returned {data.get('market')}.{data.get('code')}")
    rows: list[PriceRow] = []
    seen: set[date] = set()
    for line in data["klines"]:
        if not isinstance(line, str):
            raise _PageError("schema_changed", "Eastmoney kline is not a string")
        cells = line.split(",")
        if len(cells) < 7:
            raise _PageError("schema_changed", "Eastmoney kline has fewer than seven cells")
        try:
            trade_date = date.fromisoformat(cells[0])
        except ValueError:
            raise _PageError("schema_changed", f"Eastmoney date {cells[0]!r} is invalid") from None
        if not (window.start <= trade_date <= window.end) or trade_date > today:
            raise _PageError("schema_changed", f"Eastmoney row {trade_date} is outside the requested window")
        if trade_date in seen:
            raise _PageError("schema_changed", f"Eastmoney row {trade_date} is duplicated")
        seen.add(trade_date)
        try:
            rows.append(build_price_row(cells, trade_date, cells[1], cells[2], cells[3], cells[4], cells[5], cells[6],
                                        f"{data.get('market')}.{data.get('code')}", adjustment))
        except ValueError as exc:
            raise _PageError("schema_changed", f"Eastmoney row {trade_date}: {exc}") from None
    meta = convert_source_json({k: v for k, v in data.items() if k != "klines"})
    return rows, bound_metadata(meta)


class _PageError(Exception):
    """Signal that one source page failed validation.

    :param code: str; issue code.
    :param message: str; explanation.
    return instance: _PageError; raisable error.
    """

    def __init__(self, code: str, message: str) -> None:
        """Store the issue code and message.

        :param code: str; issue code.
        :param message: str; explanation.
        return None: None; no value is returned.
        """
        super().__init__(message)
        self.code = code


def _fetch_eastmoney(http: HttpClient, symbol: Symbol, span: DateRange, adjustment: Adjustment,
                     today: date, venues: tuple[str, ...] = ()) -> FetchResult:
    """Traverse Eastmoney calendar-year windows and return the price result.

    For a US symbol the first window is requested for each venue in `venues` until
    one knows the code; that venue then serves every later window. When no venue
    knows the code, the result is failed with the issue `symbol_not_found`.

    :param http: HttpClient; bounded client.
    :param symbol: Symbol; security.
    :param span: DateRange; inclusive range.
    :param adjustment: Adjustment; raw or forward.
    :param today: date; market-local day.
    :param venues: tuple[str, ...]; US venue numbers to try in order; empty for A-shares.
    return result: FetchResult; Eastmoney price outcome; the metadata of a US result holds `venue`.
    """
    windows = split_calendar_years(span)
    rows: list[PriceRow] = []
    fetched: list[Interval] = []
    requests: list[RequestMeta] = []
    issues: list[Issue] = []
    page_meta: list[JsonValue] = []
    failed_at: int | None = None
    venue: str | None = None
    for index, window in enumerate(windows):
        candidates: tuple[str | None, ...] = venues if venues and venue is None else (venue,)
        problem: Issue | None = None
        page_rows: list[PriceRow] = []
        meta: JsonValue = None
        for candidate in candidates:
            url = _build_eastmoney_url(symbol, window, adjustment, candidate)
            try:
                response = http.get(url, max_bytes=API_MAX_BYTES)
                requests.extend(response.requests)
                page_rows, meta = _parse_eastmoney_page(response.body, symbol, window, adjustment, today, candidate)
            except SourceError as exc:
                requests.extend(exc.requests)
                problem = relabel_issue(exc.issue, "prices", symbol, "eastmoney")
                break
            except _PageError as exc:
                if exc.code == "symbol_not_found" and candidate != candidates[-1]:
                    continue
                message = str(exc)
                if exc.code == "symbol_not_found" and len(candidates) > 1:
                    message = f"Eastmoney does not know {symbol.code} in the venues {', '.join(map(str, candidates))}"
                problem = make_issue(exc.code, message, "prices", symbol=symbol, provider="eastmoney", url=url)
                break
            venue = candidate
            break
        if problem is not None:
            issues.append(problem)
            failed_at = index
            break
        rows.extend(page_rows)
        page_meta.append(meta)
        fetched.append(Interval(window.start, window.end, "nonempty" if page_rows else "empty", len(page_rows)))
    complete = failed_at is None
    if complete and adjustment == "forward" and rows:
        anchor = rows[0]
        window = next(w for w in windows if w.start <= anchor.trade_date <= w.end)
        url = _build_eastmoney_url(symbol, window, adjustment, venue)
        try:
            response = http.get(url, max_bytes=API_MAX_BYTES)
            requests.extend(response.requests)
            recheck, _meta = _parse_eastmoney_page(response.body, symbol, window, adjustment, today, venue)
            if not recheck or _ohlc(recheck[0]) != _ohlc(anchor) or recheck[0].trade_date != anchor.trade_date:
                issues.append(make_issue("adjustment_mismatch", "forward-adjusted anchor changed during traversal",
                                         "prices", symbol=symbol, provider="eastmoney", url=url, retryable=True))
                complete = False
        except SourceError as exc:
            requests.extend(exc.requests)
            issues.append(relabel_issue(exc.issue, "prices", symbol, "eastmoney"))
            complete = False
        except _PageError as exc:
            issues.append(make_issue(exc.code, f"anchor recheck: {exc}", "prices", symbol=symbol,
                                     provider="eastmoney", url=url))
            complete = False
    unfetched = windows[failed_at:] if failed_at is not None else []
    state = decide_state(len(fetched), bool(issues), complete)
    coverage = summarize_coverage(span, fetched, unfetched, [r.trade_date for r in rows], state)
    metadata: dict[str, JsonValue] = {"pages": page_meta[:1], "page_count": len(page_meta)}
    if symbol.exchange == "US":
        metadata["venue"] = venue
    return FetchResult(state=state, coverage=coverage, requests=tuple(requests), issues=tuple(issues),
                       metadata=bound_metadata(metadata, 2 * MIB),
                       records=tuple(sorted(rows, key=lambda r: r.trade_date)))


def _ohlc(row: PriceRow) -> tuple[str, str, str, str]:
    """Return the OHLC values of a row as a tuple for anchor comparison.

    :param row: PriceRow; price row.
    return values: tuple[str, str, str, str]; (open, high, low, close).
    """
    return row.open, row.high, row.low, row.close


def _build_tencent_url(symbol: Symbol, cursor: date, adjustment: Adjustment, venue: str | None = None) -> str:
    """Build the Tencent backward-paging K-line URL and return it.

    :param symbol: Symbol; security.
    :param cursor: date; latest date to include in this page.
    :param adjustment: Adjustment; raw (empty flag) or forward (`qfq`).
    :param venue: str | None; US venue number; None for A-shares.
    return url: str; request URL.
    """
    flag = "qfq" if adjustment == "forward" else ""
    param = f"{build_tencent_code(symbol, venue)},day,,{cursor.isoformat()},{TENCENT_PAGE_SIZE},{flag}"
    return f"{TENCENT_URL}?{urlencode({'param': param})}"


def _parse_tencent_page(body: bytes, symbol: Symbol, cursor: date, adjustment: Adjustment,
                        today: date, venue: str | None = None) -> list[PriceRow]:
    """Validate one Tencent page and return its rows (unfiltered by the span), or raise _PageError.

    :param body: bytes; response body, which may be labelled text/html but must be strict JSON.
    :param symbol: Symbol; requested security.
    :param cursor: date; latest date requested.
    :param adjustment: Adjustment; raw uses key `day`, forward uses key `qfqday`.
    :param today: date; market-local day.
    :param venue: str | None; US venue number; None for A-shares.
    return rows: list[PriceRow]; rows in the page.
    """
    code = build_tencent_code(symbol, venue)
    try:
        payload = parse_strict_json(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _PageError("schema_changed", f"Tencent body is not strict JSON: {exc}") from None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        code = payload.get("code") if isinstance(payload, dict) else None
        raise _PageError("provider_error", f"Tencent code={code!r}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get(code), dict):
        raise _PageError("identity_mismatch", f"Tencent data lacks {code}")
    series = data[code]
    key = "qfqday" if adjustment == "forward" else "day"
    other = "day" if adjustment == "forward" else "qfqday"
    if key not in series:
        if other in series and series.get(other):
            raise _PageError("adjustment_mismatch", f"Tencent returned {other} instead of {key}")
        if not series or all(v in (None, [], "") for v in series.values() if not isinstance(v, dict)):
            return []
        raise _PageError("schema_changed", f"Tencent series lacks {key}")
    lines = series.get(key)
    if lines is None:
        return []
    if not isinstance(lines, list):
        raise _PageError("schema_changed", f"Tencent {key} is not an array")
    rows: list[PriceRow] = []
    seen: set[date] = set()
    for cells in lines:
        if not isinstance(cells, list) or len(cells) < 6:
            raise _PageError("schema_changed", "Tencent row has fewer than six cells")
        try:
            trade_date = date.fromisoformat(str(cells[0]))
        except ValueError:
            raise _PageError("schema_changed", f"Tencent date {cells[0]!r} is invalid") from None
        if trade_date > cursor or trade_date > today:
            raise _PageError("schema_changed", f"Tencent row {trade_date} is after the cursor or today")
        if trade_date in seen:
            raise _PageError("schema_changed", f"Tencent row {trade_date} is duplicated")
        seen.add(trade_date)
        amount = None
        if len(cells) > 8 and isinstance(cells[8], (str, int, Decimal)):
            kind, _r, _d, _a = parse_decimal_text(cells[8])
            amount = cells[8] if kind == "number" else None
        try:
            rows.append(build_price_row(cells, trade_date, cells[1], cells[2], cells[3], cells[4], cells[5], amount,
                                        code, adjustment))
        except ValueError as exc:
            raise _PageError("schema_changed", f"Tencent row {trade_date}: {exc}") from None
    return rows


def _fetch_tencent(http: HttpClient, symbol: Symbol, span: DateRange, adjustment: Adjustment,
                   today: date, venue: str | None = None) -> FetchResult:
    """Page Tencent backwards from the end of the range and return the price result.

    The cursor moves to the day before the earliest unfiltered row. Short pages
    continue; a valid empty page or a page reaching the range start terminates.

    :param http: HttpClient; bounded client.
    :param symbol: Symbol; security.
    :param span: DateRange; inclusive range.
    :param adjustment: Adjustment; raw or forward.
    :param today: date; market-local day.
    :param venue: str | None; US venue number; None for A-shares.
    return result: FetchResult; Tencent price outcome; the metadata of a US result holds `venue`.
    """
    cursor = min(span.end, today)
    rows: dict[date, PriceRow] = {}
    fetched: list[Interval] = []
    requests: list[RequestMeta] = []
    issues: list[Issue] = []
    complete = False
    anchor_cursor: date | None = None
    pages = 0
    while pages < TENCENT_MAX_PAGES:
        if cursor < span.start:
            complete = True
            break
        url = _build_tencent_url(symbol, cursor, adjustment, venue)
        pages += 1
        try:
            response = http.get(url, max_bytes=API_MAX_BYTES)
            requests.extend(response.requests)
            page = _parse_tencent_page(response.body, symbol, cursor, adjustment, today, venue)
        except SourceError as exc:
            requests.extend(exc.requests)
            issues.append(relabel_issue(exc.issue, "prices", symbol, "tencent"))
            break
        except _PageError as exc:
            issues.append(make_issue(exc.code, str(exc), "prices", symbol=symbol, provider="tencent", url=url))
            break
        if not page:
            fetched.append(Interval(span.start, cursor, "empty", 0))
            complete = True
            break
        earliest = min(r.trade_date for r in page)
        inside = [r for r in page if span.start <= r.trade_date <= span.end]
        conflict = next((r for r in inside if r.trade_date in rows and rows[r.trade_date] != r), None)
        if conflict is not None:
            issues.append(make_issue("source_changed_during_fetch", f"Tencent row {conflict.trade_date} changed",
                                     "prices", symbol=symbol, provider="tencent", url=url, retryable=True))
            break
        for row in inside:
            rows[row.trade_date] = row
        visit_start = max(earliest, span.start)
        fetched.append(Interval(visit_start, cursor, "nonempty" if inside else "empty", len(inside)))
        anchor_cursor = cursor
        next_cursor = earliest - timedelta(days=1)
        if next_cursor >= cursor:
            issues.append(make_issue("pagination_stalled", "Tencent cursor did not move backwards", "prices",
                                     symbol=symbol, provider="tencent", url=url))
            break
        if earliest <= span.start:
            complete = True
            break
        cursor = next_cursor
    else:
        issues.append(make_issue("work_limit", f"Tencent page limit {TENCENT_MAX_PAGES} reached", "prices",
                                 symbol=symbol, provider="tencent", retryable=True))
    ordered = sorted(rows.values(), key=lambda r: r.trade_date)
    if complete and adjustment == "forward" and ordered and anchor_cursor is not None:
        url = _build_tencent_url(symbol, anchor_cursor, adjustment, venue)
        try:
            response = http.get(url, max_bytes=API_MAX_BYTES)
            requests.extend(response.requests)
            recheck = {r.trade_date: r for r in _parse_tencent_page(response.body, symbol, anchor_cursor,
                                                                     adjustment, today, venue)}
            again = recheck.get(ordered[0].trade_date)
            if again is None or _ohlc(again) != _ohlc(ordered[0]):
                issues.append(make_issue("adjustment_mismatch", "forward-adjusted anchor changed during traversal",
                                         "prices", symbol=symbol, provider="tencent", url=url, retryable=True))
                complete = False
        except SourceError as exc:
            requests.extend(exc.requests)
            issues.append(relabel_issue(exc.issue, "prices", symbol, "tencent"))
            complete = False
        except _PageError as exc:
            issues.append(make_issue(exc.code, f"anchor recheck: {exc}", "prices", symbol=symbol,
                                     provider="tencent", url=url))
            complete = False
    unfetched: list[DateRange] = []
    if not complete:
        lowest = min((i.start for i in fetched), default=None)
        end = (lowest - timedelta(days=1)) if lowest else span.end
        if end >= span.start:
            unfetched.append(DateRange(span.start, end))
    state = decide_state(len(fetched), bool(issues), complete)
    coverage = summarize_coverage(span, fetched, unfetched, [r.trade_date for r in ordered], state)
    metadata: dict[str, JsonValue] = {"page_count": pages}
    if symbol.exchange == "US":
        metadata["venue"] = venue
    return FetchResult(state=state, coverage=coverage, requests=tuple(requests), issues=tuple(issues),
                       metadata=metadata, records=tuple(ordered))


# ---------------------------------------------------------------------------
# Sina structured statements
# ---------------------------------------------------------------------------


def classify_scope(source_scope: str) -> Scope:
    """Classify a Sina or filing scope label as consolidated, parent, or unknown and return it.

    `合并期末` or any label containing `合并` is consolidated. A label containing
    `母公司` without `合并` is parent. Anything else is unknown.

    :param source_scope: str; raw scope label.
    return scope: Scope; classified scope.
    """
    if "合并" in source_scope:
        return "consolidated"
    if "母公司" in source_scope:
        return "parent"
    return "unknown"


def _parse_sina_date(value: Any) -> date:
    """Parse a Sina `YYYYMMDD` period key and return the date, or raise _PageError.

    :param value: Any; period key.
    return day: date; parsed period date.
    """
    text = str(value)
    if not re.fullmatch(r"\d{8}", text):
        raise _PageError("schema_changed", f"Sina period {text!r} is not YYYYMMDD")
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        raise _PageError("schema_changed", f"Sina period {text!r} is not a real date") from None


def build_sina_record(key: str, report: Any, kind: StatementKind, symbol: Symbol,
                      issues: list[Issue]) -> StatementRecord:
    """Convert one Sina report object into a StatementRecord and return it, or raise _PageError.

    Input schema (`report`):
      - data: list[object], required; line items. Each item requires `item_title: str`
        and may contain `item_field`, `item_value`, `item_tongbi`, and extensions.
      - rType: str, optional; scope label such as `合并期末`.
      - rCurrency: str, optional; currency label.
      - publish_date: str, optional; `YYYYMMDD` publication date.
      - update_time: int | str, optional; Unix seconds of the last source update.
      - other keys: retained in `source_metadata`.

    Invalid optional dates stay None, keep their raw value, and add a warning to
    `issues`. Unclear numbers keep their raw text with value_decimal None and add a
    warning. Units are never inferred.

    :param key: str; `YYYYMMDD` period key.
    :param report: Any; report object following the schema above.
    :param kind: StatementKind; statement kind requested.
    :param symbol: Symbol; requested security (provenance only).
    :param issues: list[Issue]; issue list that this function appends warnings to.
    return record: StatementRecord; structured statement.
    """
    report_date = _parse_sina_date(key)
    if not isinstance(report, dict) or not isinstance(report.get("data"), list):
        raise _PageError("schema_changed", f"Sina report {key} lacks a data array")
    if len(report["data"]) > REPORT_MAX_ITEMS:
        raise _PageError("work_limit", f"Sina report {key} has more than {REPORT_MAX_ITEMS} items")
    items: list[StatementItem] = []
    ambiguous = 0
    for ordinal, item in enumerate(report["data"]):
        if not isinstance(item, dict) or not isinstance(item.get("item_title"), str):
            raise _PageError("schema_changed", f"Sina report {key} item {ordinal} lacks item_title")
        raw_value = item.get("item_value", Ellipsis)
        value_kind, value_raw, value_decimal, amb = parse_decimal_text(raw_value)
        ambiguous += int(amb)
        field_code = item.get("item_field")
        extras = {k: v for k, v in item.items() if k not in ("item_title", "item_field", "item_value")}
        items.append(StatementItem(ordinal=ordinal, item_field=str(field_code) if field_code is not None else None,
                                   item_title=item["item_title"], value_raw=value_raw, value_kind=value_kind,
                                   value_decimal=value_decimal,
                                   source_metadata=bound_metadata(convert_source_json(extras))))
    if ambiguous:
        issues.append(make_issue("ambiguous_number", f"{ambiguous} values in Sina {kind} {key} are not clear decimals",
                                 kind, severity="warning", symbol=symbol, provider="sina", report_date=report_date))
    source_scope = str(report.get("rType") or "")
    publish_raw = report.get("publish_date")
    publication_date = None
    if publish_raw not in (None, ""):
        try:
            publication_date = _parse_sina_date(publish_raw)
        except _PageError:
            issues.append(make_issue("invalid_optional_date", f"Sina publish_date {publish_raw!r} is invalid", kind,
                                     severity="warning", symbol=symbol, provider="sina", report_date=report_date))
    update_raw = report.get("update_time")
    update_at = None
    if update_raw not in (None, ""):
        text = str(update_raw)
        if re.fullmatch(r"\d{9,11}", text):
            update_at = datetime.fromtimestamp(int(text), tz=timezone.utc)
        else:
            issues.append(make_issue("invalid_optional_date", f"Sina update_time {update_raw!r} is invalid", kind,
                                     severity="warning", symbol=symbol, provider="sina", report_date=report_date))
    currency = report.get("rCurrency")
    metadata = {k: v for k, v in report.items() if k != "data"}
    return StatementRecord(
        report_date=report_date, kind=kind, scope=classify_scope(source_scope), source_scope=source_scope,
        source_key=key, format="structured", period_basis="source_reported", publication_date=publication_date,
        publication_date_raw=None if publish_raw is None else str(publish_raw), source_update_at=update_at,
        source_update_raw=None if update_raw is None else str(update_raw),
        currency=str(currency) if currency not in (None, "") else None, unit=None, items=tuple(items),
        source_metadata=bound_metadata(convert_source_json(metadata)), locators=(f"sina:{SINA_SOURCES[kind]}:{key}",))


def fetch_statements(http: HttpClient, symbol: Symbol, kind: StatementKind) -> FetchResult:
    """Walk every Sina report page for one statement kind and return the statement result.

    The adapter requests pages of 50 periods until it has received `report_count`
    periods, then requires one more terminal page (`data` null or no periods).
    Duplicated periods, unmatched keys, repeated pages, a changing count, an early
    terminal page, or more than 1,000 periods stop the traversal with an issue while
    keeping accepted pages. No date filter is applied. Units stay None and one
    `unit_unknown` warning is added when records exist.

    :param http: HttpClient; bounded client.
    :param symbol: Symbol; security; its wire code is sent as `paperCode`.
    :param kind: StatementKind; income (`lrb`), balance (`fzb`), or cash_flow (`llb`).
    return result: FetchResult; records are StatementRecord values sorted by report_date descending.
    """
    records: dict[str, StatementRecord] = {}
    requests: list[RequestMeta] = []
    issues: list[Issue] = []
    expected: int | None = None
    complete = False
    accepted_pages = 0
    previous_hash: str | None = None
    for page in range(1, SINA_MAX_PAGES + 2):
        params = {"paperCode": symbol.wire_code, "source": SINA_SOURCES[kind], "type": "0", "page": str(page),
                  "num": str(SINA_PAGE_SIZE)}
        url = f"{SINA_REPORT_URL}?{urlencode(params)}"
        try:
            response = http.get(url, max_bytes=API_MAX_BYTES)
            requests.extend(response.requests)
            page_hash = compute_sha256(response.body)
            terminal, page_records, count = _parse_sina_page(response.body, kind, symbol, issues)
        except SourceError as exc:
            requests.extend(exc.requests)
            issues.append(relabel_issue(exc.issue, kind, symbol, "sina"))
            break
        except _PageError as exc:
            issues.append(make_issue(exc.code, str(exc), kind, symbol=symbol, provider="sina", url=url))
            break
        if terminal:
            if expected is not None and len(records) < expected:
                issues.append(make_issue("schema_changed", "Sina returned a terminal page before report_count",
                                         kind, symbol=symbol, provider="sina", url=url))
            else:
                complete = True
            break
        if page > SINA_MAX_PAGES:
            issues.append(make_issue("work_limit", f"Sina page limit {SINA_MAX_PAGES} reached", kind, symbol=symbol,
                                     provider="sina", url=url, retryable=True))
            break
        if page_hash == previous_hash:
            issues.append(make_issue("pagination_stalled", "Sina repeated the previous page", kind, symbol=symbol,
                                     provider="sina", url=url))
            break
        previous_hash = page_hash
        if expected is not None and count != expected:
            issues.append(make_issue("source_changed_during_fetch", "Sina report_count changed between pages", kind,
                                     symbol=symbol, provider="sina", url=url, retryable=True))
            break
        expected = count
        duplicate = next((r.source_key for r in page_records if r.source_key in records), None)
        if duplicate is not None:
            issues.append(make_issue("schema_changed", f"Sina period {duplicate} is duplicated across pages", kind,
                                     symbol=symbol, provider="sina", url=url))
            break
        if len(records) + len(page_records) > SINA_MAX_PERIODS:
            issues.append(make_issue("work_limit", "Sina returned more than 1,000 periods", kind, symbol=symbol,
                                     provider="sina", url=url))
            break
        for record in page_records:
            records[record.source_key] = record
        accepted_pages += 1
        if len(records) > expected:
            issues.append(make_issue("schema_changed", "Sina returned more periods than report_count", kind,
                                     symbol=symbol, provider="sina", url=url))
            break
    ordered = sorted(records.values(), key=lambda r: r.report_date, reverse=True)
    if ordered:
        issues.append(make_issue("unit_unknown", "Sina does not state units; values keep source magnitude", kind,
                                 severity="warning", symbol=symbol, provider="sina"))
    errors = any(i.severity == "error" for i in issues)
    state = decide_state(accepted_pages, errors, complete)
    dates = [r.report_date for r in ordered]
    fetched = [Interval(d, d, "nonempty", 1) for d in sorted(set(dates))]
    coverage = summarize_coverage(None, fetched, [], dates, state)
    return FetchResult(state=state, coverage=coverage, requests=tuple(requests), issues=tuple(issues),
                       metadata={"report_count": expected, "page_count": accepted_pages, "paperCode": symbol.wire_code},
                       records=tuple(ordered))


def _parse_sina_page(body: bytes, kind: StatementKind, symbol: Symbol,
                     issues: list[Issue]) -> tuple[bool, list[StatementRecord], int]:
    """Validate one Sina page and return (terminal, records, report_count), or raise _PageError.

    :param body: bytes; response body.
    :param kind: StatementKind; requested kind.
    :param symbol: Symbol; requested security.
    :param issues: list[Issue]; warning list.
    return page: tuple[bool, list[StatementRecord], int]; terminal flag, records, and declared count.
    """
    try:
        payload = parse_strict_json(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _PageError("schema_changed", f"Sina body is not strict JSON: {exc}") from None
    result = payload.get("result") if isinstance(payload, dict) else None
    status = result.get("status") if isinstance(result, dict) else None
    if not isinstance(status, dict) or "code" not in status:
        raise _PageError("schema_changed", "Sina body lacks result.status.code")
    if status.get("code") != 0:
        raise _PageError("provider_error", f"Sina status code {status.get('code')!r}")
    assert isinstance(result, dict)
    data = result.get("data")
    if data is None:
        return True, [], 0
    if not isinstance(data, dict):
        raise _PageError("schema_changed", "Sina data is not an object")
    count_raw = data.get("report_count")
    if isinstance(count_raw, int) and not isinstance(count_raw, bool):
        count = count_raw
    elif isinstance(count_raw, str) and count_raw.isdigit():
        count = int(count_raw)
    else:
        raise _PageError("schema_changed", "Sina report_count is not an integer")
    dates = data.get("report_date")
    reports = data.get("report_list")
    if not dates and not reports:
        return True, [], count
    if not isinstance(dates, list) or not isinstance(reports, dict):
        raise _PageError("schema_changed", "Sina report_date or report_list has the wrong type")
    keys = []
    for entry in dates:
        if not isinstance(entry, dict) or "date_value" not in entry:
            raise _PageError("schema_changed", "Sina report_date entry lacks date_value")
        keys.append(str(entry["date_value"]))
    if len(set(keys)) != len(keys):
        raise _PageError("schema_changed", "Sina report_date contains duplicates")
    if set(keys) != set(reports.keys()):
        raise _PageError("schema_changed", "Sina report_date keys do not match report_list keys")
    return False, [build_sina_record(k, reports[k], kind, symbol, issues) for k in keys], count
