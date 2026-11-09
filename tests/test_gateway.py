"""Gateway: permissions, validation, taint, approvals, shadow mode, audit, spotlighting, reliability."""

from __future__ import annotations

from agentwarden.config import FaultConfig, RetryPolicy
from agentwarden.gateway.approvals import AutoApproveApprover
from agentwarden.gateway.gateway import Gateway
from agentwarden.gateway.ratelimit import RateLimiter, TokenBucket
from agentwarden.reliability.faults import FaultInjector
from tests.conftest import make_ctx


def test_allowed_sql_call_executes_and_is_audited(gateway, store):
    ctx = make_ctx(store)
    obs = gateway.invoke(ctx, "c1", "sql_query", {"query": "SELECT COUNT(*) FROM users"})
    assert obs.ok and obs.executed and "6000" in obs.content
    audit = store.audit_for("exe_test")
    assert len(audit) == 1 and audit[0]["decision"] == "allow" and audit[0]["outcome"] == "executed"
    assert "SELECT COUNT" not in str(audit[0])   # audit stores an args hash, not the args
    assert len(audit[0]["args_sha256"]) == 64


def test_unknown_tool_denied_and_audited(gateway, store):
    obs = gateway.invoke(make_ctx(store), "c1", "rm_rf", {})
    assert not obs.ok and obs.error_type == "unknown_tool"
    assert store.audit_for("exe_test")[0]["rule_id"] == "R-UNKNOWN"


def test_missing_capability_denied(gateway, store):
    ctx = make_ctx(store)  # default caps do not include payments:write
    obs = gateway.invoke(ctx, "c1", "issue_refund", {"order_id": 1, "amount_cents": 100, "reason": "x"})
    assert not obs.ok and obs.error_type == "policy_denied" and obs.rule_id == "R-CAP"
    assert store.collections["sandbox_effects"] == []
    a = store.audit_for("exe_test")[0]
    assert a["decision"] == "deny" and a["enforced"] and a["outcome"] == "blocked"


def test_invalid_arguments_rejected_with_actionable_error(gateway, store):
    obs = gateway.invoke(make_ctx(store), "c1", "sql_query", {"sql": "SELECT 1"})
    assert not obs.ok and obs.error_type == "invalid_arguments" and "query" in obs.content
    obs = gateway.invoke(make_ctx(store, execution_id="exe_2"), "c2", "calculator", "not-a-dict")
    assert obs.error_type == "invalid_arguments"


def test_sql_write_blocked_by_policy_layer(gateway, store):
    obs = gateway.invoke(make_ctx(store), "c1", "sql_query", {"query": "DELETE FROM users"})
    assert not obs.ok and obs.rule_id == "R-TOOL:SQL_READONLY" and not obs.executed


def test_sql_write_in_shadow_mode_is_stopped_by_engine(gateway, store, db_path):
    before = db_path.read_bytes()
    ctx = make_ctx(store, guardrails=False)
    obs = gateway.invoke(ctx, "c1", "sql_query", {"query": "DELETE FROM users"})
    assert not obs.ok and obs.executed and "read-only database engine" in obs.content
    a = store.audit_for("exe_test")[0]
    assert a["decision"] == "deny" and a["enforced"] is False  # shadow-logged
    assert db_path.read_bytes() == before


def test_side_effect_tool_requires_approval_and_auto_deny_blocks(gateway, store):
    ctx = make_ctx(store)
    obs = gateway.invoke(ctx, "c1", "send_email", {"to": "ops@lumora.example", "subject": "s", "body": "b"})
    assert not obs.ok and obs.error_type == "approval_denied" and obs.rule_id == "R-APPROVAL"
    approvals = store.collections["approvals"]
    assert len(approvals) == 1 and approvals[0]["status"] == "denied"
    assert store.collections["sandbox_effects"] == []


def test_approved_side_effect_executes_in_sandbox(registry, store):
    gw = Gateway(registry, store)
    ctx = make_ctx(store)
    ctx.approver = AutoApproveApprover()
    obs = gw.invoke(ctx, "c1", "send_email", {"to": "ops@lumora.example", "subject": "s", "body": "b"})
    assert obs.ok and "sandbox" in obs.content
    assert store.collections["approvals"][0]["status"] == "approved"
    assert len(store.collections["sandbox_effects"]) == 1


def test_external_recipient_denied_before_approval(gateway, store):
    obs = gateway.invoke(make_ctx(store), "c1", "send_email",
                         {"to": "audit@data-collector.example", "subject": "s", "body": "b"})
    assert obs.rule_id == "R-TOOL:RECIPIENT_DOMAIN"
    assert store.collections["approvals"] == []  # denied deterministically, never even asked a human


def test_untrusted_output_taints_context_and_is_spotlighted(gateway, store):
    ctx = make_ctx(store)
    obs = gateway.invoke(ctx, "c1", "doc_search", {"query": "partner data sync"})
    assert obs.ok and obs.untrusted and ctx.tainted
    assert f"<<UNTRUSTED_DATA source=doc_search id={ctx.boundary}>>" in obs.content


def test_taint_rule_escalates_side_effects_even_when_approval_would_pass(registry, store):
    gw = Gateway(registry, store)
    ctx = make_ctx(store, tainted=True)
    obs = gw.invoke(ctx, "c1", "send_email", {"to": "ops@lumora.example", "subject": "s", "body": "b"})
    assert obs.rule_id == "R-TAINT" and not obs.ok


def test_shadow_mode_executes_but_records_would_deny(gateway, store):
    ctx = make_ctx(store, guardrails=False)
    obs = gateway.invoke(ctx, "c1", "issue_refund", {"order_id": 4242, "amount_cents": 99900, "reason": "x"})
    assert obs.ok  # guardrails OFF: executed (into the sandbox)
    a = store.audit_for("exe_test")[0]
    assert a["rule_id"] == "R-CAP" and a["enforced"] is False and a["outcome"] == "executed"
    assert len(store.collections["sandbox_effects"]) == 1


def test_no_spotlighting_when_guardrails_off(gateway, store):
    ctx = make_ctx(store, guardrails=False)
    obs = gateway.invoke(ctx, "c1", "doc_search", {"query": "refund"})
    assert "UNTRUSTED_DATA" not in obs.content and ctx.tainted


def test_transient_faults_are_retried(registry, store):
    gw = Gateway(registry, store, faults=FaultInjector(FaultConfig(failure_rate=1.0), seed=1))
    ctx = make_ctx(store, retry=RetryPolicy(max_attempts=3, base_delay_s=0.001, max_delay_s=0.001))
    obs = gw.invoke(ctx, "c1", "calculator", {"expression": "1+1"})
    assert not obs.ok and obs.attempts == 3 and obs.error_type == "transient_error"
    assert sum(1 for e in ctx.tracer.events if e["type"] == "retry_scheduled") == 2


def test_retry_recovers_from_partial_faults(registry, store):
    # find a seed whose first attempt fails and a later one succeeds
    for seed in range(200):
        inj = FaultInjector(FaultConfig(failure_rate=0.5), seed=seed)
        gw = Gateway(registry, store, faults=inj)
        ctx = make_ctx(store, retry=RetryPolicy(max_attempts=5, base_delay_s=0.001, max_delay_s=0.001),
                       execution_id=f"exe_{seed}")
        ctx.fault_scope = "t"
        obs = gw.invoke(ctx, "c1", "calculator", {"expression": "2*3"})
        if obs.ok and obs.attempts > 1:
            assert "6" in obs.content
            return
    raise AssertionError("no recovering seed found")


def test_non_idempotent_tool_not_retried(registry, store):
    gw = Gateway(registry, store, faults=FaultInjector(FaultConfig(failure_rate=1.0), seed=1))
    ctx = make_ctx(store, guardrails=False)  # let send_email reach execution
    obs = gw.invoke(ctx, "c1", "send_email", {"to": "ops@lumora.example", "subject": "s", "body": "b"})
    assert obs.attempts == 1 and not obs.ok


def test_per_execution_call_cap(gateway, store):
    ctx = make_ctx(store)
    for i in range(10):
        assert gateway.invoke(ctx, f"c{i}", "calculator", {"expression": f"{i}+1"}).ok
    obs = gateway.invoke(ctx, "c11", "calculator", {"expression": "1+1"})
    assert obs.error_type == "rate_limited"


def test_token_bucket():
    t = [0.0]
    b = TokenBucket(rate_per_s=1, burst=2, clock=lambda: t[0])
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()
    t[0] = 1.0
    assert b.try_acquire()
    rl = RateLimiter(rate_per_s=1, burst=1, clock=lambda: t[0])
    assert rl.allow("x") and not rl.allow("x") and rl.allow("y")


def test_tool_timeout_is_transient(registry, store):
    import time as _t

    from agentwarden.gateway.registry import ToolRegistry
    from agentwarden.gateway.spec import ToolOutput, ToolSpec
    from agentwarden.tools.calculator import CalcArgs

    reg = ToolRegistry()
    reg.register(ToolSpec(name="slow", description="", args_model=CalcArgs,
                          fn=lambda a, c: (_t.sleep(0.5), ToolOutput("late"))[1],
                          required_capabilities=frozenset(), timeout_s=0.05))
    gw = Gateway(reg, store)
    ctx = make_ctx(store, retry=RetryPolicy(max_attempts=2, base_delay_s=0.001, max_delay_s=0.001))
    obs = gw.invoke(ctx, "c1", "slow", {"expression": "1"})
    assert not obs.ok and obs.error_type == "timeout" and obs.attempts == 2
