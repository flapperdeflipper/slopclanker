"""Health endpoint contract."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import mcp


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_healthz_returns_ok() -> None:
    app = mcp.http_app(path="/mcp")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service": "slopclanker"}
