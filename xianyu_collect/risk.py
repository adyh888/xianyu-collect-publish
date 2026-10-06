"""账号风控熔断：命中风控即冷却，冷却期内拒绝发起新请求。

冷却时长指数退避（5min → 10min → 20min → ... 上限 1 小时），
成功调用后 reset。对应上游 xianyu_mtop 的 VALIDATE_MARKERS 熔断经验。
"""
from __future__ import annotations

import threading
import time


class RiskBlocked(Exception):
    def __init__(self, remaining: float, reason: str = ""):
        self.remaining = remaining
        self.reason = reason
        super().__init__(f"账号处于风控冷却期，剩余 {remaining:.0f}s（原因: {reason}）")


class RiskGuard:
    COOLDOWN_BASE = 300.0
    COOLDOWN_CAP = 3600.0

    def __init__(self, name: str = "default"):
        self.name = name
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self._hits = 0
        self._last_reason = ""

    @property
    def remaining(self) -> float:
        return max(0.0, self._blocked_until - time.monotonic())

    def is_blocked(self) -> bool:
        return self.remaining > 0

    def acquire(self) -> None:
        if self.is_blocked():
            raise RiskBlocked(self.remaining, self._last_reason)

    def trip(self, reason: str = "") -> None:
        with self._lock:
            self._hits += 1
            self._last_reason = reason
            cooldown = min(self.COOLDOWN_BASE * (2 ** (self._hits - 1)), self.COOLDOWN_CAP)
            self._blocked_until = time.monotonic() + cooldown

    def reset(self) -> None:
        with self._lock:
            self._hits = 0
            self._blocked_until = 0.0
            self._last_reason = ""
