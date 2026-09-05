"""SlopClanker server: MCP tools + REST + web UI in one FastMCP app.

Agent coordination layer: projects, posts with nested comments, todos,
notes, wiki, chat, presence and file claims. Humans get a full web UI;
agent<->human talk stays in opencode sessions.

Environment (set by run.sh):
  SLOPCLANKER_HOST / SLOPCLANKER_PORT   bind address (default 0.0.0.0:8090)
  SLOPCLANKER_DB                        sqlite path (default /data/slopclanker.db)
  SLOPCLANKER_TOKEN                     bearer token; unset disables auth (dev only)
  SLOPCLANKER_HEARTBEAT_TIMEOUT         agent active window in seconds (default 900)
"""

import hmac
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from app import store
from app.db import connect

PUBLIC_PATHS = {"/", "/healthz", "/favicon.ico"}

mcp = FastMCP(
    "slopclanker",
    instructions=(
        "Townhall for agents. Say hello at session start to announce yourself "
        "and get the awareness snapshot; post, comment and close posts to talk "
        "and decide; keep todos, notes and wiki pages for knowledge; claim "
        "files before editing them."
    ),
)


def _db_path() -> str:
    return os.environ.get("SLOPCLANKER_DB", "/data/slopclanker.db")


def _heartbeat_timeout() -> int:
    return int(os.environ.get("SLOPCLANKER_HEARTBEAT_TIMEOUT", "900"))


@contextmanager
def _db():
    conn = connect(_db_path())
    try:
        yield conn
    finally:
        conn.close()


class RequestTooLarge(Exception):
    """Request body above the size cap."""


MAX_BODY_BYTES = 1_000_000


def _api(handler: Callable[[Request], Awaitable[JSONResponse]]) -> Callable[..., Any]:
    """Map ValueError/TypeError to 400 and RequestTooLarge to 413."""

    @wraps(handler)
    async def wrapped(request: Request) -> JSONResponse:
        try:
            return await handler(request)
        except RequestTooLarge as err:
            return JSONResponse({"error": str(err)}, status_code=413)
        except (TypeError, ValueError) as err:
            return JSONResponse({"error": str(err)}, status_code=400)

    return wrapped


async def _json_body(request: Request) -> dict:
    if request.headers.get("content-length"):
        try:
            if int(request.headers["content-length"]) > MAX_BODY_BYTES:
                raise RequestTooLarge(f"body over {MAX_BODY_BYTES} bytes")
        except RequestTooLarge:
            raise
        except ValueError:
            pass
    try:
        data = await request.json()
    except Exception as err:
        raise ValueError("body must be JSON") from err
    if not isinstance(data, dict):
        raise TypeError("body must be a JSON object")
    return data


def _require(data: dict, *fields: str) -> None:
    missing = [f for f in fields if not data.get(f)]
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(missing)}")


def _project_param(request: Request, data: dict | None = None) -> int | None:
    """Resolve the project: ?project= query param wins, then body 'project'
    (slug or id), then body 'project_id'. None if nothing given."""
    ref = request.query_params.get("project") or (data or {}).get("project")
    if not ref:
        pid = (data or {}).get("project_id")
        return int(pid) if pid else None
    with _db() as conn:
        found = store.get_project(conn, ref)
    if found is None:
        raise ValueError(f"project '{ref}' does not exist")
    return int(found["id"])


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "slopclanker"})


_STATIC_DIR = Path(__file__).resolve().parent / "static"


@mcp.custom_route("/", methods=["GET"])
async def index(request: Request) -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(request: Request) -> FileResponse:
    return FileResponse(_STATIC_DIR / "favicon.png", media_type="image/png")


@mcp.custom_route("/api/hello", methods=["POST"])
@_api
async def api_hello(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "name")
    with _db() as conn:
        snap = store.hello(
            conn,
            data["name"],
            session_id=data.get("session_id"),
            note=data.get("note"),
            role=data.get("role"),
            contact=data.get("contact"),
            heartbeat_timeout=_heartbeat_timeout(),
        )
    return JSONResponse(snap)


@mcp.custom_route("/api/overview", methods=["GET"])
@_api
async def api_overview(request: Request) -> JSONResponse:
    try:
        seen = float(request.query_params.get("seen", "0"))
    except ValueError:
        seen = 0.0
    with _db() as conn:
        return JSONResponse(
            store.overview(
                conn, heartbeat_timeout=_heartbeat_timeout(), seen_since=seen
            )
        )


# --- projects --------------------------------------------------------------


@mcp.custom_route("/api/projects", methods=["GET"])
@_api
async def api_list_projects(request: Request) -> JSONResponse:
    with _db() as conn:
        return JSONResponse(store.overview(conn, _heartbeat_timeout())["projects"])


@mcp.custom_route("/api/projects", methods=["POST"])
@_api
async def api_create_project(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "name", "author")
    with _db() as conn:
        project = store.create_project(
            conn,
            data["name"],
            created_by=data["author"],
            slug=data.get("slug"),
            description=data.get("description", ""),
        )
    return JSONResponse(project)


# --- posts + comments ------------------------------------------------------


@mcp.custom_route("/api/posts", methods=["POST"])
@_api
async def api_create_post(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "title", "body", "author")
    with _db() as conn:
        pid = store.create_post(
            conn,
            data["title"],
            data["body"],
            created_by=data["author"],
            kind=data.get("kind", "info"),
            audience=data.get("audience", "all"),
            project_id=_project_param(request, data) or 1,
        )
    return JSONResponse({"id": pid})


@mcp.custom_route("/api/posts", methods=["GET"])
@_api
async def api_list_posts(request: Request) -> JSONResponse:
    include_closed = request.query_params.get("include_closed") in ("1", "true", "yes")
    with _db() as conn:
        return JSONResponse(
            store.list_posts(
                conn, project_id=_project_param(request), include_closed=include_closed
            )
        )


@mcp.custom_route("/api/posts/{post_id:int}", methods=["GET"])
@_api
async def api_post_detail(request: Request) -> JSONResponse:
    with _db() as conn:
        detail = store.post_detail(conn, request.path_params["post_id"])
    if detail is None:
        return JSONResponse({"error": "post not found"}, status_code=404)
    return JSONResponse(detail)


@mcp.custom_route("/api/posts/{post_id:int}/comments", methods=["POST"])
@_api
async def api_add_comment(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "author", "body")
    with _db() as conn:
        cid = store.add_comment(
            conn,
            request.path_params["post_id"],
            data["author"],
            data["body"],
            parent_id=data.get("parent_id"),
        )
    return JSONResponse({"id": cid})


@mcp.custom_route("/api/posts/{post_id:int}/close", methods=["POST"])
@_api
async def api_close_post(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "outcome")
    with _db() as conn:
        store.close_post(conn, request.path_params["post_id"], data["outcome"])
    return JSONResponse({"ok": True})


# --- todos -----------------------------------------------------------------


@mcp.custom_route("/api/todos", methods=["POST"])
@_api
async def api_add_todo(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "author")
    with _db() as conn:
        tid = store.add_todo(
            conn,
            created_by=data["author"],
            title=data.get("title", ""),
            body=data.get("body", ""),
            priority=data.get("priority", "medium"),
            tags=data.get("tags", ""),
            assignee=data.get("assignee"),
            project_id=_project_param(request, data) or 1,
        )
    return JSONResponse({"id": tid})


@mcp.custom_route("/api/todos", methods=["GET"])
@_api
async def api_list_todos(request: Request) -> JSONResponse:
    params = request.query_params
    assignee = params.get("assignee")
    status = params.get("status", "open")
    with _db() as conn:
        todos = store.list_todos(
            conn,
            project_id=_project_param(request),
            assignee=assignee,
            name=params.get("name"),
            status=status,
        )
    return JSONResponse(todos)


@mcp.custom_route("/api/todos/{todo_id:int}", methods=["PATCH"])
@_api
async def api_update_todo(request: Request) -> JSONResponse:
    data = await _json_body(request)
    if "project" in data:
        raise ValueError("use query param ?project= to move todos between projects")
    with _db() as conn:
        todo = store.update_todo(
            conn,
            request.path_params["todo_id"],
            actor=data.get("actor", ""),
            **{k: v for k, v in data.items() if k != "actor"},
        )
    return JSONResponse(todo)


@mcp.custom_route("/api/todos/{todo_id:int}/done", methods=["POST"])
@_api
async def api_done_todo(request: Request) -> JSONResponse:
    data = (
        {}
        if request.headers.get("content-length") == "0"
        else await _json_body(request)
    )
    with _db() as conn:
        store.done_todo(conn, request.path_params["todo_id"], data.get("actor", ""))
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/todos/{todo_id:int}/reopen", methods=["POST"])
@_api
async def api_reopen_todo(request: Request) -> JSONResponse:
    with _db() as conn:
        store.reopen_todo(conn, request.path_params["todo_id"])
    return JSONResponse({"ok": True})


@mcp.custom_route("/api/todos/{todo_id:int}/archive", methods=["POST"])
@_api
async def api_archive_todo(request: Request) -> JSONResponse:
    with _db() as conn:
        store.archive_todo(conn, request.path_params["todo_id"])
    return JSONResponse({"ok": True})


# --- notes -----------------------------------------------------------------


@mcp.custom_route("/api/notes", methods=["GET"])
@_api
async def api_list_notes(request: Request) -> JSONResponse:
    with _db() as conn:
        return JSONResponse(store.list_notes(conn, _project_param(request)))


@mcp.custom_route("/api/notes", methods=["POST"])
@_api
async def api_create_note(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "title", "author")
    with _db() as conn:
        nid = store.save_note(
            conn,
            data["title"],
            created_by=data["author"],
            body=data.get("body", ""),
            project_id=_project_param(request) or int(data.get("project_id", 1)),
            tags=data.get("tags", ""),
        )
    return JSONResponse({"id": nid})


@mcp.custom_route("/api/notes/{note_id:int}", methods=["GET"])
@_api
async def api_get_note(request: Request) -> JSONResponse:
    with _db() as conn:
        note = store.get_note(conn, request.path_params["note_id"])
    if note is None:
        return JSONResponse({"error": "note not found"}, status_code=404)
    return JSONResponse(note)


@mcp.custom_route("/api/notes/{note_id:int}", methods=["PUT"])
@_api
async def api_update_note(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "title", "author")
    with _db() as conn:
        store.save_note(
            conn,
            data["title"],
            created_by=data["author"],
            body=data.get("body", ""),
            note_id=request.path_params["note_id"],
            tags=data.get("tags", ""),
        )
        note = store.get_note(conn, request.path_params["note_id"])
    return JSONResponse(note)


# --- wiki ------------------------------------------------------------------


@mcp.custom_route("/api/wiki", methods=["GET"])
@_api
async def api_list_pages(request: Request) -> JSONResponse:
    with _db() as conn:
        return JSONResponse(store.list_pages(conn, _project_param(request)))


@mcp.custom_route("/api/wiki", methods=["POST"])
@_api
async def api_create_page(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "title", "author")
    with _db() as conn:
        slug = store.save_page(
            conn,
            data["title"],
            created_by=data["author"],
            body=data.get("body", ""),
            slug=data.get("slug"),
            project_id=_project_param(request) or int(data.get("project_id", 1)),
        )
    return JSONResponse({"slug": slug})


@mcp.custom_route("/api/wiki/{slug}", methods=["GET"])
@_api
async def api_get_page(request: Request) -> JSONResponse:
    with _db() as conn:
        page = store.get_page(conn, request.path_params["slug"])
    if page is None:
        return JSONResponse({"error": "page not found"}, status_code=404)
    return JSONResponse(page)


@mcp.custom_route("/api/wiki/{slug}", methods=["PUT"])
@_api
async def api_update_page(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "title", "author")
    with _db() as conn:
        existing = store.get_page(conn, request.path_params["slug"])
        if existing is None:
            return JSONResponse({"error": "page not found"}, status_code=404)
        store.save_page(
            conn,
            data["title"],
            created_by=data["author"],
            body=data.get("body", ""),
            slug=request.path_params["slug"],
            page_id=int(existing["id"]),
        )
        page = store.get_page(conn, request.path_params["slug"])
    return JSONResponse(page)


# --- chat ------------------------------------------------------------------


@mcp.custom_route("/api/chat", methods=["GET"])
@_api
async def api_chat_list(request: Request) -> JSONResponse:
    params = request.query_params
    try:
        since = float(params.get("since", "0"))
    except ValueError:
        return JSONResponse({"error": "since must be a number"}, status_code=400)
    with _db() as conn:
        return JSONResponse(
            store.chat_list(conn, channel=params.get("channel", "general"), since=since)
        )


@mcp.custom_route("/api/chat", methods=["POST"])
@_api
async def api_chat_send(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "author", "body")
    with _db() as conn:
        cid = store.chat_send(
            conn, data["author"], data["body"], channel=data.get("channel", "general")
        )
    return JSONResponse({"id": cid})


# --- events / activity -----------------------------------------------------


@mcp.custom_route("/api/events", methods=["GET"])
@_api
async def api_events(request: Request) -> JSONResponse:
    try:
        limit = int(request.query_params.get("limit", "200"))
    except ValueError:
        limit = 200
    with _db() as conn:
        return JSONResponse(
            store.list_events(conn, project_id=_project_param(request), limit=limit)
        )


# --- agents ----------------------------------------------------------------


@mcp.custom_route("/api/agents", methods=["GET"])
@_api
async def api_list_agents(request: Request) -> JSONResponse:
    with _db() as conn:
        return JSONResponse(
            store.list_agents(conn, heartbeat_timeout=_heartbeat_timeout())
        )


@mcp.custom_route("/api/agents/{name}", methods=["GET"])
@_api
async def api_get_agent(request: Request) -> JSONResponse:
    with _db() as conn:
        agent = store.get_agent(conn, request.path_params["name"])
        if agent is None:
            return JSONResponse({"error": "agent not found"}, status_code=404)
        agent["active"] = time.time() - agent["last_seen"] <= _heartbeat_timeout()
        agent["claims"] = store.agent_claims(conn, request.path_params["name"])
    return JSONResponse(agent)


@mcp.custom_route("/api/agents/{name}", methods=["PUT"])
@_api
async def api_put_agent(request: Request) -> JSONResponse:
    data = await _json_body(request)
    with _db() as conn:
        agent = store.profile_set(
            conn,
            request.path_params["name"],
            note=data.get("note"),
            role=data.get("role"),
            contact=data.get("contact"),
        )
    return JSONResponse(agent)


# --- claims ----------------------------------------------------------------


@mcp.custom_route("/api/claims", methods=["POST"])
@_api
async def api_set_claims(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "agent", "paths")
    if not isinstance(data["paths"], list):
        raise TypeError("paths must be a list")
    with _db() as conn:
        count = store.set_claims(
            conn, data["agent"], data["paths"], note=data.get("note")
        )
    return JSONResponse({"claims": count})


@mcp.custom_route("/api/claims", methods=["GET"])
@_api
async def api_check_claims(request: Request) -> JSONResponse:
    path = request.query_params.get("path")
    if not path:
        return JSONResponse(
            {"error": "missing required query param: path"}, status_code=400
        )
    with _db() as conn:
        found = store.check_claims(
            conn,
            path,
            agent=request.query_params.get("agent"),
            heartbeat_timeout=_heartbeat_timeout(),
        )
    return JSONResponse(found)


@mcp.custom_route("/api/claims", methods=["DELETE"])
@_api
async def api_release_claims(request: Request) -> JSONResponse:
    data = await _json_body(request)
    _require(data, "agent", "paths")
    if not isinstance(data["paths"], list):
        raise TypeError("paths must be a list")
    with _db() as conn:
        store.release_claims(conn, data["agent"], data["paths"])
    return JSONResponse({"ok": True})


# --- awareness -------------------------------------------------------------


@mcp.custom_route("/api/check", methods=["GET"])
@_api
async def api_check(request: Request) -> JSONResponse:
    params = request.query_params
    if not params.get("name"):
        return JSONResponse(
            {"error": "missing required query param: name"}, status_code=400
        )
    try:
        since = float(params.get("since", "0"))
    except ValueError:
        return JSONResponse({"error": "since must be a number"}, status_code=400)
    with _db() as conn:
        result = store.check(conn, params["name"], since=since)
    return JSONResponse(result)


class BearerAuth:
    """Pure ASGI middleware: bearer token on /api and /mcp; public paths skip.

    Token is read per request from SLOPCLANKER_TOKEN; when unset (dev/tests
    without auth) everything is allowed.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http" and scope.get("path") not in PUBLIC_PATHS:
            token = os.environ.get("SLOPCLANKER_TOKEN")
            if token:
                headers = {k.lower(): v for k, v in scope.get("headers", [])}
                auth = headers.get(b"authorization", b"").decode("latin-1")
                expected = f"Bearer {token}".encode()
                if not hmac.compare_digest(auth.encode("utf-8", "replace"), expected):
                    await JSONResponse({"error": "unauthorized"}, status_code=401)(
                        scope, receive, send
                    )
                    return
        await self.app(scope, receive, send)


from app.tools import register as _register_tools

_register_tools(mcp)

asgi_app = mcp.http_app(path="/mcp", middleware=[Middleware(BearerAuth)])


def main() -> None:
    """Entry point for the add-on (run.sh execs `python3 -m app.main`).

    Serves the module-level ``asgi_app`` (which carries the BearerAuth
    middleware) with uvicorn. Do NOT use ``mcp.run()`` here: it builds its
    own Starlette app and silently drops custom middleware.
    """
    import uvicorn

    host = os.environ.get("SLOPCLANKER_HOST", "0.0.0.0")
    port = int(os.environ.get("SLOPCLANKER_PORT", "8090"))
    uvicorn.run(asgi_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
