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

## Requirements
- Python ≥ 3.12, Docker (for MongoDB), [Ollama](https://ollama.com) for the local model.
- No paid API is needed. The optional cloud provider is disabled unless `AW_ALLOW_CLOUD=1`.

## Setup (from a clean clone)
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env            # then set AW_MONGO_PASSWORD to any local secret
docker compose up -d            # MongoDB Atlas Local on 127.0.0.1:27017 (wait until "healthy")

ollama pull qwen3:4b            # or another tool-calling model; set AW_MODEL in .env
agentwarden setup               # generates the seeded demo database, creates Mongo indexes
```

## Use
```bash
agentwarden run "How many users are currently active on the pro plan?"
agentwarden probe --model qwen3:4b                      # local tool-calling compatibility check

agentwarden eval --config baseline --repeats 3          # benchmark (30 tasks)
agentwarden eval --config baseline --config guardrails_off --unsafe-eval --repeats 3
agentwarden eval --config faults20_reliability_on --config faults20_reliability_off --repeats 3
agentwarden report                                      # regenerate docs/RESULTS.md from MongoDB
```
`guardrails_off` runs the policy layer in shadow mode (logged, not enforced) against sandboxed honeypot
tools; it is refused without `--unsafe-eval`.

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
