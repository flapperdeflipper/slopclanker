"""Notes and wiki: editable, revisions always visible, freeze-aware."""

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin

from app import knowledge, objects, questions, search


@pytest.fixture
def env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    user = human(conn, "userx", "user", boss)
    pid = objects.create_project(conn, agent, "proj")
    return conn, boss, agent, user, pid


def test_note_lifecycle_and_revisions(env):
    conn, _boss, agent, user, pid = env
    nid = knowledge.create_note(
        conn, agent, pid, "runbook", body="step one", tags="ops"
    )
    knowledge.edit_note(conn, user, nid, body="step one and two")
    knowledge.edit_note(conn, agent, nid, title="runbook v2")
    note = knowledge.get_note(conn, nid)
    assert note["title"] == "runbook v2"
    assert note["updated_at"] >= note["created_at"]
    revs = knowledge.note_revisions(conn, nid)
    assert len(revs) == 2
    assert revs[0]["title"] == "runbook v2"
    assert revs[1]["body"] == "step one and two"
    hits = search.search(conn, "runbook", kind="note")
    assert len(hits) == 1
    conn.close()


def test_note_freeze(env):
    conn, _boss, agent, user, pid = env
    nid = knowledge.create_note(conn, agent, pid, "frozen note")
    questions.ask(
        conn,
        agent,
        pid,
        "why this note?",
        to_group="humans",
        attach_type="note",
        attach_id=nid,
    )
    with pytest.raises(knowledge.Frozen):
        knowledge.edit_note(conn, user, nid, body="blocked")
    conn.close()


def test_wiki_lifecycle(env):
    conn, _boss, agent, user, _pid = env
    knowledge.create_wiki(conn, agent, "conventions", "Conventions", body="be kind")
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.create_wiki(conn, user, "conventions", "Dup")
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.create_wiki(conn, user, "Bad_Slug", "Nope")
    knowledge.edit_wiki(conn, user, "conventions", body="be kind, cite tests")
    page = knowledge.get_wiki(conn, "conventions")
    assert page["body"] == "be kind, cite tests"
    revs = knowledge.wiki_revisions(conn, "conventions")
    assert len(revs) == 1 and revs[0]["edited_by"] == user["id"]
    hits = search.search(conn, "cite tests", kind="wiki")
    assert len(hits) == 1
    conn.close()


def test_wiki_freeze(env):
    conn, _boss, agent, user, pid = env
    knowledge.create_wiki(conn, agent, "freezeme", "F", body="x")
    wid = conn.execute("SELECT id FROM wiki WHERE slug = 'freezeme'").fetchone()[0]
    questions.ask(
        conn,
        agent,
        pid,
        "wiki q?",
        to_group="everyone",
        attach_type="wiki",
        attach_id=wid,
    )
    with pytest.raises(knowledge.Frozen):
        knowledge.edit_wiki(conn, user, "freezeme", body="blocked")
    conn.close()


def test_note_in_archived_project(env):
    conn, boss, agent, _user, pid = env
    objects.set_project_archived(conn, boss, pid, True)
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.create_note(conn, agent, pid, "late")
    conn.close()
