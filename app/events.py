"""Typed, hash-chained events: every mutation publishes one.

Addressed events (to_identity_id or a group) materialize into the
durable inbox; the in-process bus carries them to live SSE/wait
subscribers. Group questions resolve: answer/withdraw marks the
question's inbox rows read.
"""

import json
import sqlite3
import time

from app import chain
from app.bus import bus

GROUP_MAX = 500


def emit(
    conn: sqlite3.Connection,
    actor_id: int,
    verb: str,
    obj_type: str,
    obj_id: int,
    project_id: int | None = None,
    to_identity_id: int | None = None,
    group: str | None = None,
    payload: dict | None = None,
) -> int:
    """Append one event; fan out to inbox + bus. Returns event id."""
    ev_id, _ = chain.chained_insert(
        conn,
        "events",
        {
            "ts": time.time(),
            "actor_id": actor_id,
            "verb": verb,
            "obj_type": obj_type,
            "obj_id": obj_id,
            "project_id": project_id,
            "to_identity_id": to_identity_id,
            "payload": json.dumps(payload or {}),
        },
    )
    targets: list[int] = []
    if to_identity_id is not None:
        targets.append(to_identity_id)
    if group in ("humans", "clankers", "everyone"):
        kind = {"humans": "human", "clankers": "clanker"}.get(group)
        if kind:
            rows = conn.execute(
                "SELECT id FROM identities WHERE kind = ? AND status = 'active'"
                " LIMIT ?",
                (kind, GROUP_MAX),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM identities WHERE status = 'active' LIMIT ?",
                (GROUP_MAX,),
            ).fetchall()
        targets += [r[0] for r in rows if r[0] not in targets]
    for tid in targets:
        conn.execute(
            "INSERT OR IGNORE INTO inbox(identity_id, event_id) VALUES (?,?)",
            (tid, ev_id),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (ev_id,)).fetchone()
    event = dict(zip(row.keys(), row))
    try:
        event["payload"] = json.loads(event["payload"])
    except (TypeError, ValueError):
        pass
    bus.publish(event)
    return ev_id


def resolve(conn: sqlite3.Connection, obj_type: str, obj_id: int) -> None:
    """Mark all inbox copies of this object's events read (e.g. answered)."""
    with conn:
        conn.execute(
            "UPDATE inbox SET read_at = ? WHERE read_at IS NULL AND event_id IN"
            " (SELECT id FROM events WHERE obj_type = ? AND obj_id = ?)",
            (time.time(), obj_type, obj_id),
        )


def unread_for(
    conn: sqlite3.Connection,
    identity_id: int,
    *,
    obj_type: str | None = None,
    obj_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    rows = conn.execute(
        "SELECT e.*, i.read_at FROM inbox i JOIN events e ON e.id = i.event_id"
        " WHERE i.identity_id = :i AND i.read_at IS NULL"
        " AND (:t IS NULL OR e.obj_type = :t) AND (:o IS NULL OR e.obj_id = :o)"
        " ORDER BY e.id DESC LIMIT :l",
        {"i": identity_id, "t": obj_type, "o": obj_id, "l": limit},
    )
    return [dict(zip(r.keys(), r)) for r in rows]


def mark_read(conn: sqlite3.Connection, identity_id: int, event_ids: list[int]) -> int:
    if not event_ids:
        return 0
    marks = ",".join("?" * len(event_ids))
    with conn:
        cur = conn.execute(
            f"UPDATE inbox SET read_at = ? WHERE identity_id = ? AND read_at IS NULL"  # noqa: S608 — whitelist/placeholder  # nosec B608
            f" AND event_id IN ({marks})",
            [time.time(), identity_id, *event_ids],
        )
    return cur.rowcount


def feed_recent(
    conn: sqlite3.Connection,
    *,
    project_id: int | None = None,
    obj_type: str | None = None,
    obj_id: int | None = None,
    to_identity_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    """Newest-first tail of the log for list UIs."""
    limit = max(1, min(limit, 500))
    q = "SELECT * FROM events WHERE 1=1"
    args: list = []
    if project_id is not None:
        q += " AND project_id = ?"
        args.append(project_id)
    if obj_type is not None:
        q += " AND obj_type = ?"
        args.append(obj_type)
    if obj_id is not None:
        q += " AND obj_id = ?"
        args.append(obj_id)
    if to_identity_id is not None:
        q += " AND (to_identity_id = ? OR payload LIKE ?)"
        args += [to_identity_id, f'%"{to_identity_id}"%']
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(q, args)]


def feed(
    conn: sqlite3.Connection,
    *,
    since: int = 0,
    project_id: int | None = None,
    obj_type: str | None = None,
    obj_id: int | None = None,
    to_identity_id: int | None = None,
    limit: int = 100,
) -> list[dict]:
    limit = max(1, min(limit, 500))
    q = "SELECT * FROM events WHERE id > ?"
    args: list = [since]
    if project_id is not None:
        q += " AND project_id = ?"
        args.append(project_id)
    if obj_type is not None:
        q += " AND obj_type = ?"
        args.append(obj_type)
    if obj_id is not None:
        q += " AND obj_id = ?"
        args.append(obj_id)
    if to_identity_id is not None:
        q += " AND to_identity_id = ?"
        args.append(to_identity_id)
    rows = conn.execute(q + " ORDER BY id LIMIT ?", [*args, limit]).fetchall()
    out = []
    for r in rows:
        d = dict(zip(r.keys(), r))
        try:
            d["payload"] = json.loads(d["payload"])
        except (TypeError, ValueError):
            pass
        out.append(d)
    return out
