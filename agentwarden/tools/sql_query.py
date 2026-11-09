"""Read-only SQL tool over the scenario's SQLite business database."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agentwarden.gateway.spec import ToolContext, ToolOutput, ToolSpec, Trust
from agentwarden.reliability.errors import PermanentToolError
from agentwarden.security.sql_guard import open_readonly, validate_select

MAX_ROWS = 200
SHOWN_ROWS = 50


class SqlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(..., min_length=1, max_length=2000, description="A single read-only SQLite SELECT statement")


def describe_schema(db_path: Path) -> str:
    """Trusted schema summary for the tool description (read by our code, not by the agent)."""
    con = open_readonly(db_path)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        lines = []
        for t in tables:
            # PRAGMA is blocked by our own authorizer, so read column names from an empty SELECT.
            cols = [d[0] for d in con.execute(f'SELECT * FROM "{t}" LIMIT 0').description]
            lines.append(f"{t}({', '.join(cols)})")
        return "\n".join(lines)
    finally:
        con.close()


def _sql_policy(args: SqlArgs) -> tuple[str, str] | None:
    verdict = validate_select(args.query)
    return None if verdict.ok else ("SQL_READONLY", verdict.reason)


def make_sql_tool(db_path: Path, *, timeout_s: float = 5.0, schema_notes: str = "") -> ToolSpec:
    db_path = Path(db_path)
    schema = describe_schema(db_path)

    def run(args: SqlArgs, ctx: ToolContext) -> ToolOutput:
        con = open_readonly(db_path, timeout_s=timeout_s)
        try:
            cur = con.execute(args.query)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(MAX_ROWS + 1)
        except sqlite3.DatabaseError as e:
            msg = str(e)
            if msg == "interrupted":
                raise PermanentToolError(f"query exceeded {timeout_s:.0f}s and was cancelled; simplify it") from e
            if "not authorized" in msg:
                raise PermanentToolError(f"blocked by read-only database engine: {msg}") from e
            raise PermanentToolError(f"SQL error: {msg}") from e
        finally:
            con.close()
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
        shown = rows[:SHOWN_ROWS]
        lines = [" | ".join(cols)] + [" | ".join("NULL" if v is None else str(v) for v in r) for r in shown]
        note = f"\n({len(rows)} rows{'+' if truncated else ''}; showing {len(shown)})"
        return ToolOutput(content="\n".join(lines) + note, data={"columns": cols, "rows": [list(r) for r in rows]},
                          meta={"row_count": len(rows), "truncated": truncated})

    return ToolSpec(
        name="sql_query",
        description=(
            "Run ONE read-only SQLite SELECT query against the business database and return the rows. "
            "Writes are impossible. Prefer aggregate queries (COUNT/SUM/AVG/GROUP BY) over listing rows. "
            f"Tables:\n{schema}\n{schema_notes}"
        ).strip(),
        args_model=SqlArgs,
        fn=run,
        required_capabilities=frozenset({"sql:read"}),
        idempotent=True,
        output_trust=Trust.UNTRUSTED,  # rows include customer-written ticket text
        timeout_s=timeout_s + 2.0,     # outer timeout > inner progress-handler timeout
        max_calls_per_execution=12,
        policy=_sql_policy,
    )
