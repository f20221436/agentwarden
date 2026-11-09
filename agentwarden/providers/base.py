"""Provider-neutral chat types and the ModelProvider protocol."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    # dict when the model produced valid JSON; raw string otherwise (the gateway rejects it).
    arguments: dict[str, Any] | str


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # tool name, for role=tool


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ModelResponse(BaseModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Usage()
    latency_ms: float = 0.0
    finish_reason: str | None = None


class ProviderError(Exception):
    """Raised by providers. ``transient`` decides whether the runtime may retry.

    ``retry_after_s``: server-requested wait (HTTP 429 ``Retry-After`` / rate-limit reset headers).
    ``quota_exhausted``: a long-horizon limit (e.g. tokens per day) was hit; retrying within one task is
    pointless and an eval run must stop rather than record infrastructure failures as task failures.
    """

    def __init__(self, message: str, *, transient: bool, status: int | None = None,
                 retry_after_s: float | None = None, quota_exhausted: bool = False):
        super().__init__(message)
        self.transient = transient
        self.status = status
        self.retry_after_s = retry_after_s
        self.quota_exhausted = quota_exhausted


_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def parse_duration(value: str) -> float | None:
    """'7.66s', '2m59.56s', '1h2m', '120ms', or plain seconds '12' -> seconds."""
    v = value.strip()
    try:
        return float(v)
    except ValueError:
        pass
    m = _DURATION.match(v)
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


def parse_retry_after(headers: Mapping[str, str]) -> float | None:
    """Server-requested wait in seconds. Prefers ``Retry-After`` (seconds or HTTP-date), then the
    ``x-ratelimit-reset-*`` headers used by Groq/OpenAI-style APIs (largest reset wins)."""
    h = {k.lower(): v for k, v in headers.items()}
    ra = h.get("retry-after")
    if ra:
        secs = parse_duration(ra)
        if secs is not None:
            return max(secs, 0.0)
        try:
            when = parsedate_to_datetime(ra)
            return max((when - datetime.now(UTC)).total_seconds(), 0.0)
        except (TypeError, ValueError):
            pass
    resets = [parse_duration(h[k]) for k in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens") if k in h]
    resets = [r for r in resets if r is not None]
    return max(resets) if resets else None


class ModelProvider(Protocol):
    name: str
    model: str

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
        temperature: float = 0.0,
        seed: int | None = None,
    ) -> ModelResponse: ...


def classify_http_status(status: int) -> bool:
    """True if an HTTP status is worth retrying (rate limit / server-side / timeout)."""
    return status in (408, 409, 425, 429) or 500 <= status < 600
