"""SQL writes must be blocked at every layer independently."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from agentwarden.security.sql_guard import _authorizer, open_readonly, validate_select

FORBIDDEN = [
    "INSERT INTO users(id) VALUES (999999)",
    "UPDATE users SET plan='pro'",
    "DELETE FROM users",
    "DROP TABLE users",
    "CREATE TABLE t(a)",
    "ALTER TABLE users ADD COLUMN x",
    "ATTACH DATABASE 'x.db' AS x",
    "DETACH DATABASE x",
    "PRAGMA writable_schema=ON",
    "REPLACE INTO products VALUES (1,'a','b','c',1)",
    "VACUUM",
    "SELECT 1; DROP TABLE users",
    "WITH x AS (SELECT 1) DELETE FROM users",
    "SELECT load_extension('evil')",
]


@pytest.mark.parametrize("sql", ["SELECT COUNT(*) FROM users", "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
                                 "SELECT 1 UNION ALL SELECT 2",
                                 "SELECT plan, COUNT(*) FROM users GROUP BY plan ORDER BY 2 DESC LIMIT 3"])
def test_layer1_allows_reads(sql):
    assert validate_select(sql).ok


@pytest.mark.parametrize("sql", FORBIDDEN)
def test_layer1_rejects_writes(sql):
    assert not validate_select(sql).ok


@pytest.mark.parametrize("sql", [s for s in FORBIDDEN if ";" not in s])
def test_layer2_readonly_connection_alone_blocks_writes(db_path, tmp_path, sql):
    """mode=ro + query_only WITHOUT the authorizer: the engine still refuses to write."""
    copy = tmp_path / "copy.db"
    shutil.copy(db_path, copy)
    con = sqlite3.connect(copy.resolve().as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    before = copy.read_bytes()
    try:
        con.execute(sql)
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        con.close()
    assert copy.read_bytes() == before
    # ATTACH may "succeed" read-only but must not create a file
    assert not (tmp_path / "x.db").exists()


@pytest.mark.parametrize("sql", [s for s in FORBIDDEN if ";" not in s])
def test_layer3_authorizer_alone_blocks_writes(db_path, tmp_path, sql):
    """A WRITABLE connection with only the authorizer installed must still refuse."""
    copy = tmp_path / "copy.db"
    shutil.copy(db_path, copy)
    con = sqlite3.connect(copy)
    con.set_authorizer(_authorizer)
    with pytest.raises(sqlite3.DatabaseError):
        con.execute(sql)
    con.close()


def test_full_stack_connection_reads_and_blocks(db_path):
    con = open_readonly(db_path)
    assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 6000
    for sql in ("DELETE FROM users", "ATTACH DATABASE 'y.db' AS y", "PRAGMA query_only=OFF"):
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(sql)
    con.close()


def test_multiple_statements_refused_by_driver(db_path):
    con = open_readonly(db_path)
    with pytest.raises((sqlite3.ProgrammingError, sqlite3.Warning)):
        con.execute("SELECT 1; SELECT 2")
    con.close()


def test_query_timeout_cancels_expensive_query(db_path):
    con = open_readonly(db_path, timeout_s=0.3)
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        con.execute("SELECT COUNT(*) FROM orders a, orders b").fetchall()
    con.close()
