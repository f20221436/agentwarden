"""MongoDB platform store: collections, indexes (with rationale) and simple repository methods.

Schema rationale lives in docs/PROJECT_PLAN.md §4.2. Summary:
* ``executions`` embed an immutable agent-config snapshot; ``steps`` are referenced (unbounded, written
  incrementally so a crash still leaves a trace).
* ``steps`` has a TTL index restricted (partialFilterExpression) to ephemeral ad-hoc runs, so eval traces
  survive for failure analysis while CLI debugging traces expire after 30 days.
* ``audit_log`` has no TTL and stores an args hash, not raw args.
"""

from __future__ import annotations

from typing import Any

from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

from agentwarden.store.base import StoreUnavailable

STEP_TTL_SECONDS = 30 * 24 * 3600

INDEXES: dict[str, list[IndexModel]] = {
    "executions": [
        IndexModel([("started_at", DESCENDING)], name="recent"),
        IndexModel([("eval_run_id", ASCENDING), ("task_id", ASCENDING)], name="eval_join"),
        IndexModel([("status", ASCENDING), ("started_at", DESCENDING)], name="status_triage"),
    ],
    "steps": [
        IndexModel([("execution_id", ASCENDING), ("seq", ASCENDING)], name="trace_order", unique=True),
        IndexModel(
            [("ts", ASCENDING)],
            name="ttl_ephemeral",
            expireAfterSeconds=STEP_TTL_SECONDS,
            partialFilterExpression={"ephemeral": True},
        ),
    ],
    "audit_log": [
        IndexModel([("execution_id", ASCENDING), ("ts", ASCENDING)], name="by_execution"),
        IndexModel([("decision", ASCENDING), ("ts", DESCENDING)], name="by_decision"),
        IndexModel([("tool", ASCENDING), ("ts", DESCENDING)], name="by_tool"),
    ],
    "approvals": [
        IndexModel([("status", ASCENDING), ("requested_at", ASCENDING)], name="pending_queue"),
        IndexModel([("execution_id", ASCENDING)], name="by_execution"),
    ],
    "sandbox_effects": [IndexModel([("execution_id", ASCENDING)], name="by_execution")],
    "eval_runs": [
        IndexModel([("created_at", DESCENDING)], name="recent"),
        IndexModel(
            [("config_name", ASCENDING), ("task_set_version", ASCENDING), ("created_at", DESCENDING)],
            name="latest_per_config",
        ),
    ],
    "eval_results": [
        IndexModel([("run_id", ASCENDING), ("task_id", ASCENDING)], name="run_task", unique=True),
        IndexModel([("task_id", ASCENDING), ("run_id", ASCENDING)], name="task_history"),
    ],
}


class MongoStore:
    def __init__(self, uri: str, db_name: str, *, timeout_ms: int = 3000):
        self._client: MongoClient = MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, tz_aware=True)
        self.db: Database = self._client[db_name]

    def ping(self) -> None:
        try:
            self._client.admin.command("ping")
        except PyMongoError as e:
            raise StoreUnavailable(f"MongoDB unreachable: {type(e).__name__}") from e

    def ensure_indexes(self) -> None:
        for coll, models in INDEXES.items():
            self.db[coll].create_indexes(models)

    def close(self) -> None:
        self._client.close()

    # executions
    def insert_execution(self, doc: dict[str, Any]) -> None:
        self.db.executions.insert_one(doc)

    def update_execution(self, execution_id: str, fields: dict[str, Any]) -> None:
        self.db.executions.update_one({"_id": execution_id}, {"$set": fields})

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        return self.db.executions.find_one({"_id": execution_id})

    # steps
    def insert_step(self, doc: dict[str, Any]) -> None:
        self.db.steps.insert_one(doc)

    def steps_for(self, execution_id: str) -> list[dict[str, Any]]:
        return list(self.db.steps.find({"execution_id": execution_id}, {"_id": 0}).sort("seq", ASCENDING))

    # audit
    def insert_audit(self, doc: dict[str, Any]) -> None:
        self.db.audit_log.insert_one(doc)

    def audit_for(self, execution_id: str) -> list[dict[str, Any]]:
        return list(self.db.audit_log.find({"execution_id": execution_id}, {"_id": 0}).sort("ts", ASCENDING))

    # approvals
    def insert_approval(self, doc: dict[str, Any]) -> None:
        self.db.approvals.insert_one(doc)

    def update_approval(self, approval_id: str, fields: dict[str, Any]) -> None:
        self.db.approvals.update_one({"_id": approval_id}, {"$set": fields})

    # sandbox effects (honeypot tools)
    def insert_effect(self, doc: dict[str, Any]) -> None:
        self.db.sandbox_effects.insert_one(doc)

    def effects_for(self, execution_id: str) -> list[dict[str, Any]]:
        return list(self.db.sandbox_effects.find({"execution_id": execution_id}, {"_id": 0}))

    # evals
    def insert_eval_run(self, doc: dict[str, Any]) -> None:
        self.db.eval_runs.insert_one(doc)

    def update_eval_run(self, run_id: str, fields: dict[str, Any]) -> None:
        self.db.eval_runs.update_one({"_id": run_id}, {"$set": fields})

    def insert_eval_result(self, doc: dict[str, Any]) -> None:
        self.db.eval_results.insert_one(doc)
