"""The decision register — DESIGN §2: proposed by anyone, decided by humans.

Decisions are never edited; superseding links the prior decision.
"""

import sqlite3
import time

from app import events
from app.statemachine import open_questions_on

STATUSES = ("accepted", "rejected", "superseded")


class DecisionError(ValueError):
    """Base decisions-service failure."""


def _get(conn, decision_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    if row is None:
        raise DecisionError("no such decision")
    return row


def _project(conn, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise DecisionError("no such project")
    if row["archived"]:
        raise DecisionError("project is archived")
    return row


def create(conn, actor, project_id: int, title: str, context: str = "") -> int:
    _project(conn, project_id)
    if not (isinstance(title, str) and title.strip()):
        raise DecisionError("title required")
    if len(context or "") > 32768:
        raise DecisionError("context too long")
    with conn:
        cur = conn.execute(
            "INSERT INTO decisions(project_id, title, context, status,"
            " created_by, created_at) VALUES (?,?,?,'proposed',?,?)",
            (project_id, title.strip(), context or "", actor["id"], time.time()),
        )
    events.emit(
        conn,
        actor["id"],
        "decision.created",
        "decision",
        cur.lastrowid,
        project_id=project_id,
        payload={"title": title.strip()},
    )
    return cur.lastrowid


def list_decisions(conn, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM decisions WHERE project_id = ? ORDER BY id", (project_id,)
    ).fetchall()


def set_status(
    conn, actor, decision_id: int, status: str, supersede_id: int | None = None
) -> sqlite3.Row:
    d = _get(conn, decision_id)
    if status not in STATUSES:
        raise DecisionError("status must be " + "|".join(STATUSES))
    if actor["kind"] != "human":
        raise DecisionError("deciding is human-only")
    if d["status"] != "proposed":
        raise DecisionError(f"decision is already {d['status']}")
    if open_questions_on(conn, "decision", decision_id):
        raise DecisionError("decision is frozen by open questions")
    if status == "superseded":
        if supersede_id is None:
            raise DecisionError("superseding requires the prior decision id")
        prior = _get(conn, supersede_id)
        if prior["project_id"] != d["project_id"]:
            raise DecisionError("cannot supersede across projects")
        if prior["id"] == decision_id:
            raise DecisionError("cannot supersede itself")
    with conn:
        conn.execute(
            "UPDATE decisions SET status = ?, decided_by = ?, decided_at = ?,"
            " outcome = CASE WHEN ? = 'superseded' THEN"
            " 'superseded by #' || ? ELSE outcome END,"
            " supersedes_id = ? WHERE id = ?",
            (
                status,
                actor["id"],
                time.time(),
                status,
                str(supersede_id or ""),
                supersede_id if status == "superseded" else None,
                decision_id,
            ),
        )
    events.emit(
        conn,
        actor["id"],
        "decision." + status,
        "decision",
        decision_id,
        project_id=d["project_id"],
        payload={"supersede_id": supersede_id},
    )
    return _get(conn, decision_id)
