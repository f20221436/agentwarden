"""Scenario packs: dataset + documents + tasks + agent configs, swappable without runtime changes."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from agentwarden.config import AgentConfig
from agentwarden.gateway.registry import ToolRegistry
from agentwarden.tools.calculator import make_calculator_tool
from agentwarden.tools.doc_search import make_doc_search_tool
from agentwarden.tools.sinks import make_issue_refund_tool, make_send_email_tool
from agentwarden.tools.sql_query import make_sql_tool


class Scenario(BaseModel):
    name: str
    company: str
    root: Path
    database: Path
    documents_dir: Path
    tasks_file: Path
    task_set_version: str
    data_seed: int
    allowed_email_domains: list[str]
    default_agent: str
    agents: dict[str, AgentConfig]
    schema_notes: str = ""

    def agent(self, name: str | None = None) -> AgentConfig:
        return self.agents[name or self.default_agent]


def load_scenario(path: Path) -> Scenario:
    path = Path(path)
    root = path if path.is_dir() else path.parent
    raw = yaml.safe_load((root / "scenario.yaml").read_text(encoding="utf-8"))
    agents = {k: AgentConfig(agent_id=k, **v) for k, v in raw.pop("agents").items()}
    return Scenario(
        root=root,
        database=root / raw.pop("database"),
        documents_dir=root / raw.pop("documents_dir"),
        tasks_file=root / raw.pop("tasks_file"),
        agents=agents,
        **raw,
    )


def build_registry(scenario: Scenario) -> ToolRegistry:
    if not scenario.database.exists():
        raise FileNotFoundError(
            f"{scenario.database} missing. Generate it: python {scenario.root / 'generate.py'}")
    reg = ToolRegistry()
    reg.register(make_sql_tool(scenario.database, schema_notes=scenario.schema_notes))
    reg.register(make_doc_search_tool(scenario.documents_dir))
    reg.register(make_calculator_tool())
    reg.register(make_send_email_tool(scenario.allowed_email_domains))
    reg.register(make_issue_refund_tool())
    return reg
