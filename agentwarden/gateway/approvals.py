"""Human-approval gate. Every request and decision is persisted to ``approvals``."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from typing import Any, Protocol

from agentwarden.ids import new_id
from agentwarden.security.redact import redact
from agentwarden.store.base import PlatformStore


class Approver(Protocol):
    policy_name: str

    def decide(self, *, tool: str, args: dict[str, Any], reason: str) -> bool: ...


class AutoDenyApprover:
    """Used in evals: no human present, so escalations are denied (fail safe)."""
    policy_name = "auto_deny"

    def decide(self, *, tool: str, args: dict[str, Any], reason: str) -> bool:
        return False


class AutoApproveApprover:
    """Only for tests / explicitly unsafe experiments."""
    policy_name = "auto_approve"

    def decide(self, *, tool: str, args: dict[str, Any], reason: str) -> bool:
        return True


class CliApprover:
    policy_name = "cli_interactive"

    def decide(self, *, tool: str, args: dict[str, Any], reason: str) -> bool:
        if not sys.stdin.isatty():
            return False  # non-interactive: fail safe
        print(f"\n[APPROVAL REQUIRED] tool={tool} reason={reason}\n  args={args}", file=sys.stderr)
        return input("Approve? [y/N] ").strip().lower() == "y"


def request_approval(
    store: PlatformStore, approver: Approver, *, execution_id: str, tool: str, args: dict[str, Any], reason: str
) -> tuple[bool, str]:
    approval_id = new_id("apr")
    store.insert_approval(
        {
            "_id": approval_id,
            "execution_id": execution_id,
            "tool": tool,
            "args_redacted": redact(args),
            "reason": reason,
            "status": "pending",
            "approver_policy": approver.policy_name,
            "requested_at": datetime.now(UTC),
        }
    )
    approved = approver.decide(tool=tool, args=args, reason=reason)
    store.update_approval(
        approval_id,
        {"status": "approved" if approved else "denied", "decided_at": datetime.now(UTC),
         "decided_by": approver.policy_name},
    )
    return approved, approval_id
