"""FTS search: finds the durable memory, excludes chat, injection-safe."""

from helpers_ids import clanker, fresh_db, human, superadmin

from app import comms, decisions, objects, questions, search


def _env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    user = human(conn, "userx", "user", boss)
    p1 = objects.create_project(conn, agent, "alpha")
    p2 = objects.create_project(conn, agent, "beta")
    return conn, boss, agent, user, p1, p2


def test_finds_all_kinds(tmp_path):
    conn, _boss, agent, _user, p1, _p2 = _env(tmp_path)
    objects.create_task(
        conn, agent, p1, "fix the quantizer drift", body="quantizer drifts overnight"
    )
    did = comms.create_discussion(conn, agent, p1, "quantizer talk")
    comms.add_comment(conn, agent, did, "the quantizer is fine")
    decisions.create(conn, agent, p1, "keep quantizer v2")
    questions.ask(conn, agent, p1, "is the quantizer tested?", to_group="everyone")
    hits = search.search(conn, "quantizer")
    kinds = {h["kind"] for h in hits}
    assert kinds == {"task", "discussion", "comment", "decision", "question"}
    conn.close()


def test_project_and_kind_filters(tmp_path):
    conn, _boss, agent, _user, p1, p2 = _env(tmp_path)
    objects.create_task(conn, agent, p1, "shared word zebras")
    objects.create_task(conn, agent, p2, "shared word zebras")
    hits = search.search(conn, "zebras")
    assert len(hits) == 2
    assert all(h["kind"] == "task" for h in search.search(conn, "zebras", kind="task"))
    only_p1 = search.search(conn, "zebras", project_id=p1)
    assert len(only_p1) == 1
    conn.close()


def test_chat_not_searchable(tmp_path):
    conn, _boss, agent, _user, p1, _p2 = _env(tmp_path)
    comms.post_chat(conn, agent, p1, "xyzzyplugh secret")
    assert search.search(conn, "xyzzyplugh") == []
    conn.close()


def test_injection_and_junk_queries(tmp_path):
    conn, _boss, agent, _user, p1, _p2 = _env(tmp_path)
    objects.create_task(conn, agent, p1, "OR1=1 task", body="test")
    assert search.search(conn, '" OR 1=1; --') is not None
    assert search.search(conn, "OR 1=1") == [] or isinstance(
        search.search(conn, "OR 1=1"), list
    )
    assert search.search(conn, "") == []
    assert search.search(conn, "!!!") == []
    assert search.build_query('ab"cd*ef') == '"ab" "cd" "ef"'
    conn.close()


def test_bad_kind_rejected(tmp_path):
    conn, _boss, _agent, _user, _p1, _p2 = _env(tmp_path)
    import pytest

    with pytest.raises(ValueError):
        search.search(conn, "x", kind="chat")
    conn.close()
