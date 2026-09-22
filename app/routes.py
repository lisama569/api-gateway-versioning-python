"""Example business endpoints behind the gateway."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.context import get_context
from app.errors import UpstreamError

logger = logging.getLogger("app.routes")

router = APIRouter(prefix="/api")


class EchoPayload(BaseModel):
    message: str


def _current_version() -> str:
    ctx = get_context()
    return ctx.version if ctx else "unknown"


@router.get("/ping")
def ping():
    """Always-succeeds endpoint used to observe negotiation/rate-limit behavior."""
    ctx = get_context()
    logger.info("ping served request_id=%s version=%s", ctx.request_id if ctx else "-", _current_version())
    return {
        "status": "ok",
        "version": _current_version(),
        "request_id": ctx.request_id if ctx else None,
    }


@router.post("/echo")
def echo(payload: EchoPayload):
    """Triggers 422 VALIDATION_ERROR when the body fails schema validation."""
    return {"echo": payload.message, "version": _current_version()}


@router.get("/items")
def list_items(limit: int = Query(ge=1, le=100)):
    """Query-param validation example."""
    return {"items": [], "limit": limit, "version": _current_version()}


@router.get("/upstream-fail")
def upstream_fail():
    """Simulates a dependency failure -> 502 UPSTREAM_ERROR (no internals leaked)."""
    try:
        raise ConnectionError("dial tcp 10.0.0.9:6379: connection refused")
    except ConnectionError as exc:
        raise UpstreamError("dependency cart-service unavailable") from exc


@router.get("/boom")
def boom():
    """An unexpected business bug -> 500 INTERNAL_ERROR; raw details must not leak."""
    raise RuntimeError("secret internal detail /TOP-SECRET-KEY/")
