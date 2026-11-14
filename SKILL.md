---
name: stock-info-collector
description: Collect and query historical data for one A-share symbol (Shanghai, Shenzhen, Beijing) or one US-listed symbol — daily unadjusted and forward-adjusted prices, financial statements, periodic filings, and for US companies every value reported to the SEC with its filing date — stored in a local SQLite database. Use when an agent analyzes a listed company or prepares data for a backtest and needs saved, source-labelled history with explicit coverage gaps.
---

# Stock information collector

<a id="table-of-contents"></a>
## Table of Contents

- [When to Use This Skill](#when-to-use-this-skill)
- [How to Run the Commands](#how-to-run-the-commands)
- [Collect Data](#collect-data)
- [Show Stored Data](#show-stored-data)
- [Read the Result Correctly](#read-the-result-correctly)
- [Read US Data Correctly](#read-us-data-correctly)
- [Private Settings](#private-settings)
- [Exit Codes](#exit-codes)

## When to Use This Skill

Use this skill when you need saved historical prices or financial information for one A-share symbol or one US-listed symbol. The skill reads public web sources without an API key, stores what it observed in `data/market-data.sqlite3`, and reports what it could not collect. It does not give investment advice, run backtests, or perform OCR on scanned filings.

| Market | Prices | Financial information |
|--------|--------|-----------------------|
| A-shares | Eastmoney; Tencent as a separate fallback | Sina statements; parent-company statements from filings |
| US shares | Eastmoney; Tencent as a fallback for unadjusted prices only | From the US regulator (SEC): every reported value with its filing date, and the statements as each annual and quarterly report presented them |

[↑ Back to Table of Contents](#table-of-contents)

## How to Run the Commands

Run every command from this skill's root directory. On Windows, use the project virtual environment:

```powershell
.\.venv\Scripts\python -B scripts\market_data.py <command> SYMBOL --json [options]
```

- An A-share symbol may be written as `600519`, `sh600519`, `600519.SH`, or `SH:600519`.
- A US symbol may be written as `AAPL`, `US:AAPL`, or `AAPL.US`. Write a share class as `BRK.B` or `BRK-B`.

The `--json` flag is required. The command writes one JSON object to standard output and log lines to standard error.

[↑ Back to Table of Contents](#table-of-contents)

## Collect Data

```powershell
.\.venv\Scripts\python -B scripts\market_data.py collect 600519 --json --start 2020-01-01 --end 2025-12-31
.\.venv\Scripts\python -B scripts\market_data.py collect AAPL --json --start 2020-01-01
```

- `--start` and `--end` are inclusive ISO dates. Without them the collector requests everything from 1970-01-01 to today in the symbol's market.
- The collector contacts each source at most once per calendar day of the symbol's market: the China day for an A-share, the New York day for a US symbol. A second collect on the same day reuses stored data and makes no requests.
- `--refresh` bypasses today's checks and fetches everything again. Use it only when you have a reason, such as a known dividend adjustment or a repaired settings file.
- `--max-requests N` limits HTTP attempts (1 to 512, default 256). A full-history collect can take several minutes.
- `--filing URI` adds up to five extra filings for an A-share: an HTTPS link on an allowed Sina or exchange host, or a file path inside this skill's directory. It is not available for US symbols.

For a US symbol:

- SEC data needs the owner's contact details in the private settings file; see [Private Settings](#private-settings). Without them the prices are still collected, and the result is `partial` with the issue `sec_contact_missing`.
- Each report needs four requests. When the issues contain `work_limit`, some reports are still unread: run the same command again to continue, or raise `--max-requests`.
- A fund such as `SPY` has prices only; the issue `sec_values_unavailable` or `sec_company_not_found` says so.

[↑ Back to Table of Contents](#table-of-contents)

## Show Stored Data

`show` only reads the database. It never contacts the network.

```powershell
# Daily prices (raw by default; --adjustment forward for forward-adjusted)
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset prices --start 2025-01-01
# A-share: parent-company balance sheets from Sina and filings
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset statements --scope parent --kind balance
# US: income statements as presented in the reports
.\.venv\Scripts\python -B scripts\market_data.py show AAPL --json --dataset statements --kind income --start 2025-01-01
# US: list the concepts that the company reported to the SEC
.\.venv\Scripts\python -B scripts\market_data.py show AAPL --json --dataset facts
# US: values of chosen concepts that were public on or before a date
.\.venv\Scripts\python -B scripts\market_data.py show AAPL --json --dataset facts --concept NetIncomeLoss --concept Assets --filed-until 2024-12-31
# Filing documents and what was extracted from them
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset filings
# Every stored check with its coverage and issues
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset coverage
```

Selectors that do not apply to a dataset are rejected:

| Dataset | Accepted selectors |
|---------|--------------------|
| `prices` | `--provider auto\|eastmoney\|tencent`, `--adjustment`, `--versions` |
| `statements` | `--provider auto\|sina\|filing\|sec`, `--scope`, `--kind`, `--versions` |
| `filings` | `--provider auto\|filing\|sec`, `--versions` |
| `facts` | `--provider auto\|sec`, `--concept` (repeatable), `--filed-until`, `--versions` |
| `coverage` | `--provider` |

Results are paged with `--limit` (1 to 2,000, default 200) and `--offset`. When `pagination.next_offset` is not null, request the next page with that offset.

[↑ Back to Table of Contents](#table-of-contents)

## Read the Result Correctly

- **Empty is not complete.** An empty record list, or a missing date, never proves that no trading or reporting happened. Every group states `market_completeness: "unverified"`.
- **Check `status`, `freshness`, and `issues` before using numbers.** `partial`, `stale`, or an `error` issue means some data is missing or old.
- **Numbers are decimal strings.** Prices, volumes, and statement values keep the source text and units. Sina statements have `unit: null` because Sina does not state units; never assume yuan.
- **Missing values stay missing.** A statement item with `value_kind: "missing"` or `value_decimal: null` was not reported or was not a clear number. Do not treat it as zero.
- **Scopes are separate.** `consolidated` and `parent` statements are never substituted for each other. A `missing_parent_statements` issue lists A-share periods without parent-company figures.
- **Forward-adjusted prices come from one snapshot.** Different snapshots are never mixed. The price row of the current market day has `provisional: true`.
- **Source reuse conditions.** `source_conditions_status` is `sec_free_to_access_and_reuse` when every group in the answer comes from the SEC, which states that its filing content is free to access and reuse. It is `unresolved` in every other case: use that data for local analysis only and do not redistribute it.

[↑ Back to Table of Contents](#table-of-contents)

## Read US Data Correctly

- **Reported values (`--dataset facts`).** Without `--concept`, each record describes one concept: its name, label, unit, and how many values are stored. With `--concept`, each record is one value with `period_start`, `period_end`, `value`, `unit`, `form`, `accession`, and `filed`. The same period appears once for every filing that reported it, for example again one year later as a comparison figure.
- **Use filing dates in a backtest.** A value became public on its `filed` date, not on its `period_end`. Pass `--filed-until DATE` to see only what was public on that date.
- **Limits of the reported values.** They start in 2009, they hold standard concepts only, and company-specific items and breakdowns by segment are absent. A foreign company reports once a year.
- **Statements (`--dataset statements`).** Each record is one column of one statement page of one report. `value_decimal` is the number as displayed, and `unit` is the page's note, such as `shares in Thousands, $ in Millions`. Rows for amounts, share counts, and per-share figures follow different parts of that note, so never apply one multiplier to every row.
- **Columns.** `source_metadata.column_role` is `current` for the report's own period and `comparative` for an earlier period shown beside it. `period_basis` is `annual`, `quarter`, `year_to_date`, or `instant`. `publication_date` is the filing date. `item_field` names the concept of a row, which links it to the reported values; `source_metadata.member` names the breakdown that a row belongs to.
- **Old and unusual reports.** A report from before about late 2011 is stored as `unparsed` with the warning `unsupported_report_format`; its figures are still in the reported values. Only the first page of each statement kind is read.
- **Tickers.** Earlier tickers of a renamed company are not traced.

[↑ Back to Table of Contents](#table-of-contents)

## Private Settings

The file `config.toml` in the skill's root directory holds the owner's name and e-mail address for the US regulator (SEC). The collector sends them to SEC servers only, and only for a US symbol. A-share commands never read the file.

- **Never open, print, copy, or commit `config.toml`.** To learn its state, run the `config` command, which shows no details:

  ```powershell
  .\.venv\Scripts\python -B scripts\market_data.py config --json
  ```

- `settings.sec_contact` is `configured`, `missing`, `invalid`, or `blocked`. `settings.git_state` must be `ignored` or `no_repository`.
- When an issue appears, show its `message` to the user. The message explains the repair. Do not write contact details into any file yourself unless the user gives them to you for that purpose, and then write them only into `config.toml`.

[↑ Back to Table of Contents](#table-of-contents)

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Collect finished with fresh, complete-as-attempted data, show ran its query, or config found no error |
| 3 | Collect finished partially: some source failed, data is stale, or work is pending |
| 2 | Invalid input, such as a bad symbol, date, or selector; or an unusable or unprotected settings file |
| 1 | Storage or internal error, or show was run before any database exists |

[↑ Back to Table of Contents](#table-of-contents)
