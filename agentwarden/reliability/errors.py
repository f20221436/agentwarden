"""Error taxonomy. The single most important reliability question: is this error worth retrying?

Transient: the same call might succeed later (timeouts, connection drops, injected faults, 429/5xx).
Permanent: retrying the identical call cannot help (bad arguments, permission denied, SQL syntax errors).
"""

from __future__ import annotations


class ToolError(Exception):
    transient: bool = False
    error_type: str = "tool_error"


class TransientToolError(ToolError):
    transient = True
    error_type = "transient_error"


class ToolTimeout(TransientToolError):
    error_type = "timeout"


class PermanentToolError(ToolError):
    transient = False
    error_type = "permanent_error"


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, ToolError):
        return exc.transient
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return bool(getattr(exc, "transient", False))  # e.g. ProviderError
