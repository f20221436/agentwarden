"""Local-model tool-calling compatibility probe (layer 3).

Sends a fixed set of prompts with the real tool schemas and checks, for the FIRST model turn only:
  * did it emit a structured tool call (not prose / JSON-in-text)?
  * are the arguments valid against the tool's schema?
  * is it the expected tool?
and measures latency + tokens/s. No tools are executed; this measures the model, not the runtime.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from pydantic import ValidationError

from agentwarden.gateway.registry import ToolRegistry
from agentwarden.providers.base import ChatMessage, ModelProvider, ProviderError
from agentwarden.runtime.prompts import system_prompt

PROBES: list[tuple[str, str]] = [
    ("How many users are on the pro plan?", "sql_query"),
    ("What is the total paid revenue in cents in March 2026?", "sql_query"),
    ("How many support tickets are currently open?", "sql_query"),
    ("Which country has the most active users?", "sql_query"),
    ("What does the refund policy say about approval thresholds?", "doc_search"),
    ("What is the first-response SLA for urgent tickets?", "doc_search"),
    ("What do the experimentation guidelines say about sample ratio mismatch?", "doc_search"),
    ("Compute (137/1976)/(210/2024) - 1", "calculator"),
    ("What is 17.5% of 48210?", "calculator"),
    ("How many conversions did the checkout_redesign experiment have per variant?", "sql_query"),
]


@dataclass
class ProbeRow:
    prompt: str
    expected: str
    called: str | None
    structured: bool
    args_valid: bool
    correct_tool: bool
    latency_s: float
    out_tokens: int
    in_tokens: int
    error: str | None = None


def run_probe(provider: ModelProvider, registry: ToolRegistry, tools: list[str]) -> list[ProbeRow]:
    schemas = registry.schemas(tools)
    sp = system_prompt(company="Lumora", role="a data and support analyst assistant", canary="AW-CANARY-probe",
                       spotlighting=True)
    rows: list[ProbeRow] = []
    for prompt, expected in PROBES:
        msgs = [ChatMessage(role="system", content=sp), ChatMessage(role="user", content=prompt)]
        try:
            r = provider.chat(msgs, schemas, timeout_s=300, temperature=0.0, seed=7)
        except ProviderError as e:
            rows.append(ProbeRow(prompt, expected, None, False, False, False, 0, 0, 0, str(e)[:200]))
            continue
        tc = r.tool_calls[0] if r.tool_calls else None
        valid = False
        if tc and (spec := registry.get(tc.name)) and isinstance(tc.arguments, dict):
            try:
                spec.args_model.model_validate(tc.arguments)
                valid = True
            except ValidationError:
                valid = False
        rows.append(ProbeRow(prompt, expected, tc.name if tc else None, tc is not None, valid,
                             bool(tc and tc.name == expected), r.latency_ms / 1000, r.usage.output_tokens,
                             r.usage.input_tokens))
    return rows


def summarize(rows: list[ProbeRow]) -> dict[str, float]:
    n = len(rows)
    lat = [r.latency_s for r in rows if r.latency_s]
    return {
        "n": n,
        "structured_call_rate": sum(r.structured for r in rows) / n,
        "valid_args_rate": sum(r.args_valid for r in rows) / n,
        "correct_tool_rate": sum(r.correct_tool for r in rows) / n,
        "latency_mean_s": statistics.mean(lat) if lat else 0.0,
        "latency_max_s": max(lat) if lat else 0.0,
    }
