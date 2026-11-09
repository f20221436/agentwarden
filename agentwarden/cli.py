"""Command-line interface.

  agentwarden setup                          generate scenario data + create Mongo indexes
  agentwarden run "question" [--model M]     one agent execution (trace stored in MongoDB)
  agentwarden probe --model M [--model M2]   local tool-calling compatibility probe
  agentwarden eval --config baseline [...]   run the benchmark (see agentwarden.evals.runner)
  agentwarden report                         regenerate docs/RESULTS.md from MongoDB
"""

from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path

from agentwarden.config import Settings
from agentwarden.observability import configure_logging

DEFAULT_SCENARIO = Path("scenarios/saas_demo")


def _cmd_setup(a: argparse.Namespace, settings: Settings) -> int:
    from agentwarden.store import connect

    gen = Path(a.scenario) / "generate.py"
    sys.argv = [str(gen)]
    runpy.run_path(str(gen), run_name="__main__")
    connect(settings)
    print("MongoDB reachable; indexes ensured.")
    return 0


def _cmd_run(a: argparse.Namespace, settings: Settings) -> int:
    from agentwarden.gateway.approvals import CliApprover
    from agentwarden.gateway.gateway import Gateway
    from agentwarden.gitinfo import git_info
    from agentwarden.providers import build_provider
    from agentwarden.runtime.agent import Agent
    from agentwarden.scenario import build_registry, load_scenario
    from agentwarden.store import connect

    store = connect(settings)  # fail closed if MongoDB is down
    scenario = load_scenario(Path(a.scenario))
    cfg = scenario.agent(a.agent)
    agent = Agent(provider=build_provider(settings, a.provider, a.model), gateway=Gateway(build_registry(scenario), store),
                  store=store, config=cfg, company=scenario.company, approver=CliApprover(), git_sha=git_info()[0])
    res = agent.run(a.question, ephemeral=True)
    print(json.dumps({
        "execution_id": res.execution_id, "status": res.status, "termination_reason": res.termination_reason,
        "steps": res.steps, "tool_calls": [f"{t.tool}({'ok' if t.ok else t.error_type})" for t in res.tool_calls],
        "tokens": res.usage.model_dump(), "duration_s": round(res.duration_ms / 1000, 1),
    }, indent=2), file=sys.stderr)
    print(res.final_answer)
    return 0 if res.status == "completed" else 2


def _cmd_probe(a: argparse.Namespace, settings: Settings) -> int:
    from agentwarden.probe import run_probe, summarize
    from agentwarden.providers import build_provider
    from agentwarden.scenario import build_registry, load_scenario

    scenario = load_scenario(Path(a.scenario))
    reg = build_registry(scenario)
    for model in a.model:
        provider = build_provider(settings, a.provider, model)
        rows = run_probe(provider, reg, scenario.agent().tools)
        for r in rows:
            print(f"  [{model}] {'OK ' if r.correct_tool and r.args_valid else 'BAD'} exp={r.expected:10} "
                  f"got={r.called!s:10} valid={r.args_valid!s:5} {r.latency_s:6.1f}s out={r.out_tokens:4} "
                  f"in={r.in_tokens:5} {r.error or ''}")
        print(json.dumps({"model": model, **summarize(rows)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agentwarden")
    ap.add_argument("--scenario", default=str(DEFAULT_SCENARIO))
    ap.add_argument("--log-level", default="WARNING")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup")
    p = sub.add_parser("run")
    p.add_argument("question")
    p.add_argument("--agent")
    p.add_argument("--provider", default="ollama", choices=["ollama", "cloud"])
    p.add_argument("--model")
    p = sub.add_parser("probe")
    p.add_argument("--model", action="append", required=True)
    p.add_argument("--provider", default="ollama", choices=["ollama", "cloud"])

    from agentwarden.evals.runner import add_eval_args
    from agentwarden.evals.report import add_report_args
    add_eval_args(sub.add_parser("eval"))
    add_report_args(sub.add_parser("report"))

    a = ap.parse_args(argv)
    configure_logging(a.log_level)
    settings = Settings()
    if a.cmd == "setup":
        return _cmd_setup(a, settings)
    if a.cmd == "run":
        return _cmd_run(a, settings)
    if a.cmd == "probe":
        return _cmd_probe(a, settings)
    if a.cmd == "eval":
        from agentwarden.evals.runner import cmd_eval
        return cmd_eval(a, settings)
    if a.cmd == "report":
        from agentwarden.evals.report import cmd_report
        return cmd_report(a, settings)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
