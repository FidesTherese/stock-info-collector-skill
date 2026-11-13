# Tests

<a id="table-of-contents"></a>
## Table of Contents

- [Purpose](#purpose)
- [Test Files](#test-files)

## Purpose

This directory contains the automated `unittest` suite for the code in [`scripts/`](../scripts/README.md). The tests use a fake HTTP session, fixed clocks, temporary databases, and inline fixtures, so they never contact a live service. Run them from the project root:

```powershell
.\.venv\Scripts\python -B -m unittest discover -s tests -p "test_*.py" -v
```

[↑ Back to Table of Contents](#table-of-contents)

## Test Files

| File | What it checks |
|------|----------------|
| `test_providers.py` | Canonical JSON, decimals, symbols, HTTP limits and retries, Eastmoney, Tencent, Sina, and BSE mappings. It also defines the fake session and fixtures that other test files import. |
| `test_storage.py` | Schema and version handling, daily gates at China midnight, rollback, A→B→A revisions, retained omissions, forward snapshots, pagination, and read-only access. |
| `test_filings.py` | GBK index discovery, HTML parent statements, identity checks, table geometry, a real pypdf extraction from an inline ToUnicode PDF, PDF failures, and local path limits. |
| `test_collector.py` | A full collect run, same-day reuse, next-day recheck, refresh, Tencent fallback, Beijing aliases and conflicts, parent gaps, interruption, and storage failure. |
| `test_cli.py` | JSON envelope keys, input validation, exit codes, collect-then-show, read-only show, and the script run as a subprocess. |

[↑ Back to Table of Contents](#table-of-contents)
