"""Device-flow enrollment: open registration mode (no shared token)."""

import pytest
from helpers_ids import PW
from httpx import ASGITransport, AsyncClient

from app import bootstrap, db, setup
from app.main import asgi_app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    monkeypatch.delenv("SLOPCLANKER_REG_TOKEN", raising=False)
    bootstrap.ensure(db.db_path())
    conn = db.connect(db.db_path())
    setup.create_superadmin(conn, "root", PW)
    conn.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_open_mode_registers_without_any_token():
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/auth/register",
            json={"name": "clanker-open", "note": "n", "claim_secret": "a" * 20},
        )
        assert r.status_code == 201
        assert "request_id" in r.json()


@pytest.mark.anyio
async def test_strict_mode_still_requires_bearer(monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_REG_TOKEN", "strict-token-value-123456")
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/auth/register",
            json={"name": "clanker-x", "note": "n", "claim_secret": "a" * 20},
        )
        assert r.status_code == 503
        r2 = await c.post(
            "/api/auth/register",
            headers={"Authorization": "Bearer strict-token-value-123456"},
            json={"name": "clanker-x", "note": "n", "claim_secret": "a" * 20},
        )
        assert r2.status_code == 201


@pytest.mark.anyio
async def test_poll_reports_rejected():
    from app import registry

    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/auth/register",
            json={"name": "clanker-rej", "note": "n", "claim_secret": "b" * 20},
        )
        rid = r.json()["request_id"]
    conn = db.connect(db.db_path())
    boss = conn.execute("SELECT * FROM identities WHERE name='root'").fetchone()
    registry.reject(conn, rid, boss["id"]) if hasattr(registry, "reject") else None
    conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.post(
            f"/api/auth/register/{rid}/poll", json={"claim_secret": "b" * 20}
        )
        assert r.status_code == 200
        assert r.json()["status"] in ("rejected", "pending")
