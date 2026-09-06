"""Phase 7 UI smoke: the shell is served and its display-name directory works."""

import os

import pytest
from helpers_ids import PW
from httpx import ASGITransport, AsyncClient

from app import auth, bootstrap, db, setup
from app.main import asgi_app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SLOPCLANKER_REG_TOKEN", "test-reg-token-1234567890")
    bootstrap.ensure(db.db_path())
    conn = db.connect(db.db_path())
    boss = setup.create_superadmin(conn, "root", PW)
    cur = conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('agent-one', 'clanker', 'active', 1.0)"
    )
    conn.commit()
    tok = auth.mint_agent_token(conn, cur.lastrowid, boss["id"])
    conn.close()
    return {"token": tok, "headers": {"Authorization": f"Bearer {tok}"}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_index_serves_shell():
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "SlopClanker" in r.text
        assert "/api/stream" in r.text  # SSE wiring is in the shell


@pytest.mark.anyio
async def test_directory_requires_auth():
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get("/api/identities/directory")
        assert r.status_code == 401


@pytest.mark.anyio
async def test_directory_lists_active_names_only(_env):
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get("/api/identities/directory", headers=_env["headers"])
        assert r.status_code == 200
        names = {row["name"] for row in r.json()}
        assert "root" in names and "agent-one" in names
        # no credential/contact material leaks
        assert set(r.json()[0].keys()) == {"id", "name", "kind", "status"}


def test_ui_file_has_no_inline_handlers():
    """Escape-by-default rule: no javascript: or raw onclick attrs in shell."""
    path = os.path.join(os.path.dirname(__file__), "..", "app", "static", "index.html")
    with open(path) as fh:
        src = fh.read()
    assert "javascript:" not in src
    assert "onclick=" not in src and "onerror=" not in src
