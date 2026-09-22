"""Gateway configuration."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitConfig:
    """Per (caller, version) fixed-window quota settings."""

    limit: int = 10
    window_seconds: float = 60.0


@dataclass(frozen=True)
class Settings:
    rate_limit: RateLimitConfig = RateLimitConfig()
