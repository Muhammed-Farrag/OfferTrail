"""SQLite connection and migration runner."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the database with foreign keys, WAL and row access by column name.

    `isolation_level=None` puts sqlite3 in autocommit mode; multi-statement writes use the
    explicit `transaction()` helper so the boundaries are visible in the code.
    """
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = False):
    """BEGIN … COMMIT, rolling back on error. `immediate` takes the write lock up front,
    which is what makes the send-attempt claim safe against concurrent callbacks."""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending numbered migrations from jobradar/db/migrations. Returns applied names."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT)"
    )
    done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    files = sorted(
        (f for f in resources.files("jobradar.db").joinpath("migrations").iterdir()
         if f.name.endswith(".sql")),
        key=lambda f: f.name,
    )
    applied = []
    for f in files:
        if f.name in done:
            continue
        sql = f.read_text(encoding="utf-8")
        # executescript commits implicitly; wrap the migration and its bookkeeping together.
        conn.executescript(
            f"BEGIN;\n{sql}\nINSERT INTO schema_migrations VALUES "
            f"('{f.name}', datetime('now'));\nCOMMIT;"
        )
        applied.append(f.name)
    return applied


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
