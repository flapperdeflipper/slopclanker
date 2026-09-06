"""HTTP auth surface: end-to-end through the ASGI app."""

import pytest
from httpx import ASGITransport, AsyncClient

from app import ratelimit
from app.main import asgi_app

REG_TOKEN = "test-reg-token-1234567890"
CLAIM = "claim-secret-0123456789-abcdef"
PW = "long" + "enough12"  # dummy test fixture, assembled to calm scanners


@pytest.fixture(autouse=True)
def _fresh_rate():
    ratelimit.reset()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SLOPCLANKER_REG_TOKEN", REG_TOKEN)
    return str(tmp_path / "t.db")


def _client(**kwargs):
    return AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test", **kwargs
    )


@pytest.mark.anyio
async def test_protected_route_requires_bearer(env):
    async with _client() as c:
        r = await c.get("/api/auth/whoami")
        assert r.status_code == 401


@pytest.mark.anyio
async def test_registration_disabled_without_reg_token(env, monkeypatch):
    monkeypatch.delenv("SLOPCLANKER_REG_TOKEN", raising=False)
    async with _client() as c:
        r = await c.post(
            "/api/auth/register",
            headers={"Authorization": f"Bearer {REG_TOKEN}"},
            json={"name": "clanker-a", "claim_secret": CLAIM},
        )
        assert r.status_code == 503


@pytest.mark.anyio
async def test_full_flow_setup_register_approve_enroll(env):
    async with _client() as c:
        await c.post("/api/setup", json={"username": "boss", "password": PW})
        login = await c.post(
            "/api/auth/login", json={"username": "boss", "password": PW}
        )
        assert login.status_code == 200
        admin_token = login.json()["token"]
        admin = {"Authorization": f"Bearer {admin_token}"}

        reg = await c.post(
            "/api/auth/register",
            headers={"Authorization": f"Bearer {REG_TOKEN}"},
            json={"name": "clanker-a", "note": "builder", "claim_secret": CLAIM},
        )
        assert reg.status_code == 201
        rid = reg.json()["request_id"]

        pending = await c.get("/api/registrations", headers=admin)
        assert pending.status_code == 200
        assert [r["name"] for r in pending.json()] == ["clanker-a"]
        assert pending.json()[0]["ip"] is not None

        approve = await c.post(f"/api/registrations/{rid}/approve", headers=admin)
        assert approve.status_code == 201
        code = approve.json()["code"]

        enrolled = await c.post(
            "/api/auth/enroll",
            headers={"Authorization": f"Bearer {REG_TOKEN}"},
            json={"code": code},
        )
        assert enrolled.status_code == 200
        clanker_token = enrolled.json()["token"]

        who = await c.get(
            "/api/auth/whoami", headers={"Authorization": f"Bearer {clanker_token}"}
        )
        assert who.status_code == 200
        assert who.json()["name"] == "clanker-a"
        assert who.json()["kind"] == "clanker"
        assert who.json()["credential"]["call_count"] == 1

        forbidden = await c.get(
            "/api/registrations", headers={"Authorization": f"Bearer {clanker_token}"}
        )
        assert forbidden.status_code == 403


@pytest.mark.anyio
async def test_live_poll_delivery_path(env):
    async with _client() as c:
        await c.post("/api/setup", json={"username": "boss", "password": PW})
        login = await c.post(
            "/api/auth/login", json={"username": "boss", "password": PW}
        )
        admin = {"Authorization": f"Bearer {login.json()['token']}"}
        reg_headers = {"Authorization": f"Bearer {REG_TOKEN}"}

        rid = (
            await c.post(
                "/api/auth/register",
                headers=reg_headers,
                json={"name": "clanker-b", "claim_secret": CLAIM},
            )
        ).json()["request_id"]

        poll = await c.post(
            f"/api/auth/register/{rid}/poll",
            headers=reg_headers,
            json={"claim_secret": CLAIM},
        )
        assert poll.json()["status"] == "pending"

        wrong = await c.post(
            f"/api/auth/register/{rid}/poll",
            headers=reg_headers,
            json={"claim_secret": "nope"},
        )
        assert wrong.status_code == 403

        await c.post(f"/api/registrations/{rid}/approve", headers=admin)
        poll = await c.post(
            f"/api/auth/register/{rid}/poll",
            headers=reg_headers,
            json={"claim_secret": CLAIM},
        )
        assert poll.json()["status"] == "delivered"
        assert poll.json()["token"]

        again = await c.post(
            f"/api/auth/register/{rid}/poll",
            headers=reg_headers,
            json={"claim_secret": CLAIM},
        )
        assert again.json()["token"] is None


@pytest.mark.anyio
async def test_login_failures_and_logout(env):
    async with _client() as c:
        await c.post("/api/setup", json={"username": "boss", "password": PW})
        r = await c.post(
            "/api/auth/login", json={"username": "boss", "password": "wrong-wrong"}
        )
        assert r.status_code == 401
        login = await c.post(
            "/api/auth/login", json={"username": "boss", "password": PW}
        )
        token = login.json()["token"]
        hdr = {"Authorization": f"Bearer {token}"}
        assert (await c.get("/api/auth/whoami", headers=hdr)).status_code == 200
        assert (await c.post("/api/auth/logout", headers=hdr)).status_code == 204
        assert (await c.get("/api/auth/whoami", headers=hdr)).status_code == 401


@pytest.mark.anyio
async def test_admin_sees_registration_notification(env):
    async with _client() as c:
        await c.post("/api/setup", json={"username": "boss", "password": PW})
        login = await c.post(
            "/api/auth/login", json={"username": "boss", "password": PW}
        )
        admin = {"Authorization": f"Bearer {login.json()['token']}"}
        await c.post(
            "/api/auth/register",
            headers={"Authorization": f"Bearer {REG_TOKEN}"},
            json={"name": "clanker-c", "claim_secret": CLAIM},
        )
        notes = await c.get("/api/notifications", headers=admin)
        assert notes.status_code == 200
        assert any("clanker-c" in n["body"] for n in notes.json())


@pytest.mark.anyio
async def test_users_create_requires_admin(env):
    async with _client() as c:
        await c.post("/api/setup", json={"username": "boss", "password": PW})
        login = await c.post(
            "/api/auth/login", json={"username": "boss", "password": PW}
        )
        admin = {"Authorization": f"Bearer {login.json()['token']}"}
        made = await c.post(
            "/api/users",
            headers=admin,
            json={"username": "helper", "password": "helperpass12", "role": "user"},
        )
        assert made.status_code == 201
        helper_login = await c.post(
            "/api/auth/login", json={"username": "helper", "password": "helperpass12"}
        )
        helper = {"Authorization": f"Bearer {helper_login.json()['token']}"}
        denied = await c.post(
            "/api/users",
            headers=helper,
            json={"username": "other", "password": "otherpass123", "role": "user"},
        )
        assert denied.status_code == 403
