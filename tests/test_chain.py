"""Hash chains: verify passes, tampering is caught."""

from app import chain, db, events


def _seed(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    cur = conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('c','clanker','active',1.0)"
    )
    conn.commit()
    actor = conn.execute(
        "SELECT * FROM identities WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    from app import objects

    pid = objects.create_project(conn, actor, "p")
    tid = objects.create_task(conn, actor, pid, "t")
    return conn, actor, tid


def test_events_chain_verifies(tmp_path):
    conn, actor, _tid = _seed(tmp_path)
    for n in range(5):
        events.emit(conn, actor["id"], "test.event", "task", n, payload={"n": n})
    ok, broken = chain.verify_chain(conn, "events")
    assert ok and broken is None
    conn.close()


def test_tamper_detected(tmp_path):
    conn, actor, _tid = _seed(tmp_path)
    for n in range(5):
        events.emit(conn, actor["id"], "test.event", "task", n)
    conn.execute("UPDATE events SET payload = 'edited' WHERE id = 3")
    conn.commit()
    ok, broken = chain.verify_chain(conn, "events")
    assert not ok and broken == 3
    conn.close()


def test_tamper_via_delete_detected(tmp_path):
    conn, actor, _tid = _seed(tmp_path)
    for n in range(4):
        events.emit(conn, actor["id"], "test.event", "task", n)
    conn.execute("DELETE FROM events WHERE id = 2")
    conn.commit()
    ok, _ = chain.verify_chain(conn, "events")
    assert not ok
    conn.close()


def test_transitions_table_chains(tmp_path):
    conn, actor, tid = _seed(tmp_path)
    for n in range(3):
        chain.chained_insert(
            conn,
            "transitions",
            {
                "task_id": tid,
                "from_state": "idea",
                "to_state": "plan",
                "actor_id": actor["id"],
                "note": "",
                "created_at": float(n),
            },
        )
        conn.commit()
    assert chain.verify_chain(conn, "transitions")[0]
    conn.close()
