"""Decisions register and generic links."""

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin

from app import decisions, links, objects


@pytest.fixture
def env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    user = human(conn, "userx", "user", boss)
    p1 = objects.create_project(conn, agent, "one")
    p2 = objects.create_project(conn, agent, "two")
    return conn, boss, agent, user, p1, p2


def test_supersede_chain(env):
    conn, _boss, agent, user, p1, p2 = env
    d1 = decisions.create(conn, agent, p1, "first")
    d2 = decisions.create(conn, agent, p1, "second")
    cross = decisions.create(conn, agent, p2, "other project")
    with pytest.raises(decisions.DecisionError):
        decisions.set_status(conn, user, d2, "superseded")
    with pytest.raises(decisions.DecisionError):
        decisions.set_status(conn, user, d2, "superseded", supersede_id=cross)
    d = decisions.set_status(conn, user, d2, "superseded", supersede_id=d1)
    assert d["supersedes_id"] == d1
    conn.close()


def test_decisions_create_by_clanker_ok(env):
    conn, _boss, agent, _user, p1, _p2 = env
    did = decisions.create(conn, agent, p1, "clanker proposal", context="c")
    assert questions_status(conn, did) == "proposed"
    conn.close()


def questions_status(conn, did):
    return conn.execute("SELECT status FROM decisions WHERE id = ?", (did,)).fetchone()[
        0
    ]


def test_link_rules(env):
    conn, _boss, agent, user, p1, p2 = env
    lid = links.create(conn, agent, "project", p1, "project", p2)
    with pytest.raises(links.LinkError):
        links.create(conn, agent, "project", p1, "project", p2)
    with pytest.raises(links.LinkError):
        links.create(conn, agent, "project", p1, "project", 9999)
    with pytest.raises(links.LinkError):
        links.create(conn, agent, "project", p1, "sandwich", 1)
    with pytest.raises(links.LinkError):
        links.create(conn, agent, "project", p1, "project", p1)
    with pytest.raises(links.LinkError):
        links.remove(conn, agent, lid)
    links.remove(conn, user, lid)
    assert links.context_for(conn, "project", p1) == []
    conn.close()


def test_context_both_directions(env):
    conn, _boss, agent, _user, p1, p2 = env
    links.create(conn, agent, "project", p1, "project", p2)
    from_side = links.context_for(conn, "project", p1)
    to_side = links.context_for(conn, "project", p2)
    assert len(from_side) == 1 and from_side[0]["direction"] == "to"
    assert len(to_side) == 1 and to_side[0]["direction"] == "from"
    assert from_side[0]["id"] == p2 and to_side[0]["id"] == p1
    conn.close()
