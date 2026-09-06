"""Generic object↔object links — first-class context, listed both ways."""

import time

from app import events

OBJECT_TYPES = {
    "project": "projects",
    "task": "tasks",
    "todo": "todos",
    "discussion": "discussions",
    "decision": "decisions",
    "question": "questions",
    "note": "notes",
    "wiki": "wiki",
}


class LinkError(ValueError):
    """Base links-service failure."""


def _endpoint(conn, obj_type: str, obj_id: int) -> None:
    table = OBJECT_TYPES.get(obj_type)
    if table is None:
        raise LinkError("type must be " + "|".join(sorted(OBJECT_TYPES)))
    if not conn.execute(
        f"SELECT 1 FROM {table} WHERE id = ?",  # noqa: S608 — whitelist  # nosec B608
        (obj_id,),
    ).fetchone():
        raise LinkError(f"no such {obj_type}")


def create(conn, actor, from_type: str, from_id: int, to_type: str, to_id: int) -> int:
    _endpoint(conn, from_type, from_id)
    _endpoint(conn, to_type, to_id)
    if (from_type, from_id) == (to_type, to_id):
        raise LinkError("cannot link an object to itself")
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO links(from_type, from_id, to_type, to_id,"
                " created_by, created_at) VALUES (?,?,?,?,?,?)",
                (from_type, from_id, to_type, to_id, actor["id"], time.time()),
            )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise LinkError("link already exists") from exc
        raise
    events.emit(
        conn,
        actor["id"],
        "link.created",
        from_type,
        from_id,
        payload={"to_type": to_type, "to_id": to_id},
    )
    return cur.lastrowid


def remove(conn, actor, link_id: int) -> None:
    row = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
    if row is None:
        raise LinkError("no such link")
    if actor["kind"] != "human":
        raise LinkError("removing links is human-only")
    with conn:
        conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
    events.emit(
        conn,
        actor["id"],
        "link.removed",
        row["from_type"],
        row["from_id"],
        payload={"to_type": row["to_type"], "to_id": row["to_id"]},
    )


def context_for(conn, obj_type: str, obj_id: int) -> list[dict]:
    """Links in both directions, resolved to titles."""
    rows = conn.execute(
        "SELECT * FROM links WHERE (from_type = ? AND from_id = ?)"
        " OR (to_type = ? AND to_id = ?) ORDER BY id",
        (obj_type, obj_id, obj_type, obj_id),
    ).fetchall()
    out = []
    for r in rows:
        other = (
            (r["to_type"], r["to_id"])
            if r["from_id"] == obj_id and r["from_type"] == obj_type
            else (r["from_type"], r["from_id"])
        )
        direction = "to" if other == (r["to_type"], r["to_id"]) else "from"
        out.append(
            {
                "link_id": r["id"],
                "direction": direction,
                "type": other[0],
                "id": other[1],
                "created_by": r["created_by"],
                "created_at": r["created_at"],
            }
        )
    return out
