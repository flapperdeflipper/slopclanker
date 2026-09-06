"""Realtime layer: SSE stream with replay, long-poll wait, chat-on-the-bus.

Runs against a real uvicorn server on an ephemeral port: httpx's
ASGITransport buffers response bodies, which an endless SSE generator
would hang forever.
"""

import asyncio
import json
import socket
import threading

import pytest
import uvicorn
from httpx import AsyncClient

TOKEN = "test-token"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "rt.db"))
    monkeypatch.setenv("SLOPCLANKER_TOKEN", TOKEN)
    from app.main import asgi_app

    port = _free_port()
    config = uvicorn.Config(
        asgi_app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    for _ in range(100):
        if instance.started:
            break
        await asyncio.sleep(0.05)
    assert instance.started, "server did not start"
    yield f"http://127.0.0.1:{port}"
    instance.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
async def client(server):
    async with AsyncClient(base_url=server) as c:
        yield c


def _auth() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


async def _next_data(aiter) -> dict:
    """Read SSE lines until one data: payload; return it parsed."""
    while True:
        line = await asyncio.wait_for(aiter.__anext__(), 5)
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])


@pytest.mark.anyio
async def test_stream_sends_chat_live(client):
    opened = await client.post(
        "/api/posts", json={"title": "t", "body": "b", "author": "a"}, headers=_auth()
    )
    assert opened.status_code == 200

    async with client.stream(
        "GET", "/api/stream?types=chat&name=listener", headers=_auth()
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        aiter = resp.aiter_lines()
        hello = await asyncio.wait_for(aiter.__anext__(), 5)
        assert hello.startswith(":")

        said = await client.post(
            "/api/chat",
            json={"author": "speaker", "body": "ping over the wire"},
            headers=_auth(),
        )
        assert said.status_code == 200

        event = await _next_data(aiter)
        assert event["type"] == "chat"
        assert event["author"] == "speaker"
        assert event["body"] == "ping over the wire"
        assert isinstance(event["id"], int)


@pytest.mark.anyio
async def test_stream_replays_missed_events(client):
    first = await client.post(
        "/api/posts", json={"title": "one", "body": "b", "author": "a"}, headers=_auth()
    )
    second = await client.post(
        "/api/posts", json={"title": "two", "body": "b", "author": "a"}, headers=_auth()
    )
    feed = await client.get("/api/events", headers=_auth())
    by_obj = {row["obj_id"]: row for row in feed.json()}
    first_id = by_obj[str(first.json()["id"])]["id"]
    second_id = by_obj[str(second.json()["id"])]["id"]
    assert second_id > first_id

    async with client.stream(
        "GET", f"/api/stream?types=event&since_id={first_id}", headers=_auth()
    ) as resp:
        aiter = resp.aiter_lines()
        opened = await asyncio.wait_for(aiter.__anext__(), 5)
        assert opened.startswith(":")
        event = await _next_data(aiter)
        assert event["type"] == "event"
        assert event["id"] == second_id
        assert event["verb"]


@pytest.mark.anyio
async def test_wait_returns_when_comment_lands(client):
    created = await client.post(
        "/api/posts",
        json={"title": "q", "body": "anyone?", "author": "asker"},
        headers=_auth(),
    )
    post_id = created.json()["id"]

    result: dict = {}

    async def waiter():
        r = await client.get(f"/api/posts/{post_id}/wait?timeout=10", headers=_auth())
        result["status"] = r.status_code
        result["json"] = r.json()

    task = asyncio.create_task(waiter())
    await asyncio.sleep(1.0)
    replied = await client.post(
        f"/api/posts/{post_id}/comments",
        json={"author": "answerer", "body": "here"},
        headers=_auth(),
    )
    assert replied.status_code == 200
    await asyncio.wait_for(task, 10)

    assert result["status"] == 200
    assert result["json"]["comments"] == 1
    assert result["json"]["status"] == "open"


@pytest.mark.anyio
async def test_wait_times_out_with_204(client):
    created = await client.post(
        "/api/posts",
        json={"title": "quiet", "body": "...", "author": "asker"},
        headers=_auth(),
    )
    r = await client.get(
        f"/api/posts/{created.json()['id']}/wait?timeout=1", headers=_auth()
    )
    assert r.status_code == 204
    assert r.headers["x-slopclanker-timeout"] == "1"


@pytest.mark.anyio
async def test_wait_404_on_missing_post(client):
    r = await client.get("/api/posts/999/wait", headers=_auth())
    assert r.status_code == 404


@pytest.mark.anyio
async def test_stream_self_filter(client):
    created = await client.post(
        "/api/posts", json={"title": "t", "body": "b", "author": "a"}, headers=_auth()
    )
    async with client.stream(
        "GET", "/api/stream?types=event&name=a", headers=_auth()
    ) as resp:
        aiter = resp.aiter_lines()
        await asyncio.wait_for(aiter.__anext__(), 5)
        commented = await client.post(
            f"/api/posts/{created.json()['id']}/comments",
            json={"author": "b", "body": "reply"},
            headers=_auth(),
        )
        assert commented.status_code == 200
        event = await _next_data(aiter)
        assert event["actor"] == "b"


@pytest.mark.anyio
async def test_bus_publish_from_worker_thread():
    """Sync MCP tools run off-loop; publish must still reach subscribers."""
    from app.bus import Bus

    local = Bus()
    queue, _ = local.subscribe()
    await asyncio.to_thread(local.publish, {"type": "chat", "body": "hi"})
    got = await asyncio.wait_for(queue.get(), timeout=2)
    assert got["body"] == "hi"


@pytest.mark.anyio
async def test_bus_rebinds_when_loop_dies():
    """A stale bound loop (tests, restarts) must not swallow publishes."""
    from app.bus import Bus

    local = Bus()
    stale = asyncio.new_event_loop()

    async def bind_on_stale() -> None:
        local.subscribe()

    binder = threading.Thread(target=lambda: stale.run_until_complete(bind_on_stale()))
    binder.start()
    binder.join(timeout=5)
    stale.close()
    assert local._loop is stale

    queue, _ = local.subscribe()  # rebinds to the running (test) loop
    assert local._loop is not stale
    local.publish({"type": "chat"})
    got = await asyncio.wait_for(queue.get(), timeout=2)
    assert got["type"] == "chat"
