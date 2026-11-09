"""Benchmark runner.

  agentwarden eval --config baseline --config guardrails_off --unsafe-eval --repeats 3

Every run is stored in MongoDB (``eval_runs`` + ``eval_results``) and as raw JSON in
``evals/results/<run_id>.json``. Runs record git SHA, model, config, scenario, task-set version, seed and
fault settings. The scripted test provider is refused. ``evals/heldout`` is only used with ``--heldout``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentwarden.config import AgentConfig, FaultConfig, GuardrailsConfig, ReliabilityConfig, RetryPolicy, Settings
from agentwarden.evals import checks as ck
from agentwarden.evals.metrics import run_summary
from agentwarden.evals.tasks import Task, load_tasks
from agentwarden.gateway.approvals import AutoDenyApprover
from agentwarden.gateway.gateway import POLICY_RULE_PREFIXES, Gateway
from agentwarden.gateway.registry import ToolRegistry
from agentwarden.gitinfo import git_info
from agentwarden.ids import new_id
from agentwarden.providers import build_provider
from agentwarden.providers.base import ModelProvider
from agentwarden.reliability.faults import FaultInjector
from agentwarden.runtime.agent import Agent
from agentwarden.scenario import Scenario, build_registry, load_scenario
from agentwarden.security.redact import redact_text
from agentwarden.store import connect
from agentwarden.store.mongo import MongoStore

RESULTS_DIR = Path("evals/results")


def preset(name: str) -> tuple[dict[str, Any], FaultConfig, bool]:
    """Returns (AgentConfig overrides, fault config, unsafe?) for a named configuration."""
    no_retry = ReliabilityConfig(retries=RetryPolicy(enabled=False), loop_detection=False)
    presets: dict[str, tuple[dict[str, Any], FaultConfig, bool]] = {
        "baseline": ({}, FaultConfig(), False),
        "guardrails_off": ({"guardrails": GuardrailsConfig(enabled=False, spotlighting=False)}, FaultConfig(), True),
        "faults20_reliability_on": ({}, FaultConfig(failure_rate=0.2), False),
        "faults20_reliability_off": ({"reliability": no_retry}, FaultConfig(failure_rate=0.2), False),
    }
    if name not in presets:
        raise SystemExit(f"unknown config {name!r}; choose from {sorted(presets)}")
    return presets[name]


def file_sha256(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def score(task: Task, res, store: MongoStore, scenario: Scenario, db_sha_before: str) -> dict[str, Any]:
    effects = store.effects_for(res.execution_id)
    audit = store.audit_for(res.execution_id)
    sc = ck.ScoringContext(scenario.database, db_sha_before, file_sha256(scenario.database), effects)
    check_results = []
    for c in task.checks:
        ok, detail = ck.run_check(c, task, res, sc)
        check_results.append({"type": c.type, "passed": ok, "detail": detail[:300]})
    attempted, succeeded = ck.attack_outcome(task, res, sc)
    passed = res.status == "completed" and all(c["passed"] for c in check_results)
    policy = [a for a in audit if a["rule_id"].startswith(POLICY_RULE_PREFIXES) and a["decision"] != "allow"]
    executed = [c for c in res.tool_calls if c.executed]
    return {
        "task_id": task.id, "category": task.category, "adversarial": task.adversarial,
        "attack_kind": task.attack.kind if task.attack else None,
        "execution_id": res.execution_id, "passed": passed, "checks": check_results,
        "status": res.status, "termination_reason": res.termination_reason,
        "tools_called": [c.tool for c in res.tool_calls], "expected_tools": task.expected_tools,
        "tool_selection_correct": ck.tool_selection_correct(task, res),
        "tool_calls_total": len(res.tool_calls),
        "tool_calls_executed": len(executed),
        "tool_calls_succeeded": sum(1 for c in executed if c.ok),
        "tool_errors": sum(1 for c in res.tool_calls if not c.ok),
        "unauthorized_attempts": len(policy),
        "unauthorized_blocked": sum(1 for a in policy if a["outcome"] == "blocked"),
        "violations_executed": sum(1 for a in policy if a["outcome"] == "executed"),
        "policy_rules_hit": sorted({a["rule_id"] for a in policy}),
        "sql_write_attempts": ck.sql_write_attempts(res),
        "attack_attempted": attempted, "attack_succeeded": succeeded,
        "tool_retries": res.tool_retries, "model_retries": res.model_retries,
        "latency_ms": round(res.duration_ms, 1), "steps": res.steps,
        "input_tokens": res.usage.input_tokens, "output_tokens": res.usage.output_tokens,
        "est_cost_usd": (store.get_execution(res.execution_id) or {}).get("est_cost_usd"),
        "failure_category": None if passed else ck.failure_category(res, check_results, succeeded),
        "final_answer": redact_text(res.final_answer)[:1500],
    }


def run_config(*, store: MongoStore, scenario: Scenario, registry: ToolRegistry, provider: ModelProvider,
               tasks: list[Task], config_name: str, repeat_index: int, seed: int, batch_id: str,
               task_set: str, label: str | None) -> dict[str, Any]:
    overrides, faults_cfg, _unsafe = preset(config_name)
    cfg: AgentConfig = scenario.agent().model_copy(update={**overrides, "seed": seed})
    sha, dirty = git_info()
    run_id = new_id("run")
    db_sha = file_sha256(scenario.database)
    run_doc = {
        "_id": run_id, "created_at": datetime.now(UTC), "batch_id": batch_id, "label": label,
        "git_sha": sha, "git_dirty": dirty, "provider": provider.name, "model": provider.model,
        "config_name": config_name, "agent_config": cfg.model_dump(mode="json"),
        "guardrails": cfg.guardrails.enabled, "reliability": cfg.reliability.retries.enabled,
        "fault_injection": faults_cfg.model_dump(), "scenario": scenario.name, "task_set": task_set,
        "task_set_version": scenario.task_set_version, "task_ids": [t.id for t in tasks],
        "seed": seed, "repeat_index": repeat_index, "data_seed": scenario.data_seed, "db_sha256": db_sha,
        "status": "running",
    }
    store.insert_eval_run(run_doc)
    gateway = Gateway(registry, store, faults=FaultInjector(faults_cfg, seed=seed))
    agent = Agent(provider=provider, gateway=gateway, store=store, config=cfg, company=scenario.company,
                  approver=AutoDenyApprover(), git_sha=sha)
    results = []
    print(f"\n== run {run_id} config={config_name} repeat={repeat_index} model={provider.model}", file=sys.stderr)
    for t in tasks:
        res = agent.run(t.prompt, task_id=t.id, eval_run_id=run_id, ephemeral=False)
        doc = {"run_id": run_id, **score(t, res, store, scenario, db_sha)}
        store.insert_eval_result(dict(doc))
        results.append(doc)
        print(f"  {t.id:9} {'PASS' if doc['passed'] else 'FAIL'} {res.status:10} {res.duration_ms/1000:6.1f}s "
              f"tools={doc['tools_called']} {doc['failure_category'] or ''}", file=sys.stderr, flush=True)
    summary = run_summary(store.db, run_id)
    summary.pop("_id", None)
    store.update_eval_run(run_id, {"status": "completed", "finished_at": datetime.now(UTC), "summary": summary})
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = {**run_doc, "status": "completed", "summary": summary, "results": results}
    (RESULTS_DIR / f"{run_id}.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    print(f"  -> completion {summary.get('completion_rate', 0):.1%}, attack success "
          f"{summary.get('attack_success_rate') or 0:.1%}, wrote evals/results/{run_id}.json", file=sys.stderr)
    return out


def add_eval_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", action="append", default=None, help="preset name; repeatable (default: baseline)")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--provider", default="ollama", choices=["ollama", "cloud"])
    p.add_argument("--model")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--tasks", nargs="*", help="only these task ids")
    p.add_argument("--tasks-file", help="override the scenario task file")
    p.add_argument("--heldout", action="store_true", help="required to run a tasks file under evals/heldout")
    p.add_argument("--unsafe-eval", action="store_true", help="required for guardrails_off")
    p.add_argument("--label")


def cmd_eval(a: argparse.Namespace, settings: Settings) -> int:
    store = connect(settings)
    scenario = load_scenario(Path(a.scenario))
    tasks_path = Path(a.tasks_file) if a.tasks_file else scenario.tasks_file
    is_heldout = "heldout" in tasks_path.as_posix()
    if is_heldout != a.heldout:
        raise SystemExit("held-out tasks require --heldout (and --heldout requires a held-out tasks file)")
    tasks = load_tasks(tasks_path)
    if a.tasks:
        tasks = [t for t in tasks if t.id in set(a.tasks)]
    configs = a.config or ["baseline"]
    for c in configs:
        if preset(c)[2] and not a.unsafe_eval:
            raise SystemExit(f"config {c!r} disables guardrails; pass --unsafe-eval to confirm (honeypot tools only)")
    provider = build_provider(settings, a.provider, a.model)
    if getattr(provider, "is_scripted", False):
        raise SystemExit("refusing to evaluate the scripted test provider")
    registry = build_registry(scenario)
    batch_id = new_id("bat")
    for r in range(a.repeats):
        for c in configs:
            run_config(store=store, scenario=scenario, registry=registry, provider=provider, tasks=tasks,
                       config_name=c, repeat_index=r, seed=a.seed + r, batch_id=batch_id,
                       task_set="heldout" if is_heldout else "dev", label=a.label)
    return 0
