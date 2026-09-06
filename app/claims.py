"""File-path claims + presence — coordination without personal blocking.

A claim is advisory: it never blocks a mutation (questions are the only
blocking mechanism). Presence rides on credential heartbeats — every
authenticated request stamps last_seen, so an identity whose token has
gone quiet has stale claims.
"""

import os
import sqlite3
import time

from app import events

CLAIM_NOTE_MAX = 500
PATH_MAX = 512


class ClaimError(ValueError):
    """Base claims-service failure."""


def heartbeat_timeout() -> int:
    return int(os.environ.get("SLOPCLANKER_HEARTBEAT_TIMEOUT", "900"))


def _paths_conflict(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _check_path(path: str) -> str:
    if not (isinstance(path, str) and path.strip().startswith("/")):
        raise ClaimError("paths must be absolute")
    if len(path) > PATH_MAX:
        raise ClaimError("path too long")
    return path.rstrip() or "/"


def presence(conn, identity_id: int) -> float | None:
    """Latest credential heartbeat for an identity, if any."""
    row = conn.execute(
        "SELECT MAX(last_seen_at) FROM credentials WHERE identity_id = ?"
        " AND revoked_at IS NULL",
        (identity_id,),
    ).fetchone()
    return row[0] if row else None


def set_claims(conn, actor, paths: list[str], note: str = "") -> int:
    if not isinstance(paths, list) or not paths:
        raise ClaimError("paths required")
    if len(note or "") > CLAIM_NOTE_MAX:
        raise ClaimError("note too long")
    now = time.time()
    with conn:
        for path in paths[:64]:
            conn.execute(
                "INSERT INTO claims(identity_id, path, note, claimed_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(identity_id, path) DO UPDATE SET"
                " note = excluded.note, claimed_at = excluded.claimed_at",
                (actor["id"], _check_path(path), note or "", now),
            )
    events.emit(
        conn,
        actor["id"],
        "claim.set",
        "path",
        0,
        payload={"paths": paths[:64], "note": (note or "")[:200]},
    )
    return conn.execute(
        "SELECT COUNT(*) FROM claims WHERE identity_id = ?", (actor["id"],)
    ).fetchone()[0]


def release_claims(conn, actor, paths: list[str]) -> int:
    if not isinstance(paths, list) or not paths:
        raise ClaimError("paths required")
    with conn:
        for path in paths[:64]:
            conn.execute(
                "DELETE FROM claims WHERE identity_id = ? AND path = ?",
                (actor["id"], _check_path(path)),
            )
    events.emit(
        conn, actor["id"], "claim.released", "path", 0, payload={"paths": paths[:64]}
    )
    return conn.execute(
        "SELECT COUNT(*) FROM claims WHERE identity_id = ?", (actor["id"],)
    ).fetchone()[0]


def check_claims(conn, path: str, actor=None) -> list[dict]:
    """Conflicting claims by OTHERS on this path (or parents/children)."""
    path = _check_path(path)
    timeout = heartbeat_timeout()
    now = time.time()
    out = []
    for row in conn.execute(
        "SELECT c.*, i.name FROM claims c JOIN identities i"
        " ON i.id = c.identity_id ORDER BY c.claimed_at DESC"
    ):
        if actor is not None and row["identity_id"] == actor["id"]:
            continue
        if not _paths_conflict(path, row["path"]):
            continue
        last = presence(conn, row["identity_id"])
        out.append(
            {
                "identity_id": row["identity_id"],
                "name": row["name"],
                "path": row["path"],
                "note": row["note"],
                "claimed_at": row["claimed_at"],
                "stale": last is None or (now - last) > timeout,
            }
        )
    return out


def list_my_claims(conn, actor) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT path, note, claimed_at FROM claims WHERE identity_id = ?"
        " ORDER BY claimed_at DESC",
        (actor["id"],),
    ).fetchall()
