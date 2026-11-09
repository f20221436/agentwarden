"""Optional cloud provider: any OpenAI Chat-Completions-compatible endpoint.

Disabled unless ``AW_ALLOW_CLOUD=1`` and a key is configured (enforced in ``providers.factory``).
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


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(self, model: str, base_url: str, api_key: str, *, client: httpx.Client | None = None):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.Client()

    @staticmethod
    def _to_wire(m: ChatMessage) -> dict[str, Any]:
        out: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else tc.arguments,
                    },
                }
                for tc in m.tool_calls
            ]
        if m.role == "tool":
            out["tool_call_id"] = m.tool_call_id
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
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [self._to_wire(m) for m in messages],
            "tools": [{"type": "function", "function": t["function"]} for t in tools],
            "temperature": temperature,
        }
        if seed is not None:
            body["seed"] = seed
        t0 = time.perf_counter()
        try:
            r = self._client.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=timeout_s,
            )
        except httpx.TimeoutException as e:
            raise ProviderError(f"cloud timeout after {timeout_s:.1f}s", transient=True) from e
        except httpx.TransportError as e:
            raise ProviderError(f"cloud connection error: {type(e).__name__}", transient=True) from e
        latency_ms = (time.perf_counter() - t0) * 1000
        if r.status_code != 200:
            # Never echo headers; body text is truncated and contains no key.
            raise ProviderError(
                f"cloud HTTP {r.status_code}: {r.text[:300]}",
                transient=classify_http_status(r.status_code),
                status=r.status_code,
            )
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args: Any = fn.get("arguments", "{}")
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except json.JSONDecodeError:
                pass
            calls.append(ToolCall(id=raw.get("id") or new_id("call"), name=str(fn.get("name", "")), arguments=args))
        usage = data.get("usage") or {}
        return ModelResponse(
            content=msg.get("content") or "",
            tool_calls=calls,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason"),
        )
