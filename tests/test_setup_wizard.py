"""Setup wizard: service guards + HTTP surface."""

import pytest
from helpers_ids import PW
from httpx import ASGITransport, AsyncClient

from app import db, setup
from app.main import asgi_app


@pytest.fixture(autouse=True)
def _fresh_rate():
    from app import ratelimit

    ratelimit.reset()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


def test_create_superadmin_happy_path(tmp_path):
    conn = _conn(tmp_path)
    row = setup.create_superadmin(conn, "boss", PW)
    assert row["kind"] == "human"
    assert row["role"] == "superadmin"
    assert row["status"] == "active"
    cred = conn.execute(
        "SELECT secret_hash FROM credentials WHERE kind='password'"
    ).fetchone()
    assert setup.verify_password(cred["secret_hash"], PW)
    assert not setup.verify_password(cred["secret_hash"], "wrong password")
    assert not setup.setup_required(conn)
    conn.close()


def test_second_setup_blocked(tmp_path):
    conn = _conn(tmp_path)
    setup.create_superadmin(conn, "boss", PW)
    with pytest.raises(setup.SetupComplete):
        setup.create_superadmin(conn, "boss2", "anotherlongone")
    conn.close()


def test_name_and_password_rules(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(setup.InvalidName):
        setup.create_superadmin(conn, "Boss!", PW)
    with pytest.raises(setup.InvalidName):
        setup.create_superadmin(conn, "x", PW)
    with pytest.raises(setup.WeakPassword):
        setup.create_superadmin(conn, "boss", "short")
    with pytest.raises(setup.WeakPassword):
        setup.create_superadmin(conn, "boss", 12345678901)
    conn.close()


@pytest.mark.anyio
async def test_wizard_http_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as client:
        status = await client.get("/api/setup")
        assert status.status_code == 200
        assert status.json()["setup_required"] is True

        created = await client.post(
            "/api/setup", json={"username": "boss", "password": PW}
        )
        assert created.status_code == 201
        assert created.json() == {"ok": True, "username": "boss"}

        status = await client.get("/api/setup")
        assert status.json()["setup_required"] is False

        again = await client.post(
            "/api/setup", json={"username": "other", "password": PW}
        )
        assert again.status_code == 409


@pytest.mark.anyio
async def test_wizard_rejects_bad_input(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as client:
        r = await client.post("/api/setup", json={"username": "boss"})
        assert r.status_code == 422
        r = await client.post(
            "/api/setup", json={"username": "boss", "password": "short"}
        )
        assert r.status_code == 422
        r = await client.post(
            "/api/setup", json={"username": "Bad Name", "password": PW}
        )
        assert r.status_code == 422
        r = await client.post(
            "/api/setup",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422


@pytest.mark.anyio
async def test_wizard_rate_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as client:
        codes = []
        for _ in range(12):
            r = await client.post(
                "/api/setup", json={"username": "boss", "password": "short"}
            )
            codes.append(r.status_code)
        assert codes[-1] == 429
        assert 429 in codes


@pytest.mark.anyio
async def test_wizard_works_behind_ingress_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    prefix = "/api/hassio_ingress/abc123"
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app),
        base_url="http://test",
        headers={"X-Ingress-Path": prefix},
    ) as client:
        status = await client.get(f"{prefix}/api/setup")
        assert status.status_code == 200
        assert status.json()["setup_required"] is True
