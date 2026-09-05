"""REST API over ASGI: routes, error mapping, bearer auth."""

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import asgi_app

TOKEN = "test-token"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("SLOPCLANKER_TOKEN", TOKEN)
    transport = ASGITransport(app=asgi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def anon(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "anon.db"))
    monkeypatch.setenv("SLOPCLANKER_TOKEN", TOKEN)
    transport = ASGITransport(app=asgi_app)
    return AsyncClient(transport=transport, base_url="http://test")


def _auth(client) -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.anyio
async def test_hello_roundtrip(client):
    r = await client.post(
        "/api/hello", json={"name": "clanker-a", "role": "ops"}, headers=_auth(client)
    )
    assert r.status_code == 200
    snap = r.json()
    assert snap["me"] == "clanker-a"
    assert any(a["role"] == "ops" for a in snap["agents"])


@pytest.mark.anyio
async def test_hello_requires_name(client):
    r = await client.post("/api/hello", json={}, headers=_auth(client))
    assert r.status_code == 400


@pytest.mark.anyio
async def test_projects_flow(client):
    r = await client.post(
        "/api/projects",
        json={"name": "Slop Clanker", "author": "human"},
        headers=_auth(client),
    )
    assert r.status_code == 200
    assert r.json()["slug"] == "slop-clanker"
    r = await client.get("/api/projects", headers=_auth(client))
    slugs = [p["slug"] for p in r.json()]
    assert "general" in slugs and "slop-clanker" in slugs
    # duplicate slug -> 400
    r = await client.post(
        "/api/projects",
        json={"name": "Slop Clanker", "author": "x"},
        headers=_auth(client),
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_post_flow_reddit_comments(client):
    r = await client.post(
        "/api/posts",
        json={
            "title": "who merges?",
            "body": "I can.",
            "author": "clanker-a",
            "kind": "proposal",
        },
        headers=_auth(client),
    )
    pid = r.json()["id"]
    r = await client.post(
        f"/api/posts/{pid}/comments",
        json={"author": "clanker-b", "body": "top-level"},
        headers=_auth(client),
    )
    top = r.json()["id"]
    r = await client.post(
        f"/api/posts/{pid}/comments",
        json={"author": "clanker-a", "body": "nested reply", "parent_id": top},
        headers=_auth(client),
    )
    assert r.status_code == 200
    r = await client.get(f"/api/posts/{pid}", headers=_auth(client))
    detail = r.json()
    assert detail["body"] == "I can."
    assert [c["body"] for c in detail["comments"]] == ["top-level", "nested reply"]
    assert detail["comments"][1]["parent_id"] == top
    # close it
    r = await client.post(
        f"/api/posts/{pid}/close", json={"outcome": "b merges"}, headers=_auth(client)
    )
    assert r.json()["ok"] is True
    # comments on closed post -> 400
    r = await client.post(
        f"/api/posts/{pid}/comments",
        json={"author": "a", "body": "late"},
        headers=_auth(client),
    )
    assert r.status_code == 400
    # archive view shows it
    r = await client.get("/api/posts?include_closed=1", headers=_auth(client))
    assert any(p["status"] == "closed" and p["outcome"] == "b merges" for p in r.json())


@pytest.mark.anyio
async def test_comment_depth_capped_via_api(client):
    r = await client.post(
        "/api/posts",
        json={"title": "t", "body": "b", "author": "a"},
        headers=_auth(client),
    )
    pid = r.json()["id"]
    parent = None
    for _ in range(4):
        r = await client.post(
            f"/api/posts/{pid}/comments",
            json={"author": "a", "body": "x", "parent_id": parent},
            headers=_auth(client),
        )
        assert r.status_code == 200
        parent = r.json()["id"]
    r = await client.post(
        f"/api/posts/{pid}/comments",
        json={"author": "a", "body": "too deep", "parent_id": parent},
        headers=_auth(client),
    )
    assert r.status_code == 400
    assert "max comment depth" in r.json()["error"]


@pytest.mark.anyio
async def test_todos_flow(client):
    r = await client.post(
        "/api/todos",
        json={
            "title": "ship 0.3",
            "body": "all the things",
            "priority": "urgent",
            "tags": "release, ui",
            "assignee": "clanker-b",
            "author": "human",
        },
        headers=_auth(client),
    )
    tid = r.json()["id"]
    r = await client.get("/api/todos?status=open", headers=_auth(client))
    row = r.json()[0]
    assert row["priority"] == "urgent"
    assert row["tags"] == "release,ui"
    assert row["assignee"] == "clanker-b"
    # edit via PATCH
    r = await client.patch(
        f"/api/todos/{tid}",
        json={"priority": "low", "actor": "human"},
        headers=_auth(client),
    )
    assert r.json()["priority"] == "low"
    # done -> leaves open, appears in done and archive views
    await client.post(
        f"/api/todos/{tid}/done", json={"actor": "b"}, headers=_auth(client)
    )
    r_open = await client.get("/api/todos?status=open", headers=_auth(client))
    r_done = await client.get("/api/todos?status=done", headers=_auth(client))
    r_arch = await client.get("/api/todos?status=archive", headers=_auth(client))
    assert r_open.json() == []
    assert len(r_done.json()) == 1
    assert len(r_arch.json()) == 1
    # reopen -> archive empty again
    await client.post(f"/api/todos/{tid}/reopen", headers=_auth(client))
    r_arch = await client.get("/api/todos?status=archive", headers=_auth(client))
    assert r_arch.json() == []


@pytest.mark.anyio
async def test_bad_priority_maps_to_400(client):
    r = await client.post(
        "/api/todos",
        json={"title": "x", "author": "a", "priority": "meh"},
        headers=_auth(client),
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_notes_flow(client):
    r = await client.post(
        "/api/notes",
        json={
            "title": "deploy",
            "body": "- [ ] step one",
            "tags": "ops",
            "author": "human",
        },
        headers=_auth(client),
    )
    nid = r.json()["id"]
    r = await client.get(f"/api/notes/{nid}", headers=_auth(client))
    assert r.json()["body"] == "- [ ] step one"
    r = await client.put(
        f"/api/notes/{nid}",
        json={
            "title": "deploy",
            "body": "- [x] step one",
            "tags": "ops",
            "author": "human",
        },
        headers=_auth(client),
    )
    assert r.json()["body"] == "- [x] step one"
    r = await client.get("/api/notes", headers=_auth(client))
    assert len(r.json()) == 1
    r = await client.get("/api/notes/999", headers=_auth(client))
    assert r.status_code == 404


@pytest.mark.anyio
async def test_wiki_flow(client):
    r = await client.post(
        "/api/wiki",
        json={"title": "Runbook: Backups", "body": "see cron", "author": "human"},
        headers=_auth(client),
    )
    slug = r.json()["slug"]
    assert slug == "runbook-backups"
    r = await client.get(f"/api/wiki/{slug}", headers=_auth(client))
    assert r.json()["body"] == "see cron"
    r = await client.put(
        f"/api/wiki/{slug}",
        json={"title": "Runbook: Backups", "body": "updated", "author": "b"},
        headers=_auth(client),
    )
    assert r.json()["body"] == "updated"
    r = await client.get("/api/wiki/nope", headers=_auth(client))
    assert r.status_code == 404


@pytest.mark.anyio
async def test_chat_flow(client):
    await client.post(
        "/api/chat", json={"author": "a", "body": "hi"}, headers=_auth(client)
    )
    await client.post(
        "/api/chat", json={"author": "b", "body": "yo"}, headers=_auth(client)
    )
    r = await client.get("/api/chat", headers=_auth(client))
    assert [m["body"] for m in r.json()] == ["hi", "yo"]


@pytest.mark.anyio
async def test_events_endpoint(client):
    await client.post(
        "/api/posts",
        json={"title": "t", "body": "b", "author": "a"},
        headers=_auth(client),
    )
    r = await client.get("/api/events", headers=_auth(client))
    verbs = [e["verb"] for e in r.json()]
    assert "posted" in verbs


@pytest.mark.anyio
async def test_agents_profile_endpoints(client):
    r = await client.put(
        "/api/agents/clanker-a",
        json={"role": "greeter", "note": "bio", "contact": "oc/a"},
        headers=_auth(client),
    )
    assert r.status_code == 200
    r = await client.get("/api/agents/clanker-a", headers=_auth(client))
    assert r.json()["role"] == "greeter"
    r = await client.get("/api/agents", headers=_auth(client))
    assert any(a["name"] == "clanker-a" for a in r.json())
    r = await client.get("/api/agents/nobody", headers=_auth(client))
    assert r.status_code == 404


@pytest.mark.anyio
async def test_claims_flow(client):
    await client.post("/api/hello", json={"name": "clanker-a"}, headers=_auth(client))
    r = await client.post(
        "/api/claims",
        json={"agent": "clanker-a", "paths": ["/homeassistant/x.yaml"], "note": "edit"},
        headers=_auth(client),
    )
    assert r.json()["claims"] == 1
    r = await client.get(
        "/api/claims?path=/homeassistant/x.yaml&agent=clanker-b", headers=_auth(client)
    )
    assert len(r.json()) == 1
    r = await client.request(
        "DELETE",
        "/api/claims",
        json={"agent": "clanker-a", "paths": ["/homeassistant/x.yaml"]},
        headers=_auth(client),
    )
    assert r.json()["ok"] is True


@pytest.mark.anyio
async def test_check_endpoint(client):
    await client.post("/api/hello", json={"name": "clanker-a"}, headers=_auth(client))
    r = await client.post(
        "/api/posts",
        json={
            "title": "for b",
            "body": "x",
            "author": "clanker-a",
            "audience": "clanker-b",
        },
        headers=_auth(client),
    )
    pid = r.json()["id"]
    r = await client.get("/api/check?name=clanker-b&since=0", headers=_auth(client))
    assert [p["id"] for p in r.json()["posts"]] == [pid]


@pytest.mark.anyio
async def test_auth_missing_or_wrong_token(anon):
    r = await anon.get("/api/overview")
    assert r.status_code == 401
    r = await anon.get("/api/overview", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    r = await anon.get("/healthz")
    assert r.status_code == 200


@pytest.mark.anyio
async def test_bad_kind_maps_to_400(client):
    r = await client.post(
        "/api/posts",
        json={"title": "t", "body": "b", "author": "a", "kind": "rant"},
        headers=_auth(client),
    )
    assert r.status_code == 400
    assert "kind" in r.json()["error"]


@pytest.mark.anyio
async def test_overview_endpoint(client):
    await client.post("/api/hello", json={"name": "clanker-a"}, headers=_auth(client))
    r = await client.get("/api/overview", headers=_auth(client))
    body = r.json()
    assert body["counts"]["open_posts"] == 0
    assert any(p["slug"] == "general" for p in body["projects"])


@pytest.mark.anyio
async def test_overview_unread_counts(client):
    import time as _t

    seen = _t.time()
    await asyncio.sleep(0.02)
    await client.post(
        "/api/posts",
        json={"title": "t", "body": "b", "author": "a"},
        headers=_auth(client),
    )
    r = await client.get(f"/api/overview?seen={seen}", headers=_auth(client))
    assert r.json()["counts"]["unread_posts"] == 1
    r = await client.get(
        f"/api/overview?seen={time.time() + 60}", headers=_auth(client)
    )
    assert r.json()["counts"]["unread_posts"] == 0


@pytest.mark.anyio
async def test_oversized_body_rejected(client):
    r = await client.post(
        "/api/posts",
        json={"title": "t", "body": "x" * 1_100_000, "author": "a"},
        headers=_auth(client),
    )
    assert r.status_code == 413


@pytest.mark.anyio
async def test_todo_done_without_body(client):
    r = await client.post(
        "/api/todos", json={"title": "x", "author": "a"}, headers=_auth(client)
    )
    tid = r.json()["id"]
    r = await client.post(f"/api/todos/{tid}/done", headers=_auth(client))
    assert r.status_code == 200


@pytest.mark.anyio
async def test_index_serves_ui(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "SlopClanker" in r.text
    assert "#gate[hidden]{display:none}" in r.text
    assert 'id="tabbar"' in r.text
    assert 'id="autorefresh"' in r.text
    assert 'id="dlg-post"' in r.text
    assert 'id="dlg-todo"' in r.text
    assert 'id="dlg-note-edit"' in r.text
    assert 'id="dlg-page-edit"' in r.text
    assert 'id="dlg-profile"' in r.text
    assert "button[value=cancel], dialog button[value=close]" in r.text
    assert "MAX" not in r.text  # no leaked JS constants needed here


@pytest.mark.anyio
async def test_favicon_public(client):
    r = await client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
