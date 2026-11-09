"""Ollama provider using the native ``/api/chat`` endpoint.

Native (not the OpenAI-compatible ``/v1``) because only it lets us set ``num_ctx``, ``keep_alive``
and ``think``, and it returns exact prompt/eval token counts.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from agentwarden.ids import new_id
from agentwarden.providers.base import (
    ChatMessage,
    ModelResponse,
    ProviderError,
    ToolCall,
    Usage,
    classify_http_status,
)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str,
        base_url: str = "http://127.0.0.1:11434",
        *,
        num_ctx: int = 8192,
        think: bool | None = False,
        keep_alive: str = "30m",
        client: httpx.Client | None = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.num_ctx = num_ctx
        self.think = think
        self.keep_alive = keep_alive
        self._client = client or httpx.Client()

    @staticmethod
    def _to_wire(m: ChatMessage) -> dict[str, Any]:
        out: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            out["tool_calls"] = [
                {
                    "function": {
                        "name": tc.name,
                        "arguments": tc.arguments if isinstance(tc.arguments, dict) else {},
                    }
                }
                for tc in m.tool_calls
            ]
        if m.role == "tool" and m.name:
            out["tool_name"] = m.name
        return out

    def chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]],
        *,
        timeout_s: float,
        temperature: float = 0.0,
        seed: int | None = None,
    ) -> ModelResponse:
        options: dict[str, Any] = {"temperature": temperature, "num_ctx": self.num_ctx}
        if seed is not None:
            options["seed"] = seed
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [self._to_wire(m) for m in messages],
            "tools": tools,
            "stream": False,
            "options": options,
            "keep_alive": self.keep_alive,
        }
        if self.think is not None:
            body["think"] = self.think

        t0 = time.perf_counter()
        try:
            r = self._client.post(f"{self.base_url}/api/chat", json=body, timeout=timeout_s)
        except httpx.TimeoutException as e:
            raise ProviderError(f"ollama timeout after {timeout_s:.1f}s", transient=True) from e
        except httpx.TransportError as e:
            raise ProviderError(f"ollama connection error: {e}", transient=True) from e
        latency_ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            raise ProviderError(
                f"ollama HTTP {r.status_code}: {r.text[:300]}",
                transient=classify_http_status(r.status_code),
                status=r.status_code,
            )
        data = r.json()
        msg = data.get("message") or {}
        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    pass  # keep raw string; gateway will reject as invalid_arguments
            calls.append(ToolCall(id=raw.get("id") or new_id("call"), name=str(fn.get("name", "")), arguments=args))
        return ModelResponse(
            content=msg.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=int(data.get("prompt_eval_count") or 0),
                output_tokens=int(data.get("eval_count") or 0),
            ),
            latency_ms=latency_ms,
            finish_reason=data.get("done_reason"),
        )
