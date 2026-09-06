"""HTTP e2e: the object surface end to end, clanker + human doors."""

import time

import pytest
from helpers_ids import PW
from httpx import ASGITransport, AsyncClient

from app import auth, bootstrap, db, setup
from app.main import asgi_app

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SLOPCLANKER_REG_TOKEN", "test-reg-token-1234567890")
    from app import ratelimit

    ratelimit.reset()
    bootstrap.ensure(db.db_path())
    yield


async def _client():
    return AsyncClient(transport=ASGITransport(app=asgi_app), base_url="http://test")


def _seed_identities():
    conn = db.connect(db.db_path())
    boss = setup.create_superadmin(conn, "root", PW)
    cur = conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('agent-one','clanker','active',1.0)"
    )
    conn.commit()
    agent_tok = auth.mint_agent_token(conn, cur.lastrowid, boss["id"])
    agent = conn.execute(
        "SELECT * FROM identities WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    conn.close()
    return boss, agent, agent_tok


async def test_full_object_flow(tmp_path):
    _boss, _agent, agent_tok = _seed_identities()
    async with await _client() as c:
        h = {"Authorization": f"Bearer {agent_tok}"}
        proj = await c.post("/api/projects", headers=h, json={"name": "e2e project"})
        assert proj.status_code == 201
        pid = proj.json()["id"]
        task = await c.post(
            "/api/tasks", headers=h, json={"project_id": pid, "title": "e2e task"}
        )
        assert task.status_code == 201
        tid = task.json()["id"]
        for to in ("plan", "proposed"):
            r = await c.post(f"/api/tasks/{tid}/transition", headers=h, json={"to": to})
            assert r.status_code == 200
        denied = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "approved"}
        )
        assert denied.status_code == 403
        login = await c.post(
            "/api/auth/login", json={"username": "root", "password": PW}
        )
        assert login.status_code == 200
        hu = {"Authorization": "Bearer " + login.json()["token"]}
        r = await c.post(
            f"/api/tasks/{tid}/transition", headers=hu, json={"to": "approved"}
        )
        assert r.status_code == 200
        r = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "building"}
        )
        assert r.status_code == 200
        blocked = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "review"}
        )
        assert blocked.status_code == 422
        assert "proof" in blocked.json()["error"]
        todo = await c.post(
            f"/api/tasks/{tid}/todos", headers=h, json={"title": "step one"}
        )
        assert todo.status_code == 201
        tick = await c.post(f"/api/todos/{todo.json()['id']}/tick", headers=h, json={})
        assert tick.status_code == 200
        r = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "review"}
        )
        assert r.status_code == 422
        waive = await c.patch(
            f"/api/tasks/{tid}", headers=hu, json={"proof_waived": True}
        )
        assert waive.status_code == 200
        r = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "review"}
        )
        assert r.status_code == 200
        await c.post(f"/api/tasks/{tid}/todos", headers=h, json={"title": "left open"})
        undone = await c.post(
            f"/api/tasks/{tid}/transition", headers=hu, json={"to": "done"}
        )
        assert undone.status_code == 422
        r = await c.post(
            f"/api/tasks/{tid}/transition",
            headers=hu,
            json={"to": "done", "note": "fine as is"},
        )
        assert r.status_code == 200
        detail = await c.get(f"/api/tasks/{tid}", headers=h)
        body = detail.json()
        assert body["task"]["state"] == "done"
        assert len(body["todos"]) == 2 and len(body["transitions"]) == 6
        notdone = await c.post(
            f"/api/tasks/{tid}/transition",
            headers=hu,
            json={"to": "previous", "note": "redo"},
        )
        assert notdone.status_code == 200
        assert notdone.json()["state"] == "review"


async def test_version_conflict_and_freeze_over_http(tmp_path):
    _boss, agent, agent_tok = _seed_identities()
    async with await _client() as c:
        h = {"Authorization": f"Bearer {agent_tok}"}
        pid = (await c.post("/api/projects", headers=h, json={"name": "p"})).json()[
            "id"
        ]
        tid = (
            await c.post(
                "/api/tasks", headers=h, json={"project_id": pid, "title": "t"}
            )
        ).json()["id"]
        stale = await c.patch(
            f"/api/tasks/{tid}", headers=h, json={"body": "x", "version": 99}
        )
        assert stale.status_code == 409
        conn = db.connect(db.db_path())
        conn.execute(
            "INSERT INTO questions(project_id, body, asked_by, asked_to_group,"
            " attach_type, attach_id, status, created_at)"
            " VALUES (?, 'why?', ?, 'everyone', 'task', ?, 'open', ?)",
            (pid, agent["id"], tid, time.time()),
        )
        conn.commit()
        conn.close()
        frozen = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "plan"}
        )
        assert frozen.status_code == 409
        assert frozen.json()["questions"][0]["body"] == "why?"
        note_add = await c.post(
            f"/api/tasks/{tid}/todos", headers=h, json={"title": "nope"}
        )
        assert note_add.status_code == 409


async def test_unauthenticated_and_admin_gates(tmp_path):
    _boss, _agent, agent_tok = _seed_identities()
    async with await _client() as c:
        anon = await c.get("/api/tasks")
        assert anon.status_code == 401
        h = {"Authorization": f"Bearer {agent_tok}"}
        stacks = await c.post("/api/stacks", headers=h, json={"name": "s"})
        assert stacks.status_code == 422  # ObjectError: admins only
        assert "admins" in stacks.json()["error"]
        login = await c.post(
            "/api/auth/login", json={"username": "root", "password": PW}
        )
        hu = {"Authorization": "Bearer " + login.json()["token"]}
        stacks = await c.post("/api/stacks", headers=hu, json={"name": "s"})
        assert stacks.status_code == 201
        listing = await c.get("/api/stacks", headers=h)
        assert listing.status_code == 200 and len(listing.json()) == 1
