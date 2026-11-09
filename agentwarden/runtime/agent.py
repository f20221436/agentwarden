"""The agent loop: an explicit, bounded state machine (Decision D1).

    CallModel --tool_calls--> ExecuteTools --(limits ok)--> CallModel
    CallModel --text--> Done
    any state --limit / loop / error--> Terminated   (always recorded; nothing escapes unrecorded)
"""

from __future__ import annotations

import json
import logging
import random
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from agentwarden.config import AgentConfig
from agentwarden.gateway.approvals import Approver, AutoDenyApprover
from agentwarden.gateway.gateway import CallContext, Gateway, Observation
from agentwarden.ids import new_id
from agentwarden.observability import Tracer
from agentwarden.providers.base import ChatMessage, ModelProvider, ModelResponse, ProviderError, Usage
from agentwarden.reliability.retry import RetryOutcome, call_with_retry
from agentwarden.runtime.prompts import system_prompt
from agentwarden.security.redact import redact_text
from agentwarden.store.base import PlatformStore

log = logging.getLogger(__name__)
_THINK = re.compile(r"<think>.*?</think>", re.S)
MODEL_CALL_TIMEOUT_S = 180.0


class Reason(StrEnum):
    COMPLETED = "completed"
    MAX_STEPS = "max_steps"
    MAX_TOOL_CALLS = "max_tool_calls"
    DEADLINE = "deadline_exceeded"
    TOKEN_BUDGET = "token_budget_exceeded"
    LOOP = "loop_detected"
    TOOL_ERRORS = "too_many_tool_errors"
    MODEL_ERROR = "model_error"
    INTERNAL_ERROR = "internal_error"


_FAILED = {Reason.MODEL_ERROR, Reason.INTERNAL_ERROR}


@dataclass
class ToolCallRecord:
    tool: str
    args: Any
    ok: bool
    executed: bool
    error_type: str | None
    rule_id: str | None
    attempts: int
    latency_ms: float
    reused: bool = False


@dataclass
class ExecutionResult:
    execution_id: str
    status: str                 # completed | terminated | failed
    termination_reason: str
    final_answer: str
    canary: str
    steps: int
    tool_calls: list[ToolCallRecord]
    usage: Usage
    model_retries: int
    duration_ms: float
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def tool_retries(self) -> int:
        return sum(max(tc.attempts - 1, 0) for tc in self.tool_calls)


def _canonical(name: str, args: Any) -> str:
    return name + ":" + json.dumps(args, sort_keys=True, default=str)


class Agent:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        gateway: Gateway,
        store: PlatformStore,
        config: AgentConfig,
        company: str,
        approver: Approver | None = None,
        git_sha: str | None = None,
        price_per_mtok: tuple[float, float] | None = None,  # (input, output) USD; None = unknown
    ):
        self.provider = provider
        self.gateway = gateway
        self.store = store
        self.config = config
        self.company = company
        self.approver = approver or AutoDenyApprover()
        self.git_sha = git_sha
        self.price_per_mtok = price_per_mtok
        self.tool_schemas = gateway.registry.schemas(config.tools)

    def _cost(self, usage: Usage) -> float | None:
        if self.provider.name == "ollama":
            return 0.0
        if self.price_per_mtok is None:
            return None
        pin, pout = self.price_per_mtok
        return round(usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout, 6)

    def run(self, prompt: str, *, task_id: str | None = None, eval_run_id: str | None = None,
            ephemeral: bool = True) -> ExecutionResult:
        cfg, lim = self.config, self.config.limits
        execution_id = new_id("exe")
        canary = f"AW-CANARY-{secrets.token_hex(4)}"
        t_start, wall_start = time.monotonic(), datetime.now(UTC)
        deadline = t_start + lim.deadline_s
        self.store.insert_execution({
            "_id": execution_id, "agent_id": cfg.agent_id, "provider": self.provider.name,
            "model": self.provider.model, "agent_config": cfg.model_dump(mode="json"), "status": "running",
            "started_at": wall_start, "eval_run_id": eval_run_id, "task_id": task_id, "git_sha": self.git_sha,
            "ephemeral": ephemeral, "prompt": redact_text(prompt),
        })
        tracer = Tracer(self.store, execution_id, ephemeral=ephemeral)
        ctx = CallContext(
            execution_id=execution_id, agent_id=cfg.agent_id, capabilities=frozenset(cfg.capabilities),
            guardrails=cfg.guardrails, retry=cfg.reliability.retries, deadline=deadline, tracer=tracer,
            fault_scope=task_id or execution_id, approver=self.approver,
        )
        messages = [
            ChatMessage(role="system", content=system_prompt(
                company=self.company, role=cfg.description or "an assistant", canary=canary,
                spotlighting=cfg.guardrails.enabled and cfg.guardrails.spotlighting)),
            ChatMessage(role="user", content=prompt),
        ]
        tracer.emit("execution_started", agent_id=cfg.agent_id, model=self.provider.model,
                    guardrails=cfg.guardrails.model_dump(), reliability=cfg.reliability.model_dump(),
                    task_id=task_id)

        steps = 0
        usage = Usage()
        records: list[ToolCallRecord] = []
        model_retries = 0
        consecutive_errors = 0
        ok_cache: dict[str, str] = {}
        repeat_count: dict[str, int] = {}
        nudged = False
        final = ""
        last_content = ""
        reason: Reason | None = None
        rng = random.Random(cfg.seed)

        try:
            while reason is None:
                if steps >= lim.max_steps:
                    reason = Reason.MAX_STEPS
                    break
                if time.monotonic() >= deadline:
                    reason = Reason.DEADLINE
                    break
                steps += 1
                outcome = RetryOutcome()
                resp = self._call_model(messages, ctx, deadline, outcome, rng)
                model_retries += outcome.attempts - 1
                usage = usage + resp.usage
                content = _THINK.sub("", resp.content).strip()
                last_content = content or last_content
                if usage.total > lim.max_total_tokens:
                    reason = Reason.TOKEN_BUDGET
                    break
                if not resp.tool_calls:
                    if not content and not nudged:
                        nudged = True
                        messages.append(ChatMessage(role="user", content="Please give your final answer now."))
                        continue
                    final = content
                    reason = Reason.COMPLETED
                    break

                messages.append(ChatMessage(role="assistant", content=content, tool_calls=resp.tool_calls))
                for tc in resp.tool_calls:
                    if len(records) >= lim.max_tool_calls:
                        reason = Reason.MAX_TOOL_CALLS
                        break
                    sig = _canonical(tc.name, tc.arguments)
                    if cfg.reliability.loop_detection and sig in ok_cache:
                        repeat_count[sig] += 1
                        tracer.emit("loop_detected" if repeat_count[sig] >= cfg.reliability.loop_repeat_limit
                                    else "loop_warning", tool=tc.name, repeats=repeat_count[sig])
                        if repeat_count[sig] >= cfg.reliability.loop_repeat_limit:
                            reason = Reason.LOOP
                            break
                        obs = Observation(tc.id, tc.name, ok=True, content=ok_cache[sig] +
                                          "\nNOTE: you already made this exact call; the result above is reused. "
                                          "Use it or give your final answer.")
                        records.append(ToolCallRecord(tc.name, tc.arguments, True, False, None, None, 0, 0.0, True))
                    else:
                        obs = self.gateway.invoke(ctx, tc.id, tc.name, tc.arguments)
                        records.append(ToolCallRecord(tc.name, tc.arguments, obs.ok, obs.executed, obs.error_type,
                                                      obs.rule_id, obs.attempts, obs.latency_ms))
                        if obs.ok:
                            ok_cache[sig] = obs.content
                            repeat_count[sig] = 1
                    messages.append(ChatMessage(role="tool", content=obs.content, tool_call_id=tc.id, name=tc.name))
                    consecutive_errors = 0 if obs.ok else consecutive_errors + 1
                    if consecutive_errors >= lim.max_consecutive_tool_errors:
                        reason = Reason.TOOL_ERRORS
                        break
        except ProviderError as e:
            reason = Reason.MODEL_ERROR
            tracer.emit("error", kind="model", error=str(e)[:300], transient=e.transient)
        except Exception as e:  # safe termination: record, never crash the caller silently
            log.exception("internal error in execution %s", execution_id)
            reason = Reason.INTERNAL_ERROR
            tracer.emit("error", kind="internal", error=f"{type(e).__name__}: {e}"[:300])

        assert reason is not None
        if reason is not Reason.COMPLETED:
            final = f"[stopped: {reason.value}] {last_content}".strip()
        status = "completed" if reason is Reason.COMPLETED else ("failed" if reason in _FAILED else "terminated")
        duration_ms = (time.monotonic() - t_start) * 1000
        result = ExecutionResult(
            execution_id=execution_id, status=status, termination_reason=reason.value, final_answer=final,
            canary=canary, steps=steps, tool_calls=records, usage=usage, model_retries=model_retries,
            duration_ms=duration_ms, events=tracer.events,
        )
        tracer.emit("execution_finished", status=status, termination_reason=reason.value,
                    duration_ms=duration_ms)
        self.store.update_execution(execution_id, {
            "status": status, "termination_reason": reason.value, "ended_at": datetime.now(UTC),
            "duration_ms": round(duration_ms, 1), "final_answer": redact_text(final),
            "counters": {
                "steps": steps, "tool_calls": len(records),
                "tool_errors": sum(1 for r in records if not r.ok),
                "denied": sum(1 for r in records if r.error_type in ("policy_denied", "approval_denied")),
                "tool_retries": result.tool_retries, "model_retries": model_retries,
            },
            "usage": usage.model_dump(), "est_cost_usd": self._cost(usage),
        })
        return result

    def _call_model(self, messages: list[ChatMessage], ctx: CallContext, deadline: float,
                    outcome: RetryOutcome, rng: random.Random) -> ModelResponse:
        tracer = ctx.tracer

        def attempt(k: int) -> ModelResponse:
            remaining = deadline - time.monotonic()
            if remaining <= 1:
                raise ProviderError("execution deadline reached", transient=False)
            tracer.emit("llm_request", attempt=k, n_messages=len(messages))
            resp = self.provider.chat(messages, self.tool_schemas, timeout_s=min(MODEL_CALL_TIMEOUT_S, remaining),
                                      temperature=self.config.temperature, seed=self.config.seed)
            tracer.emit("llm_response", duration_ms=resp.latency_ms, usage=resp.usage.model_dump(),
                        content=resp.content[:1500],
                        tool_calls=[{"name": t.name, "arguments": t.arguments} for t in resp.tool_calls],
                        finish_reason=resp.finish_reason)
            return resp

        def on_retry(k: int, exc: BaseException, delay: float) -> None:
            tracer.emit("retry_scheduled", target="model", after_attempt=k, delay_s=round(delay, 3),
                        error=str(exc)[:200])

        return call_with_retry(attempt, policy=self.config.reliability.retries, retryable=True, deadline=deadline,
                               outcome=outcome, rng=rng, on_retry=on_retry)
