"""Discussions, comments and chat — DESIGN §2/§6.

Comments live ONLY inside discussions and are immutable for everyone;
humans may trash them (visible to humans, restore/purge admin). Chat is
per-project, append-only, ephemeral (excluded from FTS).
"""

import sqlite3
import time

from app import events

MAX_DEPTH = 4
DISCUSSION_KINDS = ("info", "question", "proposal", "handover")


class CommsError(ValueError):
    """Base comms-service failure."""


def _project(conn, project_id: int, *, allow_archived: bool = False) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise CommsError("no such project")
    if row["archived"] and not allow_archived:
        raise CommsError("project is archived")
    return row


def _discussion(conn, discussion_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM discussions WHERE id = ?", (discussion_id,)
    ).fetchone()
    if row is None:
        raise CommsError("no such discussion")
    return row


def create_discussion(
    conn, actor, project_id: int, title: str, kind: str = "info", body: str = ""
) -> int:
    _project(conn, project_id)
    if not (isinstance(title, str) and title.strip()):
        raise CommsError("title required")
    if len(title) > 200:
        raise CommsError("title too long")
    if len(body or "") > 32768:
        raise CommsError("body too long")
    if kind not in DISCUSSION_KINDS:
        raise CommsError("kind must be " + "|".join(DISCUSSION_KINDS))
    with conn:
        cur = conn.execute(
            "INSERT INTO discussions(project_id, title, body, kind,"
            " created_by, created_at) VALUES (?,?,?,?,?,?)",
            (project_id, title.strip(), body or "", kind, actor["id"], time.time()),
        )
    events.emit(
        conn,
        actor["id"],
        "discussion.created",
        "discussion",
        cur.lastrowid,
        project_id=project_id,
        payload={"title": title.strip(), "kind": kind},
    )
    return cur.lastrowid


def close_discussion(conn, actor, discussion_id: int, outcome: str = "") -> sqlite3.Row:
    d = _discussion(conn, discussion_id)
    is_admin = actor["kind"] == "human" and actor["role"] in ("admin", "superadmin")
    if d["created_by"] != actor["id"] and not is_admin:
        raise CommsError("creator or admin only")
    if d["status"] != "open":
        raise CommsError("discussion already closed")
    with conn:
        conn.execute(
            "UPDATE discussions SET status = 'closed', outcome = ?,"
            " closed_by = ?, closed_at = ? WHERE id = ?",
            (outcome, actor["id"], time.time(), discussion_id),
        )
    events.emit(
        conn,
        actor["id"],
        "discussion.closed",
        "discussion",
        discussion_id,
        project_id=d["project_id"],
        payload={"outcome": outcome},
    )
    return _discussion(conn, discussion_id)


def reopen_discussion(conn, actor, discussion_id: int) -> sqlite3.Row:
    d = _discussion(conn, discussion_id)
    if not (actor["kind"] == "human" and actor["role"] in ("admin", "superadmin")):
        raise CommsError("admins only")
    if d["status"] != "closed":
        raise CommsError("discussion is open")
    with conn:
        conn.execute(
            "UPDATE discussions SET status = 'open', outcome = NULL,"
            " closed_by = NULL, closed_at = NULL WHERE id = ?",
            (discussion_id,),
        )
    events.emit(
        conn,
        actor["id"],
        "discussion.reopened",
        "discussion",
        discussion_id,
        project_id=d["project_id"],
        payload={},
    )
    return _discussion(conn, discussion_id)


def list_discussions(conn, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM discussions WHERE project_id = ? ORDER BY id", (project_id,)
    ).fetchall()


def add_comment(
    conn, actor, discussion_id: int, body: str, parent_id: int | None = None
) -> int:
    d = _discussion(conn, discussion_id)
    _project(conn, d["project_id"])
    if d["status"] != "open":
        raise CommsError("discussion is closed")
    if not (isinstance(body, str) and body.strip()):
        raise CommsError("body required")
    if len(body) > 32768:
        raise CommsError("body too long")
    depth = 0
    pid = parent_id
    while pid is not None and depth < MAX_DEPTH + 1:
        parent = conn.execute(
            "SELECT id, parent_id FROM comments WHERE id = ? AND discussion_id = ?",
            (pid, discussion_id),
        ).fetchone()
        if parent is None:
            raise CommsError("no such parent comment")
        pid = parent["parent_id"]
        depth += 1
    if depth >= MAX_DEPTH:
        raise CommsError(f"nesting deeper than {MAX_DEPTH} not allowed")
    with conn:
        cur = conn.execute(
            "INSERT INTO comments(discussion_id, parent_id, author_id, body,"
            " created_at) VALUES (?,?,?,?,?)",
            (discussion_id, parent_id, actor["id"], body.strip(), time.time()),
        )
    events.emit(
        conn,
        actor["id"],
        "comment.added",
        "discussion",
        discussion_id,
        project_id=d["project_id"],
        payload={"comment_id": cur.lastrowid, "parent_id": parent_id},
    )
    return cur.lastrowid


def list_comments(conn, discussion_id: int, actor) -> list[sqlite3.Row]:
    """Trashed comments stay visible to humans (with marker); hidden else."""
    _discussion(conn, discussion_id)
    if actor["kind"] == "human":
        return conn.execute(
            "SELECT * FROM comments WHERE discussion_id = ? ORDER BY id",
            (discussion_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM comments WHERE discussion_id = ? AND trashed_at IS NULL"
        " ORDER BY id",
        (discussion_id,),
    ).fetchall()


def trash_comment(conn, actor, comment_id: int) -> None:
    row = conn.execute(
        "SELECT * FROM comments WHERE id = ? AND trashed_at IS NULL", (comment_id,)
    ).fetchone()
    if row is None:
        raise CommsError("no such comment")
    if actor["kind"] != "human":
        raise CommsError("trash is human-only")
    with conn:
        conn.execute(
            "UPDATE comments SET trashed_at = ?, trashed_by = ? WHERE id = ?",
            (time.time(), actor["id"], comment_id),
        )
    events.emit(conn, actor["id"], "comment.trashed", "comment", comment_id, payload={})


def _is_admin(actor) -> bool:
    return actor["kind"] == "human" and actor["role"] in ("admin", "superadmin")


def restore_comment(conn, actor, comment_id: int) -> None:
    row = conn.execute(
        "SELECT * FROM comments WHERE id = ? AND trashed_at IS NOT NULL", (comment_id,)
    ).fetchone()
    if row is None:
        raise CommsError("no such trashed comment")
    if not _is_admin(actor):
        raise CommsError("admins only")
    with conn:
        conn.execute(
            "UPDATE comments SET trashed_at = NULL, trashed_by = NULL WHERE id = ?",
            (comment_id,),
        )
    events.emit(
        conn, actor["id"], "comment.restored", "comment", comment_id, payload={}
    )


def purge_comment(conn, actor, comment_id: int) -> None:
    row = conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,)).fetchone()
    if row is None:
        raise CommsError("no such comment")
    if not _is_admin(actor):
        raise CommsError("admins only")
    with conn:
        conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    events.emit(conn, actor["id"], "comment.purged", "comment", comment_id, payload={})


def post_chat(conn, actor, project_id: int, body: str) -> int:
    _project(conn, project_id)
    if not (isinstance(body, str) and body.strip()):
        raise CommsError("body required")
    if len(body) > 4096:
        raise CommsError("chat messages are capped at 4096 chars")
    with conn:
        cur = conn.execute(
            "INSERT INTO chat(project_id, author_id, body, created_at)"
            " VALUES (?,?,?,?)",
            (project_id, actor["id"], body.strip(), time.time()),
        )
    events.emit(
        conn,
        actor["id"],
        "chat.posted",
        "project",
        project_id,
        project_id=project_id,
        payload={"chat_id": cur.lastrowid},
    )
    return cur.lastrowid


def list_chat(
    conn, project_id: int, since_id: int = 0, limit: int = 200
) -> list[sqlite3.Row]:
    _project(conn, project_id, allow_archived=True)
    limit = max(1, min(limit, 200))
    return conn.execute(
        "SELECT * FROM chat WHERE project_id = ? AND id > ? ORDER BY id LIMIT ?",
        (project_id, since_id, limit),
    ).fetchall()
