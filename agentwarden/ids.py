"""Time-sortable identifiers (ULID-like) so ids sort by creation time in MongoDB."""

from __future__ import annotations

import secrets
import time


def new_id(prefix: str) -> str:
    """Return e.g. ``exe_0193a1b2c3d4e5f6a7b8c9`` — 48-bit ms timestamp (hex) + 40 random bits."""
    return f"{prefix}_{int(time.time() * 1000):012x}{secrets.token_hex(5)}"
