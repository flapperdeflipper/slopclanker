"""Per-identity UI preferences (own-only, short string values)."""

import re
import sqlite3
import time

KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_VALUE = 500


def set(conn: sqlite3.Connection, identity_id: int, key: str, value: str) -> None:
    if not KEY_RE.fullmatch(key or ""):
        raise ValueError("bad key")
    conn.execute(
        "INSERT INTO prefs(identity_id, key, value, updated_at)"
        " VALUES(?, ?, ?, ?)"
        " ON CONFLICT(identity_id, key)"
        " DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (identity_id, key, str(value or "")[:MAX_VALUE], time.time()),
    )
    conn.commit()


def get_all(conn: sqlite3.Connection, identity_id: int) -> dict:
    rows = conn.execute(
        "SELECT key, value FROM prefs WHERE identity_id = ?", (identity_id,)
    ).fetchall()
    return {r["key"]: r["value"] for r in rows}
