"""HTTP e2e: SSE stream, wait endpoint, inbox, MCP over /mcp.

SSE and MCP responses are long-lived streams — read incrementally and
close; awaiting a full body would hang.
"""

import asyncio
import json

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
    agent_id = cur.lastrowid
    conn.close()
    return agent_id, tok


class _Done(Exception):
    pass


async def _raw_sse(tok, query, stop_on, actor_task=None):
    """Drive the ASGI app directly; collect frames until stop_on matches."""
    import urllib.parse

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "netloc": "test",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "query_string": urllib.parse.urlencode(query).encode(),
        "headers": [
            (b"authorization", f"Bearer {tok}".encode()),
            (b"accept", b"text/event-stream"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    frames = []
    req_sent = False
    disconnected = asyncio.Event()

    async def receive():
        nonlocal req_sent
        if not req_sent:
            req_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.body" and msg.get("body"):
            text = msg["body"].decode()
            for line in text.splitlines():
                if line.startswith("data:"):
                    frames.append(json.loads(line[5:]))
            if any(stop_on in json.dumps(f) for f in [frames[-1]] if frames) or (
                frames and stop_on in text
            ):
                disconnected.set()

    task = asyncio.create_task(asgi_app(scope, receive, send))
    try:
        if actor_task:
            await asyncio.sleep(0.2)
            await actor_task()
        await asyncio.wait_for(task, 8.0)
    except TimeoutError:
        pass
    if not task.done():
        disconnected.set()
        task.cancel()
    return frames


async def test_sse_backlog_and_live(tmp_path):
    _agent_id, tok = _seed()
    h = {"Authorization": f"Bearer {tok}"}
    transport = ASGITransport(app=asgi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        pid = (await c.post("/api/projects", headers=h, json={"name": "proj"})).json()[
            "id"
        ]

        async def poke():
            await c.post(
                "/api/tasks", headers=h, json={"project_id": pid, "title": "live one"}
            )

        frames = await _raw_sse(tok, {"project": pid}, "task.created", poke)
        verbs = {f.get("verb") for f in frames}
        assert "task.created" in verbs


async def test_sse_since_backlog_only(tmp_path):
    _agent_id, tok = _seed()
    h = {"Authorization": f"Bearer {tok}"}
    transport = ASGITransport(app=asgi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        pid = (await c.post("/api/projects", headers=h, json={"name": "proj"})).json()[
            "id"
        ]
        await c.post("/api/tasks", headers=h, json={"project_id": pid, "title": "old"})
        evs = (await c.get("/api/events", headers=h)).json()
        since = evs[0]["id"] - 1
        frames = await _raw_sse(tok, {"since": since}, "task.created")
        assert any(f.get("verb") == "task.created" for f in frames)
        assert all(f["id"] > since - 1 for f in frames)


async def test_wait_and_inbox_endpoints(tmp_path):
    _agent_id, tok = _seed()
    h = {"Authorization": f"Bearer {tok}"}
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        me = (await c.get("/api/auth/whoami", headers=h)).json()
        pid = (await c.post("/api/projects", headers=h, json={"name": "proj"})).json()[
            "id"
        ]
        empty = await c.get("/api/wait", headers=h, params={"to_me": 1, "timeout": 0.2})
        assert empty.json() == {"events": []}
        await c.post(
            "/api/questions",
            headers=h,
            json={"project_id": pid, "body": "for me", "to_identity_id": me["id"]},
        )
        inbox = await c.get("/api/inbox", headers=h)
        assert len(inbox.json()) == 1
        w = await c.get("/api/wait", headers=h, params={"to_me": 1, "timeout": 0.2})
        assert len(w.json()["events"]) == 1
        assert (await c.get("/api/inbox", headers=h)).json() == []


class _McpSession:
    def __init__(self, client, token):
        self.c = client
        self.h = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        self.session = None
        self.i = 0

    async def call(self, method, params=None):
        self.i += 1
        headers = dict(self.h)
        if self.session:
            headers["mcp-session-id"] = self.session
        body = {
            "jsonrpc": "2.0",
            "id": self.i,
            "method": method,
            "params": params or {},
        }
        async with self.c.stream("POST", "/mcp", headers=headers, json=body) as r:
            if self.session is None and "mcp-session-id" in r.headers:
                self.session = r.headers["mcp-session-id"]
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    return r.status_code, json.loads(line[5:])
                if line.startswith("{"):
                    return r.status_code, json.loads(line)
            return r.status_code, {}


async def test_mcp_tools_over_http(tmp_path):
    _agent_id, tok = _seed()
    async with asgi_app.router.lifespan_context(asgi_app):
        await _mcp_roundtrip(tok)


async def _mcp_roundtrip(tok):
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test", timeout=15.0
    ) as c:
        h = {"Authorization": f"Bearer {tok}"}
        pid = (await c.post("/api/projects", headers=h, json={"name": "proj"})).json()[
            "id"
        ]
        s = _McpSession(c, tok)
        code, init = await s.call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        )
        assert code == 200, init
        code, hello = await s.call(
            "tools/call", {"name": "hello", "arguments": {"note": "hi"}}
        )
        assert code == 200, hello
        assert "agent-one" in json.dumps(hello)
        code, made = await s.call(
            "tools/call",
            {
                "name": "task_create",
                "arguments": {"project_id": pid, "title": "from mcp"},
            },
        )
        assert code == 200, made
        code, got = await s.call(
            "tools/call", {"name": "task_get", "arguments": {"task_id": 1}}
        )
        assert "from mcp" in json.dumps(got)
        code, found = await s.call(
            "tools/call", {"name": "search", "arguments": {"query": "from mcp"}}
        )
        assert "from mcp" in json.dumps(found)
        code, _claimed = await s.call(
            "tools/call",
            {
                "name": "claims_set",
                "arguments": {"paths": ["/src/x.py"], "note": "mine"},
            },
        )
        assert code == 200
        code, checked = await s.call(
            "tools/call", {"name": "claims_check", "arguments": {"path": "/src/x.py"}}
        )
        assert "claims" in json.dumps(checked)
