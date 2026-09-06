"""Notes (per project) and wiki (global) — editable, revisions always visible."""

import re
import sqlite3
import time

from app import events
from app.statemachine import open_questions_on

MAX_TITLE = 200
MAX_TEXT = 32768
WIKI_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class KnowledgeError(ValueError):
    """Base knowledge-service failure."""


class Frozen(KnowledgeError):
    def __init__(self, questions: list[dict]):
        super().__init__("object frozen by open questions")
        self.questions = questions


def _guard(conn, attach_type: str, attach_id: int) -> None:
    frozen = open_questions_on(conn, attach_type, attach_id)
    if frozen:
        raise Frozen(frozen)


def _title(title: str) -> str:
    if not (isinstance(title, str) and title.strip()):
        raise KnowledgeError("title required")
    if len(title) > MAX_TITLE:
        raise KnowledgeError("title too long")
    return title.strip()


def _body(body: str | None) -> str:
    if body is None:
        return ""
    if len(body) > MAX_TEXT:
        raise KnowledgeError("body too long")
    return body


# --- notes -------------------------------------------------------------


def create_note(
    conn, actor, project_id: int, title: str, body: str = "", tags: str = ""
) -> int:
    row = conn.execute(
        "SELECT archived FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise KnowledgeError("no such project")
    if row["archived"]:
        raise KnowledgeError("project is archived")
    with conn:
        cur = conn.execute(
            "INSERT INTO notes(project_id, title, body, tags, created_by,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (
                project_id,
                _title(title),
                _body(body),
                tags or "",
                actor["id"],
                time.time(),
                time.time(),
            ),
        )
    events.emit(
        conn,
        actor["id"],
        "note.created",
        "note",
        cur.lastrowid,
        project_id=project_id,
        payload={"title": title.strip()},
    )
    return cur.lastrowid


def list_notes(conn, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM notes WHERE project_id = ? ORDER BY updated_at DESC",
        (project_id,),
    ).fetchall()


def get_note(conn, note_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise KnowledgeError("no such note")
    return row


def edit_note(
    conn,
    actor,
    note_id: int,
    *,
    title: str | None = None,
    body: str | None = None,
    tags: str | None = None,
) -> sqlite3.Row:
    note = get_note(conn, note_id)
    _guard(conn, "note", note_id)
    if title is not None:
        title = _title(title)
    if body is not None:
        body = _body(body)
    with conn:
        if (title is not None and title != note["title"]) or (
            body is not None and body != note["body"]
        ):
            conn.execute(
                "INSERT INTO note_revisions(note_id, title, body, edited_by,"
                " created_at) VALUES (?,?,?,?,?)",
                (
                    note_id,
                    title or note["title"],
                    body or note["body"],
                    actor["id"],
                    time.time(),
                ),
            )
        conn.execute(
            "UPDATE notes SET title = COALESCE(?, title),"
            " body = COALESCE(?, body), tags = COALESCE(?, tags),"
            " updated_at = ? WHERE id = ?",
            (title, body, tags, time.time(), note_id),
        )
    events.emit(
        conn,
        actor["id"],
        "note.edited",
        "note",
        note_id,
        project_id=note["project_id"],
        payload={},
    )
    return get_note(conn, note_id)


def note_revisions(conn, note_id: int) -> list[sqlite3.Row]:
    get_note(conn, note_id)
    return conn.execute(
        "SELECT * FROM note_revisions WHERE note_id = ? ORDER BY id DESC", (note_id,)
    ).fetchall()


# --- wiki ---------------------------------------------------------------


def create_wiki(conn, actor, slug: str, title: str, body: str = "") -> int:
    if not WIKI_SLUG_RE.match(slug or ""):
        raise KnowledgeError("slug must match [a-z0-9][a-z0-9-]*")
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO wiki(slug, title, body, created_by, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?)",
                (
                    slug,
                    _title(title),
                    _body(body),
                    actor["id"],
                    time.time(),
                    time.time(),
                ),
            )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise KnowledgeError("slug already exists") from exc
        raise
    events.emit(
        conn, actor["id"], "wiki.created", "wiki", cur.lastrowid, payload={"slug": slug}
    )
    return cur.lastrowid


def list_wiki(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM wiki ORDER BY slug").fetchall()


def get_wiki(conn, slug: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM wiki WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise KnowledgeError("no such wiki page")
    return row


def edit_wiki(
    conn, actor, slug: str, *, title: str | None = None, body: str | None = None
) -> sqlite3.Row:
    page = get_wiki(conn, slug)
    _guard(conn, "wiki", page["id"])
    if title is not None:
        title = _title(title)
    if body is not None:
        body = _body(body)
    with conn:
        if (title is not None and title != page["title"]) or (
            body is not None and body != page["body"]
        ):
            conn.execute(
                "INSERT INTO wiki_revisions(wiki_id, title, body, edited_by,"
                " created_at) VALUES (?,?,?,?,?)",
                (
                    page["id"],
                    title or page["title"],
                    body or page["body"],
                    actor["id"],
                    time.time(),
                ),
            )
        conn.execute(
            "UPDATE wiki SET title = COALESCE(?, title),"
            " body = COALESCE(?, body), updated_at = ? WHERE slug = ?",
            (title, body, time.time(), slug),
        )
    events.emit(
        conn, actor["id"], "wiki.edited", "wiki", page["id"], payload={"slug": slug}
    )
    return get_wiki(conn, slug)


def wiki_revisions(conn, slug: str) -> list[sqlite3.Row]:
    page = get_wiki(conn, slug)
    return conn.execute(
        "SELECT * FROM wiki_revisions WHERE wiki_id = ? ORDER BY id DESC", (page["id"],)
    ).fetchall()
