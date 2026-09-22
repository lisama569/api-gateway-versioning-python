"""Test setup: ensure the request-id logging filter is installed."""
from __future__ import annotations

import logging

from app.logging_conf import configure_logging

configure_logging(logging.INFO)

# pytest captures logging by default; records propagate to caplog and are also
# attached to failing tests. Run with ``pytest -s -o log_cli=true`` to watch
# request ids / negotiation rationale live.
