---
name: stock-info-collector
description: Collect and query historical data for one Shanghai, Shenzhen, or Beijing A-share symbol — daily unadjusted and forward-adjusted prices, consolidated and parent-company balance sheets, income statements, and cash-flow statements, and periodic filings — stored in a local SQLite database. Use when an agent analyzes an A-share company or prepares data for a backtest and needs saved, source-labelled history with explicit coverage gaps.
---

# Stock information collector

<a id="table-of-contents"></a>
## Table of Contents

- [When to Use This Skill](#when-to-use-this-skill)
- [How to Run the Commands](#how-to-run-the-commands)
- [Collect Data](#collect-data)
- [Show Stored Data](#show-stored-data)
- [Read the Result Correctly](#read-the-result-correctly)
- [Exit Codes](#exit-codes)

## When to Use This Skill

Use this skill when you need saved historical prices or financial statements for one A-share symbol. The skill reads public web sources without an API key, stores every observed version in `data/market-data.sqlite3`, and reports what it could not collect. It does not give investment advice, run backtests, read US shares, or perform OCR on scanned filings.

[↑ Back to Table of Contents](#table-of-contents)

## How to Run the Commands

Run every command from this skill's root directory. On Windows, use the project virtual environment:

```powershell
.\.venv\Scripts\python -B scripts\market_data.py <command> SYMBOL --json [options]
```

The symbol may be written as `600519`, `sh600519`, `600519.SH`, or `SH:600519`. The `--json` flag is required. The command writes one JSON object to standard output and log lines to standard error.

[↑ Back to Table of Contents](#table-of-contents)

## Collect Data

```powershell
.\.venv\Scripts\python -B scripts\market_data.py collect 600519 --json --start 2020-01-01 --end 2025-12-31
```

- `--start` and `--end` are inclusive ISO dates. Without them the collector requests everything from 1970-01-01 to today in China.
- The collector contacts each source at most once per China calendar day. A second collect on the same day reuses stored data and makes no requests.
- `--refresh` bypasses today's checks and fetches everything again. Use it only when you have a reason, such as a known dividend adjustment.
- `--filing URI` adds up to five extra filings: an HTTPS link on an allowed Sina or exchange host, or a file path inside this skill's directory.
- `--max-requests N` limits HTTP attempts (1 to 512, default 256). A full-history collect can take several minutes because requests are spaced one second apart.

[↑ Back to Table of Contents](#table-of-contents)

## Show Stored Data

`show` only reads the database. It never contacts the network.

```powershell
# Daily prices (raw by default; --adjustment forward for forward-adjusted)
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset prices --start 2025-01-01
# Parent-company balance sheets from Sina and filings
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset statements --scope parent --kind balance
# Filing documents and what was extracted from them
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset filings
# Every stored check with its coverage and issues
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset coverage
```

Selectors that do not apply to a dataset are rejected: prices accept `--provider auto|eastmoney|tencent`, `--adjustment`, and `--versions`; statements accept `--provider auto|sina|filing`, `--scope`, `--kind`, and `--versions`; filings accept `--provider auto|filing` and `--versions`; coverage accepts only `--provider`. Results are paged with `--limit` (1 to 2,000, default 200) and `--offset`. When `pagination.next_offset` is not null, request the next page with that offset.

[↑ Back to Table of Contents](#table-of-contents)

## Read the Result Correctly

- **Empty is not complete.** An empty record list, or a missing date, never proves that no trading or reporting happened. Every group states `market_completeness: "unverified"`.
- **Check `status`, `freshness`, and `issues` before using numbers.** `partial`, `stale`, or an `error` issue means some data is missing or old.
- **Numbers are decimal strings.** Prices, volumes, and statement values keep the source text and units. Sina statements have `unit: null` because Sina does not state units; never assume yuan.
- **Missing values stay missing.** A statement item with `value_kind: "missing"` or `value_decimal: null` was not reported or was not a clear number. Do not treat it as zero.
- **Scopes are separate.** `consolidated` and `parent` statements are never substituted for each other. A `missing_parent_statements` issue lists periods without parent-company figures.
- **Forward-adjusted prices come from one snapshot.** Different snapshots are never mixed. Today's price row has `provisional: true`.
- **Source reuse conditions are unresolved.** `source_conditions_status` is always `unresolved`. Use the data for local analysis only and do not redistribute it.

[↑ Back to Table of Contents](#table-of-contents)

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Collect finished with fresh, complete-as-attempted data, or show ran its query |
| 3 | Collect finished partially: some source failed, data is stale, or work is pending |
| 2 | Invalid input, such as a bad symbol, date, or selector |
| 1 | Storage or internal error, or show was run before any database exists |

[↑ Back to Table of Contents](#table-of-contents)
