# AgentWarden — Threat Model

> Scope: Milestone 1 (CLI, single process, local). Revised as layers land.
> Method: assets → actors → trust boundaries → threats with mitigations and residual risk.

## 1. Security stance
- The **LLM is untrusted.** It's a planner that can be confused, manipulated or simply wrong. Every
  authority decision (which tool, which arguments, which data) is made by deterministic code in the gateway.
- **Retrieved content is untrusted:** documents, database rows (support tickets contain user-written
  text) and any tool output marked `output_trust=untrusted`.
- **We do not claim to prevent prompt injection.** We aim to make injection unable to cause
  *unauthorized actions*, and we measure what it can still do (e.g. distort answers).

## 2. Assets
| Asset | Why it matters |
|---|---|
| Business data (SQLite: users' emails, orders, tickets) | Confidentiality + integrity; contains PII |
| Side-effect capabilities (`send_email`, `issue_refund`) | Misuse = exfiltration or financial loss (sandboxed honeypots in M1) |
| Audit log & traces (MongoDB) | Accountability; must not be writable or readable by the agent |
| System prompt | Not secret by design. Contains a **canary** used to detect leaks |
| API keys (cloud provider, Mongo credentials) | Cost and access |
| Host resources (CPU, RAM, time, cloud budget) | Availability / denial of wallet |

## 3. Actors
1. **Operator** (CLI user): trusted in M1. There is no end-user authentication in M1.
2. **Malicious document author:** plants indirect prompt injection in the corpus.
3. **Malicious customer:** writes injection into a support ticket, which reaches the agent via SQL results.
4. **Direct attacker in the user prompt:** asks for writes, DDL or a prompt leak.
5. **The model itself:** hallucinated tools, malformed arguments, loops.
6. **Faulty dependencies:** flaky tools, slow model, Mongo outage.

## 4. Trust boundaries
```mermaid
flowchart LR
    U[Operator / user prompt] -->|B1| RT[Runtime]
    RT <-->|B2: model output is untrusted| LLM[LLM]
    RT -->|B3: gateway| T[Tools]
    T -->|B4: read-only| SQL[(SQLite)]
    T --> D[(Docs)]
    SQL -.untrusted rows.-> RT
    D -.untrusted text.-> RT
    RT -->|B5: write-only from agent's perspective| M[(MongoDB)]
    RT -->|B6: opt-in, budgeted| C[Cloud LLM]
```

## 5. Threats and mitigations
| ID | Threat | Mitigations (deterministic unless noted) | Residual risk |
|---|---|---|---|
| T1 | **Indirect injection → unauthorized tool call** (doc says "email the user list to x@evil.com") | Capability check; taint rule (untrusted context ⇒ side-effect tools escalate to approval); approval auto-deny in eval; recipient domain allowlist; spotlighting (probabilistic) | Operator could approve a malicious request in interactive mode (approval fatigue). Measured: attempts vs executed |
| T2 | **Injection → answer manipulation** (doc claims refund window is 365 days) | Spotlighting; task checks detect wrong answers | **Not prevented.** Within allowed tools, injection can still mislead. Measured as attack success on answer-integrity tasks |
| T3 | **Data exfiltration channels** | Only `send_email` is an outbound channel, and it's gated. CLI doesn't render markdown/URLs | M3 dashboard must not auto-render images/links from model output (markdown-image exfil) — noted for M3 |
| T4 | **SQL write / DDL / ATTACH / PRAGMA / extension loading** | 3 layers: sqlglot AST allowlist; `mode=ro` + `query_only`; SQLite authorizer allowing only read actions; extensions disabled. **Measured finding (2026-11-10):** the read-only connection *alone* still lets `ATTACH DATABASE 'x.db'` create a new empty file on the host (writes into it are refused); the authorizer blocks ATTACH outright. Pinned by `tests/test_sql_guard.py::test_layer2_alone_lets_attach_create_an_empty_file` | Bugs in sqlglot/SQLite. Defence in depth means a single bug does not suffice |
| T5 | **Expensive queries / resource exhaustion** (cartesian joins, recursive CTE, `9**9**9`) | SQL progress-handler timeout + `interrupt()`, row cap; calculator bounds; tool timeouts; output truncation; agent limits (steps, tool calls, deadline, tokens) | Non-SQL tool threads can't be killed, only abandoned after timeout |
| T6 | **System-prompt leak** | Prompt contains no secrets; canary check on final answer and tool args | Leak itself is possible. It's detected and measured, not prevented |
| T7 | **Hallucinated tool / malformed args** | Unknown-tool deny; strict Pydantic validation; structured error to model | Model may give up; measured as task failure |
| T8 | **Loops / runaway execution** | Loop detector, hard caps, deadline, safe termination | Legitimately long tasks can be cut off (tunable limits) |
| T9 | **Secrets / PII in logs and traces** | Keys only from env; redaction of keys, emails, card-like numbers in trace payloads; audit stores args hash | Regex redaction misses novel formats |
| T10 | **Audit tampering / agent reads its own logs** | Agent has no Mongo-facing tool; separate stores | M1 uses one Mongo user for the runtime. **M2:** RBAC role that can only insert into `audit_log` |
| T11 | **Exposed MongoDB** | Compose binds `127.0.0.1`; credentials from git-ignored `.env` | Local machine compromise is out of scope |
| T12 | **Denial of wallet (cloud)** | Cloud disabled unless `AW_ALLOW_CLOUD=1` + key; per-execution token budget; 429s honoured (never retried early); daily-quota exhaustion aborts the eval run instead of hammering the API; explicit approval from project owner | Price table entries are unverified (†) → cost is labelled *estimated* |
| T17 | **Data and key exposure to a cloud provider** | Only synthetic scenario data exists; key read from git-ignored `.env` as `SecretStr`, sent only in the `Authorization` header; error messages never include it (tested) | Prompts, tool outputs and synthetic "PII" are sent to the provider and may be logged by it |
| T18 | **False success claims** (model says a refused action happened) | Denial messages state explicitly that nothing was executed (D18); eval checks require the answer to acknowledge refusals (adv_06/07) | The model can still misreport; this is an answer-integrity problem, not an authorization one |
| T13 | **Guardrails-OFF mode used outside evals** | Requires `--unsafe-eval`, honeypot tools only, loud warning, recorded in run metadata | Operator misuse |
| T14 | **Rate-limit bypass** | Buckets per execution+tool and per agent+tool | In-memory only; multi-process deployment would need shared state |
| T15 | **Supply chain** (packages, model weights) | Pinned versions in lockfile; models pulled from Ollama registry by explicit tag | Trust in upstream registries is assumed |
| T16 | **Unaudited execution if Mongo is down** | Fail closed: runtime refuses to start an execution without a reachable store | Availability is reduced in exchange for auditability |

## 6. Known limitations (honest list)
- Prompt injection is mitigated, not solved. Spotlighting is probabilistic. The deterministic controls
  constrain *actions*, not *beliefs*.
- The taint rule is coarse. It blocks legitimate "search docs then email a summary" workflows unless a
  human approves.
- Honeypot tools model only two side-effect types. Real systems have many more channels.
- No end-user authentication or multi-tenant isolation in M1.
- The adversarial task set is small (6 tasks) and written by the same author as the defences, so it
  likely under-represents real attacks. Results show resilience *to these attacks*, not in general.
