"""HTTP e2e: claims, notes, wiki."""

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
    ids = []
    toks = {}
    for name in ("agent-a", "agent-b"):
        cur = conn.execute(
            "INSERT INTO identities(name, kind, status,"
            " created_at) VALUES (?, 'clanker', 'active', 1.0)",
            (name,),
        )
        conn.commit()
        ids.append(cur.lastrowid)
        toks[name] = auth.mint_agent_token(conn, cur.lastrowid, boss["id"])
    conn.close()
    return ids, toks


async def test_claims_e2e(tmp_path):
    _ids, toks = _seed()
    ha = {"Authorization": f"Bearer {toks['agent-a']}"}
    hb = {"Authorization": f"Bearer {toks['agent-b']}"}
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/claims", headers=ha, json={"paths": ["/src/main.py"], "note": "mine"}
        )
        assert r.status_code == 201
        hits = await c.get("/api/claims?path=/src/main.py", headers=hb)
        assert len(hits.json()) == 1
        mine = await c.get("/api/claims", headers=ha)
        assert len(mine.json()) == 1
        rel = await c.post(
            "/api/claims/release", headers=ha, json={"paths": ["/src/main.py"]}
        )
        assert rel.json() == {"claims": 0}
        assert (await c.get("/api/claims?path=/src/main.py", headers=hb)).json() == []


async def test_notes_wiki_e2e(tmp_path):
    _ids, toks = _seed()
    h = {"Authorization": f"Bearer {toks['agent-a']}"}
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        pid = (await c.post("/api/projects", headers=h, json={"name": "proj"})).json()[
            "id"
        ]
        nid = (
            await c.post(
                f"/api/projects/{pid}/notes",
                headers=h,
                json={"title": "note", "body": "v1"},
            )
        ).json()["id"]
        edit = await c.patch(f"/api/notes/{nid}", headers=h, json={"body": "v2"})
        assert edit.status_code == 200
        detail = await c.get(f"/api/notes/{nid}", headers=h)
        assert detail.json()["note"]["body"] == "v2"
        assert len(detail.json()["revisions"]) == 1
        w = await c.post(
            "/api/wiki", headers=h, json={"slug": "home", "title": "Home", "body": "hi"}
        )
        assert w.status_code == 201
        we = await c.patch("/api/wiki/home", headers=h, json={"body": "hello"})
        assert we.status_code == 200
        got = await c.get("/api/wiki/home", headers=h)
        assert got.json()["page"]["body"] == "hello"
        assert len(got.json()["revisions"]) == 1
        found = await c.get("/api/search", headers=h, params={"q": "hello"})
        assert any(h2.get("kind") == "wiki" for h2 in found.json())
