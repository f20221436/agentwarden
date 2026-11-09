"""Deterministic scripted provider — FOR UNIT TESTS ONLY.

It replays pre-written responses so the runtime (limits, retries, gateway, tracing) can be tested
without a model. The eval runner refuses to use it, so scripted output can never be reported as real
agent execution.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentwarden.providers.base import ChatMessage, ModelResponse, ProviderError

Step = ModelResponse | ProviderError | Callable[[list[ChatMessage]], ModelResponse]


class ScriptedProvider:
    name = "scripted"
    is_scripted = True

    def __init__(self, steps: list[Step], model: str = "scripted"):
        self.model = model
        self._steps = list(steps)
        self.calls: list[list[ChatMessage]] = []

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
        temperature: float = 0.0,
        seed: int | None = None,
    ) -> ModelResponse:
        self.calls.append(list(messages))
        if not self._steps:
            return ModelResponse(content="(script exhausted)")
        step = self._steps.pop(0)
        if isinstance(step, ProviderError):
            raise step
        if callable(step) and not isinstance(step, ModelResponse):
            return step(messages)
        return step
