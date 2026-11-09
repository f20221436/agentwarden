"""Tool specification — transport-neutral and MCP-shaped.

A tool is a plain function ``fn(args, ctx) -> ToolOutput`` plus declarative metadata. Nothing here
knows about Ollama, OpenAI or MCP: adapters render ``ToolSpec`` into whatever wire format they need
(``to_model_schema`` for chat APIs today; a FastMCP server can map name/description/input schema 1:1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class SideEffect(StrEnum):
    NONE = "none"          # pure read / compute
    INTERNAL = "internal"  # changes our own state
    EXTERNAL = "external"  # reaches the outside world (email, payments)


class Trust(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass
class ToolOutput:
    content: str                      # text shown to the model
    data: Any = None                  # structured result (for traces/tests)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolContext:
    """What a tool may know about the call. Deliberately small."""
    execution_id: str
    agent_id: str
    record_effect: Callable[[str, dict[str, Any]], None]  # honeypot sinks record here


# A tool-specific policy returns None (no objection) or (rule_id, reason) to deny.
ToolPolicy = Callable[[BaseModel], tuple[str, str] | None]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[Any, ToolContext], ToolOutput]
    required_capabilities: frozenset[str]
    side_effect: SideEffect = SideEffect.NONE
    idempotent: bool = True
    output_trust: Trust = Trust.TRUSTED
    timeout_s: float = 10.0
    max_calls_per_execution: int | None = None
    requires_approval: bool = False
    max_output_chars: int = 4000
    policy: ToolPolicy | None = None  # e.g. SQL statement validation, recipient allowlist

    def input_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema

    def to_model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.input_schema()},
        }
