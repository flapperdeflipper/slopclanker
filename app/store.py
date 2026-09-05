"""Domain logic: agents, projects, posts, comments, todos, notes, wiki,
chat, events, claims. Every function takes an open connection; callers open
one per request. Mutations append to the events log in the same transaction.
"""

import re
import sqlite3
import time

MAX_COMMENT_DEPTH = 4
POST_KINDS = {"info", "question", "proposal", "handover"}
TODO_PRIORITIES = {"low", "medium", "high", "urgent"}


def _log(
    conn: sqlite3.Connection,
    actor: str,
    verb: str,
    obj_type: str,
    obj_id: str | int,
    summary: str = "",
    project_id: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events(ts, actor, verb, obj_type, obj_id, summary, project_id)"
        " VALUES(?, ?, ?, ?, ?, ?, ?)",
        (time.time(), actor, verb, obj_type, str(obj_id), summary[:200], project_id),
    )


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "untitled"


def _audience_match(audience: str, name: str) -> bool:
    if audience.strip().lower() in ("", "all"):
        return True
    names = [part.strip() for part in audience.split(",") if part.strip()]
    return name in names


def _norm_tags(tags: str | list[str] | None) -> str:
    if tags is None:
        return ""
    if isinstance(tags, list):
        tags = ",".join(str(t) for t in tags)
    return ",".join(sorted({t.strip() for t in tags.split(",") if t.strip()}))


# --------------------------------------------------------------------------
# agents / presence
# --------------------------------------------------------------------------


def _agents_with_active(
    conn: sqlite3.Connection, now: float, heartbeat_timeout: int
) -> tuple[list[dict], dict[str, bool]]:
    agents: list[dict] = []
    active_by_name: dict[str, bool] = {}
    for row in conn.execute("SELECT * FROM agents ORDER BY last_seen DESC"):
        active = (now - row["last_seen"]) <= heartbeat_timeout
        active_by_name[row["name"]] = active
        agents.append(
            {
                "name": row["name"],
                "session_id": row["session_id"],
                "note": row["note"],
                "role": row["role"],
                "contact": row["contact"],
                "started_at": row["started_at"],
                "last_seen": row["last_seen"],
                "active": active,
            }
        )
    return agents, active_by_name


def list_agents(conn: sqlite3.Connection, heartbeat_timeout: int = 900) -> list[dict]:
    agents, _ = _agents_with_active(conn, time.time(), heartbeat_timeout)
    return agents


def profile_set(
    conn: sqlite3.Connection,
    name: str,
    note: str | None = None,
    role: str | None = None,
    contact: str | None = None,
) -> dict:
    """Create or update an identity card. Only passed fields change."""
    now = time.time()
    existing = conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone()
    conn.execute(
        """
        INSERT INTO agents(name, note, role, contact, started_at, last_seen)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            note = COALESCE(excluded.note, agents.note),
            role = COALESCE(excluded.role, agents.role),
            contact = COALESCE(excluded.contact, agents.contact)
        """,
        (name, note, role, contact, now, now),
    )
    if existing is None:
        _log(conn, name, "registered", "agent", name)
    else:
        _log(conn, name, "updated profile of", "agent", name)
    conn.commit()
    agent = get_agent(conn, name)
    assert agent is not None
    return agent


def get_agent(conn: sqlite3.Connection, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def agent_claims(conn: sqlite3.Connection, name: str) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM claims WHERE agent = ? ORDER BY claimed_at DESC", (name,)
        )
    ]


def hello(
    conn: sqlite3.Connection,
    name: str,
    session_id: str | None = None,
    note: str | None = None,
    role: str | None = None,
    contact: str | None = None,
    heartbeat_timeout: int = 900,
) -> dict:
    """Register/refresh presence and return the awareness snapshot."""
    now = time.time()
    known = conn.execute("SELECT 1 FROM agents WHERE name = ?", (name,)).fetchone()
    conn.execute(
        """
        INSERT INTO agents(name, session_id, note, role, contact, started_at, last_seen)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            session_id = COALESCE(excluded.session_id, agents.session_id),
            note = COALESCE(excluded.note, agents.note),
            role = COALESCE(excluded.role, agents.role),
            contact = COALESCE(excluded.contact, agents.contact),
            last_seen = excluded.last_seen
        """,
        (name, session_id, note, role, contact, now, now),
    )
    if known is None:
        _log(conn, name, "said hello", "agent", name)
    conn.commit()
    return snapshot(conn, name, heartbeat_timeout)


def snapshot(conn: sqlite3.Connection, me: str, heartbeat_timeout: int = 900) -> dict:
    """Everything an agent should see when it wakes up."""
    now = time.time()
    agents, active_by_name = _agents_with_active(conn, now, heartbeat_timeout)
    claims = _claims_with_stale(conn, active_by_name)

    posts_for_me = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM posts WHERE status = 'open' ORDER BY created_at DESC"
        )
        if _audience_match(row["audience"], me) and row["created_by"] != me
    ]

    my_todos = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM todos
            WHERE done = 0 AND archived = 0
              AND ((scope = 'session' AND session_key = ?) OR assignee = ?)
            ORDER BY created_at DESC
            """,
            (me, me),
        )
    ]

    return {
        "me": me,
        "server_time": now,
        "agents": agents,
        "claims": claims,
        "posts_for_me": posts_for_me,
        "my_todos": my_todos,
    }


# --------------------------------------------------------------------------
# projects
# --------------------------------------------------------------------------


def create_project(
    conn: sqlite3.Connection,
    name: str,
    created_by: str,
    slug: str | None = None,
    description: str = "",
) -> dict:
    slug = slugify(slug or name)
    if conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
        raise ValueError(f"project slug '{slug}' already exists")
    now = time.time()
    cur = conn.execute(
        """
        INSERT INTO projects(slug, name, description, created_by, created_at)
        VALUES(?, ?, ?, ?, ?)
        """,
        (slug, name.strip(), description.strip(), created_by, now),
    )
    _log(
        conn,
        created_by,
        "created project",
        "project",
        slug,
        name.strip(),
        project_id=int(cur.lastrowid),
    )
    conn.commit()
    return get_project(conn, int(cur.lastrowid))  # type: ignore[return-value]


def list_projects(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY id")]


def get_project(conn: sqlite3.Connection, ref: str | int) -> dict | None:
    row = None
    if isinstance(ref, int) or str(ref).isdigit():
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (int(ref),)
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM projects WHERE slug = ?", (str(ref),)
        ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# posts + comments
# --------------------------------------------------------------------------


def create_post(
    conn: sqlite3.Connection,
    title: str,
    body: str,
    created_by: str,
    kind: str = "info",
    audience: str = "all",
    project_id: int = 1,
) -> int:
    if kind not in POST_KINDS:
        raise ValueError(f"kind must be one of {sorted(POST_KINDS)}, got {kind!r}")
    if get_project(conn, project_id) is None:
        raise ValueError(f"project {project_id} does not exist")
    now = time.time()
    cur = conn.execute(
        """
        INSERT INTO posts(project_id, title, body, kind, audience, status, created_by, created_at)
        VALUES(?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (
            project_id,
            title.strip(),
            body,
            kind,
            audience.strip() or "all",
            created_by,
            now,
        ),
    )
    pid = int(cur.lastrowid)
    _log(
        conn,
        created_by,
        "posted",
        "post",
        pid,
        title.strip()[:120],
        project_id=project_id,
    )
    conn.commit()
    return pid


def add_comment(
    conn: sqlite3.Connection,
    post_id: int,
    author: str,
    body: str,
    parent_id: int | None = None,
) -> int:
    """Append a comment; ``parent_id`` nests under another comment.

    Nesting is capped at MAX_COMMENT_DEPTH levels under the post; deeper
    replies are rejected so threads stay readable.
    """
    post = conn.execute(
        "SELECT status, project_id FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    if post is None:
        raise ValueError(f"post {post_id} does not exist")
    if post["status"] != "open":
        raise ValueError(f"post {post_id} is closed")

    depth = 0
    if parent_id is not None:
        walk = parent_id
        while walk is not None:
            row = conn.execute(
                "SELECT id, parent_id, post_id FROM comments WHERE id = ?",
                (walk,),
            ).fetchone()
            if row is None:
                raise ValueError(f"comment {parent_id} does not exist")
            if row["post_id"] != post_id:
                raise ValueError("parent comment belongs to a different post")
            depth += 1
            walk = row["parent_id"]
        if depth >= MAX_COMMENT_DEPTH:
            raise ValueError(
                f"max comment depth is {MAX_COMMENT_DEPTH}; reply at the top level instead"
            )

    cur = conn.execute(
        "INSERT INTO comments(post_id, parent_id, author, body, created_at)"
        " VALUES(?, ?, ?, ?, ?)",
        (post_id, parent_id, author, body, time.time()),
    )
    _log(
        conn,
        author,
        "replied to" if parent_id is not None else "commented on",
        "post",
        post_id,
        f"depth {depth + 1}",
        project_id=post["project_id"],
    )
    conn.commit()
    return int(cur.lastrowid)


def close_post(conn: sqlite3.Connection, post_id: int, outcome: str) -> None:
    row = conn.execute(
        "SELECT status, created_by, project_id FROM posts WHERE id = ?", (post_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"post {post_id} does not exist")
    if row["status"] != "open":
        raise ValueError(f"post {post_id} is already closed")
    conn.execute(
        "UPDATE posts SET status = 'closed', outcome = ?, closed_at = ? WHERE id = ?",
        (outcome, time.time(), post_id),
    )
    _log(
        conn,
        row["created_by"],
        "closed",
        "post",
        post_id,
        outcome[:120],
        project_id=row["project_id"],
    )
    conn.commit()


def post_detail(conn: sqlite3.Connection, post_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if row is None:
        return None
    detail = dict(row)
    detail["comments"] = [
        dict(c)
        for c in conn.execute(
            "SELECT * FROM comments WHERE post_id = ? ORDER BY created_at, id",
            (post_id,),
        )
    ]
    return detail


def list_posts(
    conn: sqlite3.Connection,
    project_id: int | None = None,
    include_closed: bool = False,
) -> list[dict]:
    where, params = [], []
    if not include_closed:
        where.append("status = 'open'")
    if project_id is not None:
        where.append("project_id = ?")
        params.append(project_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT p.*, COUNT(c.id) AS comment_count,
                   COALESCE(MAX(c.created_at), p.created_at) AS activity_at,
                   pr.slug AS project_slug
            FROM posts p
            LEFT JOIN comments c ON c.post_id = p.id
            LEFT JOIN projects pr ON pr.id = p.project_id
            {clause}
            GROUP BY p.id
            ORDER BY p.created_at DESC
            """,
            params,
        )
    ]


def _visible_open_post_ids(conn: sqlite3.Connection, me: str) -> set[int]:
    return {
        row["id"]
        for row in conn.execute("SELECT id, audience FROM posts WHERE status = 'open'")
        if _audience_match(row["audience"], me)
    }


# --------------------------------------------------------------------------
# todos
# --------------------------------------------------------------------------


def add_todo(
    conn: sqlite3.Connection,
    created_by: str,
    title: str = "",
    body: str = "",
    priority: str = "medium",
    tags: str | list[str] | None = None,
    assignee: str | None = None,
    project_id: int = 1,
    scope: str = "shared",
    session_key: str | None = None,
) -> int:
    if priority not in TODO_PRIORITIES:
        raise ValueError(
            f"priority must be one of {sorted(TODO_PRIORITIES)}, got {priority!r}"
        )
    if scope not in {"shared", "session"}:
        raise ValueError(f"scope must be 'shared' or 'session', got {scope!r}")
    if get_project(conn, project_id) is None:
        raise ValueError(f"project {project_id} does not exist")
    title = title.strip() or body.strip()[:60] or "untitled"
    if scope == "session" and not session_key:
        session_key = created_by
    cur = conn.execute(
        """
        INSERT INTO todos(title, body, priority, tags, assignee, project_id,
                          scope, session_key, created_by, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            body,
            priority,
            _norm_tags(tags),
            assignee,
            project_id,
            scope,
            session_key,
            created_by,
            time.time(),
        ),
    )
    tid = int(cur.lastrowid)
    _log(
        conn, created_by, "added todo", "todo", tid, title[:120], project_id=project_id
    )
    conn.commit()
    return tid


def list_todos(
    conn: sqlite3.Connection,
    project_id: int | None = None,
    assignee: str | None = None,
    name: str | None = None,
    status: str = "open",
) -> list[dict]:
    """Todos by status: open | done | archive (finished OR archived) | all.

    ``name`` includes that agent's session todos alongside shared ones.
    """
    where, params = [], []
    if status == "open":
        where.append("t.done = 0 AND t.archived = 0")
    elif status == "done":
        where.append("t.done = 1 AND t.archived = 0")
    elif status == "archive":
        where.append("(t.done = 1 OR t.archived = 1)")
    elif status != "all":
        raise ValueError(f"status must be open|done|archive|all, got {status!r}")
    if project_id is not None:
        where.append("t.project_id = ?")
        params.append(project_id)
    if assignee:
        where.append("t.assignee = ?")
        params.append(assignee)
    if name:
        where.append(
            "(t.scope = 'shared' OR (t.scope = 'session' AND t.session_key = ?))"
        )
        params.append(name)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT t.*, pr.slug AS project_slug FROM todos t
            LEFT JOIN projects pr ON pr.id = t.project_id
            {clause} ORDER BY t.created_at DESC
            """,
            params,
        )
    ]


def done_todo(conn: sqlite3.Connection, todo_id: int, actor: str = "") -> None:
    row = conn.execute(
        "SELECT done, title, project_id FROM todos WHERE id = ?", (todo_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"todo {todo_id} does not exist")
    if row["done"]:
        return
    conn.execute(
        "UPDATE todos SET done = 1, done_at = ? WHERE id = ?", (time.time(), todo_id)
    )
    _log(
        conn,
        actor or "?",
        "finished todo",
        "todo",
        todo_id,
        row["title"][:120],
        project_id=row["project_id"],
    )
    conn.commit()


def reopen_todo(conn: sqlite3.Connection, todo_id: int, actor: str = "") -> None:
    row = conn.execute(
        "SELECT done, project_id FROM todos WHERE id = ?", (todo_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"todo {todo_id} does not exist")
    conn.execute("UPDATE todos SET done = 0, done_at = NULL WHERE id = ?", (todo_id,))
    _log(
        conn,
        actor or "?",
        "reopened todo",
        "todo",
        todo_id,
        project_id=row["project_id"],
    )
    conn.commit()


def archive_todo(conn: sqlite3.Connection, todo_id: int, actor: str = "") -> None:
    row = conn.execute(
        "SELECT archived, title, project_id FROM todos WHERE id = ?", (todo_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"todo {todo_id} does not exist")
    if row["archived"]:
        return
    conn.execute("UPDATE todos SET archived = 1 WHERE id = ?", (todo_id,))
    _log(
        conn,
        actor or "?",
        "archived todo",
        "todo",
        todo_id,
        row["title"][:120],
        project_id=row["project_id"],
    )
    conn.commit()


def update_todo(
    conn: sqlite3.Connection,
    todo_id: int,
    actor: str = "",
    **fields: object,
) -> dict:
    allowed = {"title", "body", "priority", "tags", "assignee", "project_id", "done"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown todo field(s): {', '.join(sorted(unknown))}")
    row = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if row is None:
        raise ValueError(f"todo {todo_id} does not exist")
    sets, params = [], []
    for key, value in fields.items():
        if key == "priority" and value not in TODO_PRIORITIES:
            raise ValueError(f"priority must be one of {sorted(TODO_PRIORITIES)}")
        if key == "tags":
            value = _norm_tags(value)
        if key == "done":
            done_todo(conn, todo_id, actor) if value else reopen_todo(
                conn, todo_id, actor
            )
            continue
        sets.append(f"{key} = ?")
        params.append(value)
    if sets:
        params.append(todo_id)
        conn.execute(f"UPDATE todos SET {', '.join(sets)} WHERE id = ?", params)
        row2 = conn.execute(
            "SELECT project_id FROM todos WHERE id = ?", (todo_id,)
        ).fetchone()
        _log(
            conn,
            actor or "?",
            "updated todo",
            "todo",
            todo_id,
            project_id=row2["project_id"],
        )
        conn.commit()
    out = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return dict(out)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------


def save_note(
    conn: sqlite3.Connection,
    title: str,
    created_by: str,
    body: str = "",
    note_id: int | None = None,
    project_id: int = 1,
    tags: str | list[str] | None = None,
) -> int:
    now = time.time()
    if note_id is None:
        if get_project(conn, project_id) is None:
            raise ValueError(f"project {project_id} does not exist")
        cur = conn.execute(
            """
            INSERT INTO notes(project_id, title, body, tags, created_by, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, title.strip(), body, _norm_tags(tags), created_by, now, now),
        )
        nid = int(cur.lastrowid)
        _log(
            conn,
            created_by,
            "created note",
            "note",
            nid,
            title.strip()[:120],
            project_id=project_id,
        )
    else:
        cur = conn.execute(
            "UPDATE notes SET title = ?, body = ?, tags = ?, updated_at = ? WHERE id = ?",
            (title.strip(), body, _norm_tags(tags), now, note_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"note {note_id} does not exist")
        nid = note_id
        row = conn.execute(
            "SELECT project_id FROM notes WHERE id = ?", (nid,)
        ).fetchone()
        _log(
            conn,
            created_by,
            "updated note",
            "note",
            nid,
            title.strip()[:120],
            project_id=row["project_id"],
        )
    conn.commit()
    return nid


def list_notes(conn: sqlite3.Connection, project_id: int | None = None) -> list[dict]:
    where, params = "", []
    if project_id is not None:
        where = "WHERE project_id = ?"
        params.append(project_id)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT n.*, pr.slug AS project_slug FROM notes n
            LEFT JOIN projects pr ON pr.id = n.project_id
            {where} ORDER BY n.updated_at DESC
            """,
            params,
        )
    ]


def get_note(conn: sqlite3.Connection, note_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# wiki
# --------------------------------------------------------------------------


def save_page(
    conn: sqlite3.Connection,
    title: str,
    created_by: str,
    body: str = "",
    slug: str | None = None,
    page_id: int | None = None,
    project_id: int = 1,
) -> str:
    slug = slugify(slug or title)
    now = time.time()
    if page_id is None:
        clash = conn.execute("SELECT id FROM wiki WHERE slug = ?", (slug,)).fetchone()
        if clash:
            raise ValueError(f"wiki slug '{slug}' already exists")
        if get_project(conn, project_id) is None:
            raise ValueError(f"project {project_id} does not exist")
        conn.execute(
            """
            INSERT INTO wiki(project_id, slug, title, body, created_by, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, slug, title.strip(), body, created_by, now, now),
        )
        _log(
            conn,
            created_by,
            "created wiki page",
            "wiki",
            slug,
            title.strip()[:120],
            project_id=project_id,
        )
    else:
        cur = conn.execute(
            "UPDATE wiki SET slug = ?, title = ?, body = ?, updated_at = ? WHERE id = ?",
            (slug, title.strip(), body, now, page_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"wiki page {page_id} does not exist")
        row = conn.execute(
            "SELECT project_id FROM wiki WHERE id = ?", (page_id,)
        ).fetchone()
        _log(
            conn,
            created_by,
            "updated wiki page",
            "wiki",
            slug,
            title.strip()[:120],
            project_id=row["project_id"],
        )
    conn.commit()
    return slug


def list_pages(conn: sqlite3.Connection, project_id: int | None = None) -> list[dict]:
    where, params = "", []
    if project_id is not None:
        where = "WHERE project_id = ?"
        params.append(project_id)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT w.*, pr.slug AS project_slug FROM wiki w
            LEFT JOIN projects pr ON pr.id = w.project_id
            {where} ORDER BY w.title
            """,
            params,
        )
    ]


def get_page(conn: sqlite3.Connection, slug_or_id: str | int) -> dict | None:
    row = None
    if isinstance(slug_or_id, int) or str(slug_or_id).isdigit():
        row = conn.execute(
            "SELECT * FROM wiki WHERE id = ?", (int(slug_or_id),)
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT * FROM wiki WHERE slug = ?", (str(slug_or_id),)
        ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------


def chat_send(
    conn: sqlite3.Connection, author: str, body: str, channel: str = "general"
) -> int:
    cur = conn.execute(
        "INSERT INTO chat(channel, author, body, created_at) VALUES(?, ?, ?, ?)",
        (channel, author, body, time.time()),
    )
    conn.commit()
    return int(cur.lastrowid)


def chat_list(
    conn: sqlite3.Connection,
    channel: str = "general",
    since: float = 0.0,
    limit: int = 200,
) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM chat WHERE channel = ? AND created_at > ?
            ORDER BY created_at DESC LIMIT ?
            """,
            (channel, since, limit),
        )
    ][::-1]


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def list_events(
    conn: sqlite3.Connection, project_id: int | None = None, limit: int = 300
) -> list[dict]:
    """Newest first; with ``project_id`` only that project's events."""
    limit = max(1, min(int(limit), 1000))
    where, params = "", []
    if project_id is not None:
        where = "WHERE project_id = ?"
        params.append(project_id)
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM events {where} ORDER BY ts DESC LIMIT ?",
            (*params, limit),
        )
    ]


# --------------------------------------------------------------------------
# claims
# --------------------------------------------------------------------------


def _claims_with_stale(
    conn: sqlite3.Connection, active_by_name: dict[str, bool]
) -> list[dict]:
    return [
        {
            "agent": row["agent"],
            "path": row["path"],
            "note": row["note"],
            "claimed_at": row["claimed_at"],
            "stale": not active_by_name.get(row["agent"], False),
        }
        for row in conn.execute("SELECT * FROM claims ORDER BY claimed_at DESC")
    ]


def claims(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute("SELECT * FROM claims ORDER BY claimed_at DESC")
    ]


def _paths_conflict(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def set_claims(
    conn: sqlite3.Connection, agent: str, paths: list[str], note: str | None = None
) -> int:
    now = time.time()
    for path in paths:
        conn.execute(
            """
            INSERT INTO claims(agent, path, note, claimed_at) VALUES(?, ?, ?, ?)
            ON CONFLICT(agent, path) DO UPDATE SET
                note = COALESCE(excluded.note, claims.note),
                claimed_at = excluded.claimed_at
            """,
            (agent, path.rstrip("/"), note, now),
        )
    _log(conn, agent, "claimed", "path", ", ".join(paths)[:120], note or "")
    conn.commit()
    return int(
        conn.execute(
            "SELECT COUNT(*) AS c FROM claims WHERE agent = ?", (agent,)
        ).fetchone()["c"]
    )


def check_claims(
    conn: sqlite3.Connection,
    path: str,
    agent: str | None = None,
    heartbeat_timeout: int = 900,
) -> list[dict]:
    now = time.time()
    active = {
        row["name"]: (now - row["last_seen"]) <= heartbeat_timeout
        for row in conn.execute("SELECT name, last_seen FROM agents")
    }
    return [
        {
            "agent": row["agent"],
            "path": row["path"],
            "note": row["note"],
            "claimed_at": row["claimed_at"],
            "stale": not active.get(row["agent"], False),
        }
        for row in conn.execute("SELECT * FROM claims ORDER BY claimed_at DESC")
        if _paths_conflict(path, row["path"]) and row["agent"] != agent
    ]


def release_claims(conn: sqlite3.Connection, agent: str, paths: list[str]) -> None:
    for path in paths:
        conn.execute(
            "DELETE FROM claims WHERE agent = ? AND path = ?", (agent, path.rstrip("/"))
        )
    _log(conn, agent, "released claim", "path", ", ".join(paths)[:120])
    conn.commit()


# --------------------------------------------------------------------------
# awareness
# --------------------------------------------------------------------------


def check(conn: sqlite3.Connection, me: str, since: float = 0.0) -> dict:
    """Everything new for ``me`` since epoch ``since`` (strictly greater)."""
    now = time.time()
    visible = _visible_open_post_ids(conn, me)

    new_posts = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM posts WHERE status = 'open' AND created_at > ?"
            " ORDER BY created_at",
            (since,),
        )
        if row["id"] in visible and row["created_by"] != me
    ]

    new_comments = [
        dict(row)
        for row in conn.execute(
            """
            SELECT c.* FROM comments c JOIN posts p ON p.id = c.post_id
            WHERE c.created_at > ? AND p.status = 'open' AND c.author != ?
            ORDER BY c.created_at
            """,
            (since, me),
        )
        if row["post_id"] in visible
    ]

    new_todos = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM todos
            WHERE done = 0 AND archived = 0 AND created_at > ?
              AND ((scope = 'session' AND session_key = ?) OR assignee = ?)
            ORDER BY created_at
            """,
            (since, me, me),
        )
    ]

    return {
        "me": me,
        "server_time": now,
        "posts": new_posts,
        "comments": new_comments,
        "todos": new_todos,
    }


def unread_post_count(conn: sqlite3.Connection, since: float) -> int:
    """Open posts with activity (post or any comment) newer than ``since``."""
    return int(
        conn.execute(
            """
            SELECT COUNT(*) AS c FROM posts p
            WHERE p.status = 'open' AND COALESCE(
                (SELECT MAX(c2.created_at) FROM comments c2 WHERE c2.post_id = p.id),
                p.created_at
            ) > ?
            """,
            (since,),
        ).fetchone()["c"]
    )


def overview(
    conn: sqlite3.Connection,
    heartbeat_timeout: int = 900,
    seen_since: float = 0.0,
) -> dict:
    """Home snapshot for the web UI: agents, claims, projects, counts."""
    now = time.time()
    agents, active_by_name = _agents_with_active(conn, now, heartbeat_timeout)
    projects = list_projects(conn)
    for project in projects:
        pid = project["id"]
        project["open_posts"] = int(
            conn.execute(
                "SELECT COUNT(*) c FROM posts WHERE project_id = ? AND status = 'open'",
                (pid,),
            ).fetchone()["c"]
        )
        project["open_todos"] = int(
            conn.execute(
                "SELECT COUNT(*) c FROM todos WHERE project_id = ? AND done = 0"
                " AND archived = 0",
                (pid,),
            ).fetchone()["c"]
        )
    return {
        "server_time": now,
        "agents": agents,
        "claims": _claims_with_stale(conn, active_by_name),
        "projects": projects,
        "counts": {
            "open_posts": int(
                conn.execute(
                    "SELECT COUNT(*) c FROM posts WHERE status = 'open'"
                ).fetchone()["c"]
            ),
            "open_todos": int(
                conn.execute(
                    "SELECT COUNT(*) c FROM todos WHERE done = 0 AND archived = 0"
                ).fetchone()["c"]
            ),
            "notes": int(conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]),
            "wiki_pages": int(
                conn.execute("SELECT COUNT(*) c FROM wiki").fetchone()["c"]
            ),
            "unread_posts": unread_post_count(conn, seen_since),
        },
    }
