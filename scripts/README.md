# Scripts

<a id="table-of-contents"></a>
## Table of Contents

- [Purpose](#purpose)
- [Modules](#modules)

## Purpose

This directory contains the command-line entry point `market_data.py` and the `stock_info` package that it calls. The parent [README](../README.md) explains setup, and [`SKILL.md`](../SKILL.md) explains the commands.

[↑ Back to Table of Contents](#table-of-contents)

## Modules

| File | Responsibility |
|------|----------------|
| `market_data.py` | Parses arguments, validates input, opens the database and HTTP client, writes the JSON envelope, and returns the exit code. |
| `stock_info/providers.py` | Defines shared records and errors, the bounded HTTP client, decimal and hash helpers, the HTML block parser, and the Eastmoney, Tencent, and Sina adapters. |
| `stock_info/securities.py` | Parses symbols, reads the official Beijing Stock Exchange code mapping, and resolves aliases. |
| `stock_info/filings.py` | Discovers periodic reports on Sina index pages and extracts statements from HTML and text PDFs. |
| `stock_info/storage.py` | Owns the SQLite schema, daily check gates, revision history, and read-only queries. |
| `stock_info/collector.py` | Plans and runs the source tasks for `collect`, runs `show`, and builds the JSON envelope. |

Imports flow from `market_data.py` to `collector.py`, and from there to the source modules and storage. Every module imports shared types from `providers.py`; no module imports in the reverse direction.

[↑ Back to Table of Contents](#table-of-contents)
