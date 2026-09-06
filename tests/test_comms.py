"""Discussions, comments, chat — rules proven."""

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin

from app import comms


@pytest.fixture
def env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    other = clanker(conn, "clanker-y")
    user = human(conn, "userx", "user", boss)
    admin = human(conn, "adminx", "admin", boss)
    cur = conn.execute(
        "INSERT INTO projects(slug, name, owner_id, created_by,"
        " created_at) VALUES ('p', 'P', ?, ?, 1.0)",
        (agent["id"], agent["id"]),
    )
    conn.commit()
    return conn, boss, agent, other, user, admin, cur.lastrowid


def test_discussion_kinds_and_close(env):
    conn, _boss, agent, other, user, admin, pid = env
    did = comms.create_discussion(conn, agent, pid, "t", kind="proposal")
    with pytest.raises(comms.CommsError):
        comms.create_discussion(conn, agent, pid, "t", kind="rant")
    with pytest.raises(comms.CommsError):
        comms.close_discussion(conn, other, did)
    d = comms.close_discussion(conn, agent, did, outcome="settled")
    assert d["status"] == "closed" and d["outcome"] == "settled"
    with pytest.raises(comms.CommsError):
        comms.add_comment(conn, agent, did, "late")
    with pytest.raises(comms.CommsError):
        comms.reopen_discussion(conn, user, did)
    d = comms.reopen_discussion(conn, admin, did)
    assert d["status"] == "open"
    comms.add_comment(conn, agent, did, "back")
    conn.close()


def test_comment_depth_limit(env):
    conn, _boss, agent, _other, _user, _admin, pid = env
    did = comms.create_discussion(conn, agent, pid, "nested")
    parent = None
    for _ in range(4):
        cid = comms.add_comment(conn, agent, did, "level", parent_id=parent)
        parent = cid
    conn.close()


def test_comment_depth_six_rejected(env):
    conn, _boss, agent, _other, _user, _admin, pid = env
    did = comms.create_discussion(conn, agent, pid, "nested")
    parent = None
    for _ in range(4):
        parent = comms.add_comment(conn, agent, did, "ok", parent_id=parent)
    with pytest.raises(comms.CommsError):
        comms.add_comment(conn, agent, did, "too deep", parent_id=parent)
    with pytest.raises(comms.CommsError):
        comms.add_comment(conn, agent, did, "bad parent", parent_id=9999)
    conn.close()


def test_trash_visibility_semantics(env):
    conn, _boss, agent, _other, user, admin, pid = env
    did = comms.create_discussion(conn, agent, pid, "vis")
    cid = comms.add_comment(conn, agent, did, "to be trashed")
    with pytest.raises(comms.CommsError):
        comms.trash_comment(conn, agent, cid)
    comms.trash_comment(conn, user, cid)
    agent_view = comms.list_comments(conn, did, agent)
    human_view = comms.list_comments(conn, did, user)
    assert len(agent_view) == 0
    assert len(human_view) == 1 and human_view[0]["trashed_at"] is not None
    with pytest.raises(comms.CommsError):
        comms.restore_comment(conn, user, cid)
    comms.restore_comment(conn, admin, cid)
    assert len(comms.list_comments(conn, did, agent)) == 1
    comms.trash_comment(conn, user, cid)
    comms.purge_comment(conn, admin, cid)
    assert len(comms.list_comments(conn, did, user)) == 0
    conn.close()


def test_chat_rules(env):
    conn, _boss, agent, other, _user, _admin, pid = env
    m1 = comms.post_chat(conn, agent, pid, "hello")
    m2 = comms.post_chat(conn, other, pid, "world")
    assert m1 < m2
    assert [r["body"] for r in comms.list_chat(conn, pid)] == ["hello", "world"]
    assert [r["body"] for r in comms.list_chat(conn, pid, since_id=m1)] == ["world"]
    with pytest.raises(comms.CommsError):
        comms.post_chat(conn, agent, pid, "x" * 5000)
    conn.execute("UPDATE projects SET archived = 1 WHERE id = ?", (pid,))
    conn.commit()
    with pytest.raises(comms.CommsError):
        comms.post_chat(conn, agent, pid, "blocked")
    assert len(comms.list_chat(conn, pid)) == 2
    conn.close()
