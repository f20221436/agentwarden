"""Price lookup for estimated cost. Unknown (provider, model) -> None (cost reported as n/a, never guessed)."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

PRICES_FILE = Path(__file__).with_name("pricing.yaml")


class Price(BaseModel):
    input: float   # USD per 1M input tokens
    output: float  # USD per 1M output tokens
    verified: bool
    source: str


@cache
def _table() -> dict[str, dict[str, Any]]:
    return yaml.safe_load(PRICES_FILE.read_text(encoding="utf-8")) or {}


def lookup(provider: str, model: str) -> Price | None:
    if provider == "ollama":
        return Price(input=0.0, output=0.0, verified=True, source="local inference")
    entry = _table().get(provider, {}).get(model)
    return Price.model_validate(entry) if entry else None
