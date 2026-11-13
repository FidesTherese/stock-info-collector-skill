"""Symbol parsing, official Beijing Stock Exchange code mappings, and alias resolution.

The module never touches SQLite. It reads the official BSE code-mapping page
through providers.HttpClient and resolves aliases only from verified links.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Sequence

from .providers import (
    MAPPING_MAX_BYTES, CodeLink, Coverage, HttpClient, InputError, Issue, MappingResult, SourceError, Symbol,
    compute_sha256, decode_html, encode_canonical_json, make_issue, parse_html_blocks, relabel_issue,
)

BSE_MAPPING_URL = "https://www.bse.cn/service/code_mapping.html"
MAX_MAPPINGS = 5000
MAX_ALIASES = 8
_PREFIXES: dict[str, tuple[str, ...]] = {
    "SH": ("600", "601", "603", "605", "688", "689"),
    "SZ": ("000", "001", "002", "003", "300", "301"),
    "BJ": ("43", "44", "83", "87", "88", "92"),
}
_FORMS = (
    re.compile(r"(?P<ex>sh|sz|bj)(?P<code>\d{6})", re.IGNORECASE),
    re.compile(r"(?P<code>\d{6})\.(?P<ex>sh|sz|bj)", re.IGNORECASE),
    re.compile(r"(?P<ex>sh|sz|bj):(?P<code>\d{6})", re.IGNORECASE),
    re.compile(r"(?P<code>\d{6})"),
)


def infer_exchange(code: str) -> str | None:
    """Infer the exchange of a six-digit code from its prefix and return it, or None when unknown.

    :param code: str; six-digit code.
    return exchange: str | None; `SH`, `SZ`, `BJ`, or None.
    """
    for exchange, prefixes in _PREFIXES.items():
        if code.startswith(prefixes):
            return exchange
    return None


def parse_symbol(text: str) -> Symbol:
    """Parse a user-supplied symbol and return its canonical Symbol.

    Accepted forms are `sh600519`, `600519.SH`, `SH:600519`, and a bare six-digit
    code. The code prefix must belong to the explicit market when one is given.
    The result does not prove that the security exists. Raises InputError with code
    `invalid_symbol` for any other input.

    :param text: str; user input.
    return symbol: Symbol; canonical identity such as `SH:600519`.
    """
    value = (text or "").strip()
    for pattern in _FORMS:
        match = pattern.fullmatch(value)
        if match is None:
            continue
        code = match.group("code")
        inferred = infer_exchange(code)
        explicit = match.groupdict().get("ex")
        exchange = explicit.upper() if explicit else inferred
        if inferred is None or exchange != inferred:
            break
        return Symbol(symbol_id=f"{exchange}:{code}", exchange=exchange, code=code)  # type: ignore[arg-type]
    raise InputError(make_issue("invalid_symbol", f"{text!r} is not a supported A-share symbol", "prices"))


def build_bj_symbol(code: str) -> Symbol:
    """Build a Beijing Stock Exchange Symbol from a six-digit code and return it.

    :param code: str; six-digit code from the official mapping table.
    return symbol: Symbol; `BJ:` symbol.
    """
    return Symbol(symbol_id=f"BJ:{code}", exchange="BJ", code=code)


def lookup_code_links(http: HttpClient, *, now: datetime | None = None) -> MappingResult:
    """Read the official BSE old-code to new-code table and return the verified links.

    The parser finds the table whose header contains `旧代码` and `新代码`, then binds
    the exact six-digit values in each real row. Footnote rows (see
    is_footnote_row) are skipped. Both codes must be distinct
    six-digit values; an old code may map to only one new code and the reverse. A
    malformed or conflicting table returns state `failed` with no links. The
    `上市日期` column stays labelled text inside `cells`; `effective_date` is None.

    Output metadata: `table_headers: list[str]`, `row_count: int`, `url: str`.

    :param http: HttpClient; bounded client.
    :param now: datetime | None; aware UTC observation time; defaults to now.
    return result: MappingResult; links sorted by old code.
    """
    now = now or datetime.now(timezone.utc)
    empty = Coverage(requested=None, traversal="failed")
    try:
        response = http.get(BSE_MAPPING_URL, max_bytes=MAPPING_MAX_BYTES)
    except SourceError as exc:
        return MappingResult(state="failed", coverage=empty, requests=exc.requests,
                             issues=(relabel_issue(exc.issue, "mapping", None, "bse"),), metadata={})
    try:
        text = decode_html(response.body, response.content_type)
    except ValueError as exc:
        issue = make_issue("unsupported_charset", str(exc), "mapping", provider="bse", url=BSE_MAPPING_URL)
        return MappingResult(state="failed", coverage=empty, requests=response.requests, issues=(issue,), metadata={})
    links, headers, issue = parse_mapping_table(text, response.url, now)
    if issue is not None:
        return MappingResult(state="failed", coverage=empty, requests=response.requests, issues=(issue,),
                             metadata={"table_headers": headers, "row_count": 0, "url": response.url})
    coverage = Coverage(requested=None, row_count=len(links), traversal="succeeded")
    return MappingResult(state="succeeded", coverage=coverage, requests=response.requests, issues=(),
                         metadata={"table_headers": headers, "row_count": len(links), "url": response.url},
                         links=tuple(links))


def parse_mapping_table(text: str, url: str, now: datetime) -> tuple[list[CodeLink], list[str], Issue | None]:
    """Parse the decoded BSE mapping page and return (links, headers, failure issue or None).

    :param text: str; decoded HTML.
    :param url: str; page URL kept as evidence.
    :param now: datetime; aware UTC observation time.
    return parsed: tuple[list[CodeLink], list[str], Issue | None]; links, header cells, and an issue on failure.
    """
    def fail(message: str, headers: list[str]) -> tuple[list[CodeLink], list[str], Issue | None]:
        """Return an empty result with a schema_changed issue.

        :param message: str; failure explanation.
        :param headers: list[str]; header cells found so far.
        return parsed: tuple[list[CodeLink], list[str], Issue | None]; empty links and the issue.
        """
        return [], headers, make_issue("schema_changed", message, "mapping", provider="bse", url=url)

    parser = parse_html_blocks(text)
    table = None
    header_index = -1
    for block in parser.blocks:
        if block.kind != "table":
            continue
        for index, row in enumerate(block.rows):
            normalized = [re.sub(r"\s+", "", cell) for cell in row]
            if "旧代码" in normalized and "新代码" in normalized:
                table, header_index = block, index
                break
        if table is not None:
            break
    if table is None:
        return fail("no table with 旧代码 and 新代码 headers was found", [])
    headers = [re.sub(r"\s+", "", cell) for cell in table.rows[header_index]]
    if not table.simple:
        return fail("the mapping table uses unsupported cell spans", headers)
    old_col, new_col = headers.index("旧代码"), headers.index("新代码")
    rows = [r for r in table.rows[header_index + 1:] if any(cell.strip() for cell in r)]
    notes = [r[0].strip() for r in rows if is_footnote_row(r)]
    rows = [r for r in rows if not is_footnote_row(r)]
    if len(rows) > MAX_MAPPINGS:
        return fail(f"the mapping table has more than {MAX_MAPPINGS} rows", headers)
    if len(notes) > 5:
        return fail("the mapping table has more than five footnote rows", headers)
    forward: dict[str, str] = {}
    backward: dict[str, str] = {}
    links: list[CodeLink] = []
    for row in rows:
        if len(row) != len(headers):
            return fail("a mapping row has a different number of cells than the header", headers)
        old, new = row[old_col].strip(), row[new_col].strip()
        if not (re.fullmatch(r"\d{6}", old) and re.fullmatch(r"\d{6}", new)) or old == new:
            return fail(f"mapping row {old!r} -> {new!r} is not a pair of distinct six-digit codes", headers)
        if forward.get(old, new) != new or backward.get(new, old) != old:
            return fail(f"mapping row {old} -> {new} conflicts with another row", headers)
        if old in forward:
            continue
        forward[old], backward[new] = new, old
        cells = tuple(row)
        evidence_hash = compute_sha256(encode_canonical_json([headers, list(cells)]).encode("utf-8"))
        links.append(CodeLink(from_symbol=build_bj_symbol(old), to_symbol=build_bj_symbol(new), evidence_url=url,
                              headers=tuple(headers), cells=cells, effective_date=None, evidence_hash=evidence_hash,
                              first_seen=now, last_seen=now))
    return sorted(links, key=lambda link: link.from_symbol.code), headers, None


def is_footnote_row(row: list[str]) -> bool:
    """Report whether a mapping-table row is a footnote rather than a mapping and return the decision.

    The live BSE table ends with a row whose first cell starts with `注` (a note
    about listing dates) and whose other cells are empty. Only that exact shape is
    a footnote; any other row must be a valid mapping.

    :param row: list[str]; table row cells.
    return footnote: bool; True for a footnote row.
    """
    return bool(row) and row[0].strip().startswith("注") and all(not cell.strip() for cell in row[1:])


def resolve_symbols(symbol: Symbol, links: Sequence[CodeLink]) -> tuple[Symbol, ...]:
    """Find every code connected to the requested symbol by verified links and return them.

    The connected component is checked for forks (one code linked to two different
    codes in the same direction) and cycles; either raises InputError with code
    `mapping_conflict`. More than eight aliases raises InputError with code
    `work_limit`. Identical pairs with different evidence collapse into one edge.

    :param symbol: Symbol; requested symbol.
    :param links: Sequence[CodeLink]; stored or freshly verified links.
    return symbols: tuple[Symbol, ...]; requested symbol first, then aliases sorted by symbol_id.
    """
    edges = {(link.from_symbol, link.to_symbol) for link in links}
    neighbours: dict[Symbol, set[Symbol]] = defaultdict(set)
    for old, new in edges:
        neighbours[old].add(new)
        neighbours[new].add(old)
    component = {symbol}
    pending = [symbol]
    while pending:
        current = pending.pop()
        for other in neighbours.get(current, ()):
            if other not in component:
                component.add(other)
                pending.append(other)
    inner = [(old, new) for old, new in edges if old in component]
    outgoing: dict[Symbol, set[Symbol]] = defaultdict(set)
    incoming: dict[Symbol, set[Symbol]] = defaultdict(set)
    for old, new in inner:
        outgoing[old].add(new)
        incoming[new].add(old)
    if any(len(v) > 1 for v in outgoing.values()) or any(len(v) > 1 for v in incoming.values()):
        raise InputError(make_issue("mapping_conflict", f"code links around {symbol.symbol_id} fork", "mapping",
                                    symbol=symbol, provider="bse"))
    if len(inner) != len(component) - 1:
        raise InputError(make_issue("mapping_conflict", f"code links around {symbol.symbol_id} form a cycle",
                                    "mapping", symbol=symbol, provider="bse"))
    aliases = sorted(component - {symbol}, key=lambda s: s.symbol_id)
    if len(aliases) > MAX_ALIASES:
        raise InputError(make_issue("work_limit", f"more than {MAX_ALIASES} aliases for {symbol.symbol_id}",
                                    "mapping", symbol=symbol, provider="bse"))
    return (symbol, *aliases)
