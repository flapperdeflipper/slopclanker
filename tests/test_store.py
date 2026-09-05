"""Store domain logic: presence, projects, posts/comments, todos, notes,
wiki, chat, events, claims."""

import time

import pytest

from app import store
from app.db import connect


@pytest.fixture
def conn(tmp_path):
    return connect(tmp_path / "sc.db")


# --- agents / presence --------------------------------------------------


def test_hello_registers_agent(conn):
    snap = store.hello(conn, "clanker-a", heartbeat_timeout=900)
    agents = {a["name"]: a for a in snap["agents"]}
    assert "clanker-a" in agents
    assert agents["clanker-a"]["active"] is True


def test_hello_persists_profile_fields(conn):
    store.hello(conn, "clanker-a", role="yaml wrangler", contact="openchamber/x")
    again = store.hello(conn, "clanker-a", note="second hello")
    a = {x["name"]: x for x in again["agents"]}["clanker-a"]
    assert a["role"] == "yaml wrangler"
    assert a["contact"] == "openchamber/x"
    assert a["note"] == "second hello"


def test_profile_set_partial_update(conn):
    store.profile_set(conn, "clanker-a", note="bio", role="ops")
    store.profile_set(conn, "clanker-a", role="new role")
    agent = store.get_agent(conn, "clanker-a")
    assert agent["role"] == "new role"
    assert agent["note"] == "bio"  # untouched


def test_get_agent_unknown(conn):
    assert store.get_agent(conn, "nobody") is None


# --- projects -----------------------------------------------------------


def test_create_and_get_project(conn):
    p = store.create_project(conn, "Home Assistant", created_by="clanker-a")
    assert p["slug"] == "home-assistant"
    assert store.get_project(conn, "home-assistant")["id"] == p["id"]
    assert store.get_project(conn, p["id"])["id"] == p["id"]


def test_project_slug_clash_rejected(conn):
    store.create_project(conn, "dup", created_by="a")
    with pytest.raises(ValueError, match="already exists"):
        store.create_project(conn, "dup", created_by="a")


def test_default_project_seeded(conn):
    assert store.get_project(conn, "general") is not None


# --- posts + comments ----------------------------------------------------


def test_create_post_stores_body(conn):
    pid = store.create_post(
        conn, "Who merges?", "I can.", created_by="a", kind="proposal", audience="b"
    )
    detail = store.post_detail(conn, pid)
    assert detail["kind"] == "proposal"
    assert detail["status"] == "open"
    assert detail["body"] == "I can."
    assert detail["comments"] == []


def test_bad_kind_rejected(conn):
    with pytest.raises(ValueError, match="kind"):
        store.create_post(conn, "t", "b", created_by="a", kind="rant")


def test_bad_project_rejected(conn):
    with pytest.raises(ValueError, match="project"):
        store.create_post(conn, "t", "b", created_by="a", project_id=999)


def _chain(conn, pid, n):
    """Add n nested comments, each a child of the previous; returns ids."""
    ids = []
    parent = None
    for i in range(n):
        parent = store.add_comment(conn, pid, f"c{i}", f"lvl{i}", parent_id=parent)
        ids.append(parent)
    return ids


def test_comment_nesting_to_max_depth(conn):
    pid = store.create_post(conn, "t", "b", created_by="a")
    ids = _chain(conn, pid, store.MAX_COMMENT_DEPTH)
    assert len(ids) == store.MAX_COMMENT_DEPTH


def test_comment_beyond_max_depth_rejected(conn):
    pid = store.create_post(conn, "t", "b", created_by="a")
    ids = _chain(conn, pid, store.MAX_COMMENT_DEPTH)
    with pytest.raises(ValueError, match="max comment depth"):
        store.add_comment(conn, pid, "c", "too deep", parent_id=ids[-1])


def test_comment_parent_from_other_post_rejected(conn):
    p1 = store.create_post(conn, "t1", "b", created_by="a")
    p2 = store.create_post(conn, "t2", "b", created_by="a")
    c1 = store.add_comment(conn, p1, "a", "hi")
    with pytest.raises(ValueError, match="different post"):
        store.add_comment(conn, p2, "a", "hi", parent_id=c1)


def test_comment_on_closed_post_rejected(conn):
    pid = store.create_post(conn, "t", "b", created_by="a")
    store.close_post(conn, pid, "done")
    with pytest.raises(ValueError, match="closed"):
        store.add_comment(conn, pid, "a", "late")


def test_close_post_records_outcome(conn):
    pid = store.create_post(conn, "t", "b", created_by="a")
    store.close_post(conn, pid, "merged by b")
    detail = store.post_detail(conn, pid)
    assert detail["status"] == "closed"
    assert detail["outcome"] == "merged by b"
    with pytest.raises(ValueError, match="already closed"):
        store.close_post(conn, pid, "again")


def test_list_posts_activity_at(conn):
    import time as _t

    pid = store.create_post(conn, "t", "b", created_by="a")
    before = store.list_posts(conn)[0]["activity_at"]
    _t.sleep(0.02)
    store.add_comment(conn, pid, "b", "reply")
    after = store.list_posts(conn)[0]["activity_at"]
    assert after > before >= 0


def test_update_todo_accepts_tag_list(conn):
    tid = store.add_todo(conn, created_by="a", title="x", tags="one")
    row = store.update_todo(conn, tid, actor="a", tags=["two", "three"])
    assert row["tags"] == "three,two"


def test_unread_post_count(conn):
    import time as _t

    seen = _t.time()
    _t.sleep(0.02)
    assert store.unread_post_count(conn, seen) == 0
    pid = store.create_post(conn, "t", "b", created_by="a")
    assert store.unread_post_count(conn, seen) == 1
    store.close_post(conn, pid, "done")
    assert store.unread_post_count(conn, seen) == 0


def test_list_posts_counts_and_filters(conn):
    p = store.create_project(conn, "proj", created_by="a")
    in_proj_id = store.create_post(
        conn, "in proj", "b", created_by="a", project_id=p["id"]
    )
    store.create_post(conn, "closed one", "b", created_by="a")
    store.close_post(conn, store.list_posts(conn)[0]["id"], "wontfix")
    open_all = store.list_posts(conn)
    assert len(open_all) == 1 and open_all[0]["title"] == "in proj"
    store.add_comment(conn, in_proj_id, "a", "a comment")
    in_proj = store.list_posts(conn, project_id=p["id"], include_closed=True)
    assert len(in_proj) == 1
    assert in_proj[0]["comment_count"] == 1
    assert in_proj[0]["project_slug"] == "proj"


# --- todos ---------------------------------------------------------------


def test_add_todo_full_fields(conn):
    tid = store.add_todo(
        conn,
        created_by="a",
        title="ship it",
        body="long desc",
        priority="urgent",
        tags=["ui", "api"],
        assignee="b",
    )
    row = store.list_todos(conn)[0]
    assert row["id"] == tid
    assert row["priority"] == "urgent"
    assert row["tags"] == "api,ui"
    assert row["assignee"] == "b"


def test_add_todo_title_falls_back_to_body(conn):
    store.add_todo(conn, created_by="a", body="a very long description line here")
    assert store.list_todos(conn)[0]["title"].startswith("a very long")


def test_bad_priority_rejected(conn):
    with pytest.raises(ValueError, match="priority"):
        store.add_todo(conn, created_by="a", title="x", priority="whenever")


def test_todo_lifecycle(conn):
    tid = store.add_todo(conn, created_by="a", title="x")
    store.done_todo(conn, tid, actor="a")
    assert store.list_todos(conn, status="open") == []
    assert len(store.list_todos(conn, status="done")) == 1
    store.reopen_todo(conn, tid)
    assert len(store.list_todos(conn, status="open")) == 1
    store.archive_todo(conn, tid)
    assert store.list_todos(conn, status="open") == []
    archived = store.list_todos(conn, status="archive")
    assert len(archived) == 1 and archived[0]["archived"] == 1


def test_list_todos_by_project_and_assignee(conn):
    p = store.create_project(conn, "proj", created_by="a")
    store.add_todo(
        conn, created_by="a", title="p-todo", project_id=p["id"], assignee="b"
    )
    store.add_todo(conn, created_by="a", title="g-todo")
    assert [t["title"] for t in store.list_todos(conn, project_id=p["id"])] == [
        "p-todo"
    ]
    assert [t["title"] for t in store.list_todos(conn, assignee="b")] == ["p-todo"]


def test_update_todo_fields(conn):
    tid = store.add_todo(conn, created_by="a", title="x")
    row = store.update_todo(
        conn, tid, actor="a", priority="high", tags="one,two", assignee="b"
    )
    assert row["priority"] == "high"
    assert row["tags"] == "one,two"
    assert row["assignee"] == "b"


def test_update_todo_unknown_field(conn):
    tid = store.add_todo(conn, created_by="a", title="x")
    with pytest.raises(ValueError, match="unknown todo field"):
        store.update_todo(conn, tid, nonsense="x")


# --- notes -----------------------------------------------------------------


def test_note_create_update(conn):
    nid = store.save_note(conn, "deploy notes", created_by="a", body="- [ ] step")
    store.save_note(
        conn,
        "deploy notes v2",
        created_by="b",
        body="- [x] step",
        note_id=nid,
        tags="ops",
    )
    note = store.get_note(conn, nid)
    assert note["title"] == "deploy notes v2"
    assert note["tags"] == "ops"
    notes = store.list_notes(conn)
    assert len(notes) == 1


def test_note_unknown_update_rejected(conn):
    with pytest.raises(ValueError, match="does not exist"):
        store.save_note(conn, "x", created_by="a", note_id=99)


# --- wiki --------------------------------------------------------------------


def test_wiki_slug_auto_and_clash(conn):
    slug = store.save_page(conn, "Runbook: Backups", created_by="a")
    assert slug == "runbook-backups"
    with pytest.raises(ValueError, match="already exists"):
        store.save_page(conn, "Runbook: Backups", created_by="b")


def test_wiki_update_via_get_then_save(conn):
    store.save_page(conn, "Conventions", created_by="a", body="v1")
    page = store.get_page(conn, "conventions")
    store.save_page(
        conn, "Conventions", created_by="b", body="v2", page_id=int(page["id"])
    )
    assert store.get_page(conn, "conventions")["body"] == "v2"
    assert len(store.list_pages(conn)) == 1


# --- chat ----------------------------------------------------------------------


def test_chat_send_list_since(conn):
    store.chat_send(conn, "a", "first")
    midway = time.time()
    store.chat_send(conn, "b", "second")
    assert len(store.chat_list(conn)) == 2
    fresh = store.chat_list(conn, since=midway)
    assert [m["body"] for m in fresh] == ["second"]


# --- events ---------------------------------------------------------------------


def test_events_scoped_by_project(conn):
    p = store.create_project(conn, "proj", created_by="a")
    store.create_post(conn, "in proj", "b", created_by="a", project_id=p["id"])
    store.create_post(conn, "global", "b", created_by="a")
    store.hello(conn, "a")
    in_proj = store.list_events(conn, project_id=p["id"])
    assert all(e["project_id"] == p["id"] for e in in_proj)
    assert any(e["verb"] == "posted" for e in in_proj)
    all_events = store.list_events(conn)
    assert any(e["verb"] == "said hello" for e in all_events)
    assert not any(e["verb"] == "said hello" for e in in_proj)


def test_events_logged_for_actions(conn):
    store.create_post(conn, "t", "b", created_by="a")
    tid = store.add_todo(conn, created_by="a", title="x")
    store.done_todo(conn, tid, actor="b")
    events = store.list_events(conn)
    verbs = [(e["actor"], e["verb"], e["obj_type"]) for e in events]
    assert ("a", "posted", "post") in verbs
    assert ("a", "added todo", "todo") in verbs
    assert ("b", "finished todo", "todo") in verbs
    assert ("?", "posted", "post") not in verbs


# --- awareness --------------------------------------------------------------------


def test_check_sees_new_post_comment_todo(conn):
    t0 = time.time() - 1
    store.hello(conn, "a")
    pid = store.create_post(conn, "for b", "body", created_by="a", audience="b")
    c1 = store.add_comment(conn, pid, "a", "b should answer")
    store.add_todo(conn, created_by="a", title="b's job", assignee="b")
    out = store.check(conn, "b", since=t0)
    assert [p["id"] for p in out["posts"]] == [pid]
    assert [c["id"] for c in out["comments"]] == [c1]
    assert len(out["todos"]) == 1


def test_check_respects_audience(conn):
    t0 = time.time() - 1
    store.create_post(conn, "secret", "b only", created_by="a", audience="b")
    out = store.check(conn, "c", since=t0)
    assert out["posts"] == []


# --- claims ------------------------------------------------------------------------


def test_claims_set_check_release(conn):
    store.hello(conn, "a")
    store.set_claims(conn, "a", ["/homeassistant/automations.yaml"], note="editing")
    conflicts = store.check_claims(conn, "/homeassistant/automations.yaml", agent="b")
    assert len(conflicts) == 1
    assert conflicts[0]["agent"] == "a"
    own = store.check_claims(conn, "/homeassistant/automations.yaml", agent="a")
    assert own == []
    store.release_claims(conn, "a", ["/homeassistant/automations.yaml"])
    assert store.check_claims(conn, "/homeassistant/automations.yaml", agent="b") == []


def test_stale_claim_flagged(conn):
    store.hello(conn, "a")
    conn.execute(
        "UPDATE agents SET last_seen = ? WHERE name = 'a'", (time.time() - 9999,)
    )
    store.set_claims(conn, "a", ["/x"])
    conflicts = store.check_claims(conn, "/x", agent="b")
    assert conflicts[0]["stale"] is True
