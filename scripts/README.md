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
| `market_data.py` | Parses arguments, validates input, opens the database and HTTP client, writes the JSON envelope, and returns the exit code. It also runs the `config` command. |
| `stock_info/providers.py` | Defines shared records and errors, the bounded HTTP client, decimal and hash helpers, market-day helpers, the HTML block parser, and the Eastmoney, Tencent, and Sina adapters. The price adapters serve A-shares and US shares. The HTTP client sends the SEC contact line to SEC hosts only. |
| `stock_info/settings.py` | Reads and validates the private settings file `config.toml`, and asks Git whether the file is tracked or ignored. It never prints the contact details. |
| `stock_info/securities.py` | Parses A-share and US symbols, reads the official Beijing Stock Exchange code mapping, and resolves aliases. |
| `stock_info/sec.py` | Reads the SEC sources for a US company: the ticker list, the reported values, and the statement pages of each periodic report. |
| `stock_info/filings.py` | Discovers periodic reports on Sina index pages and extracts statements from HTML and text PDFs. |
| `stock_info/storage.py` | Owns the SQLite schema (version 2 adds the reported values), daily check gates, revision history, and read-only queries. |
| `stock_info/collector.py` | Plans and runs the source tasks for `collect` (one task order for A-shares, one for US shares), runs `show`, and builds the JSON envelope. |

Imports flow from `market_data.py` to `collector.py`, and from there to the source modules and storage. Every module imports shared types from `providers.py`; no module imports in the reverse direction. `market_data.py` gives the HTTP client a function that loads the settings, so `providers.py` does not import `settings.py`.

[↑ Back to Table of Contents](#table-of-contents)
