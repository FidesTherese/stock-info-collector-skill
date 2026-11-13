"""Filing discovery on Sina index pages and statement extraction from HTML and text PDFs.

The module reads documents through providers.HttpClient or from explicit local
files under the project root. It never writes to SQLite and never runs OCR.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from .providers import (
    HTML_MAX_BYTES, PDF_MAX_BYTES, Coverage, DateRange, DiscoveryResult, FilingRef, FilingResult, HttpClient, Interval,
    Issue, JsonValue, RequestMeta, SourceError, StatementItem, StatementKind, StatementRecord, Symbol, compute_sha256,
    convert_to_json, decode_html, make_issue, normalize_source_url, parse_decimal_text, parse_html_blocks,
    relabel_issue, validate_remote_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INDEX_URL = "https://vip.stock.finance.sina.com.cn/corp/go.php/vCB_Bulletin/stockid/{code}/page_type/{category}.phtml"
CATEGORIES: dict[str, tuple[int, int]] = {"ndbg": (12, 31), "zqbg": (6, 30), "yjdbg": (3, 31), "sjdbg": (9, 30)}
MAX_INDEX_PAGES = 10
MAX_CANDIDATES = 500
HEADING_WINDOW = 1000
PDF_MAX_PAGES = 512
PDF_PAGE_CONTENT_LIMIT = 2 * 1024 * 1024
PDF_TOTAL_CONTENT_LIMIT = 64 * 1024 * 1024
PDF_TEXT_LIMIT = 4 * 1024 * 1024
PDF_DEADLINE_SECONDS = 60.0
PDF_SETTINGS: dict[str, Any] = {
    "maximum_declared_stream_length": 2 * 1024 * 1024,
    "array_based_stream_maximum_output_length": 2 * 1024 * 1024,
    "zlib_maximum_output_length": 2 * 1024 * 1024,
    "lzw_maximum_output_length": 2 * 1024 * 1024,
    "run_length_maximum_output_length": 2 * 1024 * 1024,
    "brotli_maximum_output_length": 2 * 1024 * 1024,
    "jbig2_maximum_output_length": 2 * 1024 * 1024,
    "image_maximum_buffer_size": 2 * 1024 * 1024,
    "stream_decoding_work_maximum_length": 8388608,
    "stream_parsing_work_maximum_count": 200000,
    "array_maximum_element_count": 10000,
    "dictionary_maximum_element_count": 10000,
    "page_tree_maximum_entries": 2048,
    "page_tree_maximum_depth": 32,
    "page_tree_maximum_page_entries": 131072,
    "xform_maximum_invocations_per_extraction": 1000,
    "jbig2dec_binary": None,
}
_HEADING = re.compile(r"(母公司|合并)\s*(资产负债表|利润表|现金流量表)")
_KIND_BY_TITLE: dict[str, StatementKind] = {"资产负债表": "balance", "利润表": "income", "现金流量表": "cash_flow"}
_UNIT = re.compile(r"单位\s*[:：]\s*(百万元|千元|万元|亿元|元)")
_CURRENCY = re.compile(r"币种\s*[:：]\s*([一-鿿A-Za-z]+)")
_CODE_LABEL = re.compile(r"(?:股票代码|证券代码)\s*[:：]?\s*(\d{6})")
_FULL_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日|(\d{4})-(\d{2})-(\d{2})")
_YEAR_TO_DATE = re.compile(r"(\d{4})\s*年\s*1\s*[-—–~至]\s*(\d{1,2})\s*月")
_ANNUAL = re.compile(r"(\d{4})\s*年度")
_SKIP_WORDS = ("摘要", "英文", "English", "english", "预告", "快报", "聘", "提示性")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def classify_report_title(title: str) -> tuple[str | None, date | None]:
    """Classify a filing title as a full periodic report and return (category, candidate report date).

    Only titles with an explicit four-digit year and a clear annual, half-year,
    first-quarter, or third-quarter report phrase are accepted. Summaries, English
    versions, forecasts, flash reports, and appointment notices return
    (`skipped`, None). Corrections are kept. Unrelated titles return (None, None).

    :param title: str; anchor text from an index page.
    return classified: tuple[str | None, date | None]; category code and candidate period end.
    """
    year_match = re.search(r"(\d{4})\s*年", title)
    if year_match is None:
        return None, None
    year = int(year_match.group(1))
    if "半年度报告" in title or "半年报" in title or "中期报告" in title:
        category = "zqbg"
    elif "第一季度报告" in title or "一季度报告" in title:
        category = "yjdbg"
    elif "第三季度报告" in title or "三季度报告" in title:
        category = "sjdbg"
    elif "年度报告" in title or "年报" in title:
        category = "ndbg"
    else:
        return None, None
    if any(word in title for word in _SKIP_WORDS):
        return "skipped", None
    month, day = CATEGORIES[category]
    return category, date(year, month, day)


def discover_filings(http: HttpClient, symbol: Symbol, span: DateRange) -> DiscoveryResult:
    """Read Sina periodic-report index pages for one symbol and return candidate filings.

    The function visits the annual (`ndbg`), half-year (`zqbg`), first-quarter
    (`yjdbg`), and third-quarter (`sjdbg`) index pages and follows only links that
    point to the same code and category, at most ten pages per category. It keeps
    full reports with an explicit year, takes the publication date from the date
    printed before the link, deduplicates by URI, and sorts by period descending
    and URI. Candidates whose period lies inside `span` are returned. Discovery
    never proves that every filing was found (`coverage_verified=False`).

    Output metadata: `visited_urls: list[str]`, `pending_urls: list[str]`,
    `coverage_verified: false`, `skipped_titles: int`, `outside_range: int`.

    :param http: HttpClient; bounded client.
    :param symbol: Symbol; security whose filings are listed.
    :param span: DateRange; inclusive period range.
    return result: DiscoveryResult; candidates and navigation summary.
    """
    candidates: dict[str, FilingRef] = {}
    requests: list[RequestMeta] = []
    issues: list[Issue] = []
    visited: list[str] = []
    pending: list[str] = []
    skipped = 0
    outside = 0
    failed_categories = 0
    for category in CATEGORIES:
        start_url = INDEX_URL.format(code=symbol.code, category=category)
        queue = [start_url]
        seen = {start_url}
        pages = 0
        while queue:
            url = queue.pop(0)
            if pages >= MAX_INDEX_PAGES:
                pending.append(url)
                pending.extend(queue)
                issues.append(make_issue("work_limit", f"index page limit reached for {category}", "discovery",
                                         severity="warning", symbol=symbol, provider="filing", url=url, retryable=True))
                break
            pages += 1
            try:
                response = http.get(url, max_bytes=HTML_MAX_BYTES)
                requests.extend(response.requests)
                text = decode_html(response.body, response.content_type)
            except SourceError as exc:
                requests.extend(exc.requests)
                issues.append(relabel_issue(exc.issue, "discovery", symbol, "filing"))
                pending.append(url)
                pending.extend(queue)
                failed_categories += pages == 1
                break
            except ValueError as exc:
                issues.append(make_issue("unsupported_charset", str(exc), "discovery", symbol=symbol,
                                         provider="filing", url=url))
                failed_categories += pages == 1
                break
            visited.append(url)
            parser = parse_html_blocks(text)
            for href, title, before in parser.link_contexts:
                target = normalize_source_url(urljoin(url, href))
                if _is_same_index(target, symbol.code, category):
                    if target not in seen:
                        seen.add(target)
                        queue.append(target)
                    continue
                if "vCB_AllBulletinDetail" not in target:
                    continue
                found, report_date = classify_report_title(title)
                if found == "skipped":
                    skipped += 1
                    continue
                if found is None or report_date is None:
                    continue
                if validate_remote_url(target) is not None or _query_value(target, "stockid") != symbol.code:
                    continue
                if not (span.start <= report_date <= span.end):
                    outside += 1
                    continue
                dates = re.findall(r"(\d{4}-\d{2}-\d{2})", before)
                publication = _parse_iso(dates[-1]) if dates else None
                if target not in candidates and len(candidates) < MAX_CANDIDATES:
                    candidates[target] = FilingRef(symbol=symbol, uri=target, report_date=report_date,
                                                   publication_date=publication, origin="automatic")
    ordered = sorted(candidates.values(), key=lambda c: (-(c.report_date or date.min).toordinal(), c.uri))
    if failed_categories == len(CATEGORIES):
        state = "failed"
        issues.append(make_issue("filing_discovery_failed", "no filing index page could be read", "discovery",
                                 symbol=symbol, provider="filing", retryable=True))
    elif issues:
        state = "partial"
    else:
        state = "succeeded"
    if symbol.exchange == "BJ":
        issues.append(make_issue("discovery_coverage_unverified", "Sina index coverage for Beijing listings is"
                                 " unverified", "discovery", severity="warning", symbol=symbol, provider="filing"))
    points = sorted({c.report_date for c in ordered if c.report_date})
    coverage = Coverage(requested=span, fetched=tuple(Interval(d, d, "nonempty", 1) for d in points),
                        observed_start=points[0] if points else None, observed_end=points[-1] if points else None,
                        row_count=len(ordered), traversal=state)
    metadata: JsonValue = {"visited_urls": visited, "pending_urls": pending, "coverage_verified": False,
                           "skipped_titles": skipped, "outside_range": outside}
    return DiscoveryResult(state=state, coverage=coverage, requests=tuple(requests), issues=tuple(issues),
                           metadata=metadata, candidates=tuple(ordered))


def _is_same_index(url: str, code: str, category: str) -> bool:
    """Report whether a URL is an index page of the same code and category.

    :param url: str; absolute URL.
    :param code: str; six-digit code.
    :param category: str; index category such as `ndbg`.
    return same: bool; True for pagination links of the same index.
    """
    parts = urlsplit(url)
    return (parts.hostname == "vip.stock.finance.sina.com.cn"
            and parts.path == f"/corp/go.php/vCB_Bulletin/stockid/{code}/page_type/{category}.phtml")


def _query_value(url: str, name: str) -> str | None:
    """Return the first value of a query parameter, or None.

    :param url: str; absolute URL.
    :param name: str; parameter name.
    return value: str | None; parameter value.
    """
    values = parse_qs(urlsplit(url).query).get(name)
    return values[0] if values else None


def _parse_iso(text: str) -> date | None:
    """Parse an ISO date and return it, or None when the text is not a real date.

    :param text: str; `YYYY-MM-DD` text.
    return day: date | None; parsed date.
    """
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Statement extraction shared by HTML and PDF
# ---------------------------------------------------------------------------


@dataclass
class TextBlock:
    """Hold one text block or one table for statement extraction.

    :param kind: str; `text` or `table`.
    :param text: str; visible text of a text block.
    :param rows: list[list[str]]; cells of a table.
    :param simple: bool; False when the table geometry is unsupported.
    :param locator: str; position such as `table:3` or `page:5:line:20`.
    return instance: TextBlock; block record.
    """

    kind: str
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)
    simple: bool = True
    locator: str = ""


@dataclass
class Extraction:
    """Collect the outcome of statement extraction from one document.

    :param statements: list[StatementRecord]; accepted statements.
    :param issues: list[Issue]; warnings and errors.
    :param headings: list[str]; statement headings that were found.
    :param unparsed: list[str]; locators of rejected tables.
    return instance: Extraction; mutable outcome.
    """

    statements: list[StatementRecord] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


@dataclass
class ColumnSpec:
    """Describe one value column of a statement table.

    :param index: int; zero-based column index.
    :param report_date: date; period date of the column.
    :param basis: str; `instant`, `annual`, `year_to_date`, or `period_end`.
    :param header: str; header text.
    :param from_title: bool; True when the date came from the document title.
    return instance: ColumnSpec; column description.
    """

    index: int
    report_date: date
    basis: str
    header: str
    from_title: bool = False


def parse_column_header(header: str, kind: StatementKind) -> tuple[date | None, str | None]:
    """Read an explicit period from a column header and return (date, period basis).

    A full date is `instant` for balance sheets and `period_end` otherwise;
    `YYYY年度` is `annual`; `YYYY年1-N月` is `year_to_date`. Headers without an
    explicit period return (None, None).

    :param header: str; column header text.
    :param kind: StatementKind; statement kind.
    return period: tuple[date | None, str | None]; period end and basis.
    """
    compact = re.sub(r"\s+", "", header)
    match = _YEAR_TO_DATE.search(compact)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if 1 <= month <= 12:
            return _month_end(year, month), "year_to_date"
    match = _FULL_DATE.search(compact)
    if match:
        groups = [g for g in match.groups() if g is not None]
        try:
            day = date(int(groups[0]), int(groups[1]), int(groups[2]))
        except ValueError:
            return None, None
        return day, "instant" if kind == "balance" else "period_end"
    match = _ANNUAL.search(compact)
    if match:
        return date(int(match.group(1)), 12, 31), "annual"
    return None, None


def _month_end(year: int, month: int) -> date:
    """Return the last day of a month.

    :param year: int; year.
    :param month: int; month 1 to 12.
    return day: date; month end.
    """
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1).fromordinal(date(year, month + 1, 1).toordinal() - 1)


def plan_columns(header: list[str], kind: StatementKind, title_date: date | None,
                 issues: list[Issue], reference: FilingRef, locator: str) -> list[ColumnSpec] | None:
    """Map a statement header row to dated value columns and return them, or None when ambiguous.

    Explicit dated headers always win. When no header has a date, a `期末` or
    `本期` column may take the candidate date from the document title, with a
    warning; comparative columns without dates are then kept only as raw cells.
    Two columns with the same date make the table ambiguous.

    :param header: list[str]; header cells; the first cell is the item title column.
    :param kind: StatementKind; statement kind.
    :param title_date: date | None; candidate period from the title.
    :param issues: list[Issue]; issue list to append to.
    :param reference: FilingRef; document reference.
    :param locator: str; table locator.
    return columns: list[ColumnSpec] | None; dated columns, or None when the table is rejected.
    """
    columns: list[ColumnSpec] = []
    for index, cell in enumerate(header[1:], start=1):
        day, basis = parse_column_header(cell, kind)
        if day is not None and basis is not None:
            columns.append(ColumnSpec(index, day, basis, cell))
    if not columns and title_date is not None:
        current = [i for i, cell in enumerate(header[1:], start=1) if re.search(r"期末|本期|本年", cell)]
        if len(current) == 1:
            basis = "instant" if kind == "balance" else "year_to_date"
            columns.append(ColumnSpec(current[0], title_date, basis, header[current[0]], from_title=True))
            issues.append(make_issue("column_date_from_title", f"column {header[current[0]]!r} at {locator} uses the"
                                     " period from the document title", "filing", severity="warning",
                                     symbol=reference.symbol, provider="filing", url=reference.uri,
                                     report_date=title_date))
    dates = [c.report_date for c in columns]
    if not columns:
        issues.append(make_issue("statement_not_found", f"table at {locator} has no dated value column", "filing",
                                 severity="warning", symbol=reference.symbol, provider="filing", url=reference.uri))
        return None
    if len(set(dates)) != len(dates):
        issues.append(make_issue("ambiguous_statement_table", f"table at {locator} has competing columns for one"
                                 " date", "filing", symbol=reference.symbol, provider="filing", url=reference.uri))
        return None
    return columns


def build_table_statements(rows: list[list[str]], heading: tuple[str, str, StatementKind], unit: str | None,
                           currency: str | None, reference: FilingRef, fmt: str, locator: str,
                           extraction: Extraction) -> bool:
    """Convert one statement table into one StatementRecord per dated column and return True on success.

    The header row is the first row whose first cell contains `项目`. Every data
    row must have exactly as many cells as the header; otherwise the whole table
    is rejected (no partial table is ever promoted). Values keep their source text
    and unit without scaling.

    :param rows: list[list[str]]; table cells.
    :param heading: tuple[str, str, StatementKind]; (heading text, scope, kind).
    :param unit: str | None; unit such as `元`, or None.
    :param currency: str | None; currency such as `人民币`, or None.
    :param reference: FilingRef; document reference.
    :param fmt: str; `html` or `pdf`.
    :param locator: str; table locator.
    :param extraction: Extraction; outcome to append to.
    return accepted: bool; True when statements were added.
    """
    heading_text, scope, kind = heading
    header_index = next((i for i, r in enumerate(rows) if r and "项目" in re.sub(r"\s+", "", r[0])), None)
    if header_index is None:
        extraction.issues.append(make_issue("statement_not_found", f"table at {locator} has no 项目 header row",
                                            "filing", severity="warning", symbol=reference.symbol, provider="filing",
                                            url=reference.uri))
        extraction.unparsed.append(locator)
        return False
    header = rows[header_index]
    columns = plan_columns(header, kind, reference.report_date, extraction.issues, reference, locator)
    if columns is None:
        extraction.unparsed.append(locator)
        return False
    body = [r for r in rows[header_index + 1:] if any(cell.strip() for cell in r)]
    if not body or any(len(r) != len(header) for r in body):
        extraction.issues.append(make_issue("ambiguous_statement_table", f"table at {locator} has rows that do not"
                                            " match its header", "filing", symbol=reference.symbol,
                                            provider="filing", url=reference.uri))
        extraction.unparsed.append(locator)
        return False
    ambiguous = 0
    for column in columns:
        items = []
        for ordinal, row in enumerate(body):
            value_kind, value_raw, value_decimal, amb = parse_decimal_text(row[column.index])
            ambiguous += int(amb)
            items.append(StatementItem(ordinal=ordinal, item_field=None, item_title=row[0].strip(),
                                       value_raw=value_raw, value_kind=value_kind, value_decimal=value_decimal,
                                       source_metadata={"cells": list(row)}))
        column_locator = f"{locator}:column:{column.index}"
        extraction.statements.append(StatementRecord(
            report_date=column.report_date, kind=kind, scope=scope, source_scope=heading_text,  # type: ignore[arg-type]
            source_key=f"{reference.uri}#{column_locator}", format=fmt, period_basis=column.basis,  # type: ignore[arg-type]
            publication_date=reference.publication_date, publication_date_raw=None, source_update_at=None,
            source_update_raw=None, currency=currency, unit=unit, items=tuple(items),
            source_metadata={"header": list(header), "column_header": column.header,
                             "date_from_title": column.from_title},
            locators=(column_locator,)))
    if ambiguous:
        extraction.issues.append(make_issue("ambiguous_number", f"{ambiguous} values at {locator} are not clear"
                                            " decimals", "filing", severity="warning", symbol=reference.symbol,
                                            provider="filing", url=reference.uri))
    if unit is None or currency is None:
        extraction.issues.append(make_issue("unit_unknown", f"table at {locator} does not state its"
                                            f" {'unit' if unit is None else 'currency'}", "filing",
                                            severity="warning", symbol=reference.symbol, provider="filing",
                                            url=reference.uri))
    return True


def extract_statements(blocks: list[TextBlock], reference: FilingRef, fmt: str) -> Extraction:
    """Find statement headings and convert the table immediately after each heading into statements.

    A heading such as `母公司资产负债表` applies only to the next table and only when
    that table starts within 1,000 visible characters. The text between the
    heading and the table supplies the unit and currency. Tables with unsupported
    geometry are rejected.

    :param blocks: list[TextBlock]; document blocks in reading order.
    :param reference: FilingRef; document reference.
    :param fmt: str; `html` or `pdf`.
    return extraction: Extraction; statements, issues, headings, and rejected locators.
    """
    extraction = Extraction()
    pending: tuple[str, str, StatementKind] | None = None
    distance = 0
    context = ""
    for block in blocks:
        if block.kind == "text":
            matches = list(_HEADING.finditer(block.text))
            if matches:
                last = matches[-1]
                pending = (last.group(0).replace(" ", ""),
                           "parent" if last.group(1) == "母公司" else "consolidated", _KIND_BY_TITLE[last.group(2)])
                context = block.text[last.end():]
                distance = len(context)
                extraction.headings.append(pending[0])
            elif pending is not None:
                context += " " + block.text
                distance += len(block.text)
            if pending is not None and distance > HEADING_WINDOW:
                pending = None
            continue
        if pending is None:
            continue
        heading, pending = pending, None
        if not block.simple:
            extraction.issues.append(make_issue("ambiguous_statement_table", f"table at {block.locator} uses"
                                                " unsupported spans or nesting", "filing", symbol=reference.symbol,
                                                provider="filing", url=reference.uri))
            extraction.unparsed.append(block.locator)
            continue
        unit_match = _UNIT.search(context)
        currency_match = _CURRENCY.search(context)
        build_table_statements(block.rows, heading, unit_match.group(1) if unit_match else None,
                               currency_match.group(1) if currency_match else None, reference, fmt, block.locator,
                               extraction)
    return extraction


def verify_identity(text: str, reference: FilingRef) -> tuple[bool, str | None]:
    """Check that a document names the requested code or a verified alias and return (ok, matched code).

    A labelled `股票代码` or `证券代码` must equal an allowed code. Without a label,
    a Sina detail URL whose `stockid` equals an allowed code is accepted only when
    that code also appears in the document text.

    :param text: str; visible document text.
    :param reference: FilingRef; document reference.
    return identity: tuple[bool, str | None]; acceptance flag and matched code.
    """
    allowed = {reference.symbol.code, *(alias.code for alias in reference.verified_aliases)}
    labelled = _CODE_LABEL.findall(text)
    if labelled:
        match = next((code for code in labelled if code in allowed), None)
        return match is not None, match
    stockid = _query_value(reference.uri, "stockid") if reference.uri.startswith("https://") else None
    if stockid in allowed and stockid is not None and stockid in text:
        return True, stockid
    return False, None


# ---------------------------------------------------------------------------
# Reading documents
# ---------------------------------------------------------------------------


def read_filing(http: HttpClient, reference: FilingRef, *, monotonic: Callable[[], float] = time.monotonic,
                root: Path = PROJECT_ROOT) -> FilingResult:
    """Read one filing document and return the statements that could be extracted.

    HTTPS documents are fetched through the client (8 MiB for HTML, 16 MiB for a
    URL ending in `.pdf`). Local documents must resolve under the project root; UNC
    paths, URI authorities, and escapes are rejected. Bytes beginning with `%PDF-`
    are parsed as PDF; other bytes must decode as HTML. The document must name the
    requested code (see verify_identity). PDF attachment links in HTML are returned
    in `metadata["attachments"]` for the caller to schedule.

    Output metadata: `headings: list[str]`, `unparsed_locators: list[str]`,
    `attachments: list[FilingRef as JSON]`, `matched_code: str | None`.

    :param http: HttpClient; bounded client.
    :param reference: FilingRef; document to read.
    :param monotonic: Callable[[], float]; clock used for the PDF deadline.
    :param root: Path; project root for local files.
    return result: FilingResult; extraction outcome; status is parsed, partial, unparsed, or fetch_failed.
    """
    requests: tuple[RequestMeta, ...] = ()
    try:
        if reference.uri.startswith("https://") or reference.uri.startswith("http://"):
            url = normalize_source_url(reference.uri)
            limit = PDF_MAX_BYTES if urlsplit(url).path.lower().endswith(".pdf") else HTML_MAX_BYTES
            response = http.get(url, max_bytes=limit)
            body, content_type, requests = response.body, response.content_type, response.requests
        else:
            body, content_type = read_local_file(reference.uri, root), ""
    except SourceError as exc:
        return _fetch_failed(reference, relabel_issue(exc.issue, "filing", reference.symbol, "filing"),
                             exc.requests)
    except _LocalFileError as exc:
        issue = make_issue(exc.code, str(exc), "filing", symbol=reference.symbol, provider="filing",
                           url=reference.uri)
        return _fetch_failed(reference, issue, ())
    body_hash = compute_sha256(body)
    attachments: list[FilingRef] = []
    if body.startswith(b"%PDF-"):
        fmt = "pdf"
        blocks, text, issue = read_pdf_blocks(body, monotonic)
    else:
        fmt = "html"
        try:
            html = decode_html(body, content_type)
        except ValueError as exc:
            issue = make_issue("unsupported_charset" if "charset" in str(exc) else "unsupported_filing_format",
                               str(exc), "filing", symbol=reference.symbol, provider="filing", url=reference.uri)
            return _unparsed(reference, None, body_hash, requests, [issue], [], [], [])
        if "<" not in html[:4096]:
            issue = make_issue("unsupported_filing_format", "document is neither PDF nor HTML", "filing",
                               symbol=reference.symbol, provider="filing", url=reference.uri)
            return _unparsed(reference, None, body_hash, requests, [issue], [], [], [])
        blocks, text, attachments = read_html_blocks(html, reference)
        issue = None
    if issue is not None:
        return _unparsed(reference, fmt, body_hash, requests, [issue], [], [], attachments)
    ok, matched = verify_identity(text, reference)
    if not ok:
        issue = make_issue("filing_identity_mismatch", "document does not name the requested code or a verified"
                           " alias", "filing", symbol=reference.symbol, provider="filing", url=reference.uri)
        return _unparsed(reference, fmt, body_hash, requests, [issue], [], [], attachments)
    extraction = extract_statements(blocks, reference, fmt)
    if not extraction.statements and not any(i.code == "statement_not_found" for i in extraction.issues):
        extraction.issues.append(make_issue("statement_not_found", "no statement table follows a statement heading",
                                            "filing", severity="warning", symbol=reference.symbol,
                                            provider="filing", url=reference.uri))
    if extraction.statements:
        status = "partial" if extraction.unparsed else "parsed"
    else:
        status = "unparsed"
    state = {"parsed": "succeeded", "partial": "partial", "unparsed": "partial"}[status]
    dates = sorted({s.report_date for s in extraction.statements})
    coverage = Coverage(requested=None, fetched=tuple(Interval(d, d, "nonempty", 1) for d in dates),
                        observed_start=dates[0] if dates else None, observed_end=dates[-1] if dates else None,
                        row_count=len(extraction.statements), traversal=state)  # type: ignore[arg-type]
    metadata = {"headings": extraction.headings, "unparsed_locators": extraction.unparsed,
                "attachments": convert_to_json(attachments), "matched_code": matched}
    return FilingResult(state=state, coverage=coverage, requests=requests, issues=tuple(extraction.issues),  # type: ignore[arg-type]
                        metadata=metadata, reference=reference, format=fmt, body_sha256=body_hash,  # type: ignore[arg-type]
                        status=status, statements=tuple(extraction.statements))  # type: ignore[arg-type]


def _fetch_failed(reference: FilingRef, issue: Issue, requests: tuple[RequestMeta, ...]) -> FilingResult:
    """Build a failed FilingResult for a document that could not be read and return it.

    :param reference: FilingRef; document reference.
    :param issue: Issue; failure reason.
    :param requests: tuple[RequestMeta, ...]; HTTP attempts.
    return result: FilingResult; failed result.
    """
    return FilingResult(state="failed", coverage=Coverage(requested=None, traversal="failed"), requests=requests,
                        issues=(issue,), metadata={"headings": [], "unparsed_locators": [], "attachments": []},
                        reference=reference, status="fetch_failed")


def _unparsed(reference: FilingRef, fmt: str | None, body_hash: str, requests: tuple[RequestMeta, ...],
              issues: list[Issue], headings: list[str], unparsed: list[str],
              attachments: list[FilingRef]) -> FilingResult:
    """Build a FilingResult for a document that was read but yielded no statements and return it.

    :param reference: FilingRef; document reference.
    :param fmt: str | None; detected format.
    :param body_hash: str; SHA-256 of the document.
    :param requests: tuple[RequestMeta, ...]; HTTP attempts.
    :param issues: list[Issue]; reasons.
    :param headings: list[str]; headings found.
    :param unparsed: list[str]; rejected locators.
    :param attachments: list[FilingRef]; attachment references.
    return result: FilingResult; unparsed result in state partial.
    """
    return FilingResult(state="partial", coverage=Coverage(requested=None, traversal="partial"), requests=requests,
                        issues=tuple(issues), metadata={"headings": headings, "unparsed_locators": unparsed,
                                                        "attachments": convert_to_json(attachments)},
                        reference=reference, format=fmt, body_sha256=body_hash, status="unparsed")  # type: ignore[arg-type]


class _LocalFileError(Exception):
    """Signal that an explicit local filing path is not allowed or cannot be read.

    :param code: str; issue code.
    :param message: str; explanation.
    return instance: _LocalFileError; raisable error.
    """

    def __init__(self, code: str, message: str) -> None:
        """Store the code and message.

        :param code: str; issue code.
        :param message: str; explanation.
        return None: None; no value is returned.
        """
        super().__init__(message)
        self.code = code


def resolve_local_path(uri: str, root: Path = PROJECT_ROOT) -> Path:
    """Resolve an explicit local filing URI or path under the project root and return the path.

    `file:` URIs must not name an authority (host). UNC paths, drive-relative
    tricks, and paths that resolve outside the root raise _LocalFileError with
    code `invalid_path`.

    :param uri: str; `file:` URI, absolute path, or root-relative path.
    :param root: Path; project root.
    return path: Path; resolved file path under the root.
    """
    text = uri
    if text.lower().startswith("file:"):
        parts = urlsplit(text)
        if parts.netloc not in ("",):
            raise _LocalFileError("invalid_path", "file URI must not contain an authority")
        text = unquote(parts.path)
        if re.match(r"^/[A-Za-z]:", text):
            text = text[1:]
    if text.startswith("\\\\") or text.startswith("//"):
        raise _LocalFileError("invalid_path", "UNC paths are not allowed")
    candidate = Path(text)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        raise _LocalFileError("invalid_path", "local filing path is outside the project root") from None
    return resolved


def read_local_file(uri: str, root: Path = PROJECT_ROOT) -> bytes:
    """Read an explicit local filing within the PDF size limit and return its bytes.

    :param uri: str; explicit local URI or path.
    :param root: Path; project root.
    return body: bytes; file contents.
    """
    path = resolve_local_path(uri, root)
    if not path.is_file():
        raise _LocalFileError("invalid_path", "local filing does not exist or is not a file")
    if path.stat().st_size > PDF_MAX_BYTES:
        raise _LocalFileError("response_too_large", "local filing is larger than 16 MiB")
    return path.read_bytes()


def read_html_blocks(html: str, reference: FilingRef) -> tuple[list[TextBlock], str, list[FilingRef]]:
    """Convert decoded HTML into extraction blocks, visible text, and PDF attachment references.

    :param html: str; decoded HTML.
    :param reference: FilingRef; document reference used to build attachments.
    return parsed: tuple[list[TextBlock], str, list[FilingRef]]; blocks, visible text, and attachments.
    """
    parser = parse_html_blocks(html)
    blocks: list[TextBlock] = []
    texts: list[str] = []
    attachments: dict[str, FilingRef] = {}
    for block in parser.blocks:
        if block.kind == "table":
            blocks.append(TextBlock(kind="table", rows=block.rows, simple=block.simple,
                                    locator=f"table:{block.index}"))
            texts.append(" ".join(" ".join(r) for r in block.rows))
        else:
            blocks.append(TextBlock(kind="text", text=block.text))
            texts.append(block.text)
    if reference.uri.startswith("https://"):
        for href, _title, _before in parser.link_contexts:
            target = normalize_source_url(urljoin(reference.uri, href))
            if urlsplit(target).path.lower().endswith(".pdf") and validate_remote_url(target) is None:
                attachments.setdefault(target, FilingRef(
                    symbol=reference.symbol, uri=target, report_date=reference.report_date,
                    publication_date=reference.publication_date, origin=reference.origin,
                    verified_aliases=reference.verified_aliases))
    return blocks, "\n".join(texts), list(attachments.values())


def read_pdf_blocks(body: bytes, monotonic: Callable[[], float] = time.monotonic
                    ) -> tuple[list[TextBlock], str, Issue | None]:
    """Extract layout text from a text PDF within resource limits and convert it into blocks.

    The reader runs inside `pypdf.apply_configuration` with the DTD limits. It
    rejects encrypted files and files with more than 512 pages, checks decoded
    content size per page (2 MiB) and in total (64 MiB), limits extracted text to
    4 MiB, and stops after a 60-second cooperative deadline. LimitReachedError
    becomes `pdf_resource_limit`; other parse errors become `pdf_parse_error`.
    Pages with almost no text make the file `scanned_or_unreadable_pdf`.

    :param body: bytes; PDF bytes.
    :param monotonic: Callable[[], float]; clock for the deadline.
    return parsed: tuple[list[TextBlock], str, Issue | None]; blocks, text, and a failure issue or None.
    """
    from pypdf import PdfReader, apply_configuration
    from pypdf.errors import LimitReachedError, PyPdfError

    def fail(code: str, message: str) -> tuple[list[TextBlock], str, Issue | None]:
        """Return an empty extraction with an error issue.

        :param code: str; issue code.
        :param message: str; explanation.
        return parsed: tuple[list[TextBlock], str, Issue | None]; empty blocks and the issue.
        """
        return [], "", make_issue(code, message, "filing", provider="filing")

    deadline = monotonic() + PDF_DEADLINE_SECONDS
    pages_text: list[str] = []
    try:
        with apply_configuration(**PDF_SETTINGS):
            reader = PdfReader(BytesIO(body), strict=False, root_object_recovery_limit=1000)
            if reader.is_encrypted:
                return fail("encrypted_pdf", "PDF is encrypted")
            if len(reader.pages) > PDF_MAX_PAGES:
                return fail("work_limit", f"PDF has more than {PDF_MAX_PAGES} pages")
            total_content = 0
            total_text = 0
            for page in reader.pages:
                if monotonic() > deadline:
                    return fail("work_limit", "PDF extraction deadline reached")
                contents = page.get_contents()
                size = len(contents.get_data()) if contents is not None else 0
                if size > PDF_PAGE_CONTENT_LIMIT:
                    return fail("pdf_resource_limit", "a PDF page has more than 2 MiB of decoded content")
                total_content += size
                if total_content > PDF_TOTAL_CONTENT_LIMIT:
                    return fail("pdf_resource_limit", "PDF has more than 64 MiB of decoded content")
                text = page.extract_text(extraction_mode="layout") or ""
                total_text += len(text.encode("utf-8"))
                if total_text > PDF_TEXT_LIMIT:
                    return fail("pdf_resource_limit", "PDF text is larger than 4 MiB")
                pages_text.append(text)
                if monotonic() > deadline:
                    return fail("work_limit", "PDF extraction deadline reached")
            del reader
    except LimitReachedError as exc:
        return fail("pdf_resource_limit", f"PDF resource limit: {exc}")
    except PyPdfError as exc:
        return fail("pdf_parse_error", f"PDF cannot be parsed: {exc}")
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, RecursionError) as exc:
        return fail("pdf_parse_error", f"PDF cannot be parsed: {type(exc).__name__}")
    visible = sum(len(re.sub(r"\s+", "", t)) for t in pages_text)
    if not pages_text or visible < 20 * len(pages_text) // 4 or visible < 20:
        return fail("scanned_or_unreadable_pdf", "PDF contains little or no extractable text")
    return convert_pdf_text(pages_text), "\n".join(pages_text), None


_SPLIT = re.compile(r"\s{2,}")
_VALUE_TOKEN = re.compile(r"\(?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\)?|--?|—+|-")


def convert_pdf_text(pages: list[str]) -> list[TextBlock]:
    """Convert layout text pages into text blocks and explicit-column tables.

    A table starts at a line whose first column contains `项目` and that has at
    least one more column; columns are separated by two or more spaces. A line
    belongs to the open table only when it has exactly one cell per header column
    and every value cell is a number or a missing marker (see _fit_row). The table
    ends at the first other line, except that up to three lines at the bottom of a
    page and three lines at the top of the next page are treated as running
    footers and headers. A table open at a page break continues only when the next
    page repeats the same statement heading and header row. When the next page
    instead starts directly with value rows, the continuation is unverified and
    the table is marked non-simple so that no partial table is promoted.

    :param pages: list[str]; layout text per page.
    return blocks: list[TextBlock]; blocks in reading order with `page:P:line:L` locators.
    """
    blocks: list[TextBlock] = []
    table: TextBlock | None = None
    header: list[str] = []
    table_heading: str | None = None
    last_heading: str | None = None
    for page_no, page in enumerate(pages, start=1):
        lines = [(n, line.strip()) for n, line in enumerate(page.splitlines(), start=1) if line.strip()]
        carried = table is not None
        repeated_heading = False
        for position, (line_no, stripped) in enumerate(lines):
            tokens = _SPLIT.split(stripped)
            heading = _HEADING.search(stripped)
            if table is not None and carried:
                if heading and heading.group(0).replace(" ", "") == table_heading:
                    repeated_heading = True
                    continue
                if repeated_heading and tokens == header:
                    carried = False
                    continue
                row = _fit_row(tokens, header)
                if row is not None:
                    table.simple = False
                    table.rows.append(row)
                    carried = False
                    continue
                if position < 3 and not heading and "项目" not in tokens[0]:
                    continue
                table, carried = None, False
            elif table is not None:
                row = _fit_row(tokens, header)
                if row is not None:
                    table.rows.append(row)
                    continue
                if position >= len(lines) - 3 and not heading and "项目" not in tokens[0]:
                    continue
                table = None
            if "项目" in tokens[0] and len(tokens) >= 2:
                header = tokens
                table_heading = last_heading
                table = TextBlock(kind="table", rows=[tokens], locator=f"page:{page_no}:line:{line_no}")
                blocks.append(table)
                continue
            if heading:
                last_heading = heading.group(0).replace(" ", "")
            blocks.append(TextBlock(kind="text", text=re.sub(r"\s+", " ", stripped)))
    return blocks


def _fit_row(tokens: list[str], header: list[str]) -> list[str] | None:
    """Fit layout tokens to a header and return a row with one cell per header column, or None.

    :param tokens: list[str]; tokens of one line.
    :param header: list[str]; header tokens.
    return row: list[str] | None; aligned row, or None when the line is not a table row.
    """
    has_note = any("附注" in h for h in header[1:])
    if len(tokens) == len(header) and all(_VALUE_TOKEN.fullmatch(t) for t in tokens[1 + int(has_note):]):
        return tokens
    if has_note and len(tokens) == len(header) - 1 and all(_VALUE_TOKEN.fullmatch(t) for t in tokens[1:]):
        return [tokens[0], "", *tokens[1:]]
    return None
