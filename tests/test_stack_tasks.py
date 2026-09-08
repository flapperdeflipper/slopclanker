"""GET /api/stacks/{sid}/tasks — the stack-wide board feed."""

import pytest
from helpers_ids import PW
from httpx import ASGITransport, AsyncClient

from app import bootstrap, db, objects, setup
from app.main import asgi_app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("SLOPCLANKER_REG_TOKEN", "test-reg-token-1234567890")
    bootstrap.ensure(db.db_path())
    conn = db.connect(db.db_path())
    boss = setup.create_superadmin(conn, "root", PW)
    sid = objects.create_stack(conn, boss, name="S", slug="s")
    pa = objects.create_project(conn, boss, name="A", stack_id=sid)
    pb = objects.create_project(conn, boss, name="B", stack_id=sid)
    for title, pid in (("a-one", pa), ("a-two", pa), ("b-one", pb)):
        objects.create_task(conn, boss, project_id=pid, title=title)
    conn.close()
    return {"sid": sid}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_lists_tasks_across_projects_with_names(_env):
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        from app import auth

        conn = db.connect(db.db_path())
        tok = auth.login(conn, "root", PW)[1]
        conn.close()
        r = await c.get(
            f"/api/stacks/{_env['sid']}/tasks",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        tasks = r.json()["tasks"]
        assert [t["title"] for t in tasks] == ["a-one", "a-two", "b-one"]
        assert all("project_name" in t for t in tasks)
        assert {t["project_name"] for t in tasks} == {"A", "B"}


@pytest.mark.anyio
async def test_unknown_stack_404(_env):
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        from app import auth

        conn = db.connect(db.db_path())
        tok = auth.login(conn, "root", PW)[1]
        conn.close()
        r = await c.get(
            "/api/stacks/999/tasks", headers={"Authorization": f"Bearer {tok}"}
        )
        assert r.status_code == 404


@pytest.mark.anyio
async def test_requires_auth(_env):
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as c:
        r = await c.get(f"/api/stacks/{_env['sid']}/tasks")
        assert r.status_code == 401
