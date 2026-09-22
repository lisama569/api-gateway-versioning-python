"""Logging setup: every record carries the per-request correlation id."""
from __future__ import annotations

import logging

from app.context import get_context

_DEFAULT_FORMAT = (
    "%(asctime)s %(levelname)s [request_id=%(request_id)s] %(name)s: %(message)s"
)


class RequestContextFilter(logging.Filter):
    """Populate ``record.request_id`` from the active ``RequestContext``."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_context()
        record.request_id = ctx.request_id if ctx is not None else "-"
        return True


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, "_gateway_handler", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler._gateway_handler = True  # type: ignore[attr-defined]
        handler.addFilter(RequestContextFilter())
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        root.addHandler(handler)
    return logging.getLogger("app")
