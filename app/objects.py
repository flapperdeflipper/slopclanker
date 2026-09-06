"""Stacks, projects, tasks and todos services — DESIGN §2 object rules."""

import re
import sqlite3
import time

from app import events
from app import permissions as perms
from app.statemachine import VersionConflict, open_questions_on

MAX_TITLE = 200
MAX_TEXT = 32768
UNSET = object()
PRIORITIES = ("low", "medium", "high", "urgent")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ObjectError(ValueError):
    """Base object-service failure."""


class SlugInvalid(ObjectError):
    pass


class TooLong(ObjectError):
    pass


class Frozen(ObjectError):
    """Object frozen by open questions."""

    def __init__(self, questions: list[dict]):
        super().__init__("object frozen by open questions")
        self.questions = questions


def _require(cond, exc):
    if not cond:
        raise exc


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug


def _check_name(name: str) -> str:
    _require(isinstance(name, str) and name.strip(), ObjectError("name required"))
    _require(len(name) <= MAX_TITLE, TooLong(f"name > {MAX_TITLE} chars"))
    return name.strip()


def _check_text(body: str | None) -> str:
    _require(body is None or len(body) <= MAX_TEXT, TooLong(f"text > {MAX_TEXT} chars"))
    return body or ""


def _guard_freeze(conn, attach_type: str, attach_id: int) -> None:
    frozen = open_questions_on(conn, attach_type, attach_id)
    if frozen:
        raise Frozen(frozen)


def _unique_slug(conn, table: str, slug: str) -> str:
    base, candidate, n = slug, slug, 2
    while conn.execute(
        f"SELECT 1 FROM {table} WHERE slug = ?",  # noqa: S608 — whitelist/placeholder  # nosec B608
        (candidate,),
    ).fetchone():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


# --- stacks -----------------------------------------------------------


def create_stack(
    conn, actor, name: str, description: str = "", slug: str | None = None
) -> int:
    _require(perms.can(actor, perms.STACKS_MANAGE), ObjectError("admins only"))
    name = _check_name(name)
    description = _check_text(description)
    if slug is None:
        slug = _slugify(name)
    _require(bool(SLUG_RE.match(slug or "")), SlugInvalid("bad slug"))
    with conn:
        cur = conn.execute(
            "INSERT INTO stacks(slug, name, description, created_by, created_at)"
            " VALUES (?,?,?,?,?)",
            (
                _unique_slug(conn, "stacks", slug),
                name,
                description,
                actor["id"],
                time.time(),
            ),
        )
    events.emit(
        conn,
        actor["id"],
        "stack.created",
        "stack",
        cur.lastrowid,
        payload={"name": name},
    )
    return cur.lastrowid


def list_stacks(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM stacks ORDER BY name").fetchall()


# --- projects ---------------------------------------------------------


def create_project(
    conn,
    actor,
    name: str,
    description: str = "",
    stack_id: int | None = None,
    slug: str | None = None,
) -> int:
    _require(perms.can(actor, perms.PROJECTS_CREATE), ObjectError("not allowed"))
    name = _check_name(name)
    description = _check_text(description)
    if slug is None:
        slug = _slugify(name)
    _require(bool(SLUG_RE.match(slug or "")), SlugInvalid("bad slug"))
    _require(
        stack_id is None
        or conn.execute("SELECT 1 FROM stacks WHERE id = ?", (stack_id,)).fetchone(),
        ObjectError("no such stack"),
    )
    now = time.time()
    with conn:
        cur = conn.execute(
            "INSERT INTO projects(slug, name, description, stack_id, owner_id,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                _unique_slug(conn, "projects", slug),
                name,
                description,
                stack_id,
                actor["id"],
                actor["id"],
                now,
            ),
        )
    events.emit(
        conn,
        actor["id"],
        "project.created",
        "project",
        cur.lastrowid,
        payload={"name": name},
    )
    return cur.lastrowid


def get_project(conn, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    _require(row is not None, ObjectError("no such project"))
    return row


def list_projects(
    conn, *, stack_id: int | None = None, include_archived: bool = False
) -> list[sqlite3.Row]:
    q = "SELECT * FROM projects WHERE 1=1"
    args: list = []
    if not include_archived:
        q += " AND archived = 0"
    if stack_id is not None:
        q += " AND stack_id = ?"
        args.append(stack_id)
    return conn.execute(q + " ORDER BY name", args).fetchall()


def edit_project(
    conn,
    actor,
    project_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    stack_id: int | None = None,
) -> sqlite3.Row:
    proj = get_project(conn, project_id)
    _require(
        perms.can(actor, perms.PROJECT_EDIT, {"owner_id": proj["owner_id"]}),
        ObjectError("owner or superadmin only"),
    )
    _guard_freeze(conn, "project", project_id)
    if name is not None:
        name = _check_name(name)
    if description is not None:
        description = _check_text(description)
    _require(
        stack_id is None
        or conn.execute("SELECT 1 FROM stacks WHERE id = ?", (stack_id,)).fetchone(),
        ObjectError("no such stack"),
    )
    with conn:
        conn.execute(
            "UPDATE projects SET name = COALESCE(?, name),"
            " description = COALESCE(?, description),"
            " stack_id = COALESCE(?, stack_id) WHERE id = ?",
            (name, description, stack_id, project_id),
        )
    events.emit(
        conn,
        actor["id"],
        "project.edited",
        "project",
        project_id,
        payload={"name": name or proj["name"]},
    )
    return get_project(conn, project_id)


def set_project_archived(conn, actor, project_id: int, archived: bool) -> sqlite3.Row:
    proj = get_project(conn, project_id)
    _require(
        perms.can(actor, perms.PROJECT_ARCHIVE, {"owner_id": proj["owner_id"]}),
        ObjectError("admin owner or superadmin only"),
    )
    with conn:
        conn.execute(
            "UPDATE projects SET archived = ? WHERE id = ?", (int(archived), project_id)
        )
    verb = "project.archived" if archived else "project.unarchived"
    events.emit(
        conn, actor["id"], verb, "project", project_id, payload={"name": proj["name"]}
    )
    return get_project(conn, project_id)


def adopt_project(conn, actor, project_id: int) -> sqlite3.Row:
    proj = get_project(conn, project_id)
    _require(perms.can(actor, perms.PROJECT_ADOPT), ObjectError("admins only"))
    with conn:
        conn.execute(
            "UPDATE projects SET owner_id = ? WHERE id = ?", (actor["id"], project_id)
        )
    events.emit(
        conn,
        actor["id"],
        "project.adopted",
        "project",
        project_id,
        payload={"name": proj["name"]},
    )
    return get_project(conn, project_id)


def purge_project(conn, actor, project_id: int) -> None:
    """Catastrophic, irreversible; the chained events survive as audit."""
    proj = get_project(conn, project_id)
    _require(
        perms.can(actor, perms.PROJECT_PURGE, {"owner_id": proj["owner_id"]}),
        ObjectError("admin owner or superadmin only"),
    )
    task_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM tasks WHERE project_id = ?", (project_id,)
        )
    ]
    with conn:
        if task_ids:
            marks = ",".join("?" * len(task_ids))
            for tbl in ("todos", "transitions", "task_revisions", "proofs"):
                conn.execute(
                    f"DELETE FROM {tbl} WHERE task_id IN ({marks})",  # noqa: S608  # nosec B608
                    task_ids,
                )
            conn.execute(
                f"DELETE FROM tasks WHERE id IN ({marks})",  # noqa: S608  # nosec B608
                task_ids,
            )
        conn.execute(
            "DELETE FROM comments WHERE discussion_id IN"
            " (SELECT id FROM discussions WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute("DELETE FROM discussions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM questions WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM chat WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM decisions WHERE project_id = ?", (project_id,))
        conn.execute(
            "DELETE FROM note_revisions WHERE note_id IN"
            " (SELECT id FROM notes WHERE project_id = ?)",
            (project_id,),
        )
        conn.execute("DELETE FROM notes WHERE project_id = ?", (project_id,))
        conn.execute(
            "DELETE FROM links WHERE"
            " (from_type = 'project' AND from_id = ?) OR"
            " (to_type = 'project' AND to_id = ?)",
            (project_id, project_id),
        )
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    events.emit(
        conn,
        actor["id"],
        "project.purged",
        "project",
        project_id,
        payload={"name": proj["name"]},
    )


# --- tasks ------------------------------------------------------------

_BODY_FROZEN_FOR_AGENTS = {"approved", "building", "review", "done"}


def create_task(
    conn,
    actor,
    project_id: int,
    title: str,
    body: str = "",
    priority: str = "medium",
    assignee_id: int | None = None,
    tags: str = "",
) -> int:
    proj = get_project(conn, project_id)
    _require(perms.can(actor, perms.TASKS_CREATE), ObjectError("not allowed"))
    _require(not proj["archived"], ObjectError("project is archived"))
    _guard_freeze(conn, "project", project_id)
    title = _check_name(title)
    body = _check_text(body)
    _require(
        priority in PRIORITIES,
        ObjectError("priority must be one of " + ",".join(PRIORITIES)),
    )
    if assignee_id is not None:
        _require(
            conn.execute(
                "SELECT 1 FROM identities WHERE id = ? AND status = 'active'",
                (assignee_id,),
            ).fetchone(),
            ObjectError("no such assignee"),
        )
    now = time.time()
    with conn:
        cur = conn.execute(
            "INSERT INTO tasks(project_id, title, body, state, priority, tags,"
            " assignee_id, created_by, state_changed_by, state_changed_at,"
            " created_at) VALUES (?,?,?,'idea',?,?,?,?,?,?,?)",
            (
                project_id,
                title,
                body,
                priority,
                tags,
                assignee_id,
                actor["id"],
                actor["id"],
                now,
                now,
            ),
        )
    events.emit(
        conn,
        actor["id"],
        "task.created",
        "task",
        cur.lastrowid,
        project_id=project_id,
        to_identity_id=assignee_id,
        payload={"title": title},
    )
    return cur.lastrowid


def get_task(conn, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    _require(row is not None, ObjectError("no such task"))
    return row


def list_tasks(
    conn,
    *,
    project_id: int | None = None,
    state: str | None = None,
    assignee_id: int | None = None,
) -> list[sqlite3.Row]:
    q = "SELECT * FROM tasks WHERE 1=1"
    args: list = []
    if project_id is not None:
        q += " AND project_id = ?"
        args.append(project_id)
    if state is not None:
        q += " AND state = ?"
        args.append(state)
    if assignee_id is not None:
        q += " AND assignee_id = ?"
        args.append(assignee_id)
    return conn.execute(q + " ORDER BY id", args).fetchall()


def edit_task(
    conn,
    actor,
    task_id: int,
    *,
    body: str | None = None,
    title: str | None = None,
    priority: str | None = None,
    tags: str | None = None,
    assignee_id=UNSET,
    proof_waived: bool | None = None,
    version: int | None = None,
) -> sqlite3.Row:
    task = get_task(conn, task_id)
    if version is not None and version != task["version"]:
        raise VersionConflict("stale version; refresh and retry")
    _guard_freeze(conn, "task", task_id)
    if body is not None:
        body = _check_text(body)
        _require(
            not (actor["kind"] != "human" and task["state"] in _BODY_FROZEN_FOR_AGENTS),
            ObjectError("body frozen for agents once approved"),
        )
    if title is not None:
        title = _check_name(title)
    if proof_waived is not None:
        _require(actor["kind"] == "human", ObjectError("waiver is human-only"))
    if assignee_id is not UNSET and assignee_id != task["assignee_id"]:
        _require(perms.can(actor, perms.TASKS_ASSIGN), ObjectError("not allowed"))
        _require(
            assignee_id is None
            or conn.execute(
                "SELECT 1 FROM identities WHERE id = ? AND status = 'active'",
                (assignee_id,),
            ).fetchone(),
            ObjectError("no such assignee"),
        )
    if priority is not None:
        _require(
            priority in PRIORITIES,
            ObjectError("priority must be one of " + ",".join(PRIORITIES)),
        )

    changed: list[str] = []
    with conn:
        if body is not None and body != task["body"]:
            conn.execute(
                "INSERT INTO task_revisions(task_id, title, body, edited_by,"
                " created_at) VALUES (?,?,?,?,?)",
                (task_id, title or task["title"], body, actor["id"], time.time()),
            )
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, task_id))
            changed += ["body", "revision", "version"]
        if title is not None and title != task["title"]:
            conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (title, task_id))
            changed += ["title", "version"]
        if priority is not None and priority != task["priority"]:
            conn.execute(
                "UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id)
            )
            changed += ["priority", "version"]
        if tags is not None and tags != task["tags"]:
            conn.execute("UPDATE tasks SET tags = ? WHERE id = ?", (tags, task_id))
            changed += ["tags", "version"]
        if assignee_id is not UNSET and assignee_id != task["assignee_id"]:
            conn.execute(
                "UPDATE tasks SET assignee_id = ? WHERE id = ?", (assignee_id, task_id)
            )
            changed += ["assignee", "version"]
        if proof_waived is not None and int(bool(proof_waived)) != task["proof_waived"]:
            conn.execute(
                "UPDATE tasks SET proof_waived = ?, proof_waived_by = ? WHERE id = ?",
                (
                    int(bool(proof_waived)),
                    actor["id"] if proof_waived else None,
                    task_id,
                ),
            )
            changed += ["proof_waived", "version"]
        if "version" in changed:
            conn.execute(
                "UPDATE tasks SET version = version + 1 WHERE id = ?", (task_id,)
            )
    if changed:
        events.emit(
            conn,
            actor["id"],
            "task.edited",
            "task",
            task_id,
            project_id=task["project_id"],
            payload={"fields": changed},
        )
    return get_task(conn, task_id)


# --- todos ------------------------------------------------------------


def add_todo(conn, actor, task_id: int, title: str) -> int:
    task = get_task(conn, task_id)
    _require(perms.can(actor, perms.TODOS_ADD), ObjectError("not allowed"))
    _require(
        not get_project(conn, task["project_id"])["archived"],
        ObjectError("project is archived"),
    )
    _guard_freeze(conn, "task", task_id)
    title = _check_name(title)
    with conn:
        cur = conn.execute(
            "INSERT INTO todos(task_id, title, created_by, created_at)"
            " VALUES (?,?,?,?)",
            (task_id, title, actor["id"], time.time()),
        )
        conn.execute("UPDATE tasks SET version = version + 1 WHERE id = ?", (task_id,))
    events.emit(
        conn,
        actor["id"],
        "todo.added",
        "task",
        task_id,
        project_id=task["project_id"],
        payload={"title": title},
    )
    return cur.lastrowid


def get_todo(conn, todo_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM todos WHERE id = ? AND trashed_at IS NULL", (todo_id,)
    ).fetchone()
    _require(row is not None, ObjectError("no such todo"))
    return row


def tick_todo(
    conn, actor, todo_id: int, done: bool, version: int | None = None
) -> sqlite3.Row:
    todo = get_todo(conn, todo_id)
    if version is not None and version != todo["version"]:
        raise VersionConflict("stale version; refresh and retry")
    _require(perms.can(actor, perms.TODOS_TICK), ObjectError("not allowed"))
    _guard_freeze(conn, "task", todo["task_id"])
    _guard_freeze(conn, "todo", todo_id)
    with conn:
        conn.execute(
            "UPDATE todos SET done = ?, done_by = ?, done_at = ?,"
            " version = version + 1 WHERE id = ?",
            (
                int(done),
                actor["id"] if done else None,
                time.time() if done else None,
                todo_id,
            ),
        )
        conn.execute(
            "UPDATE tasks SET version = version + 1 WHERE id = ?", (todo["task_id"],)
        )
    events.emit(
        conn,
        actor["id"],
        "todo.ticked" if done else "todo.unticked",
        "task",
        todo["task_id"],
        payload={"todo_id": todo_id},
    )
    return get_todo(conn, todo_id)


def trash_todo(conn, actor, todo_id: int) -> None:
    todo = get_todo(conn, todo_id)
    _require(actor["kind"] == "human", ObjectError("trash is human-only"))
    _guard_freeze(conn, "task", todo["task_id"])
    _guard_freeze(conn, "todo", todo_id)
    with conn:
        conn.execute(
            "UPDATE todos SET trashed_at = ?, trashed_by = ? WHERE id = ?",
            (time.time(), actor["id"], todo_id),
        )
        conn.execute(
            "UPDATE tasks SET version = version + 1 WHERE id = ?", (todo["task_id"],)
        )
    events.emit(
        conn,
        actor["id"],
        "todo.trashed",
        "task",
        todo["task_id"],
        payload={"todo_id": todo_id},
    )
