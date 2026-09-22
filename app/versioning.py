"""Version registry and negotiation rules.

Negotiation semantics (header ``X-API-Version``):

* header absent/empty -> the registry's single default version is selected;
* header present and known + available (active or deprecated) -> that version;
* header present but unknown -> ``VersionUnknownError`` (404);
* header present but retired -> ``VersionRetiredError`` (410).

The raw header is normalized by stripping surrounding whitespace and a
case-insensitive ``v`` prefix, e.g. ``" V1 "`` -> ``"1"``.
"""
from __future__ import annotations

from app.errors import VersionRetiredError, VersionUnknownError
from app.models import NegotiationResult, VersionInfo, VersionStatus


def normalize_version(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if value[:1].lower() == "v" and len(value) > 1:
        value = value[1:]
    return value.strip()


class VersionRegistry:
    def __init__(self, versions: list[VersionInfo]):
        defaults = [v for v in versions if v.default]
        if len(defaults) != 1:
            raise ValueError("exactly one default version is required")
        self._versions: dict[str, VersionInfo] = {v.version: v for v in versions}
        self._default = defaults[0]

    @property
    def default_version(self) -> str:
        return self._default.version

    def get(self, version: str) -> VersionInfo | None:
        return self._versions.get(version)

    def negotiate(self, requested_raw: str | None) -> NegotiationResult:
        requested = normalize_version(requested_raw)
        if requested is None:
            return NegotiationResult(
                requested=None,
                resolved=self._default.version,
                info=self._default,
                used_default=True,
                reason="no X-API-Version header; selecting registry default",
            )
        info = self._versions.get(requested)
        if info is None:
            raise VersionUnknownError(
                f"unknown api version: {requested!r}",
                details={"requested": requested, "known": sorted(self._versions)},
            )
        if info.status is VersionStatus.RETIRED:
            raise VersionRetiredError(
                f"api version {requested!r} has been retired",
                details={
                    "requested": requested,
                    "sunset": info.sunset,
                    "successor": info.successor,
                },
            )
        return NegotiationResult(
            requested=requested,
            resolved=info.version,
            info=info,
            used_default=False,
            reason=f"explicit request matched registered version {info.version!r}",
        )


def build_default_registry() -> VersionRegistry:
    return VersionRegistry(
        [
            VersionInfo(
                version="1",
                status=VersionStatus.RETIRED,
                sunset="2026-01-01",
                successor="2",
            ),
            VersionInfo(version="2", status=VersionStatus.DEPRECATED, successor="3", default=True),
            VersionInfo(version="3", status=VersionStatus.ACTIVE),
        ]
    )
