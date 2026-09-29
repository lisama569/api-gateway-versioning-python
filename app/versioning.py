"""接口版本注册、协商与运行时策略管理。"""
from __future__ import annotations

import enum
import threading
from collections.abc import Callable
from dataclasses import dataclass


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
class QuotaPolicy:
    """单个版本的配额策略：额度大小与统计窗口。"""

    limit: int
    window_seconds: float


@dataclass(frozen=True)
class VersionSnapshot:
    """一次协商/判定看到的不可变版本视图。

    并发调整策略时，进行中的请求始终依据同一份快照判定，
    状态与配额策略不会在一次请求内发生变化。
    """

    info: VersionInfo
    quota: QuotaPolicy
    requested: str | None
    used_default: bool


# 版本状态发生变化（例如运行时停用）时的通知回调。
StatusListener = Callable[[str, VersionStatus, VersionStatus], None]


def _validate_quota(policy: QuotaPolicy) -> None:
    if policy.limit <= 0:
        raise ValueError("limit 必须为正整数")
    if policy.window_seconds <= 0:
        raise ValueError("window_seconds 必须为正数")


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
    def __init__(
        self,
        versions: dict[str, VersionInfo],
        default: str,
        quotas: dict[str, QuotaPolicy] | None = None,
        default_quota: QuotaPolicy | None = None,
        status_listener: StatusListener | None = None,
    ) -> None:
        if default not in versions:
            raise ValueError(f"默认版本 {default} 必须在注册表中")
        self._versions = dict(versions)
        self._default = default
        self._quotas: dict[str, QuotaPolicy] = dict(quotas or {})
        for policy in self._quotas.values():
            _validate_quota(policy)
        self._default_quota = default_quota
        self._status_listener = status_listener
        self._lock = threading.RLock()

    @property
    def default(self) -> str:
        with self._lock:
            return self._default

    def _quota_for_locked(self, name: str) -> QuotaPolicy:
        policy = self._quotas.get(name) or self._default_quota
        if policy is None:
            raise RuntimeError(f"版本 {name} 未配置配额策略且无默认配额")
        return policy

    def _snapshot_locked(
        self, info: VersionInfo, requested: str | None, used_default: bool
    ) -> VersionSnapshot:
        return VersionSnapshot(
            info=info,
            quota=self._quota_for_locked(info.name),
            requested=requested,
            used_default=used_default,
        )

    def negotiate(self, requested: str | None) -> VersionSnapshot:
        """协商请求版本。

        - 未声明版本：回落到默认版本；
        - 未知版本：抛 UnknownVersionError；
        - 已停用版本：抛 DisabledVersionError；
        - 已弃用版本：允许使用，调用方据此附加弃用提示。

        返回的快照同时携带该版本当前生效的配额策略，
        后续配额判定与本次协商看到的是同一份策略。
        """
        with self._lock:
            used_default = requested is None
            name = self._default if used_default else str(requested)
            info = self._versions.get(name)
            if info is None:
                raise UnknownVersionError(requested)
            if info.status is VersionStatus.DISABLED:
                raise DisabledVersionError(name)
            return self._snapshot_locked(info, requested, used_default)

    def update_policy(
        self,
        name: str,
        *,
        limit: int | None = None,
        window_seconds: float | None = None,
        status: VersionStatus | None = None,
        sunset: str | None = None,
    ) -> VersionSnapshot:
        """运行时调整某个版本的额度、窗口或状态，无需重启服务。

        - 只改额度/窗口：已用计数不归零，下一次请求按新策略判定；
        - 状态变为 DISABLED：之后的协商立即拒绝，并通知监听方
          （如配额器清掉该版本的调用方桶，停用版本不再占额度）；
        - 未知版本抛 UnknownVersionError；非法额度抛 ValueError。
        """
        if limit is not None and limit <= 0:
            raise ValueError("limit 必须为正整数")
        if window_seconds is not None and window_seconds <= 0:
            raise ValueError("window_seconds 必须为正数")

        listener: StatusListener | None = None
        with self._lock:
            info = self._versions.get(name)
            if info is None:
                raise UnknownVersionError(name)

            if limit is not None or window_seconds is not None:
                current = self._quota_for_locked(name)
                self._quotas[name] = QuotaPolicy(
                    limit=current.limit if limit is None else limit,
                    window_seconds=(
                        current.window_seconds
                        if window_seconds is None
                        else window_seconds
                    ),
                )

            old_status = info.status
            if status is not None and status is not old_status:
                info = VersionInfo(
                    name=name,
                    status=status,
                    sunset=(info.sunset if sunset is None else sunset),
                )
                self._versions[name] = info
                listener = self._status_listener
            elif sunset is not None:
                info = VersionInfo(name=name, status=info.status, sunset=sunset)
                self._versions[name] = info

            snapshot = self._snapshot_locked(info, name, name == self._default)

        # 状态变更通知在锁外触发，避免监听方回调造成死锁。
        if listener is not None and status is not None and status is not old_status:
            listener(name, old_status, status)
        return snapshot

    def describe(self, name: str) -> VersionSnapshot:
        """查询某个版本当前生效的状态与配额策略（停用版本也可查询）。"""
        with self._lock:
            info = self._versions.get(name)
            if info is None:
                raise UnknownVersionError(name)
            return self._snapshot_locked(info, name, name == self._default)

    def snapshot_state(self) -> tuple[dict[str, VersionInfo], dict[str, QuotaPolicy]]:
        """导出当前注册表状态（frozen 数据，浅拷贝字典即可隔离）。"""
        with self._lock:
            return dict(self._versions), dict(self._quotas)

    def restore_state(
        self,
        versions: dict[str, VersionInfo],
        quotas: dict[str, QuotaPolicy],
    ) -> None:
        """恢复注册表状态，主要用于测试之间的隔离。"""
        with self._lock:
            self._versions = dict(versions)
            self._quotas = dict(quotas)
