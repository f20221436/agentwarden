from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path

import pytest

from agentwarden.config import AgentConfig, GuardrailsConfig, RetryPolicy
from agentwarden.gateway.gateway import CallContext, Gateway
from agentwarden.observability import Tracer
from agentwarden.scenario import Scenario, build_registry, load_scenario
from agentwarden.store.memory import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = ROOT / "scenarios" / "saas_demo"


def _load_generator():
    spec = importlib.util.spec_from_file_location("saas_generate", SCENARIO_DIR / "generate.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def db_path(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("data") / "lumora.db"
    _load_generator().generate(out, seed=42)
    return out


@pytest.fixture(scope="session")
def scenario(db_path) -> Scenario:
    sc = load_scenario(SCENARIO_DIR)
    return sc.model_copy(update={"database": db_path})


@pytest.fixture(scope="session")
def registry(scenario):
    return build_registry(scenario)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def agent_config(scenario) -> AgentConfig:
    return scenario.agent()


def make_ctx(store, *, caps=("sql:read", "docs:read", "math", "email:send"), guardrails=True, tainted=False,
             retry: RetryPolicy | None = None, deadline_s: float = 30.0, execution_id: str = "exe_test") -> CallContext:
    return CallContext(
        execution_id=execution_id, agent_id="tester", capabilities=frozenset(caps),
        guardrails=GuardrailsConfig(enabled=guardrails, spotlighting=guardrails),
        retry=retry or RetryPolicy(base_delay_s=0.001, max_delay_s=0.002),
        deadline=time.monotonic() + deadline_s, tracer=Tracer(store, execution_id, ephemeral=True), tainted=tainted,
    )


@pytest.fixture
def gateway(registry, store) -> Gateway:
    return Gateway(registry, store)


@pytest.fixture(scope="session")
def mongo_store():
    """Real MongoDB in a throwaway database. Skips locally if unreachable; fails if AW_REQUIRE_MONGO=1."""
    from agentwarden.config import Settings
    from agentwarden.store import MongoStore, StoreUnavailable

    settings = Settings()
    store = MongoStore(settings.resolved_mongo_uri(), "agentwarden_test", timeout_ms=2000)
    try:
        store.ping()
    except StoreUnavailable as e:
        if os.environ.get("AW_REQUIRE_MONGO") == "1":
            pytest.fail(f"MongoDB required but unreachable: {e}")
        pytest.skip(f"MongoDB unreachable: {e}")
    store._client.drop_database("agentwarden_test")
    store.ensure_indexes()
    yield store
    store._client.drop_database("agentwarden_test")
    store.close()
