"""Durable inbox, live bus, wait(), SSE stream."""

import asyncio

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin

from app import db, events, objects, questions, statemachine
from app.bus import bus
from app.realtime import wait_for


@pytest.fixture
def env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    user = human(conn, "userx", "user", boss)
    pid = objects.create_project(conn, agent, "proj")
    db.db_path() if db.db_path() != ":memory:" else str(tmp_path / "t.db")
    return conn, boss, agent, user, pid, str(tmp_path / "t.db")


def test_addressed_event_lands_in_inbox(env):
    conn, _boss, agent, user, pid, _path = env
    tid = objects.create_task(conn, agent, pid, "t", assignee_id=agent["id"])
    statemachine.transition(conn, tid, "plan", agent)
    statemachine.transition(conn, tid, "proposed", agent)
    statemachine.transition(conn, tid, "approved", user)
    statemachine.transition(conn, tid, "building", agent)
    import time as _t

    conn.execute(
        "INSERT INTO proofs(task_id, provider, kind, url, added_by, added_at)"
        " VALUES (?, 'github', 'pr', 'u', ?, ?)",
        (tid, agent["id"], _t.time()),
    )
    conn.commit()
    statemachine.transition(conn, tid, "review", agent)
    statemachine.transition(conn, tid, "done", user)
    ev_id = statemachine.transition(conn, tid, "previous", user, note="redo")["id"]
    addressed = events.feed(conn, to_identity_id=agent["id"])
    assert {e["verb"] for e in addressed} == {"task.created", "task.transitioned"}
    unread = events.unread_for(conn, agent["id"])
    assert len(unread) == 2
    assert events.mark_read(conn, agent["id"], [u["id"] for u in unread]) == 2
    assert events.unread_for(conn, agent["id"]) == []
    assert events.mark_read(conn, agent["id"], [ev_id]) == 0
    conn.close()


def test_group_question_fans_out_and_resolves(env):
    conn, _boss, agent, _user, pid, _path = env
    agent2 = clanker(conn, "clanker-y")
    questions.ask(conn, agent, pid, "group q", to_group="clankers")
    assert len(events.unread_for(conn, agent["id"])) == 1
    assert len(events.unread_for(conn, agent2["id"])) == 1
    q = conn.execute("SELECT id FROM questions LIMIT 1").fetchone()[0]
    questions.answer(conn, agent2, q, "done")
    assert events.unread_for(conn, agent2["id"]) == []
    conn.close()


def test_bus_pubsub_bounded(env):
    conn, _boss, agent, _user, _pid, _path = env
    q = bus.subscribe()
    events.emit(conn, agent["id"], "test.ev", "task", 1, payload={"x": 1})
    got = q.get_nowait()
    assert got["verb"] == "test.ev" and got["payload"] == {"x": 1}
    for i in range(300):
        events.emit(conn, agent["id"], "test.ev", "task", i)
    assert q.qsize() <= 256
    bus.unsubscribe(q)
    size = q.qsize()
    events.emit(conn, agent["id"], "test.ev", "task", 999)
    assert q.qsize() == size
    conn.close()


@pytest.mark.anyio
async def test_wait_to_me_returns_and_marks_read(tmp_path):
    path = str(tmp_path / "t.db")
    conn = db.init_db(path)
    boss = superadmin(conn)
    agent = clanker(conn)
    human(conn, "userx", "user", boss)
    pid = objects.create_project(conn, agent, "proj")
    questions.ask(conn, agent, pid, "for the agent", to_identity_id=agent["id"])
    conn.close()
    rows = await wait_for(agent_row_id(path), to_me=True, timeout=0.1, db_path=path)
    assert len(rows) == 1 and rows[0]["verb"] == "question.asked"
    rows2 = await wait_for(agent_row_id(path), to_me=True, timeout=0.1, db_path=path)
    assert rows2 == []


def agent_row_id(path):
    conn = db.connect(path)
    row = conn.execute("SELECT id FROM identities WHERE kind = 'clanker'").fetchone()
    conn.close()
    return row[0]


@pytest.mark.anyio
async def test_wait_blocks_until_event(tmp_path):
    path = str(tmp_path / "t.db")
    conn = db.init_db(path)
    superadmin(conn)
    agent = clanker(conn)
    pid = objects.create_project(conn, agent, "proj")
    actor_id = agent["id"]
    conn.close()

    async def poke():
        await asyncio.sleep(0.2)
        c = db.connect(path)
        a = c.execute("SELECT * FROM identities WHERE id = ?", (actor_id,)).fetchone()
        objects.create_task(c, a, pid, "late task")
        c.close()

    task = asyncio.create_task(poke())
    rows = await wait_for(
        actor_id, obj_type="task", to_me=False, timeout=5.0, db_path=path
    )
    await task
    assert len(rows) == 1
    assert rows[0]["verb"] == "task.created"


@pytest.mark.anyio
async def test_wait_timeout_empty(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path).close()
    rows = await wait_for(999, to_me=False, timeout=0.1, db_path=path)
    assert rows == []
