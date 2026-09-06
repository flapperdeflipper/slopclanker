"""Proof endpoints over HTTP: auth, human-only trash, gate behavior."""

import pytest
from helpers_ids import clanker, fresh_db, human, superadmin
from httpx import ASGITransport, AsyncClient

from app import auth, bootstrap, db, objects
from app.main import asgi_app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SLOPCLANKER_REG_TOKEN", "test-reg-token-1234567890")
    bootstrap.ensure(db.db_path())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_proof_endpoints_flow(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    user = human(conn, "theu", "user", boss)
    pid = objects.create_project(conn, agent, "proj")
    tid = objects.create_task(conn, agent, pid, "t")
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    utok = auth.mint_agent_token(conn, user["id"], boss["id"])
    conn.close()
    ah = {"Authorization": f"Bearer {atok}"}
    uh = {"Authorization": f"Bearer {utok}"}
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get(f"/api/tasks/{tid}/proofs")
        assert r.status_code == 401

        r = await c.post(
            f"/api/tasks/{tid}/proofs",
            headers=ah,
            json={"url": "https://github.com/o/r/pull/6"},
        )
        assert r.status_code == 201
        assert r.json()["kind"] == "pr" and r.json()["provider"] == "github"

        r = await c.get(f"/api/tasks/{tid}/proofs", headers=ah)
        assert len(r.json()) == 1
        proof_id = r.json()[0]["id"]

        r = await c.post(f"/api/proofs/{proof_id}/trash", headers=ah)
        assert r.status_code == 403  # clankers never remove

        r = await c.post(f"/api/proofs/{proof_id}/trash", headers=uh)
        assert r.status_code == 200

        r = await c.post(f"/api/tasks/{tid}/proofs", headers=ah, json={"url": ""})
        assert r.status_code == 422

        r = await c.post(f"/api/tasks/{tid}/proofs/check", headers=ah)
        assert r.status_code == 200  # inert (no tokens) but never a crash


@pytest.mark.anyio
async def test_proof_add_frozen_via_http(tmp_path):
    from app import questions

    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    pid = objects.create_project(conn, agent, "proj")
    tid = objects.create_task(conn, agent, pid, "fz")
    questions.ask(
        conn, agent, pid, "hold?", to_group="humans", attach_type="task", attach_id=tid
    )
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.post(
            f"/api/tasks/{tid}/proofs",
            headers={"Authorization": f"Bearer {atok}"},
            json={"url": "https://github.com/o/r/pull/7"},
        )
        assert r.status_code == 422
        assert "frozen" in r.json()["error"]
