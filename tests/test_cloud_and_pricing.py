"""Cloud provider rate limiting (429 / Retry-After), quota exhaustion, pricing, reliability presets."""

from __future__ import annotations

import random

import httpx
import pytest

from agentwarden.config import RetryPolicy
from agentwarden.evals.runner import preset
from agentwarden.gateway.gateway import Gateway, denial_message
from agentwarden.pricing import lookup
from agentwarden.providers import provider_name
from agentwarden.providers.base import (
    ChatMessage,
    ModelResponse,
    ProviderError,
    Usage,
    parse_duration,
    parse_retry_after,
)
from agentwarden.providers.openai_compat import OpenAICompatProvider
from agentwarden.providers.scripted import ScriptedProvider
from agentwarden.reliability.retry import RetryOutcome, call_with_retry
from agentwarden.runtime.agent import Agent

OK_BODY = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


@pytest.mark.parametrize("value,secs", [("7.66s", 7.66), ("2m59.56s", 179.56), ("1h2m", 3720), ("120ms", 0.12),
                                        ("12", 12.0), ("0.5", 0.5)])
def test_parse_duration(value, secs):
    assert parse_duration(value) == pytest.approx(secs)


def test_parse_retry_after_prefers_header_then_reset_headers():
    assert parse_retry_after({"Retry-After": "3"}) == 3
    assert parse_retry_after({"x-ratelimit-reset-requests": "2s", "x-ratelimit-reset-tokens": "7.5s"}) == 7.5
    assert parse_retry_after({}) is None


def test_reset_header_ignored_when_budget_not_exhausted():
    """Real Groq header shape (2026-11-10): the per-DAY request budget resets in 1m26s but 999 remain, so it
    must not count; otherwise every per-minute token throttle would look like quota exhaustion."""
    tpm_throttle = {"x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "999",
                    "x-ratelimit-reset-requests": "1m26.4s", "x-ratelimit-limit-tokens": "8000",
                    "x-ratelimit-remaining-tokens": "0", "x-ratelimit-reset-tokens": "7.882s"}
    assert parse_retry_after(tpm_throttle) == pytest.approx(7.882)
    not_blocked = {**tpm_throttle, "x-ratelimit-remaining-tokens": "6949"}
    assert parse_retry_after(not_blocked) is None
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0  # past date -> no wait


def _provider(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider("m", "https://api.groq.com/openai/v1", "test-key", name="groq",
                                client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_429_becomes_transient_error_with_retry_after():
    p = _provider(lambda req: httpx.Response(429, headers={"retry-after": "4"},
                                             json={"error": {"message": "Rate limit reached: tokens per minute (TPM)"}}))
    with pytest.raises(ProviderError) as ei:
        p.chat([ChatMessage(role="user", content="x")], [], timeout_s=5)
    e = ei.value
    assert e.transient and e.status == 429 and e.retry_after_s == 4 and not e.quota_exhausted
    assert "test-key" not in str(e)


def test_daily_quota_429_is_not_transient():
    p = _provider(lambda req: httpx.Response(429, headers={"retry-after": "3000"},
                                             json={"error": {"message": "Rate limit reached: tokens per day (TPD)"}}))
    with pytest.raises(ProviderError) as ei:
        p.chat([ChatMessage(role="user", content="x")], [], timeout_s=5)
    assert ei.value.quota_exhausted and not ei.value.transient


def test_success_parses_usage_and_sends_bearer_key():
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json=OK_BODY)
    r = _provider(handler).chat([ChatMessage(role="user", content="x")], [], timeout_s=5)
    assert r.content == "hi" and r.usage.input_tokens == 1000 and seen["auth"] == "Bearer test-key"


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.sleeps.append(d)
        self.t += d


def test_retry_waits_at_least_retry_after():
    clock = Clock()
    calls = {"n": 0}

    def fn(k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("429", transient=True, status=429, retry_after_s=9.0)
        return "ok"
    out = call_with_retry(fn, policy=RetryPolicy(base_delay_s=0.1, max_delay_s=0.2), retryable=True, deadline=100,
                          outcome=RetryOutcome(), rng=random.Random(0), sleep=clock.sleep, clock=clock)
    assert out == "ok" and clock.sleeps == [9.0]


def test_retry_after_above_cap_fails_fast_and_flags_quota():
    clock = Clock()
    err = ProviderError("429", transient=True, status=429, retry_after_s=500.0)

    def fn(k):
        raise err
    with pytest.raises(ProviderError):
        call_with_retry(fn, policy=RetryPolicy(max_retry_after_s=60), retryable=True, deadline=10_000,
                        outcome=RetryOutcome(), sleep=clock.sleep, clock=clock)
    assert clock.sleeps == [] and err.quota_exhausted


def test_agent_reports_quota_exhaustion(registry, store, scenario):
    agent = Agent(provider=ScriptedProvider([ProviderError("TPD", transient=False, status=429, quota_exhausted=True)]),
                  gateway=Gateway(registry, store), store=store, config=scenario.agent(), company="Lumora")
    res = agent.run("q")
    assert res.status == "failed" and res.error_kind == "quota_exhausted"
    assert store.get_execution(res.execution_id)["error_kind"] == "quota_exhausted"


def test_reliability_off_keeps_model_retries_on():
    on, _, _ = preset("faults20_reliability_on")
    off, faults, _ = preset("faults20_reliability_off")
    rel = off["reliability"]
    assert not rel.tool_retries.enabled and not rel.loop_detection
    assert rel.model_retries.enabled and faults.failure_rate == 0.2 and on == {}


def test_pricing_lookup_and_cost(registry, store, scenario):
    assert lookup("ollama", "anything").input == 0.0
    p = lookup("groq", "llama-3.3-70b-versatile")
    assert p is not None and p.verified is False and p.source
    assert lookup("groq", "no-such-model") is None
    prov = ScriptedProvider([ModelResponse(content="done", usage=Usage(input_tokens=1_000_000,
                                                                        output_tokens=1_000_000))])
    prov.name = "groq"
    prov.model = "llama-3.3-70b-versatile"
    agent = Agent(provider=prov, gateway=Gateway(registry, store), store=store, config=scenario.agent(),
                  company="Lumora")
    res = agent.run("q")
    assert store.get_execution(res.execution_id)["est_cost_usd"] == pytest.approx(p.input + p.output)


def test_unknown_price_is_none_not_zero(registry, store, scenario):
    prov = ScriptedProvider([ModelResponse(content="done")])
    prov.name, prov.model = "groq", "mystery-model"
    agent = Agent(provider=prov, gateway=Gateway(registry, store), store=store, config=scenario.agent(),
                  company="Lumora")
    assert store.get_execution(agent.run("q").execution_id)["est_cost_usd"] is None


def test_provider_name_from_url():
    assert provider_name("https://api.groq.com/openai/v1") == "groq"
    assert provider_name("https://api.openai.com/v1") == "openai"


def test_denial_message_says_nothing_happened():
    m = denial_message("sql_query", "R-TOOL:SQL_READONLY", "only SELECT queries are allowed")
    assert "NOT executed" in m and "nothing was changed" in m and "never describe it as completed" in m
