"""MCP tools over the in-memory fastmcp Client."""

import pytest
from fastmcp import Client

from app import store
from app.db import connect
from app.main import mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "tools.db"))
    monkeypatch.delenv("SLOPCLANKER_TOKEN", raising=False)
    return Client(mcp)


@pytest.mark.anyio
async def test_hello_snapshot_via_tool(client):
    async with client:
        result = await client.call_tool(
            "hello", {"name": "clanker-a", "note": "working on x"}
        )
        snap = result.data
        assert snap["me"] == "clanker-a"
        assert any(a["name"] == "clanker-a" and a["active"] for a in snap["agents"])


@pytest.mark.anyio
async def test_profile_tools(client):
    async with client:
        r = await client.call_tool(
            "profile_set", {"name": "clanker-a", "role": "greeter", "contact": "oc/a"}
        )
        assert r.data["role"] == "greeter"
        r = await client.call_tool("profile_get", {"name": "clanker-a"})
        assert r.data["agent"]["contact"] == "oc/a"
        r = await client.call_tool("profile_get", {"name": "nobody"})
        assert r.data["agent"] is None


@pytest.mark.anyio
async def test_post_comment_close_and_check(client):
    async with client:
        await client.call_tool("hello", {"name": "clanker-a"})
        await client.call_tool("hello", {"name": "clanker-b"})
        r = await client.call_tool(
            "post",
            {
                "author": "clanker-a",
                "title": "who merges?",
                "body": "I can.",
                "kind": "proposal",
                "audience": "clanker-b",
            },
        )
        pid = r.data["post_id"]

        r = await client.call_tool(
            "post", {"author": "clanker-b", "body": "go ahead", "post_id": pid}
        )
        top = r.data["id"]
        r = await client.call_tool(
            "post",
            {"author": "clanker-a", "body": "nested", "post_id": pid, "parent_id": top},
        )
        assert r.data["parent_id"] == top

        r = await client.call_tool("check", {"name": "clanker-b", "since": 0})
        assert [p["id"] for p in r.data["posts"]] == [pid]
        # only OTHERS' comments show up: clanker-a's nested reply, not b's own
        assert [c["body"] for c in r.data["comments"]] == ["nested"]

        r = await client.call_tool("close", {"post_id": pid, "outcome": "b merges"})
        assert r.data["ok"] is True


@pytest.mark.anyio
async def test_post_project_resolution(client, tmp_path, monkeypatch):
    async with client:
        r = await client.call_tool(
            "post",
            {"author": "a", "title": "in proj", "body": "b", "project": "general"},
        )
        pid = r.data["post_id"]
        conn = connect(tmp_path / "tools.db")
        assert store.post_detail(conn, pid)["project_id"] == 1


@pytest.mark.anyio
async def test_todos_tools(client):
    async with client:
        r = await client.call_tool(
            "todos_add",
            {
                "author": "a",
                "title": "ship",
                "priority": "high",
                "tags": ["ui", "api"],
                "assignee": "b",
            },
        )
        tid = r.data["id"]
        r = await client.call_tool("todos_list", {"name": "b"})
        assert r.data["todos"][0]["priority"] == "high"
        await client.call_tool("todos_done", {"todo_id": tid})
        r = await client.call_tool("todos_list", {"status": "archive"})
        assert len(r.data["todos"]) == 1


@pytest.mark.anyio
async def test_notes_and_wiki_tools(client):
    async with client:
        r = await client.call_tool(
            "notes_save", {"author": "a", "title": "plan", "body": "- [ ] x"}
        )
        nid = r.data["id"]
        await client.call_tool(
            "notes_save",
            {"author": "a", "title": "plan v2", "body": "- [x] x", "note_id": nid},
        )
        r = await client.call_tool("notes_list", {})
        assert r.data["notes"][0]["title"] == "plan v2"

        r = await client.call_tool(
            "wiki_save", {"author": "a", "title": "Conventions", "body": "v1"}
        )
        assert r.data["slug"] == "conventions"
        await client.call_tool(
            "wiki_save", {"author": "b", "title": "Conventions", "body": "v2"}
        )
        r = await client.call_tool("wiki_get", {"slug": "conventions"})
        assert r.data["page"]["body"] == "v2"


@pytest.mark.anyio
async def test_chat_and_events_tools(client):
    async with client:
        await client.call_tool("chat_say", {"author": "a", "body": "hi"})
        r = await client.call_tool("chat_read", {})
        assert [m["body"] for m in r.data["messages"]] == ["hi"]
        await client.call_tool("post", {"author": "a", "title": "ev", "body": "b"})
        r = await client.call_tool("events", {"limit": 50})
        assert any(e["verb"] == "posted" for e in r.data["events"])


@pytest.mark.anyio
async def test_claims_tools(client):
    async with client:
        await client.call_tool("hello", {"name": "clanker-a"})
        r = await client.call_tool(
            "claims_set",
            {"agent": "clanker-a", "paths": ["/config/x.yaml"], "note": "editing"},
        )
        assert r.data["claims"] == 1
        r = await client.call_tool(
            "claims_check", {"path": "/config/x.yaml", "agent": "clanker-b"}
        )
        assert len(r.data["claims"]) == 1
        r = await client.call_tool(
            "claims_release", {"agent": "clanker-a", "paths": ["/config/x.yaml"]}
        )
        assert r.data["ok"] is True
