"""Per-request context: request id, caller, negotiated version."""
from __future__ import annotations

import contextvars
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    caller: str
    version: str


_current: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "request_context", default=None
)


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_context(ctx: RequestContext) -> contextvars.Token:
    return _current.set(ctx)


def reset_context(token: contextvars.Token) -> None:
    _current.reset(token)


def get_context() -> RequestContext | None:
    return _current.get()
