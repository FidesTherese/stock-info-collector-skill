# Stock information collector

<a id="table-of-contents"></a>
## Table of Contents

- [What This Project Does](#what-this-project-does)
- [Set Up](#set-up)
- [Settings](#settings)
- [Run the Collector](#run-the-collector)
- [Run the Tests](#run-the-tests)
- [Where Things Are](#where-things-are)
- [Known Limits](#known-limits)
- [License](#license)

## What This Project Does

This project is an agent skill. It collects historical information about one Shanghai, Shenzhen, or Beijing A-share symbol and saves it in a local SQLite database:

- daily unadjusted and forward-adjusted prices from Eastmoney, with Tencent as a separate fallback source;
- balance sheets, income statements, and cash-flow statements from Sina;
- parent-company statements extracted from periodic reports (HTML pages and text PDFs) found on Sina;
- official old-code to new-code mappings for Beijing Stock Exchange listings.

The database keeps every version that the collector observed. The JSON output always says which data is missing, stale, or unverified. An AI agent uses the skill through [`SKILL.md`](SKILL.md).

[↑ Back to Table of Contents](#table-of-contents)

## Set Up

The project needs Python 3.11 or newer. On Windows 11, run these commands in PowerShell from this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
```

On macOS or Linux, use `python3 -m venv .venv` and `.venv/bin/python -m pip install -r requirements.txt`.

No API key is needed. A-share collection needs no settings at all. The [Settings](#settings) section describes the one private file that access to the US regulator's data will need.

[↑ Back to Table of Contents](#table-of-contents)

## Settings

The US Securities and Exchange Commission (SEC) asks every automated tool to state who operates it: a name and an e-mail address. The collector reads these details from a private file named `config.toml` in this directory. The repository contains only an empty template, [`config.example.toml`](config.example.toml). It never contains anyone's details, and the source code contains none either.

US share collection, which will use these details, is not implemented yet. Today the details are read by the `config` command, and by the HTTP client before a request to an SEC server.

**Each person uses their own details.** Follow these steps in order:

1. Check that Git ignores the private file. This repository does not publish a `.gitignore` file, so a fresh copy has no ignore rules.

   ```powershell
   .\.venv\Scripts\python -B scripts\market_data.py config --json
   ```

   The answer must contain `"git_state": "ignored"`, or `"no_repository"` when the directory is not inside a Git repository. When it contains `"not_ignored"`, add an ignore rule and run the command again:

   ```powershell
   Add-Content .git\info\exclude "config.toml"
   ```

2. Copy the template and write your own name and address between the quotation marks. Use plain letters in the name, for example `Jose` for `José`.

   ```powershell
   Copy-Item config.example.toml config.toml
   notepad config.toml
   ```

3. Run the `config` command again. The answer must contain `"sec_contact": "configured"`.

On macOS or Linux, the matching commands are `.venv/bin/python -B scripts/market_data.py config --json`, `echo "config.toml" >> .git/info/exclude`, and `cp config.example.toml config.toml`.

The collector protects the details in these ways:

- It sends them to SEC servers only (`www.sec.gov` and `data.sec.gov`), and to no other source.
- It reads the file only when it is about to contact the SEC.
- It never reads a file that Git tracks or does not ignore. It reports `config_tracked_by_git` or `config_not_ignored` and explains the repair.
- It never prints or logs the details, and it never stores them in the database. The `config` command shows only whether the details are present.

Never commit `config.toml`, and never write your details into `config.example.toml`.

[↑ Back to Table of Contents](#table-of-contents)

## Run the Collector

Collect one year of data for Kweichow Moutai, then read the stored raw prices:

```powershell
.\.venv\Scripts\python -B scripts\market_data.py collect 600519 --json --start 2025-01-01 --end 2025-12-31
.\.venv\Scripts\python -B scripts\market_data.py show 600519 --json --dataset prices --start 2025-01-01 --end 2025-12-31
```

The collect command prints a JSON summary and exits with 0 when everything it attempted succeeded, or 3 when some data is missing. Read [`SKILL.md`](SKILL.md) for every option, the meaning of the JSON fields, and the exit codes.

[↑ Back to Table of Contents](#table-of-contents)

## Run the Tests

The tests use fake HTTP responses and temporary databases. They never contact a live service.

```powershell
.\.venv\Scripts\python -B -m unittest discover -s tests -p "test_*.py" -v
```

[↑ Back to Table of Contents](#table-of-contents)

## Where Things Are

- [`SKILL.md`](SKILL.md) tells an agent how to use the commands.
- [`scripts/`](scripts/README.md) contains the command-line entry point and the `stock_info` package.
- [`tests/`](tests/README.md) contains the automated tests.
- [`data/`](data/README.md) holds the local database, which Git does not track.
- [`requirements.txt`](requirements.txt) lists the two runtime dependencies.
- [`config.example.toml`](config.example.toml) is the empty template for the private settings file `config.toml`.
- `doc/` holds internal design records for the project's developers; they are not part of the public repository.

[↑ Back to Table of Contents](#table-of-contents)

## Known Limits

- Source reuse conditions are not established. Use collected data locally and do not redistribute it.
- The sources do not guarantee complete history. An empty result is never proof that no trading or reporting happened.
- Scanned (image-only) PDF filings are reported as unreadable; there is no OCR.
- Shanghai and Shenzhen code changes are not resolved, because no official mapping source is configured.
- Text extraction from real report PDFs usually rejects the tables as ambiguous. The collector therefore reads a PDF attachment only when its HTML page yielded no statement.
- A statement whose page-break pieces do not line up is rejected rather than saved partially. A rejected consolidated statement is only a warning when Sina's structured data covers the same periods; a rejected parent-company statement makes the collect `partial`.
- The sources were checked live on 2026-11-13 for one Shanghai, one Shenzhen, and one Beijing symbol. Their page formats can change without notice.

[↑ Back to Table of Contents](#table-of-contents)

## License

The code is released into the public domain under [The Unlicense](LICENSE). Anyone may copy, modify, publish, or sell it for any purpose, without attribution. The license covers only the code. It does not cover data that the collector downloads, whose reuse conditions are not established.

[↑ Back to Table of Contents](#table-of-contents)
