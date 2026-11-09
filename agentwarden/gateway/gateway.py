"""The Tool Gateway — the single choke point between the model and any tool.

Pipeline for every call (order is deliberate; see docs/PROJECT_PLAN.md §3.3):
  1. unknown tool?                       -> deny (never shadowed)
  2. per-execution call cap / rate limit -> deny (never shadowed: protects backends)
  3. argument schema validation          -> structured error back to the model (never shadowed)
  4. policy: capability, tool policy, taint, approval  -> enforced, or shadow-logged when guardrails OFF
  5. audit record (every call, whatever the outcome)
  6. execute: fault injection + timeout + retry (transient errors on idempotent tools only)
  7. post-process: truncate, mark context tainted if output is untrusted, spotlight-wrap
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from agentwarden.config import GuardrailsConfig, RetryPolicy
from agentwarden.gateway import policy as pol
from agentwarden.gateway.approvals import Approver, AutoDenyApprover, request_approval
from agentwarden.gateway.ratelimit import RateLimiter
from agentwarden.gateway.registry import ToolRegistry
from agentwarden.gateway.spec import ToolContext, ToolOutput, ToolSpec, Trust
from agentwarden.ids import new_id
from agentwarden.observability import Tracer
from agentwarden.reliability.errors import PermanentToolError, ToolError, ToolTimeout
from agentwarden.reliability.faults import FaultInjector
from agentwarden.reliability.retry import RetryOutcome, call_with_retry
from agentwarden.security.untrusted import new_boundary, wrap_untrusted
from agentwarden.store.base import PlatformStore

POLICY_RULE_PREFIXES = ("R-CAP", "R-TOOL", "R-TAINT", "R-APPROVAL")


@dataclass
class CallContext:
    """Per-execution gateway state."""
    execution_id: str
    agent_id: str
    capabilities: frozenset[str]
    guardrails: GuardrailsConfig
    retry: RetryPolicy
    deadline: float                      # time.monotonic() value
    tracer: Tracer
    fault_scope: str = ""                # eval task id, for deterministic fault injection
    approver: Approver = field(default_factory=AutoDenyApprover)
    tainted: bool = False
    boundary: str = field(default_factory=new_boundary)
    calls_per_tool: dict[str, int] = field(default_factory=dict)


@dataclass
class Observation:
    call_id: str
    tool: str
    ok: bool
    content: str
    error_type: str | None = None
    rule_id: str | None = None
    executed: bool = False
    attempts: int = 0
    latency_ms: float = 0.0
    untrusted: bool = False


def denial_message(tool: str, rule_id: str, reason: str) -> str:
    """What the model sees when the gateway refuses a call. Explicit about the outcome, because a model told
    only "denied" was observed claiming the action had succeeded (eval task adv_06, task set v3)."""
    return (f"DENIED ({rule_id}): {reason}. The '{tool}' call was NOT executed and nothing was changed. "
            "Do not retry it. In your answer, tell the user plainly that this action was refused and did not "
            "happen; never describe it as completed.")


def _args_hash(raw: Any) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()


class Gateway:
    def __init__(
        self,
        registry: ToolRegistry,
        store: PlatformStore,
        *,
        faults: FaultInjector | None = None,
        rate_limiter: RateLimiter | None = None,
        max_workers: int = 4,
        rng: random.Random | None = None,
    ):
        self.registry = registry
        self.store = store
        self.faults = faults
        self.rate_limiter = rate_limiter or RateLimiter()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tool")
        self._rng = rng or random.Random()

    # -- helpers -----------------------------------------------------------------------------
    def _audit(self, ctx: CallContext, *, tool: str, call_id: str, raw_args: Any, decision: str, rule_id: str,
               reason: str, enforced: bool, outcome: str) -> None:
        self.store.insert_audit({
            "ts": datetime.now(UTC),
            "execution_id": ctx.execution_id,
            "agent_id": ctx.agent_id,
            "call_id": call_id,
            "tool": tool,
            "decision": decision,
            "rule_id": rule_id,
            "reason": reason,
            "enforced": enforced,
            "outcome": outcome,
            "guardrails_enabled": ctx.guardrails.enabled,
            "capabilities": sorted(ctx.capabilities),
            "args_sha256": _args_hash(raw_args),
        })
        ctx.tracer.emit("gateway_decision", tool=tool, call_id=call_id, decision=decision, rule_id=rule_id,
                        reason=reason, enforced=enforced, outcome=outcome)

    @staticmethod
    def _refuse(call_id: str, tool: str, error_type: str, rule_id: str, msg: str) -> Observation:
        return Observation(call_id, tool, ok=False, error_type=error_type, rule_id=rule_id,
                           content=denial_message(tool, rule_id, msg))

    # -- main entry point ----------------------------------------------------------------------
    def invoke(self, ctx: CallContext, call_id: str, name: str, raw_args: Any) -> Observation:
        ctx.tracer.emit("tool_call_requested", tool=name, call_id=call_id, args=raw_args)
        spec = self.registry.get(name)
        if spec is None:
            self._audit(ctx, tool=name, call_id=call_id, raw_args=raw_args, decision="deny",
                        rule_id="R-UNKNOWN", reason="no such tool", enforced=True, outcome="blocked")
            return self._refuse(call_id, name, "unknown_tool", "R-UNKNOWN",
                                f"tool '{name}' does not exist. Available: {self.registry.names()}")

        n = ctx.calls_per_tool.get(name, 0) + 1
        ctx.calls_per_tool[name] = n
        if (spec.max_calls_per_execution and n > spec.max_calls_per_execution) or not self.rate_limiter.allow(name):
            self._audit(ctx, tool=name, call_id=call_id, raw_args=raw_args, decision="deny",
                        rule_id="R-RATE", reason="rate limit exceeded", enforced=True, outcome="blocked")
            return self._refuse(call_id, name, "rate_limited", "R-RATE", "rate limit exceeded for this tool")

        if not isinstance(raw_args, dict):
            obs = Observation(call_id, name, ok=False, error_type="invalid_arguments", rule_id="R-SCHEMA",
                              content="ERROR (invalid_arguments): arguments must be a JSON object.")
            self._audit(ctx, tool=name, call_id=call_id, raw_args=raw_args, decision="deny", rule_id="R-SCHEMA",
                        reason="arguments not a JSON object", enforced=True, outcome="blocked")
            return obs
        try:
            args = spec.args_model.model_validate(raw_args)
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, er['loc'])) or '<root>'}: {er['msg']}" for er in e.errors())
            self._audit(ctx, tool=name, call_id=call_id, raw_args=raw_args, decision="deny", rule_id="R-SCHEMA",
                        reason=problems[:300], enforced=True, outcome="blocked")
            return Observation(call_id, name, ok=False, error_type="invalid_arguments", rule_id="R-SCHEMA",
                               content=f"ERROR (invalid_arguments): {problems}. Fix the arguments and try again.")

        decision = pol.evaluate(spec, args, capabilities=ctx.capabilities, tainted=ctx.tainted)
        if decision.action == "allow":
            self._audit(ctx, tool=name, call_id=call_id, raw_args=raw_args, decision="allow",
                        rule_id=decision.rule_id, reason=decision.reason, enforced=True, outcome="executed")
        elif not ctx.guardrails.enabled:
            # Shadow mode: record what WOULD have happened, then allow (guardrails-OFF baseline).
            self._audit(ctx, tool=name, call_id=call_id, raw_args=raw_args, decision=decision.action,
                        rule_id=decision.rule_id, reason=decision.reason, enforced=False, outcome="executed")
        elif decision.action == "deny":
            self._audit(ctx, tool=name, call_id=call_id, raw_args=raw_args, decision="deny",
                        rule_id=decision.rule_id, reason=decision.reason, enforced=True, outcome="blocked")
            return self._refuse(call_id, name, "policy_denied", decision.rule_id, decision.reason)
        else:  # escalate
            approved, approval_id = request_approval(self.store, ctx.approver, execution_id=ctx.execution_id,
                                                     tool=name, args=raw_args, reason=decision.reason)
            self._audit(ctx, tool=name, call_id=call_id, raw_args=raw_args, decision="escalate",
                        rule_id=decision.rule_id, reason=f"{decision.reason} (approval {approval_id})",
                        enforced=True, outcome="executed" if approved else "blocked")
            if not approved:
                return self._refuse(call_id, name, "approval_denied", decision.rule_id,
                                    f"{decision.reason}; approval was denied")

        return self._execute(ctx, spec, args, call_id, ordinal=n)

    # -- execution with reliability -------------------------------------------------------------
    def _execute(self, ctx: CallContext, spec: ToolSpec, args: Any, call_id: str, *, ordinal: int) -> Observation:
        tool_ctx = ToolContext(
            execution_id=ctx.execution_id,
            agent_id=ctx.agent_id,
            record_effect=lambda tool, payload: self.store.insert_effect(
                {"execution_id": ctx.execution_id, "tool": tool, "payload": payload, "ts": datetime.now(UTC)}),
        )
        outcome = RetryOutcome()

        def attempt(k: int) -> ToolOutput:
            remaining = ctx.deadline - time.monotonic()
            if remaining <= 0:
                raise PermanentToolError("execution deadline reached before tool call")
            t0 = time.perf_counter()
            try:
                if self.faults:
                    self.faults.maybe_fail(scope=ctx.fault_scope, tool=spec.name, ordinal=ordinal, attempt=k)
                fut = self._pool.submit(spec.fn, args, tool_ctx)
                try:
                    out = fut.result(timeout=min(spec.timeout_s, remaining))
                except FutureTimeout as e:
                    fut.cancel()  # threads cannot be killed; an already-running call is abandoned
                    raise ToolTimeout(f"{spec.name} exceeded {spec.timeout_s:.1f}s") from e
                ctx.tracer.emit("tool_attempt", tool=spec.name, call_id=call_id, attempt=k, ok=True,
                                duration_ms=(time.perf_counter() - t0) * 1000)
                return out
            except Exception as exc:
                err = exc if isinstance(exc, ToolError) else PermanentToolError(f"{type(exc).__name__}: {exc}")
                ctx.tracer.emit("tool_attempt", tool=spec.name, call_id=call_id, attempt=k, ok=False,
                                error_type=err.error_type, error=str(err)[:300],
                                duration_ms=(time.perf_counter() - t0) * 1000)
                raise err from exc

        def on_retry(k: int, exc: BaseException, delay: float) -> None:
            ctx.tracer.emit("retry_scheduled", tool=spec.name, call_id=call_id, after_attempt=k,
                            delay_s=round(delay, 3), error=str(exc)[:200])

        t0 = time.perf_counter()
        try:
            out = call_with_retry(attempt, policy=ctx.retry, retryable=spec.idempotent, deadline=ctx.deadline,
                                  outcome=outcome, rng=self._rng, on_retry=on_retry)
        except ToolError as e:
            latency = (time.perf_counter() - t0) * 1000
            hint = " This looks transient; you may retry." if e.transient else ""
            ctx.tracer.emit("tool_result", tool=spec.name, call_id=call_id, ok=False, error_type=e.error_type,
                            attempts=outcome.attempts, duration_ms=latency)
            return Observation(call_id, spec.name, ok=False, error_type=e.error_type, executed=True,
                               attempts=outcome.attempts, latency_ms=latency,
                               content=f"ERROR ({e.error_type}): {e}.{hint}")
        latency = (time.perf_counter() - t0) * 1000

        text = out.content
        if len(text) > spec.max_output_chars:
            text = text[: spec.max_output_chars] + f"\n...[truncated to {spec.max_output_chars} chars]"
        untrusted = spec.output_trust is Trust.UNTRUSTED
        if untrusted:
            ctx.tainted = True
            if ctx.guardrails.enabled and ctx.guardrails.spotlighting:
                text = wrap_untrusted(text, source=spec.name, boundary=ctx.boundary)
        ctx.tracer.emit("tool_result", tool=spec.name, call_id=call_id, ok=True, attempts=outcome.attempts,
                        untrusted=untrusted, output=out.content[:2000], meta=out.meta, duration_ms=latency)
        return Observation(call_id, spec.name, ok=True, content=text, executed=True, attempts=outcome.attempts,
                           latency_ms=latency, untrusted=untrusted)


def new_call_id() -> str:
    return new_id("call")
