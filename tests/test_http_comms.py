"""HTTP e2e: comms surface — discussion, question freeze, search."""

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


def _seed():
    conn = db.connect(db.db_path())
    boss = setup.create_superadmin(conn, "root", PW)
    cur = conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('agent-one','clanker','active',1.0)"
    )
    conn.commit()
    tok = auth.mint_agent_token(conn, cur.lastrowid, boss["id"])
    conn.close()
    return tok


async def test_comms_flow(tmp_path):
    tok = _seed()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        h = {"Authorization": f"Bearer {tok}"}
        pid = (await c.post("/api/projects", headers=h, json={"name": "proj"})).json()[
            "id"
        ]
        did = (
            await c.post(
                f"/api/projects/{pid}/discussions",
                headers=h,
                json={"title": "the portal talk", "kind": "proposal"},
            )
        ).json()["id"]
        cid = (
            await c.post(
                f"/api/discussions/{did}/comments",
                headers=h,
                json={"body": "portal goes brr"},
            )
        ).json()["id"]
        reply = (
            await c.post(
                f"/api/discussions/{did}/comments",
                headers=h,
                json={"body": "agreed", "parent_id": cid},
            )
        ).json()["id"]
        assert reply > cid
        login = await c.post(
            "/api/auth/login", json={"username": "root", "password": PW}
        )
        hu = {"Authorization": "Bearer " + login.json()["token"]}
        trashed = await c.post(f"/api/comments/{cid}/trash", headers=hu)
        assert trashed.status_code == 204
        clanker_view = (await c.get(f"/api/discussions/{did}", headers=h)).json()
        assert len(clanker_view) == 1
        human_view = (await c.get(f"/api/discussions/{did}", headers=hu)).json()
        assert len(human_view) == 2
        restored = await c.post(f"/api/comments/{cid}/restore", headers=hu)
        assert restored.status_code == 204
        assert len((await c.get(f"/api/discussions/{did}", headers=h)).json()) == 2


async def test_question_freeze_e2e(tmp_path):
    tok = _seed()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        h = {"Authorization": f"Bearer {tok}"}
        pid = (await c.post("/api/projects", headers=h, json={"name": "proj"})).json()[
            "id"
        ]
        tid = (
            await c.post(
                "/api/tasks", headers=h, json={"project_id": pid, "title": "task"}
            )
        ).json()["id"]
        login = await c.post(
            "/api/auth/login", json={"username": "root", "password": PW}
        )
        {"Authorization": "Bearer " + login.json()["token"]}
        me = (await c.get("/api/auth/whoami", headers=h)).json()
        qid = (
            await c.post(
                "/api/questions",
                headers=h,
                json={
                    "project_id": pid,
                    "body": "why?",
                    "to_identity_id": me["id"],
                    "attach_type": "task",
                    "attach_id": tid,
                },
            )
        ).json()["id"]
        blocked = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "plan"}
        )
        assert blocked.status_code == 409
        listed = (await c.get("/api/questions?open=1&to_me=1", headers=h)).json()
        assert len(listed) == 1
        answered = await c.post(
            f"/api/questions/{qid}/answer", headers=h, json={"answer": "because"}
        )
        assert answered.status_code == 200
        ok = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "plan"}
        )
        assert ok.status_code == 200


async def test_search_e2e(tmp_path):
    tok = _seed()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        h = {"Authorization": f"Bearer {tok}"}
        pid = (await c.post("/api/projects", headers=h, json={"name": "proj"})).json()[
            "id"
        ]
        await c.post(
            "/api/tasks", headers=h, json={"project_id": pid, "title": "unicorn galore"}
        )
        empty = await c.get("/api/search?q=unicorn", headers=h)
        assert empty.status_code == 200
        assert any("unicorn" in (h2["title"] or "") for h2 in empty.json())
        none = await c.get("/api/search?q=", headers=h)
        assert none.json() == []
