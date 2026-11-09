"""Metrics computed with MongoDB aggregation pipelines over ``eval_results`` (definitions: RESULTS.md §5)."""

from __future__ import annotations

from typing import Any

from pymongo.database import Database


def _sum_if(cond: Any) -> dict[str, Any]:
    return {"$sum": {"$cond": [cond, 1, 0]}}


def _ratio(num: str, den: str) -> dict[str, Any]:
    return {"$cond": [{"$gt": [f"${den}", 0]}, {"$divide": [f"${num}", f"${den}"]}, None]}


def summary_pipeline(match: dict[str, Any], group_key: Any = "$run_id") -> list[dict[str, Any]]:
    return [
        {"$match": match},
        {"$group": {
            "_id": group_key,
            "tasks": {"$sum": 1},
            "passed": _sum_if("$passed"),
            "tool_selection_correct": _sum_if("$tool_selection_correct"),
            "tool_calls_executed": {"$sum": "$tool_calls_executed"},
            "tool_calls_succeeded": {"$sum": "$tool_calls_succeeded"},
            "tool_calls_total": {"$sum": "$tool_calls_total"},
            "unauthorized_attempts": {"$sum": "$unauthorized_attempts"},
            "unauthorized_blocked": {"$sum": "$unauthorized_blocked"},
            "violations_executed": {"$sum": "$violations_executed"},
            "executions_with_violation": _sum_if({"$gt": ["$violations_executed", 0]}),
            "sql_write_attempts": {"$sum": "$sql_write_attempts"},
            "adversarial": _sum_if("$adversarial"),
            "attacks_attempted": _sum_if("$attack_attempted"),
            "attacks_succeeded": _sum_if("$attack_succeeded"),
            "adversarial_passed": _sum_if({"$and": ["$adversarial", "$passed"]}),
            "abnormal_terminations": _sum_if({"$ne": ["$status", "completed"]}),
            "tool_retries": {"$sum": "$tool_retries"},
            "model_retries": {"$sum": "$model_retries"},
            "tool_errors": {"$sum": "$tool_errors"},
            "latency_mean_ms": {"$avg": "$latency_ms"},
            "latency_pct": {"$percentile": {"input": "$latency_ms", "p": [0.5, 0.95], "method": "approximate"}},
            "tokens_total": {"$sum": {"$add": ["$input_tokens", "$output_tokens"]}},
            "tokens_passed": {"$sum": {"$cond": ["$passed", {"$add": ["$input_tokens", "$output_tokens"]}, 0]}},
            "cost_total": {"$sum": {"$ifNull": ["$est_cost_usd", 0]}},
            "cost_known": {"$min": {"$cond": [{"$eq": ["$est_cost_usd", None]}, 0, 1]}},
        }},
        {"$addFields": {
            "completion_rate": _ratio("passed", "tasks"),
            "tool_selection_accuracy": _ratio("tool_selection_correct", "tasks"),
            "tool_execution_success_rate": _ratio("tool_calls_succeeded", "tool_calls_executed"),
            "blocked_rate": _ratio("unauthorized_blocked", "unauthorized_attempts"),
            "policy_violation_rate": _ratio("executions_with_violation", "tasks"),
            "attack_success_rate": _ratio("attacks_succeeded", "adversarial"),
            "adversarial_pass_rate": _ratio("adversarial_passed", "adversarial"),
            "error_rate": _ratio("abnormal_terminations", "tasks"),
            "latency_p50_ms": {"$arrayElemAt": ["$latency_pct", 0]},
            "latency_p95_ms": {"$arrayElemAt": ["$latency_pct", 1]},
            "tokens_per_successful_task": _ratio("tokens_passed", "passed"),
            "cost_per_successful_task": {"$cond": [{"$and": [{"$eq": ["$cost_known", 1]}, {"$gt": ["$passed", 0]}]},
                                                   {"$divide": ["$cost_total", "$passed"]}, None]},
        }},
        {"$project": {"latency_pct": 0}},
    ]


def run_summary(db: Database, run_id: str) -> dict[str, Any]:
    out = list(db.eval_results.aggregate(summary_pipeline({"run_id": run_id})))
    return out[0] if out else {}


def category_breakdown(db: Database, run_ids: list[str]) -> list[dict[str, Any]]:
    return list(db.eval_results.aggregate([
        {"$match": {"run_id": {"$in": run_ids}}},
        {"$group": {"_id": "$category", "n": {"$sum": 1}, "passed": _sum_if("$passed")}},
        {"$addFields": {"rate": _ratio("passed", "n")}},
        {"$sort": {"_id": 1}},
    ]))


def failure_analysis(db: Database, run_ids: list[str], examples: int = 3) -> list[dict[str, Any]]:
    return list(db.eval_results.aggregate([
        {"$match": {"run_id": {"$in": run_ids}, "passed": False}},
        {"$sort": {"task_id": 1}},
        {"$group": {"_id": "$failure_category", "count": {"$sum": 1}, "tasks": {"$addToSet": "$task_id"},
                    "examples": {"$push": "$execution_id"}}},
        {"$project": {"count": 1, "tasks": 1, "examples": {"$slice": ["$examples", examples]}}},
        {"$sort": {"count": -1, "_id": 1}},
    ]))
