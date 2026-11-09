"""Deterministic policy engine. No LLM is ever asked whether something is safe.

Rules are evaluated in a fixed order and the first objection wins:
  R-CAP      capability check           -> deny
  R-TOOL:*   tool-specific policy       -> deny     (e.g. SQL statement validation, recipient allowlist)
  R-TAINT    untrusted data in context + side-effecting tool -> escalate (human approval)
  R-APPROVAL tool requires approval     -> escalate
Unknown tools, schema validation and rate limits are handled by the gateway itself and are never shadowed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from agentwarden.gateway.spec import SideEffect, ToolSpec

Action = Literal["allow", "deny", "escalate"]


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_id: str
    reason: str


ALLOW = Decision("allow", "R-ALLOW", "all policy checks passed")


def evaluate(spec: ToolSpec, args: BaseModel, *, capabilities: frozenset[str], tainted: bool) -> Decision:
    missing = spec.required_capabilities - capabilities
    if missing:
        return Decision("deny", "R-CAP", f"caller lacks capabilities {sorted(missing)}")
    if spec.policy is not None:
        objection = spec.policy(args)
        if objection is not None:
            rule, reason = objection
            return Decision("deny", f"R-TOOL:{rule}", reason)
    if tainted and spec.side_effect is not SideEffect.NONE:
        return Decision(
            "escalate", "R-TAINT", "untrusted content is in context; side-effecting call needs human approval"
        )
    if spec.requires_approval:
        return Decision("escalate", "R-APPROVAL", "tool requires human approval")
    return ALLOW
