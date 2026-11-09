"""Provider-neutral chat types and the ModelProvider protocol."""

from __future__ import annotations

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
    """Raised by providers. ``transient`` decides whether the runtime may retry."""

    def __init__(self, message: str, *, transient: bool, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


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
