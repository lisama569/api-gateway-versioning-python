"""Shared domain types."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VersionStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


class ErrorCode(str, Enum):
    VERSION_UNKNOWN = "VERSION_UNKNOWN"
    VERSION_RETIRED = "VERSION_RETIRED"
    RATE_LIMITED = "RATE_LIMITED"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class VersionInfo:
    version: str
    status: VersionStatus
    sunset: str | None = None
    successor: str | None = None
    default: bool = False

    @property
    def deprecated(self) -> bool:
        return self.status is VersionStatus.DEPRECATED

    @property
    def available(self) -> bool:
        return self.status in (VersionStatus.ACTIVE, VersionStatus.DEPRECATED)


@dataclass(frozen=True)
class NegotiationResult:
    requested: str | None
    resolved: str
    info: VersionInfo
    used_default: bool
    reason: str


@dataclass(frozen=True)
class ErrorBody:
    error: ErrorCode
    message: str
    request_id: str
    details: dict | None = None
