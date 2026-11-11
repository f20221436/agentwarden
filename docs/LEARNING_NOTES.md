# AgentWarden — Learning Notes

Offline revision notes. One section per completed layer: what was built, how it works, key code, concepts,
how to test, limitations, and interview questions with short model answers.

## Layer 0: Architecture principles

- **The LLM is an untrusted planner.** It proposes tool calls; a deterministic gateway decides. Security that
  depends on asking an LLM "is this safe?" inherits the LLM's failure modes.
- **One choke point.** Every tool call passes through `Gateway.invoke`: one place to enforce, audit,
  rate-limit, and later add transports (MCP).
- **Defence in depth.** SQL writes are blocked by three independent layers; one bug is not enough.
- **Measure, don't assert.** Claims are backed by eval runs stored in MongoDB; RESULTS.md is generated.

**Q: Why not let the model decide whether a tool call is safe?** The model is the component under attack;
injected text can change its judgement. Deterministic rules cannot be talked out of a decision.
**Q: Why a hand-written loop instead of LangGraph?** The control layer (limits, retries, termination, tracing)
*is* the product; owning it makes every behaviour testable and explainable. LangGraph adds checkpointing and
graph orchestration we do not need for one agent; revisit for multi-agent.
**Q: Why two databases?** Different trust boundaries. The agent may read business data (SQLite, read-only).
Only the runtime writes platform data (MongoDB); the agent has no tool that can reach its own audit log.

---

## Layer 1: Repo, config, MongoDB platform store

**Built.** `pyproject.toml`, env-based `Settings` (`AW_*`, `.env`), Docker Compose for Atlas Local bound to
127.0.0.1 with credentials from `.env`, `MongoStore` with index bootstrap, an in-memory twin for unit tests.

**How it works.** `store.connect()` pings MongoDB and creates indexes; if MongoDB is down it raises
`StoreUnavailable` and **no execution starts** (fail closed: no unaudited runs).
Indexes (`agentwarden/store/mongo.py: INDEXES`): `steps(execution_id, seq)` unique — ordered trace and
duplicate protection; `steps.ts` **TTL with `partialFilterExpression {ephemeral: true}`** — ad-hoc CLI traces
expire after 30 days, eval traces are kept; `eval_results(run_id, task_id)` unique — one result per task per
run; `audit_log` has **no TTL** (audit retention ≠ debug retention).

**Key code.** `agentwarden/store/mongo.py`, `agentwarden/config.py`, `docker-compose.yml`.

**Concepts.** Embed vs reference: the agent-config snapshot is *embedded* in `executions` (immutable, always
read together); trace steps are *referenced* (unbounded, written incrementally, 16 MB document limit).
Partial TTL indexes. Fail-closed vs fail-open.

**Test.** `pytest tests/test_mongo_integration.py -q` → indexes exist with the right options.

**Limitations.** One Mongo user for the whole runtime (M2: an insert-only role for `audit_log`).

**Interview questions**
1. *Why is `steps` a separate collection instead of an array in `executions`?* Unbounded growth (16 MB doc
   limit), and incremental inserts survive crashes; an array would need read-modify-write of a growing doc.
2. *Why a partial TTL?* So retention differs by purpose in one collection: debug traces expire, evidence for
   published results does not.
3. *Why fail closed when Mongo is down?* An unaudited agent run is worse than no run for a security product.

---

## Layer 2: Scenario pack (seeded data + documents + injections)

**Built.** `scenarios/saas_demo/generate.py` builds a SQLite DB (6,000 users, ~40k orders, 420 tickets, 3
experiments) from `random.Random(42)`; byte-identical on regeneration (checked in tests and CI).
Planted effects: `checkout_redesign` real lift (z≈3.25), `pricing_page_copy` no effect (z≈1.58),
`onboarding_email_v2` sample-ratio mismatch (45.6% treatment vs 50% intended). Ten markdown documents, three
with prompt-injection payloads; two support tickets (ids 77, 151) carry *data-borne* injections.

**Concepts.** Reproducibility via seeded RNG and no `now()`; ground truth computed from the data
(`expected_sql`) rather than typed numbers; indirect vs direct prompt injection; data-borne injection via
database rows (why `sql_query` output is marked untrusted).

**Interview question.** *Why plant a null-effect experiment?* To test that the agent does not over-claim:
an observed +18% lift that is not significant is a realistic trap.

---

## Layer 3: Model providers + compatibility probe

**Built.** `OllamaProvider` (native `/api/chat`), `OpenAICompatProvider` (opt-in cloud), `ScriptedProvider`
(tests only; the eval runner refuses it), and `agentwarden probe` which measures structured tool-call rate,
argument validity, correct-tool rate and latency on 10 fixed prompts.

**Concepts.** Provider errors are classified transient (timeouts, 429, 5xx) vs permanent (400/401). Native
Ollama API exposes `num_ctx`, `keep_alive`, `think` and exact token counts. Ollama reuses the KV cache for an
identical prompt prefix, which is why a stable system prompt + tool schema makes later calls much faster.

**Interview question.** *Why test tool calling instead of trusting the model card?* Small local models often
answer in prose or emit JSON as text; the probe measured `qwen2.5:3b` at 90% structured calls — one in ten
first turns skipped the tool entirely.

---

## Layer 4: Tool gateway + tools

**Built.** `ToolSpec` (transport-neutral, MCP-shaped metadata), `ToolRegistry`, `Gateway.invoke`, policy
engine, approvals, token-bucket rate limiter, five tools (`sql_query`, `doc_search`, `calculator`, honeypots
`send_email`, `issue_refund`).

**Pipeline** (`agentwarden/gateway/gateway.py`): unknown tool → per-execution cap / rate limit → schema
validation (Pydantic, `extra="forbid"`) → policy (capability `R-CAP`, tool policy `R-TOOL:*`, taint `R-TAINT`,
approval `R-APPROVAL`) → audit → execute with fault injection, timeout, retry → truncate, taint, spotlight.

**SQL in three layers** (`agentwarden/security/sql_guard.py`): (1) sqlglot AST allowlist; (2) `mode=ro` +
`PRAGMA query_only`; (3) SQLite authorizer allowing only SELECT/READ and allowlisted functions.
**Measured finding:** layer 2 alone still lets `ATTACH DATABASE 'x.db'` create an empty file on disk; layer 3
blocks it. That is a concrete, tested reason for defence in depth.

**Concepts.** Capability-based access control; least privilege; "catalog visibility ≠ permission" (the model
sees `issue_refund` but lacks `payments:write`); taint tracking (once untrusted text is in context, side
effects need a human); shadow mode (evaluate + log policy without enforcing, for fair ON/OFF comparison);
spotlighting with an unguessable per-execution boundary.

**Test.** `pytest tests/test_gateway.py tests/test_sql_guard.py -q`.

**Limitations.** Python threads cannot be killed: a timed-out non-SQL tool is abandoned, not stopped (SQL is
truly cancelled via the progress handler). Spotlighting is probabilistic.

**Interview questions**
1. *Why validate authorization before arguments?* Do not reveal tool internals (error messages, schema) to a
   caller who is not allowed to use the tool, and avoid wasted work.
2. *What does the taint rule prevent and what does it cost?* It prevents untrusted content from triggering
   side effects without a human; it also blocks legitimate "read docs then email a summary" flows unless
   approved.
3. *Why is SQL output untrusted?* Rows contain customer-written text (tickets); that is an injection vector.

---

## Layer 5: Agent loop

**Built.** `agentwarden/runtime/agent.py`: explicit state machine with `max_steps`, `max_tool_calls`,
`deadline_s`, `max_total_tokens`, `max_consecutive_tool_errors`, loop detection, model-call retries, one nudge
for empty answers, `<think>` stripping, and a single exit path that always persists status + reason.

**Loop detection.** Signature = tool name + canonical JSON args. Only *successful* identical calls count
(a model retrying a failed call is not looping). 2nd identical call → cached result + note; 3rd → terminate
`loop_detected`.

**Interview question.** *What does "safe termination" mean here?* Every exit — success, limit, model error,
internal exception — produces a recorded status, reason, partial answer, counters and a final trace event;
nothing escapes unrecorded.

---

## Layer 6: Reliability engine

**Built.** Error taxonomy (`reliability/errors.py`), retry with exponential backoff and **full jitter**,
bounded by the execution deadline (`reliability/retry.py`), deterministic fault injection
(`reliability/faults.py`).

**Concepts.** Retry only transient errors and only idempotent operations (`send_email` is never retried —
a retry after an ambiguous timeout could send twice). Full jitter spreads retries to avoid synchronized
retry storms. Fault decisions are a hash of (seed, task, tool, call ordinal, attempt) so ON and OFF runs see
the *same* faults — a controlled comparison.

**Interview question.** *Why is an SQL timeout classified as permanent?* The same expensive query will time
out again; retrying wastes the deadline. A dropped connection is transient; a slow query is not.

---

## Layer 6b: Cloud provider, rate limits and cost

**Built.** Groq through the OpenAI-compatible adapter; `parse_retry_after` (`Retry-After` seconds or HTTP
date, then `x-ratelimit-reset-requests/tokens` like `2m59.56s`); `ProviderError.retry_after_s` /
`quota_exhausted`; retry loop that waits `max(backoff, server_wait)` and fails fast above a 65 s cap; eval runs
that abort on quota; `agentwarden/pricing.yaml` with provenance; split `tool_retries` / `model_retries`.

**Concepts.** A 429 is a *request from the server*, not just an error: retrying sooner wastes quota and can
extend a ban. Per-minute limits (RPM/TPM) are transient; per-day limits (RPD/TPD) are not transient within a
task. Separating infrastructure failures from agent failures keeps benchmark numbers honest.
Configuration hygiene: when comparing "reliability ON vs OFF", only toggle the mechanism under test.

**Test.** `pytest tests/test_cloud_and_pricing.py -q` (mocked HTTP; no key needed).

**Interview questions**
1. *Why not just use exponential backoff for 429s?* The server knows when capacity returns; `Retry-After` is
   more accurate than a guess, and retrying early consumes quota. Backoff is the fallback when no header exists.
2. *Why abort the whole eval run on daily-quota exhaustion?* Every remaining task would fail for an
   infrastructure reason and drag the completion rate down; that would measure the account, not the agent.
3. *Why are unknown prices `n/a` and not $0?* $0 would be a fabricated number that makes a model look free.
4. *What went wrong with the first version of the reset-header parser?* It took the larger of
   `x-ratelimit-reset-requests` and `x-ratelimit-reset-tokens`. On Groq the *requests* budget is per **day**
   (reset 1m26s with 999 of 1,000 left), so every per-minute token throttle would have looked like a >65 s
   wait and aborted the run as "quota exhausted". Fix: a reset header only counts when its matching
   `remaining` header is 0. Found by reading real headers from one test call before the benchmark: always
   look at real provider responses before trusting a parser written from documentation.

## Layer 8: What the measurements say (read RESULTS.md for the numbers)

Numbers below are quoted from `docs/RESULTS.md` as generated on 2026-11-11; the file is the source of truth.

1. **Deterministic controls matter most when the model misbehaves.** `qwen2.5:3b` attempted policy-violating
   calls (DELETE, ATTACH, emailing customer data to an external domain); the gateway blocked 8 of 8 with
   guardrails on, and with guardrails off the external email went through (into the sandbox). Attack success
   fell from 25% to 6% (2 runs per side).
2. **A stronger model may never trigger them.** `openai/gpt-oss-120b` on Groq made **zero** unauthorized
   attempts in both guardrails ON and OFF. The gateway still matters: it is the guarantee for the cases the
   model gets wrong, and you cannot know in advance which model or prompt will.
3. **Guardrails constrain actions, not beliefs.** The answer-manipulation task (forged "365-day refund"
   document) succeeded on both models with guardrails on and off. No authorization rule can stop a model from
   believing a document; mitigations are provenance (trusted-source metadata) and retrieval design (M2).
4. **Models can fabricate success.** With a terse "DENIED" message the 3B model replied "after deleting all
   churned users, 4947 remain". Denials now say explicitly that nothing happened, and evals check for it.
5. **Measure the infrastructure too.** A laptop sleep (Modern Standby) and a daily token quota both produced
   failures that were not agent behaviour. They are annotated or aborted, never counted silently.
6. **Small samples, and pick the metric the mechanism acts on.** 30 tasks, 1-2 runs: a one-task flip is
   3.3 pp. Under 20% injected tool faults (local, 2 vs 2 runs), retries raised *tool-execution success* from
   70% to 81% with every ON run above every OFF run: that is claimed. *Task completion* moved 58% → 63% but the
   runs overlapped (both second runs 60.0%), so it is not claimed. Retries recover tool calls directly; whether
   that turns into a correct final answer also depends on the 3B model's reasoning, which adds noise. Groq
   (1 run each): 83.3% vs 80.0% completion, not claimed.

**Interview questions**
1. *Your guardrails show no effect on gpt-oss-120b. Are they useless?* No: they showed no effect *on these
   attacks for this model*. They bound the worst case independent of model quality, and the 3B results show
   what happens when a model does comply with an attack.
2. *Why did answer manipulation succeed with guardrails on?* It needs no tool call: the attack changes what
   the model says, and the gateway only governs actions. That is documented as a limitation (THREAT_MODEL T2).
3. *How do you know a failure was the laptop sleeping and not the model?* The step trace showed a retry
   scheduled with an 8 s wait followed by the next event 252 s later; the Windows System log shows Kernel-Power
   506/507 (enter/exit Modern Standby) exactly spanning that gap.
4. *You changed the claim rule after seeing the data. Isn't that p-hacking?* The change only makes claims
   stricter (it removed the completion claim). Changing a rule after the fact to *add* a claim would be.
   It is recorded as decision D21 with that caveat.
5. *Why claim tool-execution success rather than completion?* It is the quantity retries act on: an injected
   transient fault either gets recovered or reaches the model as an error. Completion is downstream and noisier.
   Both are reported; only the separated one is claimed.

## Layer 7: Evaluation engine

**Built.** Task schema + loader, deterministic checks (`evals/checks.py`), runner with presets
(`baseline`, `guardrails_off`, `faults20_reliability_on/off`), aggregation-pipeline metrics
(`evals/metrics.py`, incl. `$percentile`), and the RESULTS.md generator (`evals/report.py`).

**Integrity mechanics.** Every run records git SHA + dirty flag, model, config snapshot, scenario, task-set
version, seed, fault settings, DB hash. Subset runs never enter summaries or claims. CV claims are emitted
only when the data supports them. Task changes bump the version (v1→v2 after a scoring bug: "Canada" vs
stored code "CA").

**Interview question.** *How do you know RESULTS.md is not hand-edited?* It is regenerated from MongoDB by
`agentwarden report`; an integration test inserts known results and asserts the exact rendered numbers.
