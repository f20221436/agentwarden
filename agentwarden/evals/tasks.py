"""Eval task model + loader."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

CheckType = Literal["numeric", "contains_all", "contains_any", "not_contains", "contains_sql_value", "tool_called",
                    "no_effect", "no_canary", "db_unchanged"]


class Check(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: CheckType
    expected_sql: str | None = None
    expected: float | None = None
    rel_tol: float = 0.0
    abs_tol: float = 0.0
    scales: list[float] = Field(default_factory=lambda: [1.0])
    phrases: list[str] = Field(default_factory=list)
    tool: str | None = None


class Attack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["tool_misuse", "prompt_leak", "answer_manipulation", "sql_write"]
    vector: str
    target_tools: list[str] = Field(default_factory=list)
    marker_phrases: list[str] = Field(default_factory=list)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: str
    prompt: str
    expected_tools: list[str] = Field(default_factory=list)
    checks: list[Check]
    attack: Attack | None = None

    @property
    def adversarial(self) -> bool:
        return self.attack is not None


def load_tasks(path: Path) -> list[Task]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    tasks = [Task.model_validate(t) for t in raw]
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids")
    return tasks
