"""Blocking clarifications — DESIGN §11.

An open question attached to object O freezes O for everyone (humans
included) until answered or withdrawn. Unattached questions block
nothing. Talking about the problem stays possible: asking further
questions, discussions, chat and links are never frozen.
"""

import sqlite3
import time

from app import events, notify, ratelimit
from app.statemachine import open_questions_on  # noqa: F401 — re-exported use

GROUPS = ("humans", "clankers", "everyone")
ASK_RATE = (10, 3600)


class QuestionError(ValueError):
    """Base questions-service failure."""


class InvalidTarget(QuestionError):
    pass


class NotAddressee(QuestionError):
    pass


class AlreadyResolved(QuestionError):
    pass


class RateLimited(QuestionError):
    pass


def _project_exists(conn, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise QuestionError("no such project")
    return row


def ask(
    conn,
    actor,
    project_id: int,
    body: str,
    *,
    to_identity_id: int | None = None,
    to_group: str | None = None,
    attach_type: str | None = None,
    attach_id: int | None = None,
) -> int:
    """Ask; optionally attach to one object. Rate-limited per identity."""
    _project_exists(conn, project_id)
    if not (isinstance(body, str) and body.strip()):
        raise QuestionError("body required")
    if (to_identity_id is None) == (to_group is None):
        raise QuestionError("exactly one of to_identity_id or to_group")
    if to_group is not None and to_group not in GROUPS:
        raise InvalidTarget("group must be humans|clankers|everyone")
    if to_identity_id is not None:
        row = conn.execute(
            "SELECT * FROM identities WHERE id = ? AND status = 'active'",
            (to_identity_id,),
        ).fetchone()
        if row is None:
            raise InvalidTarget("no such addressee")
    if not ratelimit.allow(f"ask:{actor['id']}", ASK_RATE[0], ASK_RATE[1]):
        raise RateLimited("too many questions; try later")
    if attach_type is not None or attach_id is not None:
        if attach_type is None or attach_id is None:
            raise QuestionError("attach needs both type and id")
        _attach_exists(conn, attach_type, attach_id)

    with conn:
        cur = conn.execute(
            "INSERT INTO questions(project_id, body, asked_by, asked_to_id,"
            " asked_to_group, attach_type, attach_id, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,'open',?)",
            (
                project_id,
                body.strip(),
                actor["id"],
                to_identity_id,
                to_group,
                attach_type,
                attach_id,
                time.time(),
            ),
        )
    qid = cur.lastrowid
    events.emit(
        conn,
        actor["id"],
        "question.asked",
        "question",
        qid,
        project_id=project_id,
        to_identity_id=to_identity_id,
        group=to_group,
        payload={
            "body": body.strip(),
            "group": to_group,
            "attach_type": attach_type,
            "attach_id": attach_id,
        },
    )
    if to_identity_id is not None:
        target = conn.execute(
            "SELECT kind FROM identities WHERE id = ?", (to_identity_id,)
        ).fetchone()
        if target and target["kind"] == "human":
            notify.notify(
                conn,
                "attention",
                f"question for you: {body.strip()[:120]}",
                identity_id=to_identity_id,
            )
    elif to_group in ("humans", "everyone"):
        notify.notify(
            conn, "attention", f"question for {to_group}: {body.strip()[:120]}"
        )
    return qid


def _attach_exists(conn, attach_type: str, attach_id: int) -> None:
    tables = {
        "project": "projects",
        "task": "tasks",
        "todo": "todos",
        "discussion": "discussions",
        "decision": "decisions",
        "question": "questions",
        "note": "notes",
        "wiki": "wiki",
    }
    table = tables.get(attach_type)
    if table is None:
        raise QuestionError("bad attach type")
    if not conn.execute(
        f"SELECT 1 FROM {table} WHERE id = ?",  # noqa: S608 — whitelist  # nosec B608
        (attach_id,),
    ).fetchone():
        raise QuestionError("no such attached object")


def _is_addressee(conn, actor, q: sqlite3.Row) -> bool:
    if q["asked_to_id"] == actor["id"]:
        return True
    group = q["asked_to_group"]
    if group == "everyone":
        return True
    if group == "humans":
        return actor["kind"] == "human"
    if group == "clankers":
        return actor["kind"] == "clanker"
    return False


def _get(conn, qid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()
    if row is None:
        raise QuestionError("no such question")
    return row


def answer(conn, actor, qid: int, answer: str) -> sqlite3.Row:
    q = _get(conn, qid)
    if q["status"] != "open":
        raise AlreadyResolved(f"question is {q['status']}")
    if not _is_addressee(conn, actor, q):
        raise NotAddressee("only the addressee may answer")
    if not (isinstance(answer, str) and answer.strip()):
        raise QuestionError("answer text required")
    with conn:
        conn.execute(
            "UPDATE questions SET status = 'answered', answer = ?,"
            " answered_by = ?, answered_at = ? WHERE id = ?",
            (answer.strip(), actor["id"], time.time(), qid),
        )
    events.emit(
        conn,
        actor["id"],
        "question.answered",
        "question",
        qid,
        project_id=q["project_id"],
        to_identity_id=q["asked_by"],
        payload={"answer": answer.strip()[:200]},
    )
    events.resolve(conn, "question", qid)
    asker = conn.execute(
        "SELECT kind FROM identities WHERE id = ?", (q["asked_by"],)
    ).fetchone()
    if asker and asker["kind"] == "human":
        notify.notify(
            conn, "attention", "your question was answered", identity_id=q["asked_by"]
        )
    return _get(conn, qid)


def withdraw(conn, actor, qid: int) -> sqlite3.Row:
    q = _get(conn, qid)
    if q["status"] != "open":
        raise AlreadyResolved(f"question is {q['status']}")
    if q["asked_by"] != actor["id"] and not _is_admin(actor):
        raise QuestionError("asker or admin only")
    with conn:
        conn.execute(
            "UPDATE questions SET status = 'withdrawn', withdrawn_by = ?,"
            " withdrawn_at = ? WHERE id = ?",
            (actor["id"], time.time(), qid),
        )
    events.emit(
        conn,
        actor["id"],
        "question.withdrawn",
        "question",
        qid,
        project_id=q["project_id"],
        payload={},
    )
    events.resolve(conn, "question", qid)
    return _get(conn, qid)


def reassign(conn, actor, qid: int, to_identity_id: int) -> sqlite3.Row:
    q = _get(conn, qid)
    if not _is_admin(actor):
        raise QuestionError("admins only")
    if q["status"] != "open":
        raise AlreadyResolved(f"question is {q['status']}")
    row = conn.execute(
        "SELECT 1 FROM identities WHERE id = ? AND status = 'active'",
        (to_identity_id,),
    ).fetchone()
    if row is None:
        raise InvalidTarget("no such addressee")
    with conn:
        conn.execute(
            "UPDATE questions SET asked_to_id = ?, asked_to_group = NULL WHERE id = ?",
            (to_identity_id, qid),
        )
    events.emit(
        conn,
        actor["id"],
        "question.reassigned",
        "question",
        qid,
        project_id=q["project_id"],
        to_identity_id=to_identity_id,
        payload={},
    )
    return _get(conn, qid)


def _is_admin(actor) -> bool:
    return actor["kind"] == "human" and actor["role"] in ("admin", "superadmin")


def list_questions(
    conn,
    *,
    open_only: bool = False,
    to_actor=None,
    attach_type: str | None = None,
    attach_id: int | None = None,
    project_id: int | None = None,
) -> list[sqlite3.Row]:
    q = "SELECT * FROM questions WHERE 1=1"
    args: list = []
    if open_only:
        q += " AND status = 'open'"
    if to_actor is not None:
        q += " AND (asked_to_id = ? OR asked_to_group IN (?, ?))"
        kind_group = "humans" if to_actor["kind"] == "human" else "clankers"
        args += [to_actor["id"], "everyone", kind_group]
    if attach_type is not None:
        q += " AND attach_type = ?"
        args.append(attach_type)
    if attach_id is not None:
        q += " AND attach_id = ?"
        args.append(attach_id)
    if project_id is not None:
        q += " AND project_id = ?"
        args.append(project_id)
    return conn.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()
