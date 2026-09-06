"""Health endpoint contract."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import asgi_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_healthz_returns_ok() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=asgi_app), base_url="http://test"
    ) as client:
        r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "service": "slopclanker"}
