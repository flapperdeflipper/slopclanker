"""Object rules: DESIGN §2 proven — ownership, archive, purge cascade."""

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin

from app import objects
from app.statemachine import VersionConflict


@pytest.fixture
def env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    agent2 = clanker(conn, "clanker-y")
    user = human(conn, "userx", "user", boss)
    admin = human(conn, "adminx", "admin", boss)
    return conn, boss, agent, agent2, user, admin


def test_stack_create_admin_only(env):
    conn, _boss, agent, _agent2, user, admin = env
    objects.create_stack(conn, admin, "Infra")
    with pytest.raises(objects.ObjectError):
        objects.create_stack(conn, agent, "nope")
    with pytest.raises(objects.ObjectError):
        objects.create_stack(conn, user, "nope")
    assert len(objects.list_stacks(conn)) == 1
    conn.close()


def test_slug_dedup(env):
    conn, _boss, agent, agent2, _user, _admin = env
    p1 = objects.create_project(conn, agent, "Same Name")
    p2 = objects.create_project(conn, agent2, "Same Name")
    s1 = conn.execute("SELECT slug FROM projects WHERE id = ?", (p1,)).fetchone()[0]
    s2 = conn.execute("SELECT slug FROM projects WHERE id = ?", (p2,)).fetchone()[0]
    assert s1 == "same-name" and s2 == "same-name-2"
    with pytest.raises(objects.SlugInvalid):
        objects.create_project(conn, agent, "ok", slug="Bad Slug!")
    conn.close()


def test_project_owner_edit_rules(env):
    conn, boss, agent, agent2, _user, admin = env
    pid = objects.create_project(conn, agent, "mine")
    objects.edit_project(conn, agent, pid, description="d")
    objects.edit_project(conn, boss, pid, name="renamed")
    with pytest.raises(objects.ObjectError):
        objects.edit_project(conn, agent2, pid, name="hijack")
    with pytest.raises(objects.ObjectError):
        objects.edit_project(conn, admin, pid, name="nope")
    conn.close()


def test_archive_permissions(env):
    conn, boss, agent, _agent2, user, admin = env
    own = objects.create_project(conn, admin, "own")
    other = objects.create_project(conn, agent, "other")
    with pytest.raises(objects.ObjectError):
        objects.set_project_archived(conn, admin, other, True)
    with pytest.raises(objects.ObjectError):
        objects.set_project_archived(conn, user, own, True)
    with pytest.raises(objects.ObjectError):
        objects.set_project_archived(conn, agent, other, True)
    objects.set_project_archived(conn, admin, own, True)
    objects.set_project_archived(conn, boss, other, True)
    assert objects.get_project(conn, other)["archived"] == 1
    conn.close()


def test_adopt_then_archive(env):
    conn, _boss, agent, _agent2, user, admin = env
    pid = objects.create_project(conn, agent, "adoptable")
    with pytest.raises(objects.ObjectError):
        objects.adopt_project(conn, user, pid)
    objects.adopt_project(conn, admin, pid)
    objects.set_project_archived(conn, admin, pid, True)
    assert objects.get_project(conn, pid)["archived"] == 1
    conn.close()


def test_no_new_objects_in_archived_project(env):
    conn, boss, agent, _agent2, _user, _admin = env
    pid = objects.create_project(conn, agent, "frozen")
    objects.set_project_archived(conn, boss, pid, True)
    with pytest.raises(objects.ObjectError):
        objects.create_task(conn, agent, pid, "t")
    tid = objects.create_task(
        conn, agent, objects.create_project(conn, agent, "live"), "t"
    )
    todo = objects.add_todo(conn, agent, tid, "ok")
    assert todo
    conn.close()


def test_purge_cascades(env):
    conn, boss, agent, _agent2, _user, admin = env
    pid = objects.create_project(conn, agent, "gone")
    tid = objects.create_task(conn, agent, pid, "t")
    objects.add_todo(conn, agent, tid, "todo")
    from app import statemachine

    statemachine.transition(conn, tid, "plan", agent)
    conn.execute(
        "INSERT INTO proofs(task_id, provider, kind, url, added_by, added_at)"
        " VALUES (?, 'github', 'pr', 'u', ?, 1.0)",
        (tid, agent["id"]),
    )
    conn.execute(
        "INSERT INTO discussions(project_id, title, created_by, created_at)"
        " VALUES (?, 'd', ?, 1.0)",
        (pid, agent["id"]),
    )
    conn.execute(
        "INSERT INTO comments(discussion_id, author_id, body, created_at)"
        " VALUES (1, ?, 'c', 1.0)",
        (agent["id"],),
    )
    conn.execute(
        "INSERT INTO notes(project_id, title, created_by, created_at, updated_at)"
        " VALUES (?, 'n', ?, 1.0, 1.0)",
        (pid, agent["id"]),
    )
    conn.execute(
        "INSERT INTO note_revisions(note_id, title, body, edited_by, created_at)"
        " VALUES (1, 'n', 'b', ?, 1.0)",
        (agent["id"],),
    )
    conn.execute(
        "INSERT INTO links(from_type, from_id, to_type, to_id, created_by,"
        " created_at) VALUES ('project', ?, 'project', ?, ?, 1.0)",
        (pid, pid, agent["id"]),
    )
    conn.commit()
    with pytest.raises(objects.ObjectError):
        objects.purge_project(conn, admin, pid)
    objects.purge_project(conn, boss, pid)
    for tbl in (
        "tasks",
        "todos",
        "transitions",
        "proofs",
        "task_revisions",
        "discussions",
        "comments",
        "notes",
        "note_revisions",
        "links",
    ):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert n == 0, f"{tbl} not empty"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM events WHERE verb = 'project.purged'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_body_frozen_for_agents_after_approval(env):
    conn, _boss, agent, _agent2, user, _admin = env
    pid = objects.create_project(conn, agent, "p")
    tid = objects.create_task(conn, agent, pid, "t")
    objects.edit_task(conn, agent, tid, body="draft ok")
    from app import statemachine

    for to in ("plan", "proposed"):
        statemachine.transition(conn, tid, to, agent)
    statemachine.transition(conn, tid, "approved", user)
    with pytest.raises(objects.ObjectError):
        objects.edit_task(conn, agent, tid, body="nope")
    t = objects.edit_task(conn, user, tid, body="human edit")
    revs = conn.execute("SELECT COUNT(*) FROM task_revisions").fetchone()[0]
    assert revs == 2 and t["version"] == 6
    conn.close()


def test_assignee_validation(env):
    conn, _boss, agent, agent2, _user, _admin = env
    pid = objects.create_project(conn, agent, "p")
    with pytest.raises(objects.ObjectError):
        objects.create_task(conn, agent, pid, "t", assignee_id=99999)
    tid = objects.create_task(conn, agent, pid, "t", assignee_id=agent2["id"])
    t = objects.edit_task(conn, agent, tid, assignee_id=None)
    assert t["assignee_id"] is None
    conn.close()


def test_todo_rules(env):
    conn, _boss, agent, agent2, user, _admin = env
    pid = objects.create_project(conn, agent, "p")
    tid = objects.create_task(conn, agent, pid, "t")
    todo = objects.add_todo(conn, agent, tid, "step")
    t = objects.tick_todo(conn, agent2, todo, True)
    assert t["done"] == 1 and t["done_by"] == agent2["id"]
    t = objects.tick_todo(conn, agent2, todo, False, version=t["version"])
    assert t["done"] == 0 and t["done_by"] is None
    with pytest.raises(VersionConflict):
        objects.tick_todo(conn, agent, todo, True, version=1)
    with pytest.raises(objects.ObjectError):
        objects.trash_todo(conn, agent, todo)
    objects.trash_todo(conn, user, todo)
    with pytest.raises(objects.ObjectError):
        objects.get_todo(conn, todo)
    conn.close()
