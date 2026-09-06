"""The nine-state task machine — DESIGN §3, enforced in ONE place.

No route may set `state` directly; everything goes through transition().
Human-only: approve (proposed->approved), done (review->done), trash,
not-done/restore. Attempted violations are logged as events.
"""

import sqlite3
import time

from app import events

TRANSITIONS: dict[str, set[str]] = {
    "idea": {"plan", "paused", "trashed"},
    "plan": {"proposed", "paused", "trashed"},
    "proposed": {"approved", "plan", "paused", "trashed"},
    "approved": {"building", "paused", "trashed"},
    "building": {"review", "paused", "trashed"},
    "review": {"done", "building", "plan", "approved", "paused", "trashed"},
    "paused": {"plan", "proposed", "approved", "building", "review", "trashed"},
    "done": {"previous"},
    "trashed": {"previous"},
}

DEFAULT_PREVIOUS = {"done": "review", "trashed": "plan"}


class TransitionError(ValueError):
    """Base state-machine failure."""


class IllegalTransition(TransitionError):
    """from -> to is not in the table."""


class HumanRequired(TransitionError):
    """Only humans may perform this transition."""


class BlockedByQuestions(TransitionError):
    """Open questions attached; the task is frozen."""

    def __init__(self, questions: list[dict]):
        super().__init__("task is frozen by open questions")
        self.questions = questions


class ProofRequired(TransitionError):
    """building -> review needs an MR/PR proof link or a human waiver."""


class TodosOutstanding(TransitionError):
    """review -> done blocked by unticked todos; override needs a reason."""


class VersionConflict(TransitionError):
    """Stale write; refresh and retry."""


def is_human_only(frm: str, to: str) -> bool:
    return (
        to == "trashed"
        or to == "previous"
        or (frm, to) in {("proposed", "approved"), ("review", "done")}
    )


def open_questions_on(
    conn: sqlite3.Connection, attach_type: str, attach_id: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT id, body, asked_by FROM questions"
        " WHERE status = 'open' AND attach_type = ? AND attach_id = ?",
        (attach_type, attach_id),
    ).fetchall()
    return [dict(r) for r in rows]


def _proof_ok(conn: sqlite3.Connection, task: sqlite3.Row) -> bool:
    if task["proof_waived"]:
        return True
    row = conn.execute(
        "SELECT 1 FROM proofs WHERE task_id = ? AND trashed_at IS NULL"
        " AND kind IN ('mr','pr') LIMIT 1",
        (task["id"],),
    ).fetchone()
    return row is not None


def _todos_outstanding(conn: sqlite3.Connection, task_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM todos WHERE task_id = ? AND done = 0"
        " AND trashed_at IS NULL",
        (task_id,),
    ).fetchone()[0]


def transition(
    conn: sqlite3.Connection,
    task_id: int,
    to_state: str,
    actor: dict | sqlite3.Row,
    note: str = "",
    version: int | None = None,
) -> sqlite3.Row:
    """Validate and apply one transition; append-only log + event."""
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise LookupError("no such task")
    if version is not None and version != task["version"]:
        raise VersionConflict("stale version; refresh and retry")

    frm = task["state"]
    target = to_state
    if to_state == "previous":
        target = task["previous_state"] or DEFAULT_PREVIOUS.get(frm, "plan")

    if to_state not in TRANSITIONS.get(frm, set()):
        raise IllegalTransition(f"cannot go {frm} -> {to_state}")
    if is_human_only(frm, to_state) and actor["kind"] != "human":
        events.emit(
            conn,
            actor["id"],
            "task.transition_denied",
            "task",
            task_id,
            project_id=task["project_id"],
            payload={"from": frm, "to": to_state, "reason": "human-only"},
        )
        raise HumanRequired(f"{frm} -> {to_state} is human-only")

    frozen = open_questions_on(conn, "task", task_id)
    if frozen:
        raise BlockedByQuestions(frozen)

    if frm == "building" and target == "review" and not _proof_ok(conn, task):
        raise ProofRequired(
            "building -> review requires an MR/PR proof link or a human waiver"
        )

    if frm == "review" and target == "done":
        outstanding = _todos_outstanding(conn, task_id)
        if outstanding and not (note or "").strip():
            raise TodosOutstanding(
                f"{outstanding} unticked todos; force done with a reason note"
            )

    now = time.time()
    previous_state = frm if target in ("done", "trashed") else None
    with conn:
        conn.execute(
            "UPDATE tasks SET state = ?, previous_state = ?,"
            " state_changed_by = ?, state_changed_at = ?, version = version + 1"
            " WHERE id = ?",
            (target, previous_state, actor["id"], now, task_id),
        )
        from app import chain

        chain.chained_insert(
            conn,
            "transitions",
            {
                "task_id": task_id,
                "from_state": frm,
                "to_state": target,
                "actor_id": actor["id"],
                "note": note or "",
                "created_at": now,
            },
        )
    to_identity = task["assignee_id"] if frm == "done" else None
    events.emit(
        conn,
        actor["id"],
        "task.transitioned",
        "task",
        task_id,
        project_id=task["project_id"],
        to_identity_id=to_identity,
        payload={"from": frm, "to": target, "note": note or "", "title": task["title"]},
    )
    if target in ("proposed", "review"):
        from app import notify

        notify.notify(
            conn,
            "attention",
            f"task '{task['title']}' arrived in {target}",
        )
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
