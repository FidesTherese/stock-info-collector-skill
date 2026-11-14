"""Command-line entry point for collecting and showing stored A-share market data.

Usage (from the project root):
    python -B scripts/market_data.py collect SYMBOL --json [--start DATE] [--end DATE] [--refresh]
        [--filing URI ...] [--db PATH] [--max-requests N]
    python -B scripts/market_data.py show SYMBOL --json --dataset prices|statements|filings|coverage [...]
    python -B scripts/market_data.py config --json

The command writes one JSON envelope to standard output and log lines to
standard error. Exit codes: 0 satisfied collect, executed show, or usable
settings; 3 partial collect; 2 invalid input or unusable settings; 1 fatal
storage or internal error.

The `config` command reports the state of the private settings file
`config.toml` without showing the contact details in it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_info.collector import build_envelope, build_error_envelope, collect, show  # noqa: E402
from stock_info.filings import _LocalFileError, resolve_local_path  # noqa: E402
from stock_info.providers import (  # noqa: E402
    FILING_HOSTS, CollectRequest, DateRange, FilingRef, HttpClient, InputError, Issue, QueryRequest, SourceError,
    StorageError, Symbol, compute_china_day, make_issue, normalize_source_url, validate_remote_url,
)
from stock_info.securities import parse_symbol  # noqa: E402
from stock_info.settings import load_settings, summarize_settings  # noqa: E402
from stock_info.storage import Store  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = "data/market-data.sqlite3"
FLOOR_DATE = date(1970, 1, 1)
EXIT_OK, EXIT_FATAL, EXIT_INPUT, EXIT_PARTIAL = 0, 1, 2, 3
DATASET_SELECTORS: dict[str, dict[str, Any]] = {
    "prices": {"providers": ("auto", "eastmoney", "tencent"), "allowed": {"adjustment", "versions"}},
    "statements": {"providers": ("auto", "sina", "filing"), "allowed": {"scope", "kind", "versions"}},
    "filings": {"providers": ("auto", "filing"), "allowed": {"versions"}},
    "coverage": {"providers": ("auto", "eastmoney", "tencent", "sina", "filing", "bse"), "allowed": set()},
}


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the collect, show, and config commands and return it.

    Optional selectors default to None so that validation can reject a selector
    that was given explicitly but does not apply to the chosen dataset. The config
    command takes no symbol.

    return parser: argparse.ArgumentParser; configured parser.
    """
    parser = argparse.ArgumentParser(prog="market_data.py", description="Collect and show A-share market data.")
    commands = parser.add_subparsers(dest="command", required=True)
    config = commands.add_parser("config", help="report the state of the private settings file")
    config.add_argument("--json", action="store_true", help="required; write a JSON object")
    for name in ("collect", "show"):
        sub = commands.add_parser(name)
        sub.add_argument("symbol", metavar="SYMBOL")
        sub.add_argument("--json", action="store_true", help="required; write a JSON envelope")
        sub.add_argument("--start", default=None, help="inclusive ISO start date, default 1970-01-01")
        sub.add_argument("--end", default=None, help="inclusive ISO end date, default today in China")
        sub.add_argument("--db", default=None, help=f"root-relative database path, default {DEFAULT_DB}")
        if name == "collect":
            sub.add_argument("--refresh", action="store_true", help="bypass today's checks and recheck everything")
            sub.add_argument("--filing", action="append", default=None, metavar="URI",
                             help="explicit HTTPS or local filing, up to five")
            sub.add_argument("--max-requests", type=int, default=None, help="HTTP attempt budget, 1-512")
        else:
            sub.add_argument("--dataset", required=True, choices=sorted(DATASET_SELECTORS))
            sub.add_argument("--provider", default=None)
            sub.add_argument("--adjustment", default=None, choices=("raw", "forward"))
            sub.add_argument("--scope", default=None, choices=("all", "consolidated", "parent", "unknown"))
            sub.add_argument("--kind", default=None, choices=("all", "income", "balance", "cash_flow"))
            sub.add_argument("--versions", default=None, choices=("latest", "all"))
            sub.add_argument("--limit", type=int, default=None)
            sub.add_argument("--offset", type=int, default=None)
    return parser


def raise_input_error(code: str, message: str) -> None:
    """Raise an InputError with the given code and message.

    :param code: str; issue code such as `invalid_range`.
    :param message: str; explanation.
    return None: None; never returns.
    """
    raise InputError(make_issue(code, message, "prices"))


def parse_range(start: str | None, end: str | None, today: date) -> DateRange:
    """Validate the inclusive ISO date range and return it.

    The start defaults to 1970-01-01 and the end to today in China. Invalid,
    reversed, and future dates raise InputError `invalid_range`. The floor is not a
    listing date.

    :param start: str | None; ISO start date.
    :param end: str | None; ISO end date.
    :param today: date; China calendar day.
    return span: DateRange; validated range.
    """
    try:
        first = date.fromisoformat(start) if start else FLOOR_DATE
        last = date.fromisoformat(end) if end else today
    except ValueError:
        raise InputError(make_issue("invalid_range", "dates must use the ISO form YYYY-MM-DD", "prices")) from None
    if first > last:
        raise_input_error("invalid_range", "the start date is after the end date")
    if last > today:
        raise_input_error("invalid_range", "the end date is in the future")
    return DateRange(first, last)


def resolve_db_path(value: str | None) -> Path:
    """Resolve the database path relative to the project root and return it.

    Paths that escape the root raise InputError `invalid_path`.

    :param value: str | None; root-relative path, or None for the default.
    return path: Path; absolute database path under the root.
    """
    candidate = Path(value or DEFAULT_DB)
    if candidate.is_absolute() or str(value or "").startswith(("\\\\", "//")):
        raise_input_error("invalid_path", "--db must be relative to the project root")
    resolved = (ROOT / candidate).resolve()
    try:
        resolved.relative_to(ROOT.resolve())
    except ValueError:
        raise_input_error("invalid_path", "--db must stay inside the project root")
    return resolved


def build_filing_refs(values: list[str] | None, symbol: Symbol) -> tuple[FilingRef, ...]:
    """Validate explicit filing URIs and return them as references.

    At most five values are allowed. HTTPS values must name an allowed Sina or
    exchange filing host; other values must be local files under the project root.

    :param values: list[str] | None; explicit URIs or paths.
    :param symbol: Symbol; requested symbol.
    return references: tuple[FilingRef, ...]; explicit references.
    """
    refs = []
    values = values or []
    if len(values) > 5:
        raise_input_error("invalid_path", "at most five --filing values are allowed")
    for value in values:
        if value.lower().startswith(("https://", "http://")):
            url = normalize_source_url(value)
            reason = validate_remote_url(url)
            if reason is not None:
                raise_input_error("invalid_path", f"--filing {value!r}: {reason}")
            if (urlsplit(url).hostname or "") not in FILING_HOSTS:
                raise_input_error("invalid_path", f"--filing {value!r}: URL host is not an allowed filing host")
        else:
            try:
                path = resolve_local_path(value, ROOT)
            except _LocalFileError as exc:
                raise InputError(make_issue("invalid_path", f"--filing {value!r}: {exc}", "prices")) from None
            if not path.is_file():
                raise_input_error("invalid_path", f"--filing {value!r} is not a file")
            url = path.relative_to(ROOT.resolve()).as_posix()
        refs.append(FilingRef(symbol=symbol, uri=url, report_date=None, publication_date=None, origin="explicit"))
    return tuple(refs)


def build_query(args: argparse.Namespace, symbol: Symbol, span: DateRange) -> QueryRequest:
    """Validate show selectors against the dataset and return the query.

    Selectors that do not apply to the dataset are rejected even when they repeat a
    default value. The limit must be 1 to 2,000 and the offset nonnegative.

    :param args: argparse.Namespace; parsed show arguments.
    :param symbol: Symbol; requested symbol.
    :param span: DateRange; validated range.
    return query: QueryRequest; validated query.
    """
    rules = DATASET_SELECTORS[args.dataset]
    for name in ("adjustment", "scope", "kind", "versions"):
        if getattr(args, name) is not None and name not in rules["allowed"]:
            raise_input_error("invalid_selector", f"--{name} does not apply to --dataset {args.dataset}")
    provider = args.provider or "auto"
    if provider not in rules["providers"]:
        raise_input_error("invalid_selector", f"--provider {provider} does not apply to --dataset {args.dataset}")
    limit = 200 if args.limit is None else args.limit
    offset = 0 if args.offset is None else args.offset
    if not 1 <= limit <= 2000:
        raise_input_error("invalid_selector", "--limit must be between 1 and 2000")
    if offset < 0:
        raise_input_error("invalid_selector", "--offset must be nonnegative")
    return QueryRequest(symbol=symbol, span=span, dataset=args.dataset, provider=provider,
                        adjustment=args.adjustment or "raw", scope=args.scope or "all", kind=args.kind or "all",
                        versions=args.versions or "latest", limit=limit, offset=offset)


def write_json(envelope: Any) -> None:
    """Write one JSON envelope as UTF-8 to standard output.

    :param envelope: Any; JSON-ready envelope.
    return None: None; no value is returned.
    """
    data = json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    stream = getattr(sys.stdout, "buffer", None)
    if stream is not None:
        stream.write(data)
        stream.flush()
    else:
        sys.stdout.write(data.decode("utf-8"))


def configure_logging() -> logging.Handler:
    """Attach a plain-message standard-error handler to the `stock_info` logger and return it.

    return handler: logging.Handler; handler to remove after the command.
    """
    logger = logging.getLogger("stock_info")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    return handler


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run collect, show, or config, write the JSON envelope, and return the exit code.

    Argument syntax errors and `--help` use argparse's standard text on standard
    error (exit 2 for errors). Validation errors, storage errors, and unexpected
    errors still produce a JSON envelope. Resources are closed in every case.

    The config command writes the object described by summarize_settings and
    returns 2 when its status is `error`. The collect command gives the HTTP client
    a function that loads the settings; the client calls it only before its first
    request to an SEC host, so other runs never read the settings file.

    :param argv: Sequence[str] | None; arguments without the program name; None reads sys.argv.
    return code: int; 0, 1, 2, or 3.
    """
    args = build_parser().parse_args(argv)
    handler = configure_logging()
    store: Store | None = None
    http: HttpClient | None = None
    symbol: Symbol | None = None
    span: DateRange | None = None
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    try:
        if not args.json:
            raise_input_error("invalid_selector", "--json is required")
        if args.command == "config":
            summary = summarize_settings(load_settings(ROOT))
            write_json(summary)
            return EXIT_INPUT if summary["status"] == "error" else EXIT_OK
        symbol = parse_symbol(args.symbol)
        today = compute_china_day(clock())
        span = parse_range(args.start, args.end, today)
        db_path = resolve_db_path(args.db)
        if args.command == "collect":
            max_requests = 256 if args.max_requests is None else args.max_requests
            if not 1 <= max_requests <= 512:
                raise_input_error("invalid_selector", "--max-requests must be between 1 and 512")
            request = CollectRequest(symbol=symbol, span=span, refresh=args.refresh,
                                     filings=build_filing_refs(args.filing, symbol))
            store = Store(db_path)
            http = HttpClient(max_requests=max_requests, sec_access=lambda: load_settings(ROOT).sec_access)
            envelope = collect(store, http, request, clock)
            write_json(envelope)
            return EXIT_OK if envelope["status"] == "ok" else EXIT_PARTIAL
        query = build_query(args, symbol, span)
        store = Store(db_path, readonly=True)
        envelope = show(store, query, clock())
        write_json(envelope)
        return EXIT_OK
    except InputError as exc:
        write_json(build_error_envelope(args.command, exc.issue, symbol, span))
        return EXIT_INPUT
    except StorageError as exc:
        write_json(build_error_envelope(args.command, exc.issue, symbol, span))
        return EXIT_FATAL
    except KeyboardInterrupt:
        issue = make_issue("interrupted_check", "the command was interrupted; saved work is kept", "prices",
                           retryable=True)
        envelope = build_error_envelope(args.command, issue, symbol, span)
        envelope["status"] = "partial"
        write_json(envelope)
        return EXIT_PARTIAL
    except (SourceError, Exception) as exc:  # unexpected errors never report success
        issue: Issue = make_issue("internal_error", f"{type(exc).__name__}: {exc}", "prices")
        write_json(build_error_envelope(args.command, issue, symbol, span))
        return EXIT_FATAL
    finally:
        if http is not None:
            http.close()
        if store is not None:
            store.close()
        logging.getLogger("stock_info").removeHandler(handler)


__all__ = ["main", "build_parser", "build_envelope"]

if __name__ == "__main__":
    sys.exit(main())
