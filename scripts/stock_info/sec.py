"""SEC sources for US companies: the ticker list, reported values, and statements from report pages.

Every request goes through providers.HttpClient, which sends the owner's contact
line to SEC hosts and limits the request rate. The module never touches SQLite.
Report pages and filing summaries are untrusted input: they are size-limited,
parsed without executing anything, and no link inside them is followed.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Iterable, Sequence

from .providers import (
    API_MAX_BYTES, HTML_MAX_BYTES, REPORT_MAX_ITEMS, SEC_FACTS_MAX_BYTES, Coverage, DateRange, FactRow, FetchResult,
    FilingRef, FilingResult, HttpClient, Issue, JsonValue, MappingResult, RequestMeta, SourceError, StatementItem,
    StatementKind, StatementRecord, Symbol, compute_sha256, make_issue, parse_decimal_text, parse_strict_json,
    relabel_issue,
)

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SEC_FOLDER_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/"
PERIODIC_FORMS = ("10-K", "10-Q", "20-F", "40-F")
STATEMENT_KINDS: tuple[StatementKind, ...] = ("income", "balance", "cash_flow")
MAX_FACT_ROWS = 500_000
MAX_CELL_CHARS = 2000
MAX_ROW_CELLS = 64

_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
_LONG_NAME = re.compile(r"\S+ - (\w+) - (.+)", re.DOTALL)
_HTML_FILE = re.compile(r"R\d{1,4}\.htm")
_XML_FILE = re.compile(r"R\d{1,4}\.xml")
_CONCEPT = re.compile(r"defref_([A-Za-z0-9\-]+_[A-Za-z0-9]+)")
_DATE = re.compile(r"\b([A-Z][a-z]{2,3})\.? (\d{1,2}), (\d{4})\b")
_MONTHS_ENDED = re.compile(r"(\d{1,2}) Months? Ended")
_CURRENCY_PREFIX = re.compile(r"\(?([A-Z]{3})\b[^)]*\)?\s*")
_COLUMN_CURRENCY = re.compile(r"\b([A-Z]{3})\b")
_FOOTNOTE_MARK = re.compile(r"\[\d+\]")
_SPACES = re.compile(r"[\s\u00a0]+")
_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Sept": 9,
           "Oct": 10, "Nov": 11, "Dec": 12}
_VALUE_CLASSES = frozenset({"nump", "num", "text"})


@dataclass(frozen=True)
class SecFiling:
    """Identify one periodic report in the SEC filing archive and return the record.

    :param reference: FilingRef; folder URL of the filing, its period end, and its filing date.
    :param form: str; form type such as `10-K` or `20-F`.
    :param accession: str; SEC identifier of the filing, such as `0000320193-26-000026`.
    return instance: SecFiling; immutable report identity.
    """

    reference: FilingRef
    form: str
    accession: str


@dataclass(frozen=True)
class ReportPage:
    """Describe one entry of a filing summary and return the record.

    :param position: int; one-based position of the entry in the summary.
    :param long_name: str; full name such as `9952151 - Statement - CONSOLIDATED BALANCE SHEETS`.
    :param category: str | None; middle part of the long name, such as `Statement`, or None.
    :param title: str; last part of the long name, or the whole long name when it has no parts.
    :param html_file: str | None; page file such as `R3.htm`, or None when the entry has no HTML page.
    :param xml_file: str | None; old-format page file such as `R1.xml`, or None.
    return instance: ReportPage; immutable summary entry.
    """

    position: int
    long_name: str
    category: str | None
    title: str
    html_file: str | None
    xml_file: str | None

    @property
    def is_statement(self) -> bool:
        """Report whether the entry is a financial statement page and return the answer.

        return answer: bool; True when the category is `Statement`.
        """
        return self.category == "Statement"


@dataclass(frozen=True)
class HeaderCell:
    """Hold one header cell of a report table and return the record.

    :param css: str; class of the cell, `tl` for the title cell or `th` for a column heading.
    :param colspan: int; number of columns that the cell spans, at least 1.
    :param text: str; cell text with single spaces.
    return instance: HeaderCell; immutable header cell.
    """

    css: str
    colspan: int
    text: str


@dataclass(frozen=True)
class ReportRow:
    """Hold one body row of a report table and return the record.

    :param css: str; class of the row; `rh` marks the heading of a breakdown.
    :param label: str; row label with single spaces.
    :param concept: str | None; concept named by the label link, such as `us-gaap_Assets`, or None.
    :param custom: bool; True when the row is company-specific.
    :param values: tuple[str, ...]; text of each value cell in column order; empty text for an empty cell.
    return instance: ReportRow; immutable body row.
    """

    css: str
    label: str
    concept: str | None
    custom: bool
    values: tuple[str, ...]


@dataclass(frozen=True)
class ReportTable:
    """Hold the content of one report table and return the record.

    :param title: str; text of the title cell, which names the statement and its units.
    :param header_rows: tuple[tuple[HeaderCell, ...], ...]; header rows from top to bottom.
    :param rows: tuple[ReportRow, ...]; body rows in page order.
    return instance: ReportTable; immutable table content.
    """

    title: str
    header_rows: tuple[tuple[HeaderCell, ...], ...]
    rows: tuple[ReportRow, ...]


# ---------------------------------------------------------------------------
# Ticker list and reported values
# ---------------------------------------------------------------------------


def lookup_sec_company(http: HttpClient, symbol: Symbol) -> MappingResult:
    """Find a US ticker in the SEC ticker list and return the company record as a mapping result.

    Input (JSON from the SEC): `fields` is a list of column names that must include
    `cik`, `name`, `ticker`, and `exchange`; `data` is a list of rows in that column
    order. `exchange` may be null.

    Output metadata: `found: bool` and `ticker: str`; when found also `cik: int`,
    `name: str`, and `sec_exchange: str | None`. The result has no code links. A
    ticker that is not in the list succeeds with the warning `sec_company_not_found`.
    Rows with different company numbers for one ticker fail with
    `sec_company_ambiguous`. An unexpected shape fails with `schema_changed`.

    :param http: HttpClient; bounded client that carries the SEC contact line.
    :param symbol: Symbol; US symbol whose code is the ticker as the SEC writes it.
    return result: MappingResult; company record in the metadata.
    """
    def fail(issue: Issue, requests: tuple[RequestMeta, ...]) -> MappingResult:
        """Build a failed lookup result and return it.

        :param issue: Issue; reason for the failure.
        :param requests: tuple[RequestMeta, ...]; HTTP attempts.
        return result: MappingResult; failed result.
        """
        return MappingResult(state="failed", coverage=Coverage(requested=None, traversal="failed"),
                             requests=requests, issues=(issue,), metadata={})

    try:
        response = http.get(SEC_TICKERS_URL, max_bytes=API_MAX_BYTES)
    except SourceError as exc:
        return fail(relabel_issue(exc.issue, "mapping", symbol, "sec"), exc.requests)
    try:
        payload = parse_strict_json(response.body)
        fields = list(payload["fields"])
        columns = [fields.index(name) for name in ("cik", "name", "ticker", "exchange")]
        matches = [row for row in payload["data"] if row[columns[2]] == symbol.code]
        numbers = {int(row[columns[0]]) for row in matches}
    except (ValueError, KeyError, TypeError, IndexError, UnicodeDecodeError):
        issue = make_issue("schema_changed", "the SEC ticker list does not have the expected shape", "mapping",
                           symbol=symbol, provider="sec", url=SEC_TICKERS_URL)
        return fail(issue, response.requests)
    if len(numbers) > 1:
        issue = make_issue("sec_company_ambiguous", f"the SEC ticker list names {len(numbers)} companies for"
                           f" {symbol.code}", "mapping", symbol=symbol, provider="sec", url=SEC_TICKERS_URL)
        return fail(issue, response.requests)
    if not matches:
        issue = make_issue("sec_company_not_found", f"{symbol.code} is not in the SEC ticker list, so no SEC data"
                           " can be collected for it", "mapping", severity="warning", symbol=symbol, provider="sec",
                           url=SEC_TICKERS_URL)
        return MappingResult(state="succeeded", coverage=Coverage(requested=None, traversal="succeeded"),
                             requests=response.requests, issues=(issue,),
                             metadata={"found": False, "ticker": symbol.code})
    row = matches[0]
    exchange = row[columns[3]]
    metadata: dict[str, JsonValue] = {"found": True, "cik": int(row[columns[0]]), "name": str(row[columns[1]]),
                                      "sec_exchange": str(exchange) if exchange is not None else None,
                                      "ticker": symbol.code}
    return MappingResult(state="succeeded", coverage=Coverage(requested=None, row_count=1, traversal="succeeded"),
                         requests=response.requests, issues=(), metadata=metadata)


def _build_fact(taxonomy: str, concept: str, label: Any, unit: str, value: Any) -> FactRow:
    """Validate one reported value and return it as a FactRow, or raise ValueError.

    Input (one JSON object of the SEC): `end` (ISO date, required), `val` (integer
    or decimal number, required), `accn` (text, required), and the optional keys
    `start` and `filed` (ISO dates), `fy` (integer), `fp`, `form`, and `frame` (text).

    :param taxonomy: str; group of the concept.
    :param concept: str; concept name.
    :param label: Any; SEC label of the concept, text or None.
    :param unit: str; unit of the value.
    :param value: Any; JSON object of one value.
    return row: FactRow; validated value.
    """
    number = value["val"]
    if isinstance(number, bool) or not isinstance(number, (int, Decimal)):
        raise ValueError("a value is not a number")
    number = Decimal(number)
    if not number.is_finite():
        raise ValueError("a value is not finite")
    accession = value["accn"]
    if not isinstance(accession, str) or not accession:
        raise ValueError("a value has no filing identifier")
    start, filed, year = value.get("start"), value.get("filed"), value.get("fy")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ValueError("a fiscal year is not a whole number")
    texts = {key: value.get(key) for key in ("fp", "form", "frame")}
    if any(text is not None and not isinstance(text, str) for text in texts.values()):
        raise ValueError("a text field of a value is not text")
    return FactRow(taxonomy=taxonomy, concept=concept, label=label if isinstance(label, str) else None, unit=unit,
                   period_start=date.fromisoformat(start) if start is not None else None,
                   period_end=date.fromisoformat(value["end"]), value=format(number, "f"), accession=accession,
                   fiscal_year=year, fiscal_period=texts["fp"], form=texts["form"],
                   filed=date.fromisoformat(filed) if filed is not None else None, frame=texts["frame"])


def fetch_sec_facts(http: HttpClient, symbol: Symbol, cik: int) -> FetchResult:
    """Fetch every financial value that a company reported to the SEC and return it as a fetch result.

    Input (JSON from the SEC): `cik` (number or text of the company number),
    `entityName` (text), and `facts`, an object of groups such as `us-gaap`. Each
    group is an object of concepts; each concept has `label` (text or null) and
    `units`, an object that maps a unit such as `USD` to a list of values. See
    _build_fact for the keys of one value.

    Output: FactRow records sorted by group, concept, unit, period end, period
    start, and filing. Metadata: `cik: int`, `entity_name: str | None`, and
    `concept_count: int`. HTTP 404 means that the SEC holds no values for the
    company: the result succeeds without records and with the warning
    `sec_values_unavailable`. A payload for another company fails with
    `identity_mismatch`. One malformed value rejects the whole answer with
    `schema_changed`, so a partly understood answer is never stored.

    :param http: HttpClient; bounded client that carries the SEC contact line.
    :param symbol: Symbol; US symbol, used to label issues.
    :param cik: int; SEC company number from lookup_sec_company.
    return result: FetchResult; records are FactRow values.
    """
    url = SEC_FACTS_URL.format(cik=cik)
    empty = Coverage(requested=None, traversal="failed")
    try:
        response = http.get(url, max_bytes=SEC_FACTS_MAX_BYTES)
    except SourceError as exc:
        if exc.requests and exc.requests[-1].status == 404:
            issue = make_issue("sec_values_unavailable", "the SEC company-data service holds no reported values for"
                               " this company", "facts", severity="warning", symbol=symbol, provider="sec", url=url)
            return FetchResult(state="succeeded", coverage=Coverage(requested=None, traversal="succeeded"),
                               requests=exc.requests, issues=(issue,), metadata={"cik": cik})
        return FetchResult(state="failed", coverage=empty, requests=exc.requests,
                           issues=(relabel_issue(exc.issue, "facts", symbol, "sec"),), metadata={"cik": cik})

    def fail(code: str, message: str) -> FetchResult:
        """Build a failed values result and return it.

        :param code: str; issue code.
        :param message: str; explanation.
        return result: FetchResult; failed result without records.
        """
        issue = make_issue(code, message, "facts", symbol=symbol, provider="sec", url=url)
        return FetchResult(state="failed", coverage=empty, requests=response.requests, issues=(issue,),
                           metadata={"cik": cik})

    rows: list[FactRow] = []
    concept_count = 0
    try:
        payload = parse_strict_json(response.body)
        if int(payload["cik"]) != cik:
            return fail("identity_mismatch", f"the SEC returned values for company number {payload['cik']}")
        for taxonomy, concepts in payload["facts"].items():
            for concept, info in concepts.items():
                concept_count += 1
                for unit, values in info["units"].items():
                    for value in values:
                        rows.append(_build_fact(str(taxonomy), str(concept), info.get("label"), str(unit), value))
                        if len(rows) > MAX_FACT_ROWS:
                            return fail("work_limit", f"the SEC returned more than {MAX_FACT_ROWS} values")
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError) as exc:
        return fail("schema_changed", f"the SEC reported values do not have the expected shape: {exc}")
    rows.sort(key=lambda r: (r.taxonomy, r.concept, r.unit, r.period_end, r.period_start or date.min, r.accession))
    ends = [r.period_end for r in rows]
    coverage = Coverage(requested=None, observed_start=min(ends) if ends else None,
                        observed_end=max(ends) if ends else None, row_count=len(rows), traversal="succeeded")
    entity = payload.get("entityName")
    metadata: dict[str, JsonValue] = {"cik": cik, "entity_name": entity if isinstance(entity, str) else None,
                                      "concept_count": concept_count}
    return FetchResult(state="succeeded", coverage=coverage, requests=response.requests, issues=(),
                       metadata=metadata, records=tuple(rows))


def plan_sec_filings(rows: Iterable[tuple[str, str | None, str | None, str, str, int]], symbol: Symbol, cik: int,
                     span: DateRange) -> list[SecFiling]:
    """Derive the periodic reports of a company from its stored values and return them, newest filing first.

    The reported values name every filing that contributed a value. A filing is
    kept when its form starts with `10-K`, `10-Q`, `20-F`, or `40-F`, its identifier
    is well formed, and it has at least one value outside the group `dei` (a filing
    with cover-page values only has no statements). The period end of a filing is
    estimated as the latest period end, not after the filing date, that carries at
    least half as many values as the most frequent period end. Comparative periods
    are earlier, and events after the period end carry only a few values, so the
    estimate is the period that the report is about. Only filings whose estimate
    lies in `span` are returned.

    :param rows: Iterable[tuple[str, str | None, str | None, str, str, int]]; one item per filing, period
        end, and group: (accession, form, filing date as ISO text, period end as ISO text, group, value count).
    :param symbol: Symbol; US symbol of the company.
    :param cik: int; SEC company number, used in the folder URL.
    :param span: DateRange; inclusive range for the estimated period end.
    return filings: list[SecFiling]; planned reports ordered by filing date, newest first.
    """
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    details: dict[str, tuple[str, str | None]] = {}
    financial: set[str] = set()
    for accession, form, filed, period_end, taxonomy, count in rows:
        if not form or not form.upper().startswith(PERIODIC_FORMS) or _ACCESSION.fullmatch(accession) is None:
            continue
        counts[accession][period_end] += count
        details[accession] = (form, filed)
        if taxonomy != "dei":
            financial.add(accession)
    planned: list[SecFiling] = []
    for accession in financial:
        form, filed = details[accession]
        most = max(counts[accession].values())
        major = [end for end, count in counts[accession].items() if count * 2 >= most and (not filed or end <= filed)]
        if not major:
            continue
        estimate = date.fromisoformat(max(major))
        if not span.start <= estimate <= span.end:
            continue
        reference = FilingRef(symbol=symbol, uri=SEC_FOLDER_URL.format(cik=cik, accession=accession.replace("-", "")),
                              report_date=estimate, publication_date=date.fromisoformat(filed) if filed else None,
                              origin="automatic")
        planned.append(SecFiling(reference=reference, form=form, accession=accession))
    planned.sort(key=lambda f: (f.reference.publication_date or date.min, f.accession), reverse=True)
    return planned


# ---------------------------------------------------------------------------
# Filing summary and statement page selection
# ---------------------------------------------------------------------------


def parse_filing_summary(body: bytes) -> list[ReportPage]:
    """Parse a `FilingSummary.xml` document and return its entries in file order.

    Input (XML): a `FilingSummary` element whose `Report` elements each hold a
    `LongName` such as `9952151 - Statement - CONSOLIDATED BALANCE SHEETS`, and
    either `HtmlFileName` (`R3.htm`) or, in old filings, `XmlFileName` (`R1.xml`).
    A page file name is accepted only in the form `R<number>.htm` or
    `R<number>.xml`; any other name becomes None, so a summary cannot direct the
    collector to another path or host. Raises ValueError for invalid XML and for a
    document that declares a document type or an entity.

    :param body: bytes; content of `FilingSummary.xml`.
    return pages: list[ReportPage]; one entry per `Report` element.
    """
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise ValueError("the filing summary declares a document type")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ValueError(f"the filing summary is not valid XML: {exc}") from None
    pages: list[ReportPage] = []
    for position, element in enumerate(root.iter("Report"), start=1):
        long_name = _SPACES.sub(" ", element.findtext("LongName") or "").strip()
        match = _LONG_NAME.fullmatch(long_name)
        html_file = (element.findtext("HtmlFileName") or "").strip()
        xml_file = (element.findtext("XmlFileName") or "").strip()
        pages.append(ReportPage(
            position=position, long_name=long_name, category=match.group(1) if match else None,
            title=match.group(2).strip() if match else long_name,
            html_file=html_file if _HTML_FILE.fullmatch(html_file) else None,
            xml_file=xml_file if _XML_FILE.fullmatch(xml_file) else None))
    return pages


def select_statement_pages(pages: Sequence[ReportPage]) -> dict[StatementKind, ReportPage]:
    """Choose the income, balance-sheet, and cash-flow page of a report by title and return them.

    Only statement entries are considered, and titles are compared without case.
    A title with `parenthetical` is skipped. `cash flow` gives the cash-flow
    statement. `balance sheet`, `financial position`, or `financial condition`
    gives the balance sheet. A title with `equity`, `shareholders`, `stockholders`,
    or `capital` is skipped. `operations`, `income`, `earnings`, `profit`, or `loss`
    gives an income candidate; a candidate without `comprehensive` is preferred,
    because some companies show comprehensive income on a separate page and others
    in one combined statement. The first page in file order wins for each kind. A
    kind without a matching page is absent from the result.

    :param pages: Sequence[ReportPage]; entries from parse_filing_summary.
    return chosen: dict[StatementKind, ReportPage]; chosen page per statement kind.
    """
    chosen: dict[StatementKind, ReportPage] = {}
    combined: ReportPage | None = None
    for page in pages:
        title = page.title.lower()
        if not page.is_statement or "parenthetical" in title:
            continue
        if "cash flow" in title:
            chosen.setdefault("cash_flow", page)
        elif any(word in title for word in ("balance sheet", "financial position", "financial condition")):
            chosen.setdefault("balance", page)
        elif any(word in title for word in ("equity", "shareholders", "stockholders", "capital")):
            continue
        elif any(word in title for word in ("operations", "income", "earnings", "profit", "loss")):
            if "comprehensive" in title:
                combined = combined or page
            else:
                chosen.setdefault("income", page)
    if "income" not in chosen and combined is not None:
        chosen["income"] = combined
    return chosen


# ---------------------------------------------------------------------------
# Report pages
# ---------------------------------------------------------------------------


def _clean(text: str) -> str:
    """Replace every run of spaces, line breaks, and non-breaking spaces by one space and return the trimmed text.

    :param text: str; raw text.
    return text: str; text with single spaces.
    """
    return _SPACES.sub(" ", text).strip()


class _ReportTableParser(HTMLParser):
    """Collect the header and body rows of the first table with the class `report`.

    The parser reads only cells that belong directly to that table. Tables nested
    inside it (footnote text) and every other table of the page (concept
    definitions) are ignored. Script content is never interpreted.

    return instance: _ReportTableParser; parser whose `header_rows` and `rows` hold the result after `close()`.
    """

    def __init__(self) -> None:
        """Start with no table found and empty row lists.

        return None: None; no value is returned.
        """
        super().__init__(convert_charrefs=True)
        self.found = False
        self.header_rows: list[tuple[HeaderCell, ...]] = []
        self.rows: list[ReportRow] = []
        self._depth = 0
        self._finished = False
        self._row: list[dict[str, Any]] | None = None
        self._row_css = ""
        self._cell: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track table nesting and open rows, cells, and label links of the report table.

        :param tag: str; lower-case tag name.
        :param attrs: list[tuple[str, str | None]]; attribute names and values.
        return None: None; no value is returned.
        """
        if self._finished:
            return
        values = {name: value or "" for name, value in attrs}
        if tag == "table":
            if self._depth:
                self._depth += 1
            elif "report" in values.get("class", "").split():
                self._depth, self.found = 1, True
            return
        if self._depth != 1:
            return
        if tag == "tr":
            self._row, self._row_css, self._cell = [], values.get("class", ""), None
        elif tag in ("td", "th") and self._row is not None:
            span = values.get("colspan", "1")
            self._cell = {"tag": tag, "css": values.get("class", ""), "colspan": int(span) if span.isdigit() else 1,
                          "text": [], "concept": None}
            if len(self._row) >= MAX_ROW_CELLS:
                raise ValueError("a row of the report table has too many cells")
            self._row.append(self._cell)
        elif tag == "a" and self._cell is not None and self._cell["concept"] is None:
            match = _CONCEPT.search(values.get("onclick", ""))
            if match:
                self._cell["concept"] = match.group(1)
        elif tag in ("br", "div", "p") and self._cell is not None:
            self._cell["text"].append(" ")  # a line break or a new block separates words

    def handle_endtag(self, tag: str) -> None:
        """Track table nesting and close cells and rows of the report table.

        :param tag: str; lower-case tag name.
        return None: None; no value is returned.
        """
        if self._finished or not self._depth:
            return
        if tag == "table":
            self._depth -= 1
            self._finished = self._depth == 0
        elif self._depth == 1 and tag in ("td", "th"):
            self._cell = None
        elif self._depth == 1 and tag in ("div", "p") and self._cell is not None:
            self._cell["text"].append(" ")
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            self._store_row(self._row, self._row_css)
            self._row = self._cell = None

    def handle_data(self, data: str) -> None:
        """Add text to the open cell of the report table.

        :param data: str; text between tags.
        return None: None; no value is returned.
        """
        if self._depth == 1 and self._cell is not None and not self._finished:
            self._cell["text"].append(data)
            if sum(len(part) for part in self._cell["text"]) > MAX_CELL_CHARS:
                raise ValueError("a cell of the report table is too long")

    def _store_row(self, cells: list[dict[str, Any]], css: str) -> None:
        """Classify one finished row as a header row, a body row, or neither, and store it.

        A row with a `th` element is a header row. A row whose first cell has the
        class `pl` is a body row; its value cells are the cells with the class
        `nump`, `num`, or `text`. Every other row, such as a spacer or the footnote
        row, is dropped.

        :param cells: list[dict[str, Any]]; collected cells with `tag`, `css`, `colspan`, `text`, and `concept`.
        :param css: str; class of the row.
        return None: None; no value is returned.
        """
        if any(cell["tag"] == "th" for cell in cells):
            self.header_rows.append(tuple(HeaderCell(css=cell["css"], colspan=cell["colspan"],
                                                     text=_clean("".join(cell["text"]))) for cell in cells))
            return
        if not cells or "pl" not in cells[0]["css"].split():
            return
        if len(self.rows) >= REPORT_MAX_ITEMS:
            raise ValueError(f"the report table has more than {REPORT_MAX_ITEMS} rows")
        first = cells[0]
        values = tuple(_clean("".join(cell["text"])) for cell in cells[1:]
                       if _VALUE_CLASSES & set(cell["css"].split()))
        self.rows.append(ReportRow(css=css, label=_clean("".join(first["text"])), concept=first["concept"],
                                   custom="custom" in first["css"].split(), values=values))


def parse_report_page(text: str) -> ReportTable:
    """Read the statement table of one SEC report page and return its content.

    Input (HTML): one `table` with the class `report`. Its header holds a title
    cell (`th` with the class `tl`) and column heading cells (`th` with the class
    `th`). Each body row starts with a label cell (class `pl`, or `pl custom` for a
    company-specific row) whose link names the concept in its `onclick` text, and
    continues with one value cell per column (class `nump`, `num`, or `text`).
    Footnote-mark cells, the footnote row, nested tables, and the definition
    tables after the report table are ignored. Raises ValueError when the page has
    no report table or when the table exceeds a size limit.

    :param text: str; decoded page content.
    return table: ReportTable; title, header rows, and body rows.
    """
    parser = _ReportTableParser()
    parser.feed(text)
    parser.close()
    if not parser.found:
        raise ValueError("the page has no report table")
    title = next((cell.text for row in parser.header_rows for cell in row if "tl" in cell.css.split()), "")
    return ReportTable(title=title, header_rows=tuple(parser.header_rows), rows=tuple(parser.rows))


def _parse_heading_date(text: str) -> date | None:
    """Find a date such as `Jun. 27, 2026` in a column heading and return it, or None.

    :param text: str; heading text.
    return day: date | None; parsed date, or None when the text holds no valid date.
    """
    match = _DATE.search(text)
    if match is None or match.group(1) not in _MONTHS:
        return None
    try:
        return date(int(match.group(3)), _MONTHS[match.group(1)], int(match.group(2)))
    except ValueError:
        return None


def _read_columns(table: ReportTable) -> list[tuple[date, str | None, str | None]]:
    """Read the date columns of a report table and return them in column order.

    A heading cell with a date is a date column. Heading cells without a date in
    the rows above are period labels such as `3 Months Ended`; each label covers as
    many date columns as its `colspan` says. When the labels do not cover the date
    columns exactly, no column gets a label.

    :param table: ReportTable; table content.
    return columns: list[tuple[date, str | None, str | None]]; (date, period label, currency) per column.
    """
    dates: list[tuple[date, str | None]] = []
    labels: list[str] = []
    for row in table.header_rows:
        for cell in row:
            if "th" not in cell.css.split():
                continue
            day = _parse_heading_date(cell.text)
            if day is None:
                labels.extend([cell.text] * cell.colspan)
                continue
            currency = _COLUMN_CURRENCY.search(_DATE.sub("", cell.text))
            dates.append((day, currency.group(1) if currency else None))
    if len(labels) != len(dates):
        labels = []
    return [(day, labels[index] if labels else None, currency) for index, (day, currency) in enumerate(dates)]


def _split_title(cell_title: str, page_title: str) -> tuple[str | None, str | None]:
    """Split the unit note off a table title and return the currency and the unit text.

    The title cell starts with the statement name and continues with a note such
    as `- USD ($) shares in Thousands, $ in Millions`, or in the older form
    `(USD $) In Millions, except Share data in Thousands`. A three-letter code at
    the start of the note is the currency; the rest is the unit text.

    :param cell_title: str; text of the title cell.
    :param page_title: str; statement name from the filing summary.
    return parts: tuple[str | None, str | None]; (currency, unit text); each is None when absent.
    """
    if cell_title.lower().startswith(page_title.lower()):
        note = cell_title[len(page_title):]
    else:
        note = cell_title.rpartition(" - ")[2] if " - " in cell_title else ""
    note = note.strip().lstrip("-").strip()
    match = _CURRENCY_PREFIX.match(note)
    if match:
        return match.group(1), note[match.end():].strip() or None
    return None, note or None


def _build_item(ordinal: int, row: ReportRow, value: str | None, member: str | None) -> StatementItem:
    """Build one statement item from a table row and one of its value cells and return it.

    The cell text is kept as `value_raw`. For the decimal, currency signs,
    footnote marks such as `[1]`, and spaces are removed; parentheses mean a
    negative number. An empty or absent cell is a missing value, never zero.

    :param ordinal: int; zero-based position of the row.
    :param row: ReportRow; table row.
    :param value: str | None; text of the value cell, or None when the row has no cell for the column.
    :param member: str | None; breakdown heading above the row, or None.
    return item: StatementItem; line item with `member` and `custom` in its metadata.
    """
    metadata: JsonValue = {"member": member, "custom": row.custom}
    if not value:
        return StatementItem(ordinal=ordinal, item_field=row.concept, item_title=row.label, value_raw=None,
                             value_kind="missing", value_decimal=None, source_metadata=metadata)
    cleaned = _FOOTNOTE_MARK.sub("", value)
    for sign in ("$", "€", "£", "¥", " "):
        cleaned = cleaned.replace(sign, "")
    kind, _raw, decimal, _ambiguous = parse_decimal_text(cleaned)
    return StatementItem(ordinal=ordinal, item_field=row.concept, item_title=row.label, value_raw=value,
                         value_kind=kind, value_decimal=decimal, source_metadata=metadata)


def build_page_statements(table: ReportTable, kind: StatementKind, filing: SecFiling,
                          page: ReportPage) -> list[StatementRecord]:
    """Build one statement record per date column of a report table and return the records.

    Each record keeps the displayed values and the unit note of the page; nothing
    is multiplied. A balance sheet is an `instant`. For a period heading `N Months
    Ended`, 12 months is `annual`, 3 months is `quarter`, and any other number is
    `year_to_date`; without such a heading the basis is `source_reported`. A
    column is `current` when its date equals the latest column date of the page,
    otherwise `comparative`. A row with the class `rh` is the heading of a
    breakdown; the rows after it carry that heading as `member`.

    Raises ValueError when the table has no date column, or when a body row other
    than a breakdown heading has a number of value cells that differs from the
    number of date columns. The reason names the row.

    :param table: ReportTable; content from parse_report_page.
    :param kind: StatementKind; `income`, `balance`, or `cash_flow`.
    :param filing: SecFiling; report that contains the page.
    :param page: ReportPage; summary entry of the page; its `html_file` must not be None.
    return records: list[StatementRecord]; one record per date column, in column order.
    """
    columns = _read_columns(table)
    if not columns:
        raise ValueError("the report table has no date column")
    for row in table.rows:
        if row.css != "rh" and len(row.values) != len(columns):
            raise ValueError(f"the row {row.label!r} has {len(row.values)} value cells for {len(columns)} date columns")
    title_currency, unit = _split_title(table.title, page.title)
    latest = max(day for day, _label, _currency in columns)
    reference = filing.reference
    records: list[StatementRecord] = []
    for index, (day, label, currency) in enumerate(columns):
        months_match = _MONTHS_ENDED.search(label or "")
        months = int(months_match.group(1)) if months_match else None
        if kind == "balance":
            basis = "instant"
        elif months is None:
            basis = "source_reported"
        else:
            basis = {12: "annual", 3: "quarter"}.get(months, "year_to_date")
        items: list[StatementItem] = []
        member: str | None = None
        for ordinal, row in enumerate(table.rows):
            if row.css == "rh":
                member = row.label
                items.append(_build_item(ordinal, row, None, None))
                continue
            items.append(_build_item(ordinal, row, row.values[index], member))
        locator = f"{page.html_file}#c{index + 1}"
        metadata: JsonValue = {"form": filing.form, "accession": filing.accession, "page": page.html_file,
                               "title": page.title, "period_label": label, "months": months,
                               "column_role": "current" if day == latest else "comparative"}
        records.append(StatementRecord(
            report_date=day, kind=kind, scope="consolidated" if "consolidated" in page.title.lower() else "unknown",
            source_scope=page.title, source_key=f"{reference.uri}#{locator}", format="html", period_basis=basis,
            publication_date=reference.publication_date,
            publication_date_raw=reference.publication_date.isoformat() if reference.publication_date else None,
            source_update_at=None, source_update_raw=None, currency=currency or title_currency, unit=unit,
            items=tuple(items), source_metadata=metadata, locators=(locator,)))
    return records


def read_sec_filing(http: HttpClient, filing: SecFiling) -> FilingResult:
    """Read the three financial statements of one periodic report and return the document outcome.

    The function fetches `FilingSummary.xml` from the filing folder, chooses one
    page per statement kind by title, and fetches and parses each chosen page: at
    most four requests. The stored reference carries the exact period end: the
    latest balance-sheet column date, or else the latest column date.

    Outcomes:
      - all three kinds read: state `succeeded`, status `parsed`;
      - some kinds read: state `partial`, status `partial`, with the error
        `statement_page_not_found` for a kind without a page, and
        `ambiguous_statement_table` or the fetch error for a page that failed;
      - no kind read because of errors: state `failed`, status `unparsed`;
      - the report has only old-format XML pages: state `succeeded`, status
        `unparsed`, warning `unsupported_report_format`;
      - the report lists no statement page: state `succeeded`, status `unparsed`,
        warning `statement_pages_absent`;
      - the summary cannot be fetched: state `failed`, status `fetch_failed`;
      - the summary cannot be parsed: state `failed`, status `unparsed`, error `schema_changed`.

    Output metadata: `form: str`, `accession: str`, `statement_titles: list[str]`
    (every statement title of the summary), and `pages: list[dict]` with `kind`,
    `file`, and `title` for each page that was read.

    :param http: HttpClient; bounded client that carries the SEC contact line.
    :param filing: SecFiling; report to read.
    return result: FilingResult; statements and issues; the reference URI equals the filing folder URL.
    """
    reference = filing.reference
    symbol = reference.symbol
    requests: list[RequestMeta] = []
    issues: list[Issue] = []
    metadata: dict[str, JsonValue] = {"form": filing.form, "accession": filing.accession, "statement_titles": [],
                                      "pages": []}

    def finish(state: str, status: str, statements: Sequence[StatementRecord] = (), fmt: str | None = "html",
               body_hash: str | None = None, exact: FilingRef | None = None) -> FilingResult:
        """Assemble the document outcome and return it.

        :param state: str; check state.
        :param status: str; extraction status.
        :param statements: Sequence[StatementRecord]; extracted statements.
        :param fmt: str | None; document format, or None when nothing was fetched.
        :param body_hash: str | None; SHA-256 of the summary bytes.
        :param exact: FilingRef | None; reference with the exact period end, or None to keep the planned one.
        return result: FilingResult; document outcome.
        """
        days = [record.report_date for record in statements]
        coverage = Coverage(requested=None, observed_start=min(days) if days else None,
                            observed_end=max(days) if days else None, row_count=len(statements),
                            traversal=state)  # type: ignore[arg-type]
        return FilingResult(state=state, coverage=coverage, requests=tuple(requests),  # type: ignore[arg-type]
                            issues=tuple(issues), metadata=metadata, reference=exact or reference,
                            format=fmt, body_sha256=body_hash, status=status,  # type: ignore[arg-type]
                            statements=tuple(statements))

    def note(code: str, message: str, severity: str = "error", url: str | None = None) -> None:
        """Add one issue about this report.

        :param code: str; issue code.
        :param message: str; explanation.
        :param severity: str; `warning` or `error`.
        :param url: str | None; affected page URL; defaults to the filing folder.
        return None: None; no value is returned.
        """
        issues.append(make_issue(code, message, "filing", severity=severity, symbol=symbol,  # type: ignore[arg-type]
                                 provider="sec", url=url or reference.uri, report_date=reference.report_date))

    summary_url = reference.uri + "FilingSummary.xml"
    try:
        response = http.get(summary_url, max_bytes=API_MAX_BYTES)
    except SourceError as exc:
        requests.extend(exc.requests)
        issues.append(relabel_issue(exc.issue, "filing", symbol, "sec"))
        return finish("failed", "fetch_failed", fmt=None)
    requests.extend(response.requests)
    body_hash = compute_sha256(response.body)
    try:
        pages = [page for page in parse_filing_summary(response.body) if page.is_statement]
    except ValueError as exc:
        note("schema_changed", str(exc), url=summary_url)
        return finish("failed", "unparsed", body_hash=body_hash)
    metadata["statement_titles"] = [page.title for page in pages]
    if not pages:
        note("statement_pages_absent", f"the {filing.form} report lists no financial statement page", "warning")
        return finish("succeeded", "unparsed", body_hash=body_hash)
    if all(page.html_file is None for page in pages):
        note("unsupported_report_format", f"the {filing.form} report filed on {reference.publication_date} has"
             " statement pages in the old XML format, which is not read; its values are in the reported values",
             "warning")
        return finish("succeeded", "unparsed", body_hash=body_hash)
    chosen = select_statement_pages(pages)
    statements: list[StatementRecord] = []
    read: list[JsonValue] = []
    for kind in STATEMENT_KINDS:
        page = chosen.get(kind)
        if page is None or page.html_file is None:
            note("statement_page_not_found", f"the report has no readable page for the {kind} statement")
            continue
        page_url = reference.uri + page.html_file
        try:
            answer = http.get(page_url, max_bytes=HTML_MAX_BYTES)
        except SourceError as exc:
            requests.extend(exc.requests)
            issues.append(relabel_issue(exc.issue, "filing", symbol, "sec"))
            continue
        requests.extend(answer.requests)
        try:
            table = parse_report_page(answer.body.decode("utf-8", errors="replace"))
            records = build_page_statements(table, kind, filing, page)
        except ValueError as exc:
            note("ambiguous_statement_table", f"{page.html_file}: {exc}", url=page_url)
            continue
        statements.extend(records)
        read.append({"kind": kind, "file": page.html_file, "title": page.title})
    metadata["pages"] = read
    exact = reference
    if statements:
        balance_days = [record.report_date for record in statements if record.kind == "balance"]
        exact = replace(reference, report_date=max(balance_days or [r.report_date for r in statements]))
    if len(read) == len(STATEMENT_KINDS) and not issues:
        return finish("succeeded", "parsed", statements, body_hash=body_hash, exact=exact)
    if statements:
        return finish("partial", "partial", statements, body_hash=body_hash, exact=exact)
    return finish("failed", "unparsed", body_hash=body_hash)
