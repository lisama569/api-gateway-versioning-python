"""接口版本注册与协商。"""
from __future__ import annotations

import enum
import threading
from dataclasses import dataclass, replace


class VersionStatus(enum.Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    DISABLED = "disabled"


@dataclass(frozen=True)
class VersionInfo:
    name: str
    status: VersionStatus
    sunset: str | None = None


@dataclass(frozen=True)
class NegotiatedVersion:
    info: VersionInfo
    requested: str | None
    used_default: bool


class VersionError(Exception):
    """版本协商失败的基类。"""

    def __init__(self, requested: str | None) -> None:
        super().__init__(requested)
        self.requested = requested


class UnknownVersionError(VersionError):
    """请求了注册表中不存在的版本。"""


class DisabledVersionError(VersionError):
    """请求的版本已停用。"""


class VersionRegistry:
    def __init__(self, versions: dict[str, VersionInfo], default: str) -> None:
        self._versions = dict(versions)
        self._default = default
        self._lock = threading.RLock()

    @property
    def default(self) -> str:
        return self._default

    def get(self, name: str) -> VersionInfo | None:
        with self._lock:
            return self._versions.get(name)

    def set_status(self, name: str, status: VersionStatus) -> VersionInfo:
        """运行时切换版本状态（active/deprecated/disabled）。

        从下一个请求起生效；版本协商本身无状态，并发请求要么按旧状态
        要么按新状态判定，不会出现中间态。
        """
        with self._lock:
            info = self._versions.get(name)
            if info is None:
                raise UnknownVersionError(name)
            updated = replace(info, status=status)
            self._versions[name] = updated
            return updated

    def negotiate(self, requested: str | None) -> NegotiatedVersion:
        """协商请求版本。

        - 未声明版本：回落到默认版本（默认版本必须为 ACTIVE）。
        - 未知版本：抛 UnknownVersionError。
        - 已停用版本：抛 DisabledVersionError。
        - 已弃用版本：允许使用，调用方据此附加弃用提示。
        """
        used_default = requested is None
        name = self._default if used_default else str(requested)
        info = self._versions.get(name)
        if info is None:
            raise UnknownVersionError(requested)
        if info.status is VersionStatus.DISABLED:
            raise DisabledVersionError(name)
        return NegotiatedVersion(info=info, requested=requested, used_default=used_default)
