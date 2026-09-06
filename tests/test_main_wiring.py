"""Production wiring: main() bootstraps the DB and serves asgi_app."""

from app.main import asgi_app


def test_main_serves_asgi_app(monkeypatch, tmp_path):
    import app.main as main_module

    monkeypatch.setenv("SLOPCLANKER_DB", str(tmp_path / "t.db"))
    served = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.setdefault("app", app))
    main_module.main()
    assert served["app"] is asgi_app
