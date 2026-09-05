"""Production wiring: main() must serve the middleware-wrapped asgi_app."""

import pytest

import app.main as main_module
from app.main import asgi_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_main_serves_asgi_app(monkeypatch):
    served = {}
    monkeypatch.setattr(main_module.os.environ, "get", lambda k, d=None: d)
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.setdefault("app", app))
    main_module.main()
    assert served["app"] is asgi_app
