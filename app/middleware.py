"""Pure ASGI middleware shared by the doors."""

import ipaddress
import os
from collections.abc import Callable
from typing import Any

from starlette.responses import JSONResponse

from app import auth, bootstrap, db, ratelimit

PUBLIC_EXACT = {"/healthz", "/", "/favicon.ico", "/api/setup"}
PUBLIC_PREFIXES = (
    "/api/auth/register",
    "/api/auth/enroll",
    "/api/auth/login",
    "/api/auth/reenroll",
)


# Per-identity API budget (DESIGN §10): requests / window seconds.
API_RATE = (1200, 300)


class IngressPath:
    """Strip the Home Assistant ingress prefix so routes match.

    HA ingress forwards ``/api/hassio_ingress/<token>/foo`` as-is and sets
    ``X-Ingress-Path: /api/hassio_ingress/<token>``. Browsers keep the
    prefixed URLs (the UI uses relative paths); we strip the prefix for
    routing only.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            prefix = headers.get(b"x-ingress-path", b"").decode("latin-1")
            path = scope.get("path", "")
            if prefix and path.startswith(prefix):
                scope = dict(scope)
                scope["path"] = path[len(prefix) :] or "/"
        await self.app(scope, receive, send)


def client_ip(peer_host: str | None, x_forwarded_for: str | None) -> str | None:
    """Socket peer IP is authoritative; X-Forwarded-For only from trusted proxy.

    A direct-door client can forge X-Forwarded-For, so it is honored only
    when the connection itself originates from SLOPCLANKER_TRUSTED_PROXY.
    """
    if peer_host and x_forwarded_for:
        trusted = os.environ.get("SLOPCLANKER_TRUSTED_PROXY", "")
        for cidr in trusted.split(","):
            cidr = cidr.strip()
            if not cidr:
                continue
            try:
                if ipaddress.ip_address(peer_host) in ipaddress.ip_network(
                    cidr, strict=False
                ):
                    return x_forwarded_for.split(",")[0].strip()
            except ValueError:
                continue
    return peer_host


def scope_ip(scope: dict) -> str | None:
    peer = scope.get("client")
    peer_host = peer[0] if peer else None
    xff = None
    for k, v in scope.get("headers", []):
        if k.lower() == b"x-forwarded-for":
            xff = v.decode("latin-1")
    return client_ip(peer_host, xff)


class BearerIdentity:
    """Resolve the Authorization bearer to an identity; 401 otherwise.

    Public paths skip resolution; the registration endpoints under
    PUBLIC_PREFIXES carry their own registration-token guard in-handler.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path not in PUBLIC_EXACT and not path.startswith(PUBLIC_PREFIXES):
                headers = {k.lower(): v for k, v in scope.get("headers", [])}
                raw = headers.get(b"authorization", b"").decode("latin-1")
                token = raw[7:] if raw.lower().startswith("bearer ") else ""
                ip = scope_ip(scope)
                ua = headers.get(b"user-agent", b"").decode("latin-1") or None
                row = None
                conn = db.connect(bootstrap.ensure(db.db_path()))
                try:
                    row = auth.authenticate(conn, token, ip=ip, user_agent=ua)
                finally:
                    conn.close()
                if row is None:
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(
                        scope, receive, send
                    )
                    return
                if not ratelimit.allow(
                    f"api:{row['id']}", limit=API_RATE[0], window=API_RATE[1]
                ):
                    await JSONResponse(
                        {"error": "rate limit exceeded"}, status_code=429
                    )(scope, receive, send)
                    return
                scope = dict(scope)
                scope.setdefault("state", {})
                scope["state"]["identity"] = dict(row)
        await self.app(scope, receive, send)
