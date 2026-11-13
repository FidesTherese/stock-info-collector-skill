"""Stock information collector package for one A-share symbol at a time.

The package collects daily prices, financial statements, company filings, and
official Beijing Stock Exchange code mappings, and it stores them in SQLite.
"""

import logging as _logging

_logging.getLogger("stock_info").addHandler(_logging.NullHandler())
