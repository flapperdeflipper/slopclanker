"""Hash-chained append-only rows (transitions, events).

Each row commits to its predecessor: row_hash = sha256(prev_hash ||
canonical-json(fields)). Any out-of-band edit — even direct sqlite
tampering — becomes detectable via verify_chain().
"""

import hashlib
import json
import sqlite3


def _canonical(fields: dict) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)


def chained_insert(
    conn: sqlite3.Connection, table: str, fields: dict
) -> tuple[int, str]:
    """Append a row carrying the chain hashes; returns (id, row_hash)."""
    last = conn.execute(
        f"SELECT row_hash FROM {table} ORDER BY id DESC LIMIT 1"  # noqa: S608 — whitelist/placeholder  # nosec B608
    ).fetchone()
    prev = last[0] if last else None
    body = dict(fields)
    body["prev_hash"] = prev
    row_hash = hashlib.sha256(_canonical(body).encode()).hexdigest()
    cols = list(fields) + ["prev_hash", "row_hash"]
    placeholders = ",".join("?" * len(cols))
    cur = conn.execute(
        f"INSERT INTO {table}({','.join(cols)}) VALUES ({placeholders})",  # noqa: S608 — whitelist/placeholder  # nosec B608
        [fields[c] for c in fields] + [prev, row_hash],
    )
    return cur.lastrowid, row_hash


def verify_chain(conn: sqlite3.Connection, table: str) -> tuple[bool, int | None]:
    """True when the whole chain recomputes; else (False, first broken id)."""
    prev = None
    for row in conn.execute(
        f"SELECT * FROM {table} ORDER BY id"  # noqa: S608 — whitelist/placeholder  # nosec B608
    ):
        body = {k: v for k, v in zip(row.keys(), row) if k not in ("row_hash", "id")}
        if body.get("prev_hash") != prev:
            return False, row["id"]
        if hashlib.sha256(_canonical(body).encode()).hexdigest() != row["row_hash"]:
            return False, row["id"]
        prev = row["row_hash"]
    return True, None
