# Tests

<a id="table-of-contents"></a>
## Table of Contents

- [Purpose](#purpose)
- [Test Files](#test-files)

## Purpose

This directory contains the automated `unittest` suite for the code in [`scripts/`](../scripts/README.md). The tests use a fake HTTP session, fixed clocks, temporary databases, and inline fixtures, so they never contact a live service. The contact details in the tests are made up. Some tests run the real `git` program in a temporary repository or in the project directory; they are skipped, or pass without a Git answer, when Git is not installed. When the owner has filled in the private settings file, one test also checks that no file which Git tracks or could add repeats the contact details; a failure lists only file names. Run the suite from the project root:

```powershell
.\.venv\Scripts\python -B -m unittest discover -s tests -p "test_*.py" -v
```

[↑ Back to Table of Contents](#table-of-contents)

## Test Files

| File | What it checks |
|------|----------------|
| `test_providers.py` | Canonical JSON, decimals, symbols, HTTP limits and retries, the SEC contact line reaching SEC hosts only, Eastmoney, Tencent, Sina, and BSE mappings. It also defines the fake session and fixtures that other test files import. |
| `test_settings.py` | Validation of the private settings file, the Git check with a fake and a real Git program, blocked and missing contacts, the empty published template, the search for contact details in publishable files, and the rule that contact details never appear in printed or stored text. |
| `test_storage.py` | Schema and version handling, daily gates at China midnight, rollback, A→B→A revisions, retained omissions, forward snapshots, pagination, and read-only access. |
| `test_filings.py` | GBK index discovery, HTML parent statements, identity checks, table geometry, a real pypdf extraction from an inline ToUnicode PDF, PDF failures, and local path limits. |
| `test_collector.py` | A full collect run, same-day reuse, next-day recheck, refresh, Tencent fallback, Beijing aliases and conflicts, parent gaps, interruption, and storage failure. |
| `test_cli.py` | JSON envelope keys, input validation, exit codes, collect-then-show, read-only show, the `config` command, and the script run as a subprocess. |
| `test_us.py` | US symbol forms, the New York day around both clock changes, the venue search for US prices, the limits of the Tencent fallback, the request gap for SEC hosts, the storage and queries of reported values, the schema upgrade, and the New York gate. |
| `test_sec.py` | The SEC ticker list, the parsing of reported values, the planning of reports with their estimated period end, the choice of statement pages by title, and the reading of report pages modelled on real ones. |
| `test_us_collect.py` | A whole US collection in a fake world: task order, reuse on the same day, a missing contact, the request budget, the Tencent fallback, and the US selectors of the command line. |

[↑ Back to Table of Contents](#table-of-contents)
