"""Read-only SQL enforcement — three independent layers (docs/PROJECT_PLAN.md §5).

Layer 1  validate_select(): AST allowlist via sqlglot. Exactly one statement; root must be a SELECT or a
         set operation of SELECTs; no DML/DDL/PRAGMA/ATTACH nodes anywhere; no dangerous functions.
         This is a *policy* layer (shadowed when guardrails are OFF).
Layer 2  open_readonly(): ``mode=ro`` URI + ``PRAGMA query_only=ON``. The engine itself refuses writes.
Layer 3  _authorizer(): SQLite calls this for every action while compiling a statement; we allow only
         reads and allowlisted functions. Denies ATTACH, PRAGMA, writes, load_extension, etc.
Layers 2 and 3 are infrastructure: always on.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Pragma", "Attach", "Detach",
    "Merge", "TruncateTable", "Transaction", "Commit", "Rollback", "Use", "Set", "Copy", "LoadData",
)
_FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NODE_NAMES if hasattr(exp, n))
_SET_OPS = tuple(getattr(exp, n) for n in ("SetOperation", "Union", "Intersect", "Except") if hasattr(exp, n))
_DANGEROUS_FUNCS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "sqlite_compileoption_get"}

ALLOWED_FUNCTIONS = {
    "count", "sum", "avg", "min", "max", "total", "group_concat", "abs", "round", "lower", "upper", "length",
    "substr", "substring", "coalesce", "ifnull", "nullif", "iif", "instr", "replace", "trim", "ltrim", "rtrim",
    "printf", "format", "typeof", "like", "glob", "date", "time", "datetime", "julianday", "strftime",
    "unixepoch", "cast", "sqrt", "pow", "power", "log", "log10", "ln", "exp", "floor", "ceil", "ceiling",
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile", "lag", "lead", "first_value",
    "last_value", "nth_value", "json_extract", "hex", "sign", "mod",
}


@dataclass(frozen=True)
class SqlVerdict:
    ok: bool
    reason: str


def validate_select(sql: str) -> SqlVerdict:
    """Layer 1. Pure function: never touches the database."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except SqlglotError as e:
        return SqlVerdict(False, f"unparseable SQL: {str(e).splitlines()[0][:200]}")
    if len(statements) != 1:
        return SqlVerdict(False, f"exactly one statement allowed, got {len(statements)}")
    root = statements[0]
    if not isinstance(root, (exp.Select, *_SET_OPS)):
        return SqlVerdict(False, f"only SELECT queries are allowed (got {type(root).__name__.upper()})")
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            return SqlVerdict(False, f"forbidden construct: {type(node).__name__.upper()}")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _DANGEROUS_FUNCS:
                return SqlVerdict(False, f"forbidden function: {name}")
    return SqlVerdict(True, "ok")


_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    getattr(sqlite3, "SQLITE_RECURSIVE", 33),
}


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _src: str | None) -> int:
    """Layer 3: invoked by SQLite for each operation while preparing a statement."""
    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in ALLOWED_FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class QueryTimeout(Exception):
    pass


def open_readonly(db_path: Path, *, timeout_s: float | None = None) -> sqlite3.Connection:
    """Layers 2 + 3. Returns a connection that cannot write, attach, or load extensions."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, check_same_thread=False)
    con.execute("PRAGMA query_only = ON")  # before the authorizer (which denies PRAGMA)
    con.set_authorizer(_authorizer)
    if timeout_s is not None:
        deadline = time.monotonic() + timeout_s
        # Return non-zero to abort the running statement -> sqlite3.OperationalError("interrupted").
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)
    return con
