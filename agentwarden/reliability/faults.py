"""Deterministic fault injection for measuring reliability.

The decision for each attempt is a pure function of (seed, scope, tool, call ordinal, attempt), so the
*same* calls fail in reliability-ON and reliability-OFF runs — a fair comparison. ``scope`` is normally
the eval task id.
"""

from __future__ import annotations

import hashlib
import time

from agentwarden.config import FaultConfig
from agentwarden.reliability.errors import TransientToolError


def _unit(*parts: object) -> float:
    h = hashlib.sha256(":".join(map(str, parts)).encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64


class FaultInjector:
    def __init__(self, config: FaultConfig, seed: int = 0, sleep=time.sleep):
        self.config = config
        self.seed = seed
        self._sleep = sleep
        self.injected = 0

    def applies_to(self, tool: str) -> bool:
        return self.config.active and (self.config.tools is None or tool in self.config.tools)

    def maybe_fail(self, *, scope: str, tool: str, ordinal: int, attempt: int) -> None:
        if not self.applies_to(tool):
            return
        if self.config.latency_ms:
            self._sleep(self.config.latency_ms / 1000)
        if _unit(self.seed, scope, tool, ordinal, attempt) < self.config.failure_rate:
            self.injected += 1
            raise TransientToolError(f"injected transient fault ({tool}, attempt {attempt})")
