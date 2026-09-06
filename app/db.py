"""SQLite storage: schema v2 connection factory and init."""

import sqlite3
from pathlib import Path

from app.schema import SCHEMA_V2, SCHEMA_VERSION


def db_path() -> Path:
    """Active database path (SLOPCLANKER_DB overrides; default /data)."""
    import os

    return Path(os.environ.get("SLOPCLANKER_DB", "/data/slopclanker.db"))


def connect(path: str | Path) -> sqlite3.Connection:
    """Open the v2 database with the house pragmas applied."""
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    """Create (or verify) a fresh schema-v2 database; returns an open conn."""
    conn = connect(path)
    conn.executescript(SCHEMA_V2)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def is_v2(path: str | Path) -> bool:
    """True when the file exists and carries schema_version = v2."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        conn = sqlite3.connect(str(p), timeout=2.0)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return row is not None and row[0] == str(SCHEMA_VERSION)
