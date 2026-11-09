# AgentWarden

A reliable, secure, observable and **measured** runtime for tool-using LLM agents.

The LLM is treated as an untrusted planner. Every tool call goes through one gateway that enforces
capability permissions, schema validation, a read-only SQL guard (three independent layers), a taint rule
for untrusted content, human approval, rate limits, timeouts and retries — all in deterministic code —
and records every decision in MongoDB. An evaluation runner measures what this buys, and
[`docs/RESULTS.md`](docs/RESULTS.md) is generated from those stored runs.

- Architecture & decisions: [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md)
- Threat model: [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)
- Measured results: [docs/RESULTS.md](docs/RESULTS.md)
- Learning notes: [docs/LEARNING_NOTES.md](docs/LEARNING_NOTES.md)

## Results at a glance
See [docs/RESULTS.md](docs/RESULTS.md) (generated from MongoDB): local `qwen2.5:3b` vs Groq
`openai/gpt-oss-120b` on 30 tasks, guardrails ON/OFF and reliability ON/OFF under fault injection, with
failure analysis, infrastructure incidents and limitations.

## Requirements
- Python ≥ 3.12, Docker (for MongoDB), [Ollama](https://ollama.com) for the local model.
- No paid API is needed. The optional cloud provider is disabled unless `AW_ALLOW_CLOUD=1` (see below).

## Setup (from a clean clone)
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env            # then set AW_MONGO_PASSWORD to any local secret
docker compose up -d            # MongoDB Atlas Local on 127.0.0.1:27017 (wait until "healthy")

ollama pull qwen2.5:3b          # benchmark model (see PROJECT_PLAN D14); set AW_MODEL in .env
agentwarden setup               # generates the seeded demo database, creates Mongo indexes
```

## Use
```bash
agentwarden run "How many users are currently active on the pro plan?"
agentwarden probe --model qwen2.5:3b                    # local tool-calling compatibility check

agentwarden eval --config baseline --repeats 3          # benchmark (30 tasks)
agentwarden eval --config baseline --config guardrails_off --unsafe-eval --repeats 3
agentwarden eval --config faults20_reliability_on --config faults20_reliability_off --repeats 3
agentwarden report                                      # regenerate docs/RESULTS.md from MongoDB
```
`guardrails_off` runs the policy layer in shadow mode (logged, not enforced) against sandboxed honeypot
tools; it is refused without `--unsafe-eval`.

## Cloud model (optional, e.g. Groq)
Any OpenAI-compatible Chat Completions API works. It is **off by default**; nothing is sent to a cloud
provider unless you opt in. In `.env`:
```bash
AW_ALLOW_CLOUD=1
AW_CLOUD_BASE_URL=https://api.groq.com/openai/v1
AW_CLOUD_API_KEY=gsk_...                 # never commit .env (it is git-ignored)
AW_CLOUD_MODEL=openai/gpt-oss-120b      # list what your key can use: GET {base_url}/models
```
```bash
agentwarden run "How many users are on the pro plan?" --provider cloud
agentwarden eval --provider cloud --config baseline --config guardrails_off --unsafe-eval
agentwarden eval --provider cloud --config faults20_reliability_on --config faults20_reliability_off
```
- **Rate limits.** HTTP 429 responses are retried after the server's `Retry-After` (or `x-ratelimit-reset-*`)
  wait, never sooner, up to 65 s per wait. A longer wait or a daily-quota message (tokens/requests per day)
  means the quota is exhausted: the execution fails fast and an eval run is stored as `aborted` and excluded
  from RESULTS.md, instead of recording infrastructure failures as agent failures.
- **Cost.** Estimated from [`agentwarden/pricing.yaml`](agentwarden/pricing.yaml) (USD per 1M tokens, with a
  source and a `verified` flag per entry). Unknown models are reported as `n/a`, never as $0.
- The demo data sent to the provider is synthetic (seeded generator); no real customer data exists here.
- Groq free/dev tiers are limited per organisation (e.g. 8,000 tokens/min and 1,000 requests/day were reported
  by the API for `openai/gpt-oss-120b` on 2026-11-10; tokens/day is not in the headers — check the console).
  One 30-task run uses roughly 60–100k tokens.

## Reliability configurations
`faults20_reliability_on/off` inject 20% transient failures into **tool** calls. Reliability OFF disables
tool retries and loop detection; **model-call retries stay on in both**, so provider rate limits do not
confound the tool-reliability comparison.

## Long benchmark runs on a laptop
Windows may enter Modern Standby mid-run, which freezes the process and can fail a task on its deadline.
Keep the machine awake (plugged in, lid open) and, after a run, annotate any affected executions:
`python scripts/annotate_standby.py` (Windows only; annotation never changes scores).

## Tests
```bash
pytest -q                     # unit + Mongo integration (Mongo tests skip locally if Mongo is down)
AW_REQUIRE_MONGO=1 pytest -q  # what CI does: Mongo tests must run
ruff check agentwarden tests scenarios
```

## Layout
```
agentwarden/   providers/ runtime/ gateway/ reliability/ security/ tools/ store/ evals/ cli.py
scenarios/saas_demo/   generate.py (seeded data) · documents/ (incl. injection payloads) · tasks/ · scenario.yaml
evals/results/         raw JSON per eval run
evals/heldout/         held-out tasks (owner only; never used for development)
```
