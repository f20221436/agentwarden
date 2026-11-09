"""Tool registry: registration + discovery."""

from __future__ import annotations

from agentwarden.gateway.spec import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, names: list[str]) -> list[dict]:
        """Model-facing schemas for the tools an agent is configured to see (discovery)."""
        missing = [n for n in names if n not in self._tools]
        if missing:
            raise KeyError(f"agent references unknown tools: {missing}")
        return [self._tools[n].to_model_schema() for n in names]
