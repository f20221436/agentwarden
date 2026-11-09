"""In-memory PlatformStore for unit tests (no MongoDB needed). Not used for real executions."""

from __future__ import annotations

import copy
from typing import Any


class MemoryStore:
    def __init__(self) -> None:
        self.collections: dict[str, list[dict[str, Any]]] = {
            k: []
            for k in ("executions", "steps", "audit_log", "approvals", "sandbox_effects", "eval_runs", "eval_results")
        }

    def ping(self) -> None:
        return None

    def ensure_indexes(self) -> None:
        return None

    def _find(self, coll: str, **match: Any) -> list[dict[str, Any]]:
        return [d for d in self.collections[coll] if all(d.get(k) == v for k, v in match.items())]

    def _update(self, coll: str, _id: str, fields: dict[str, Any]) -> None:
        for d in self._find(coll, _id=_id):
            d.update(copy.deepcopy(fields))

    def insert_execution(self, doc: dict[str, Any]) -> None:
        self.collections["executions"].append(copy.deepcopy(doc))

    def update_execution(self, execution_id: str, fields: dict[str, Any]) -> None:
        self._update("executions", execution_id, fields)

    def get_execution(self, execution_id: str) -> dict[str, Any] | None:
        found = self._find("executions", _id=execution_id)
        return found[0] if found else None

    def insert_step(self, doc: dict[str, Any]) -> None:
        if self._find("steps", execution_id=doc["execution_id"], seq=doc["seq"]):
            raise ValueError("duplicate (execution_id, seq)")  # mirrors the unique index
        self.collections["steps"].append(copy.deepcopy(doc))

    def steps_for(self, execution_id: str) -> list[dict[str, Any]]:
        return sorted(self._find("steps", execution_id=execution_id), key=lambda d: d["seq"])

    def insert_audit(self, doc: dict[str, Any]) -> None:
        self.collections["audit_log"].append(copy.deepcopy(doc))

    def audit_for(self, execution_id: str) -> list[dict[str, Any]]:
        return self._find("audit_log", execution_id=execution_id)

    def insert_approval(self, doc: dict[str, Any]) -> None:
        self.collections["approvals"].append(copy.deepcopy(doc))

    def update_approval(self, approval_id: str, fields: dict[str, Any]) -> None:
        self._update("approvals", approval_id, fields)

    def insert_effect(self, doc: dict[str, Any]) -> None:
        self.collections["sandbox_effects"].append(copy.deepcopy(doc))

    def effects_for(self, execution_id: str) -> list[dict[str, Any]]:
        return self._find("sandbox_effects", execution_id=execution_id)

    def insert_eval_run(self, doc: dict[str, Any]) -> None:
        self.collections["eval_runs"].append(copy.deepcopy(doc))

    def update_eval_run(self, run_id: str, fields: dict[str, Any]) -> None:
        self._update("eval_runs", run_id, fields)

    def insert_eval_result(self, doc: dict[str, Any]) -> None:
        self.collections["eval_results"].append(copy.deepcopy(doc))
