"""Redaction for anything persisted to traces / audit (THREAT_MODEL T9).

* dict keys that look secret -> value replaced entirely
* API-key / bearer-token shapes -> [REDACTED_KEY]
* card-like digit runs (13-19 digits) -> [REDACTED_PAN]
* email local parts -> ``[email]@domain`` (domain kept: it is useful audit evidence, e.g. exfil targets)
Regex redaction is best-effort and can miss novel formats (documented limitation).
"""

from __future__ import annotations

import re
from typing import Any

# Full-match on the key so "access_token"/"api_key" are redacted but "input_tokens" is not.
_SECRET_KEYS = re.compile(r"(.*[_-])?(password|passwd|secret|token|api[_-]?key|authorization|cookie|credentials?)",
                          re.I)
_KEY_SHAPES = re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{16,}|AKIA[0-9A-Z]{16})\b")
_PAN = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")


def redact_text(s: str) -> str:
    s = _KEY_SHAPES.sub("[REDACTED_KEY]", s)
    s = _PAN.sub("[REDACTED_PAN]", s)
    return _EMAIL.sub(lambda m: f"[email]@{m.group(1)}", s)


def redact(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: ("[REDACTED]" if isinstance(k, str) and _SECRET_KEYS.fullmatch(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj
