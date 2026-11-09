"""Integration tests against a real MongoDB (marker: mongo)."""

from __future__ import annotations

import pytest

from agentwarden.evals.metrics import run_summary
from agentwarden.evals.report import build_report
from agentwarden.gateway.gateway import Gateway
from agentwarden.providers.base import ModelResponse, ToolCall
from agentwarden.providers.scripted import ScriptedProvider
from agentwarden.runtime.agent import Agent

pytestmark = pytest.mark.mongo


def test_indexes_created(mongo_store):
    steps = mongo_store.db.steps.index_information()
    assert steps["trace_order"]["unique"]
    assert steps["ttl_ephemeral"]["expireAfterSeconds"] == 30 * 24 * 3600
    assert dict(steps["ttl_ephemeral"]["partialFilterExpression"]) == {"ephemeral": True}
    assert mongo_store.db.eval_results.index_information()["run_task"]["unique"]
    assert "by_execution" in mongo_store.db.audit_log.index_information()


def test_execution_trace_and_audit_persisted(mongo_store, registry, scenario):
    steps = [ModelResponse(tool_calls=[ToolCall(id="1", name="sql_query", arguments={"query": "SELECT 1"})]),
             ModelResponse(tool_calls=[ToolCall(id="2", name="issue_refund",
                                                arguments={"order_id": 1, "amount_cents": 5, "reason": "x"})]),
             ModelResponse(content="done")]
    agent = Agent(provider=ScriptedProvider(steps), gateway=Gateway(registry, mongo_store), store=mongo_store,
                  config=scenario.agent(), company="Lumora")
    res = agent.run("q", ephemeral=True)
    doc = mongo_store.get_execution(res.execution_id)
    assert doc["status"] == "completed" and doc["counters"]["denied"] == 1
    trace = mongo_store.steps_for(res.execution_id)
    assert [e["seq"] for e in trace] == list(range(1, len(trace) + 1))
    assert all(e["ephemeral"] for e in trace)
    tool_results = [e for e in trace if e["type"] == "tool_result"]
    assert tool_results and tool_results[0]["duration_ms"] is not None  # latency recorded
    audit = mongo_store.audit_for(res.execution_id)
    assert [a["decision"] for a in audit] == ["allow", "deny"] and audit[1]["rule_id"] == "R-CAP"


def test_aggregation_metrics_and_report_match_stored_data(mongo_store):
    db = mongo_store.db
    rid = "run_test_metrics"
    db.eval_runs.insert_one({"_id": rid, "created_at": __import__("datetime").datetime.now(), "batch_id": "b1",
                             "config_name": "baseline", "model": "m", "provider": "ollama", "task_set": "dev",
                             "task_set_version": "vT", "seed": 1, "status": "completed", "full_task_set": True,
                             "git_sha": "abc1234", "fault_injection": {"failure_rate": 0.0}})
    rows = [
        dict(task_id="a", passed=True, adversarial=False, latency_ms=1000, tool_calls_executed=1,
             tool_calls_succeeded=1, attack_succeeded=False, attack_attempted=False),
        dict(task_id="b", passed=False, adversarial=True, latency_ms=3000, tool_calls_executed=2,
             tool_calls_succeeded=1, attack_succeeded=True, attack_attempted=True, failure_category="attack_succeeded"),
        dict(task_id="c", passed=True, adversarial=True, latency_ms=2000, tool_calls_executed=0,
             tool_calls_succeeded=0, attack_succeeded=False, attack_attempted=True),
    ]
    for r in rows:
        base = dict(run_id=rid, category="x", status="completed", tool_selection_correct=True, tool_calls_total=1,
                    unauthorized_attempts=1, unauthorized_blocked=1, violations_executed=0, sql_write_attempts=0,
                    tool_retries=0, model_retries=0, tool_errors=0, input_tokens=100, output_tokens=10,
                    est_cost_usd=0.0, execution_id="exe_" + r["task_id"], failure_category=None)
        db.eval_results.insert_one({**base, **r})
    s = run_summary(db, rid)
    assert s["tasks"] == 3 and s["completion_rate"] == pytest.approx(2 / 3)
    assert s["attack_success_rate"] == pytest.approx(0.5)
    assert s["tool_execution_success_rate"] == pytest.approx(2 / 3)
    assert s["latency_mean_ms"] == pytest.approx(2000)
    assert s["blocked_rate"] == 1.0 and s["tokens_per_successful_task"] == pytest.approx(110)
    report = build_report(db)
    assert "| baseline | ollama · m | vT (3 tasks) | 1 | 66.7% |" in report
    assert "`run_test_metrics`" in report and "attack_succeeded" in report
