"""DESIGN §10 CI proof: fuzz corpora, permission negatives, spoofing."""

import pytest
from helpers_ids import PW, clanker, fresh_db, human, superadmin
from httpx import ASGITransport, AsyncClient

from app import auth, bootstrap, db, objects
from app.main import asgi_app

SQLI = [
    "x' OR '1'='1",
    "x'; DROP TABLE tasks;--",
    'x" UNION SELECT secret_hash FROM credentials--',
    "x' OR 1=1;--",
    "x')) OR (('a' = 'a",
]
XSS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    '"><svg onload=alert(1)>',
    "javascript:alert(1)",
]


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SLOPCLANKER_REG_TOKEN", "test-reg-token-1234567890")
    from app import ratelimit

    ratelimit.reset()
    bootstrap.ensure(db.db_path())


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _seeded(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    user = human(conn, "theuser", "user", boss)
    pid = objects.create_project(conn, agent, "proj")
    return conn, boss, agent, user, pid


@pytest.mark.anyio
async def test_sqli_corpus_inert(tmp_path):
    conn, boss, agent, user, pid = _seeded(tmp_path)
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    h = {"Authorization": f"Bearer {atok}"}
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        for i, payload in enumerate(SQLI):
            r = await c.post(
                "/api/tasks",
                headers=h,
                json={
                    "project_id": 1,
                    "title": f"t{i} {payload}",
                    "body": payload,
                    "tags": payload,
                },
            )
            assert r.status_code == 201, (payload, r.text)
        r = await c.get("/api/tasks", headers=h)
        body = r.text
        assert '"secret_hash":' not in body
        import json as _json

        for payload in SQLI:
            escaped = _json.dumps(payload)[1:-1]
            assert escaped in body  # stored verbatim, never executed
        # no tautology explosion: exactly the 5 we created
        assert len(r.json()) == 5


@pytest.mark.anyio
async def test_xss_corpus_stored_verbatim_json_only(tmp_path):
    conn, boss, agent, user, pid = _seeded(tmp_path)
    tid = objects.create_task(conn, agent, pid, "xss")
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    h = {"Authorization": f"Bearer {atok}"}
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        for payload in XSS:
            r = await c.patch(f"/api/tasks/{tid}", headers=h, json={"body": payload})
            assert "text/html" not in r.headers.get("content-type", "")
            assert payload in r.text  # stored verbatim, data never markup


@pytest.mark.anyio
async def test_traversal_corpus_404(tmp_path):
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        for path in (
            "/../etc/passwd",
            "/../../etc/passwd",
            "/static/../../secrets.yaml",
            "/%2e%2e/%2e%2e/etc/passwd",
        ):
            r = await c.get(path)
            assert r.status_code in (401, 404), (path, r.status_code)
            assert "root:" not in r.text


@pytest.mark.anyio
async def test_mass_assignment_probes(tmp_path):
    conn, boss, agent, user, pid = _seeded(tmp_path)
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    h = {"Authorization": f"Bearer {atok}"}
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/tasks",
            headers=h,
            json={
                "project_id": pid,
                "title": "ma",
                "state": "done",
                "proof_waived": True,
                "version": 99,
                "created_by": user["id"],
                "assignee_id": user["id"],
                "done_by": user["id"],
            },
        )
        assert r.status_code == 201
        r = await c.get(f"/api/tasks/{r.json()['id']}", headers=h)
        row = r.json()["task"]
        assert row["state"] == "idea" and row["proof_waived"] == 0
        assert row["created_by"] == agent["id"] and row["version"] == 1


@pytest.mark.anyio
async def test_clanker_cannot_approve_done_trash_waive(tmp_path):
    conn, boss, agent, user, pid = _seeded(tmp_path)
    tid = objects.create_task(conn, agent, pid, "neg")
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    h = {"Authorization": f"Bearer {atok}"}
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        for to in ("trashed",):
            r = await c.post(f"/api/tasks/{tid}/transition", headers=h, json={"to": to})
            assert r.status_code == 403
        await c.post(f"/api/tasks/{tid}/transition", headers=h, json={"to": "plan"})
        await c.post(f"/api/tasks/{tid}/transition", headers=h, json={"to": "proposed"})
        r = await c.post(
            f"/api/tasks/{tid}/transition", headers=h, json={"to": "approved"}
        )
        assert r.status_code == 403
        r = await c.patch(f"/api/tasks/{tid}", headers=h, json={"proof_waived": True})
        assert r.status_code == 422  # human-only waiver refused
        conn = db.connect(db.db_path())
        assert (
            conn.execute(
                "SELECT proof_waived FROM tasks WHERE id = ?", (tid,)
            ).fetchone()[0]
            == 0
        )
        conn.close()


@pytest.mark.anyio
async def test_revoked_identity_is_instant_401(tmp_path):
    conn, boss, agent, user, pid = _seeded(tmp_path)
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    auth.revoke_identity(conn, agent["id"])
    conn.commit()
    conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get("/api/tasks", headers={"Authorization": f"Bearer {atok}"})
        assert r.status_code == 401


@pytest.mark.anyio
async def test_reenroll_rotates_old_token_out(tmp_path):
    conn, boss, agent, user, pid = _seeded(tmp_path)
    old = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get("/api/tasks", headers={"Authorization": f"Bearer {old}"})
        assert r.status_code == 200
        conn = db.connect(db.db_path())
        auth.mint_agent_token(conn, agent["id"], boss["id"])  # re-mint rotates
        conn.close()
        r = await c.get("/api/tasks", headers={"Authorization": f"Bearer {old}"})
        assert r.status_code == 401


@pytest.mark.anyio
async def test_registration_token_scope(tmp_path):
    conn = fresh_db(tmp_path)
    superadmin(conn)
    conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        for hdrs, expect in (
            ({"Authorization": "Bearer wrong-token-entirely"}, 503),
            ({}, 503),
            ({"Authorization": "Bearer test-reg-token-1234567890"}, 201),
        ):
            r = await c.post(
                "/api/auth/register",
                headers=hdrs,
                json={
                    "name": "wannabe",
                    "note": "n",
                    "claim_secret": "claim-secret-0123456789",
                },
            )
            assert r.status_code == expect, (hdrs, r.status_code)
        conn = db.connect(db.db_path())
        n = conn.execute(
            "SELECT COUNT(*) FROM registrations WHERE name = 'wannabe'"
        ).fetchone()[0]
        conn.close()
        assert n == 1  # only the correctly-tokened attempt registered


@pytest.mark.anyio
async def test_xff_spoof_ignored_unless_trusted(tmp_path, monkeypatch):
    monkeypatch.delenv("SLOPCLANKER_TRUSTED_PROXY", raising=False)
    conn = fresh_db(tmp_path)
    superadmin(conn)
    conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/auth/register",
            headers={
                "X-Forwarded-For": "6.6.6.6",
                "Authorization": "Bearer test-reg-token-1234567890",
            },
            json={
                "name": "spoofer",
                "note": "n",
                "claim_secret": "claim-secret-0123456789",
            },
        )
        assert r.status_code == 201
        conn = db.connect(db.db_path())
        ip = conn.execute(
            "SELECT ip FROM registrations WHERE name = 'spoofer'"
        ).fetchone()[0]
        conn.close()
        assert ip != "6.6.6.6"  # socket peer wins; forged XFF discarded


@pytest.mark.anyio
async def test_ingress_prefix_replay(tmp_path):
    conn, boss, agent, user, pid = _seeded(tmp_path)
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    prefix = "/api/hassio_ingress/abc123"
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get(
            f"{prefix}/api/tasks",
            headers={"Authorization": f"Bearer {atok}", "X-Ingress-Path": prefix},
        )
        assert r.status_code == 200
        r = await c.get(
            f"{prefix}/api/projects", headers={"Authorization": f"Bearer {atok}"}
        )  # prefix without header
        assert r.status_code == 404  # header is the routing contract


@pytest.mark.anyio
async def test_per_identity_rate_limit(tmp_path):
    from app import middleware as mw

    conn, boss, agent, user, pid = _seeded(tmp_path)
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    monkey_limits = (5, 300)
    orig = mw.API_RATE
    mw.API_RATE = monkey_limits
    try:
        async with AsyncClient(
            transport=ASGITransport(app=asgi_app), base_url="http://test"
        ) as c:
            codes = []
            for _ in range(7):
                r = await c.get(
                    "/api/tasks", headers={"Authorization": f"Bearer {atok}"}
                )
                codes.append(r.status_code)
            assert codes[:5] == [200] * 5
            assert codes[5:] == [429, 429]
    finally:
        mw.API_RATE = orig


@pytest.mark.anyio
async def test_admin_export_requires_admin(tmp_path):
    conn, boss, agent, user, pid = _seeded(tmp_path)
    atok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    conn.close()
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get(
            "/api/admin/export", headers={"Authorization": f"Bearer {atok}"}
        )
        assert r.status_code == 403
        # superadmin via ui login
        r = await c.post("/api/auth/login", json={"username": "root", "password": PW})
        tok = r.json()["token"]
        r = await c.get("/api/admin/export", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        data = r.json()
        assert "transitions" in data["tables"] and "events" in data["tables"]
        blob = str(data)
        assert PW not in blob  # no plaintext secrets ever
