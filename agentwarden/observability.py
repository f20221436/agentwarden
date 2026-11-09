"""Tracing + structured logging.

The Tracer writes ordered events to ``steps`` immediately (not batched at the end) so that a crashed or
killed execution still leaves a trace up to the failure point. Payloads are redacted before persisting.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from agentwarden.security.redact import redact
from agentwarden.store.base import PlatformStore


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": datetime.now(UTC).isoformat(), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage()}
        out.update(getattr(record, "fields", {}))
        return json.dumps(redact(out), default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class Tracer:
    def __init__(self, store: PlatformStore, execution_id: str, *, ephemeral: bool):
        self.store = store
        self.execution_id = execution_id
        self.ephemeral = ephemeral
        self._seq = 0
        self.events: list[dict[str, Any]] = []  # in-memory copy (used by eval scoring)

    def emit(self, type_: str, *, duration_ms: float | None = None, **payload: Any) -> None:
        self._seq += 1
        doc = {
            "execution_id": self.execution_id,
            "seq": self._seq,
            "type": type_,
            "ts": datetime.now(UTC),
            "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
            "ephemeral": self.ephemeral,
            "payload": redact(payload),
        }
        self.events.append(doc)
        self.store.insert_step(dict(doc))
