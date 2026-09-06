"""Notifications: the human attention queue rows."""

import sqlite3
import time


def notify(
    conn: sqlite3.Connection,
    kind: str,
    body: str,
    identity_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO notifications(identity_id, kind, body, created_at)"
        " VALUES (?,?,?,?)",
        (identity_id, kind, body, time.time()),
    )
    conn.commit()
