"""SlopClanker v1.0 server: FastMCP app shell (phases 1-2).

Surface: /healthz, setup wizard, the clanker registration pipeline,
human login, and the admin identity surface. Object routes land with
phase 3+. Entrypoint: ``python3 -m app.main`` (run.sh execs it); main()
serves the middleware-wrapped asgi_app with uvicorn — do NOT use
mcp.run(), it builds its own Starlette app and drops custom middleware.
"""

import hmac
import json
import logging
import os
import time
from pathlib import Path

import anyio
import uvicorn
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)

from app import (
    auth,
    bootstrap,
    claims,
    comms,
    db,
    decisions,
    events,
    knowledge,
    links,
    objects,
    proofs,
    questions,
    ratelimit,
    realtime,
    registry,
    search,
    setup,
    statemachine,
    tools,
)
from app import (
    export as export_mod,
)
from app import permissions as perms
from app.middleware import BearerIdentity, IngressPath, scope_ip
from app.schema import SCHEMA_VERSION

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 1_048_576
STATIC_DIR = Path(__file__).parent / "static"

mcp = FastMCP(name="slopclanker")
tools.register(mcp)


def _db():
    return db.connect(bootstrap.ensure(db.db_path()))


def _actor(request: Request) -> dict | None:
    try:
        return request.state.identity
    except AttributeError:
        return None


def _reg_token_ok(request: Request) -> bool:
    """Registration endpoints authenticate with the shared registration token."""
    expected = os.environ.get("SLOPCLANKER_REG_TOKEN", "")
    if not expected:
        return False
    raw = request.headers.get("authorization", "")
    presented = raw[7:] if raw.lower().startswith("bearer ") else ""
    return hmac.compare_digest(presented.encode(), expected.encode())


class _BadBody(Exception):
    pass


async def _json_body(request: Request) -> dict:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        raise _BadBody("body too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise _BadBody("body too large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise _BadBody("invalid json")
    if not isinstance(data, dict):
        raise _BadBody("expected a json object")
    return data


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "slopclanker"})


@mcp.custom_route("/", methods=["GET"])
async def index(request: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(request: Request) -> Response:
    return Response(status_code=204)


@mcp.custom_route("/api/setup", methods=["GET"])
async def api_setup_status(request: Request) -> JSONResponse:
    conn = _db()
    try:
        return JSONResponse(
            {
                "service": "slopclanker",
                "schema_version": SCHEMA_VERSION,
                "setup_required": setup.setup_required(conn),
            }
        )
    finally:
        conn.close()


@mcp.custom_route("/api/setup", methods=["POST"])
async def api_setup_create(request: Request) -> JSONResponse:
    ip = scope_ip(request.scope)
    user_agent = request.headers.get("user-agent")
    if not ratelimit.allow(f"setup:{ip}", limit=10):
        return JSONResponse({"error": "too many attempts"}, status_code=429)
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        return JSONResponse(
            {"error": "username and password are required"}, status_code=422
        )
    conn = _db()
    try:
        try:
            row = setup.create_superadmin(
                conn, username, password, ip=ip, user_agent=user_agent
            )
        except setup.SetupComplete:
            return JSONResponse({"error": "setup already done"}, status_code=409)
        except setup.SetupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        conn.close()
    return JSONResponse({"ok": True, "username": row["name"]}, status_code=201)


@mcp.custom_route("/api/auth/register", methods=["POST"])
async def api_register(request: Request) -> JSONResponse:
    if not _reg_token_ok(request):
        return JSONResponse({"error": "registration disabled"}, status_code=503)
    ip = scope_ip(request.scope)
    if not ratelimit.allow(f"register:{ip}", limit=5):
        return JSONResponse({"error": "too many attempts"}, status_code=429)
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        try:
            rid = registry.register_request(
                conn,
                data.get("name", ""),
                data.get("note", ""),
                data.get("claim_secret", ""),
                ip,
                request.headers.get("user-agent"),
            )
        except setup.InvalidName as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except registry.NameTaken as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except registry.RegistryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        conn.close()
    return JSONResponse({"request_id": rid}, status_code=201)


@mcp.custom_route("/api/auth/register/{rid:int}/poll", methods=["POST"])
async def api_register_poll(request: Request) -> JSONResponse:
    rid = request.path_params["rid"]
    if not _reg_token_ok(request):
        return JSONResponse({"error": "registration disabled"}, status_code=503)
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        try:
            result = registry.poll(
                conn, rid, data.get("claim_secret", ""), ip=scope_ip(request.scope)
            )
        except registry.NotFound:
            return JSONResponse({"error": "no such registration"}, status_code=404)
        except registry.WrongClaim:
            return JSONResponse({"error": "claim secret mismatch"}, status_code=403)
        except registry.RegistryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        conn.close()
    return JSONResponse(result)


@mcp.custom_route("/api/auth/enroll", methods=["POST"])
async def api_enroll(request: Request) -> JSONResponse:
    if not _reg_token_ok(request):
        return JSONResponse({"error": "registration disabled"}, status_code=503)
    ip = scope_ip(request.scope)
    if not ratelimit.allow(f"enroll:{ip}", limit=10):
        return JSONResponse({"error": "too many attempts"}, status_code=429)
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        try:
            token = registry.enroll(conn, data.get("code", ""), ip=ip)
        except registry.RegistryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        conn.close()
    return JSONResponse({"token": token})


@mcp.custom_route("/api/auth/reenroll", methods=["POST"])
async def api_reenroll(request: Request) -> JSONResponse:
    if not _reg_token_ok(request):
        return JSONResponse({"error": "registration disabled"}, status_code=503)
    ip = scope_ip(request.scope)
    if not ratelimit.allow(f"reenroll:{ip}", limit=2):
        return JSONResponse({"error": "too many attempts"}, status_code=429)
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        registry.reenroll_request(conn, data.get("name", ""), ip)
    finally:
        conn.close()
    return JSONResponse({"ok": True}, status_code=202)


@mcp.custom_route("/api/auth/login", methods=["POST"])
async def api_login(request: Request) -> JSONResponse:
    ip = scope_ip(request.scope)
    if not ratelimit.allow(f"login:{ip}", limit=10):
        return JSONResponse({"error": "too many attempts"}, status_code=429)
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        try:
            row, token, expires = auth.login(
                conn,
                data.get("username", ""),
                data.get("password", ""),
                ip,
                request.headers.get("user-agent"),
            )
        except auth.AuthError:
            return JSONResponse({"error": "invalid credentials"}, status_code=401)
    finally:
        conn.close()
    return JSONResponse(
        {
            "token": token,
            "expires_at": expires,
            "identity": {"id": row["id"], "name": row["name"], "role": row["role"]},
        }
    )


@mcp.custom_route("/api/auth/whoami", methods=["GET"])
async def api_whoami(request: Request) -> JSONResponse:
    actor = _actor(request)
    conn = _db()
    try:
        cred = conn.execute(
            "SELECT id, kind, label, issued_at, expires_at, last_seen_at,"
            " last_ip, call_count FROM credentials WHERE id = ?",
            (actor["cred_id"],),
        ).fetchone()
    finally:
        conn.close()
    return JSONResponse(
        {
            "id": actor["id"],
            "name": actor["name"],
            "kind": actor["kind"],
            "role": actor["role"],
            "credential": dict(cred) if cred else None,
        }
    )


@mcp.custom_route("/api/auth/logout", methods=["POST"])
async def api_logout(request: Request) -> JSONResponse:
    actor = _actor(request)
    auth.revoke_credential(_db(), actor["cred_id"])
    return Response(status_code=204)


@mcp.custom_route("/api/admin/export", methods=["GET"])
async def api_admin_export(request: Request) -> JSONResponse:
    """Full JSON dump (admin-only): every table, chain hashes included."""
    actor = _actor(request)
    if actor is None or actor.get("role") not in ("admin", "superadmin"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = _db()
    try:
        data = export_mod.export_all(conn)
    finally:
        conn.close()
    return JSONResponse(data)


@mcp.custom_route("/api/registrations", methods=["GET"])
async def api_registrations(request: Request) -> JSONResponse:
    actor = _actor(request)
    if not perms.can(actor, perms.VIEW_IDENTITIES):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    status = request.query_params.get("status", "pending")
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT r.*, d.name AS decided_by_name FROM registrations r"
            " LEFT JOIN identities d ON d.id = r.decided_by"
            " WHERE r.status = ? ORDER BY r.created_at DESC LIMIT 200",
            (status,),
        ).fetchall()
        out = []
        for r in rows:
            prior = conn.execute(
                "SELECT COUNT(*) FROM identities WHERE reg_ip = ? AND id != ?",
                (r["ip"], r["identity_id"] or -1),
            ).fetchone()[0]
            item = dict(r)
            item["prior_from_ip"] = prior
            out.append(item)
    finally:
        conn.close()
    return JSONResponse(out)


@mcp.custom_route("/api/registrations/{rid:int}/approve", methods=["POST"])
async def api_registration_approve(request: Request) -> JSONResponse:
    rid = request.path_params["rid"]
    actor = _actor(request)
    if not perms.can(actor, perms.APPROVE_REGISTRATION):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = _db()
    try:
        try:
            result = registry.approve(conn, rid, actor["id"])
        except registry.NotFound:
            return JSONResponse({"error": "no such registration"}, status_code=404)
        except registry.RegistryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        conn.close()
    return JSONResponse(result, status_code=201)


@mcp.custom_route("/api/registrations/{rid:int}/reject", methods=["POST"])
async def api_registration_reject(request: Request) -> JSONResponse:
    rid = request.path_params["rid"]
    actor = _actor(request)
    if not perms.can(actor, perms.REJECT_REGISTRATION):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = _db()
    try:
        try:
            registry.reject(conn, rid, actor["id"])
        except registry.NotFound:
            return JSONResponse({"error": "no such registration"}, status_code=404)
        except registry.RegistryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        conn.close()
    return Response(status_code=204)


@mcp.custom_route("/api/identities/directory", methods=["GET"])
async def api_identities_directory(request: Request) -> JSONResponse:
    """Name map for display; ids/names/kinds only — no contact or creds."""
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, name, kind, status FROM identities"
            " WHERE status = 'active' ORDER BY name LIMIT 1000"
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/identities", methods=["GET"])
async def api_identities(request: Request) -> JSONResponse:
    actor = _actor(request)
    if not perms.can(actor, perms.VIEW_IDENTITIES):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = _db()
    try:
        ids = conn.execute(
            "SELECT * FROM identities ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
        creds = conn.execute(
            "SELECT identity_id, id, kind, label, issued_at, expires_at,"
            " revoked_at, last_seen_at, last_ip, call_count"
            " FROM credentials ORDER BY issued_at DESC"
        ).fetchall()
    finally:
        conn.close()
    by_identity: dict[int, list] = {}
    for c in creds:
        by_identity.setdefault(c["identity_id"], []).append(dict(c))
    out = []
    for i in ids:
        item = dict(i)
        item["credentials"] = by_identity.get(i["id"], [])
        out.append(item)
    return JSONResponse(out)


@mcp.custom_route("/api/identities/{iid:int}/revoke", methods=["POST"])
async def api_identity_revoke(request: Request) -> JSONResponse:
    iid = request.path_params["iid"]
    actor = _actor(request)
    conn = _db()
    try:
        target = conn.execute(
            "SELECT kind, role FROM identities WHERE id = ?", (iid,)
        ).fetchone()
        if target is None:
            return JSONResponse({"error": "no such identity"}, status_code=404)
        if iid == actor["id"]:
            return JSONResponse({"error": "cannot revoke yourself"}, status_code=422)
        if not perms.can(
            actor,
            perms.REVOKE_IDENTITY,
            {"target_kind": target["kind"], "target_role": target["role"]},
        ):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        auth.revoke_identity(conn, iid)
    finally:
        conn.close()
    return Response(status_code=204)


@mcp.custom_route("/api/identities/{iid:int}/code", methods=["POST"])
async def api_identity_code(request: Request) -> JSONResponse:
    iid = request.path_params["iid"]
    actor = _actor(request)
    if not perms.can(actor, perms.ISSUE_CODE):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = _db()
    try:
        target = conn.execute(
            "SELECT kind, status FROM identities WHERE id = ?", (iid,)
        ).fetchone()
        if (
            target is None
            or target["kind"] != "clanker"
            or target["status"] != "active"
        ):
            return JSONResponse(
                {"error": "no active clanker identity"}, status_code=404
            )
        code, expires = registry.issue_code(conn, iid, actor["id"])
    finally:
        conn.close()
    return JSONResponse({"code": code, "expires_at": expires}, status_code=201)


@mcp.custom_route("/api/users", methods=["POST"])
async def api_users_create(request: Request) -> JSONResponse:
    actor = _actor(request)
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    role = data.get("role", "user")
    if not perms.can(actor, perms.CREATE_USER, {"role": role}):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    conn = _db()
    try:
        try:
            row = auth.create_human(
                conn,
                data.get("username", ""),
                data.get("password", ""),
                role,
                actor["id"],
            )
        except setup.SetupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except Exception as exc:
            if "UNIQUE" in str(exc):
                return JSONResponse({"error": "name already in use"}, status_code=409)
            raise
    finally:
        conn.close()
    return JSONResponse(
        {"id": row["id"], "name": row["name"], "role": row["role"]}, status_code=201
    )


@mcp.custom_route("/api/notifications", methods=["GET"])
async def api_notifications(request: Request) -> JSONResponse:
    actor = _actor(request)
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE (identity_id IS NULL OR identity_id = ?)"
            " AND read_at IS NULL ORDER BY created_at DESC LIMIT 100",
            (actor["id"],),
        ).fetchall()
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/notifications/{nid:int}/read", methods=["POST"])
async def api_notification_read(request: Request) -> JSONResponse:
    nid = request.path_params["nid"]
    actor = _actor(request)
    conn = _db()
    try:
        with conn:
            conn.execute(
                "UPDATE notifications SET read_at = ? WHERE id = ?"
                " AND (identity_id IS NULL OR identity_id = ?)",
                (time.time(), nid, actor["id"]),
            )
    finally:
        conn.close()
    return Response(status_code=204)


def _require_actor(request: Request) -> tuple[dict | None, JSONResponse | None]:
    actor = _actor(request)
    if actor is None:
        return None, JSONResponse({"error": "authentication required"}, status_code=401)
    return actor, None


def _svc_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, (statemachine.BlockedByQuestions, objects.Frozen)):
        return JSONResponse(
            {"error": str(exc), "questions": exc.questions}, status_code=409
        )
    if isinstance(exc, (statemachine.VersionConflict,)):
        return JSONResponse({"error": str(exc)}, status_code=409)
    if isinstance(exc, statemachine.HumanRequired):
        return JSONResponse({"error": str(exc)}, status_code=403)
    if isinstance(exc, proofs.ProofError):
        if "human-only" in str(exc):
            return JSONResponse({"error": str(exc)}, status_code=403)
        if str(exc).startswith("no such"):
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse({"error": str(exc)}, status_code=422)
    if isinstance(exc, (objects.ObjectError, statemachine.TransitionError)):
        if str(exc).startswith("no such"):
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse({"error": str(exc)}, status_code=422)
    if isinstance(exc, LookupError):
        return JSONResponse({"error": str(exc)}, status_code=404)
    raise exc


@mcp.custom_route("/api/stacks", methods=["GET"])
async def api_stacks_list(request: Request) -> JSONResponse:
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = objects.list_stacks(conn)
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/stacks", methods=["POST"])
async def api_stacks_create(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        sid = objects.create_stack(
            conn,
            actor,
            data.get("name", ""),
            description=data.get("description", ""),
            slug=data.get("slug"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": sid}, status_code=201)


@mcp.custom_route("/api/projects", methods=["GET"])
async def api_projects_list(request: Request) -> JSONResponse:
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        stack = request.query_params.get("stack")
        rows = objects.list_projects(
            conn,
            stack_id=int(stack) if stack and stack.isdigit() else None,
            include_archived=request.query_params.get("archived") == "1",
        )
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/projects", methods=["POST"])
async def api_projects_create(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        pid = objects.create_project(
            conn,
            actor,
            data.get("name", ""),
            description=data.get("description", ""),
            stack_id=data.get("stack_id"),
            slug=data.get("slug"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": pid}, status_code=201)


@mcp.custom_route("/api/projects/{pid:int}", methods=["GET"])
async def api_project_get(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        proj = objects.get_project(conn, pid)
        tasks = objects.list_tasks(conn, project_id=pid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse({"project": dict(proj), "tasks": [dict(t) for t in tasks]})


@mcp.custom_route("/api/projects/{pid:int}", methods=["PATCH"])
async def api_project_edit(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        proj = objects.edit_project(
            conn,
            actor,
            pid,
            name=data.get("name"),
            description=data.get("description"),
            stack_id=data.get("stack_id"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(proj))


async def _project_action(request: Request, fn) -> JSONResponse:
    pid = request.path_params["pid"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        result = fn(conn, actor, pid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    if result is None:
        return Response(status_code=204)
    return JSONResponse(dict(result))


@mcp.custom_route("/api/projects/{pid:int}/archive", methods=["POST"])
async def api_project_archive(request: Request) -> JSONResponse:
    return await _project_action(
        request, lambda c, a, p: objects.set_project_archived(c, a, p, True)
    )


@mcp.custom_route("/api/projects/{pid:int}/unarchive", methods=["POST"])
async def api_project_unarchive(request: Request) -> JSONResponse:
    return await _project_action(
        request, lambda c, a, p: objects.set_project_archived(c, a, p, False)
    )


@mcp.custom_route("/api/projects/{pid:int}/adopt", methods=["POST"])
async def api_project_adopt(request: Request) -> JSONResponse:
    return await _project_action(request, objects.adopt_project)


@mcp.custom_route("/api/projects/{pid:int}/purge", methods=["POST"])
async def api_project_purge(request: Request) -> JSONResponse:
    return await _project_action(request, objects.purge_project)


@mcp.custom_route("/api/tasks", methods=["GET"])
async def api_tasks_list(request: Request) -> JSONResponse:
    _actor, err = _require_actor(request)
    if err:
        return err
    qp = request.query_params
    conn = _db()
    try:

        def _qint(name):
            v = qp.get(name)
            return int(v) if v and v.isdigit() else None

        rows = objects.list_tasks(
            conn,
            project_id=_qint("project"),
            state=qp.get("state"),
            assignee_id=_qint("assignee"),
        )
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/tasks", methods=["POST"])
async def api_tasks_create(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        tid = objects.create_task(
            conn,
            actor,
            data.get("project_id", 0),
            data.get("title", ""),
            body=data.get("body", ""),
            priority=data.get("priority", "medium"),
            assignee_id=data.get("assignee_id"),
            tags=data.get("tags", ""),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": tid}, status_code=201)


@mcp.custom_route("/api/tasks/{tid:int}", methods=["GET"])
async def api_task_get(request: Request) -> JSONResponse:
    tid = request.path_params["tid"]
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        task = dict(objects.get_task(conn, tid))
        todos = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM todos WHERE task_id = ? AND trashed_at IS NULL"
                " ORDER BY sort, id",
                (tid,),
            )
        ]
        transitions = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM transitions WHERE task_id = ? ORDER BY id", (tid,)
            )
        ]
        proofs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM proofs WHERE task_id = ? AND trashed_at IS NULL"
                " ORDER BY id",
                (tid,),
            )
        ]
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse(
        {"task": task, "todos": todos, "transitions": transitions, "proofs": proofs}
    )


@mcp.custom_route("/api/tasks/{tid:int}", methods=["PATCH"])
async def api_task_edit(request: Request) -> JSONResponse:
    tid = request.path_params["tid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        assignee = data.get("assignee_id", objects.UNSET)
        task = objects.edit_task(
            conn,
            actor,
            tid,
            body=data.get("body"),
            title=data.get("title"),
            priority=data.get("priority"),
            tags=data.get("tags"),
            assignee_id=assignee,
            proof_waived=data.get("proof_waived"),
            version=data.get("version"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(task))


@mcp.custom_route("/api/tasks/{tid:int}/transition", methods=["POST"])
async def api_task_transition(request: Request) -> JSONResponse:
    tid = request.path_params["tid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        task = statemachine.transition(
            conn,
            tid,
            data.get("to", ""),
            actor,
            note=data.get("note", ""),
            version=data.get("version"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(task))


@mcp.custom_route("/api/tasks/{tid:int}/todos", methods=["POST"])
async def api_todo_add(request: Request) -> JSONResponse:
    tid = request.path_params["tid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        td = objects.add_todo(conn, actor, tid, data.get("title", ""))
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": td}, status_code=201)


async def _todo_action(request: Request, fn) -> JSONResponse:
    todo_id = request.path_params["toid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody:
        data = {}
    conn = _db()
    try:
        result = fn(conn, actor, todo_id, data)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    if result is None:
        return Response(status_code=204)
    return JSONResponse(dict(result))


@mcp.custom_route("/api/todos/{toid:int}/tick", methods=["POST"])
async def api_todo_tick(request: Request) -> JSONResponse:
    return await _todo_action(
        request,
        lambda c, a, t, d: objects.tick_todo(c, a, t, True, version=d.get("version")),
    )


@mcp.custom_route("/api/todos/{toid:int}/untick", methods=["POST"])
async def api_todo_untick(request: Request) -> JSONResponse:
    return await _todo_action(
        request,
        lambda c, a, t, d: objects.tick_todo(c, a, t, False, version=d.get("version")),
    )


@mcp.custom_route("/api/todos/{toid:int}", methods=["DELETE"])
async def api_todo_trash(request: Request) -> JSONResponse:
    return await _todo_action(request, lambda c, a, t, d: objects.trash_todo(c, a, t))


@mcp.custom_route("/api/tasks/{tid:int}/proofs", methods=["GET"])
async def api_proofs_list(request: Request) -> JSONResponse:
    tid = request.path_params["tid"]
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = proofs.list_proofs(conn, tid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse(rows)


@mcp.custom_route("/api/tasks/{tid:int}/proofs", methods=["POST"])
async def api_proof_add(request: Request) -> JSONResponse:
    tid = request.path_params["tid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        row = proofs.add_proof(
            conn,
            actor,
            tid,
            data.get("url", ""),
            provider=data.get("provider"),
            repo=data.get("repo"),
            number=data.get("number"),
            kind=data.get("kind"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse(row, status_code=201)


@mcp.custom_route("/api/proofs/{pid:int}/trash", methods=["POST"])
async def api_proof_trash(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        row = proofs.trash_proof(conn, actor, pid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    finally:
        conn.close()
    return JSONResponse(row)


@mcp.custom_route("/api/tasks/{tid:int}/proofs/check", methods=["POST"])
async def api_proofs_check(request: Request) -> JSONResponse:
    tid = request.path_params["tid"]
    _actor, err = _require_actor(request)
    if err:
        return err

    def _run():
        conn = _db()  # fresh connection: sqlite objects stay in one thread
        try:
            return proofs.check_task(conn, tid)
        finally:
            conn.close()

    try:
        rows = await anyio.to_thread.run_sync(_run)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _svc_error(exc)
    return JSONResponse(rows)


def _com_error(exc: Exception) -> JSONResponse:
    from app import comms as _c

    if isinstance(
        exc,
        (
            _c.CommsError,
            questions.QuestionError,
            decisions.DecisionError,
            links.LinkError,
        ),
    ):
        if isinstance(exc, questions.RateLimited):
            return JSONResponse({"error": str(exc)}, status_code=429)
        msg = str(exc)
        code = 404 if msg.startswith("no such") else 422
        if (
            "human-only" in msg
            or "admins only" in msg
            or "asker or admin" in msg
            or "only the addressee" in msg
        ):
            code = 403
        return JSONResponse({"error": msg}, status_code=code)
    return _svc_error(exc)


@mcp.custom_route("/api/projects/{pid:int}/discussions", methods=["GET"])
async def api_discussions_list(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = comms.list_discussions(conn, pid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/projects/{pid:int}/discussions", methods=["POST"])
async def api_discussions_create(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        did = comms.create_discussion(
            conn,
            actor,
            pid,
            data.get("title", ""),
            kind=data.get("kind", "info"),
            body=data.get("body", ""),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": did}, status_code=201)


@mcp.custom_route("/api/discussions/{did:int}", methods=["GET"])
async def api_discussion_get(request: Request) -> JSONResponse:
    did = request.path_params["did"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = comms.list_comments(conn, did, actor)
        return JSONResponse([dict(r) for r in rows])
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()


@mcp.custom_route("/api/discussions/{did:int}/close", methods=["POST"])
async def api_discussion_close(request: Request) -> JSONResponse:
    did = request.path_params["did"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody:
        data = {}
    conn = _db()
    try:
        row = comms.close_discussion(conn, actor, did, outcome=data.get("outcome", ""))
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(row))


@mcp.custom_route("/api/discussions/{did:int}/reopen", methods=["POST"])
async def api_discussion_reopen(request: Request) -> JSONResponse:
    did = request.path_params["did"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        row = comms.reopen_discussion(conn, actor, did)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(row))


@mcp.custom_route("/api/discussions/{did:int}/comments", methods=["POST"])
async def api_comment_add(request: Request) -> JSONResponse:
    did = request.path_params["did"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        cid = comms.add_comment(
            conn, actor, did, data.get("body", ""), parent_id=data.get("parent_id")
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": cid}, status_code=201)


@mcp.custom_route("/api/comments/{cid:int}/trash", methods=["POST"])
async def api_comment_trash(request: Request) -> JSONResponse:
    cid = request.path_params["cid"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        comms.trash_comment(conn, actor, cid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return Response(status_code=204)


@mcp.custom_route("/api/comments/{cid:int}/restore", methods=["POST"])
async def api_comment_restore(request: Request) -> JSONResponse:
    cid = request.path_params["cid"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        comms.restore_comment(conn, actor, cid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return Response(status_code=204)


@mcp.custom_route("/api/comments/{cid:int}", methods=["DELETE"])
async def api_comment_purge(request: Request) -> JSONResponse:
    cid = request.path_params["cid"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        comms.purge_comment(conn, actor, cid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return Response(status_code=204)


@mcp.custom_route("/api/projects/{pid:int}/chat", methods=["GET"])
async def api_chat_list(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    _actor, err = _require_actor(request)
    if err:
        return err
    since = request.query_params.get("since", "0")
    conn = _db()
    try:
        rows = comms.list_chat(conn, pid, since_id=int(since) if since.isdigit() else 0)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/projects/{pid:int}/chat", methods=["POST"])
async def api_chat_post(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        mid = comms.post_chat(conn, actor, pid, data.get("body", ""))
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": mid}, status_code=201)


@mcp.custom_route("/api/projects/{pid:int}/decisions", methods=["GET"])
async def api_decisions_list(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = decisions.list_decisions(conn, pid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/projects/{pid:int}/decisions", methods=["POST"])
async def api_decisions_create(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        did = decisions.create(
            conn, actor, pid, data.get("title", ""), context=data.get("context", "")
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": did}, status_code=201)


@mcp.custom_route("/api/decisions/{did:int}/status", methods=["POST"])
async def api_decision_status(request: Request) -> JSONResponse:
    did = request.path_params["did"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        row = decisions.set_status(
            conn,
            actor,
            did,
            data.get("status", ""),
            supersede_id=data.get("supersede_id"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(row))


@mcp.custom_route("/api/questions", methods=["GET"])
async def api_questions_list(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    qp = request.query_params
    conn = _db()
    try:
        rows = questions.list_questions(
            conn,
            open_only=qp.get("open") == "1",
            to_actor=actor if qp.get("to_me") == "1" else None,
            attach_type=qp.get("attach_type"),
            attach_id=int(qp["attach_id"])
            if qp.get("attach_id", "").isdigit()
            else None,
            project_id=int(qp["project"]) if qp.get("project", "").isdigit() else None,
        )
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/questions", methods=["POST"])
async def api_questions_ask(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        qid = questions.ask(
            conn,
            actor,
            data.get("project_id", 0),
            data.get("body", ""),
            to_identity_id=data.get("to_identity_id"),
            to_group=data.get("to_group"),
            attach_type=data.get("attach_type"),
            attach_id=data.get("attach_id"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": qid}, status_code=201)


@mcp.custom_route("/api/questions/{qid:int}/answer", methods=["POST"])
async def api_question_answer(request: Request) -> JSONResponse:
    qid = request.path_params["qid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        row = questions.answer(conn, actor, qid, data.get("answer", ""))
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(row))


@mcp.custom_route("/api/questions/{qid:int}/withdraw", methods=["POST"])
async def api_question_withdraw(request: Request) -> JSONResponse:
    qid = request.path_params["qid"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        row = questions.withdraw(conn, actor, qid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(row))


@mcp.custom_route("/api/questions/{qid:int}/reassign", methods=["POST"])
async def api_question_reassign(request: Request) -> JSONResponse:
    qid = request.path_params["qid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        row = questions.reassign(conn, actor, qid, data.get("to_identity_id", 0))
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(row))


@mcp.custom_route("/api/links", methods=["POST"])
async def api_links_create(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        lid = links.create(
            conn,
            actor,
            data.get("from_type", ""),
            data.get("from_id", 0),
            data.get("to_type", ""),
            data.get("to_id", 0),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": lid}, status_code=201)


@mcp.custom_route("/api/links/{lid:int}", methods=["DELETE"])
async def api_links_remove(request: Request) -> JSONResponse:
    lid = request.path_params["lid"]
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        links.remove(conn, actor, lid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return Response(status_code=204)


@mcp.custom_route("/api/context", methods=["GET"])
async def api_context(request: Request) -> JSONResponse:
    _actor, err = _require_actor(request)
    if err:
        return err
    qp = request.query_params
    conn = _db()
    try:
        rows = links.context_for(
            conn, qp.get("type", ""), int(qp["id"]) if qp.get("id", "").isdigit() else 0
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _com_error(exc)
    finally:
        conn.close()
    return JSONResponse(rows)


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request: Request) -> JSONResponse:
    _actor, err = _require_actor(request)
    if err:
        return err
    qp = request.query_params
    conn = _db()
    try:
        rows = search.search(
            conn,
            qp.get("q", ""),
            project_id=int(qp["project"]) if qp.get("project", "").isdigit() else None,
            kind=qp.get("kind"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    finally:
        conn.close()
    return JSONResponse(rows)


def _know_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, knowledge.Frozen):
        return JSONResponse(
            {"error": str(exc), "questions": exc.questions}, status_code=409
        )
    if isinstance(exc, (knowledge.KnowledgeError, claims.ClaimError)):
        msg = str(exc)
        code = 404 if msg.startswith("no such") else 422
        return JSONResponse({"error": msg}, status_code=code)
    return _com_error(exc)


@mcp.custom_route("/api/projects/{pid:int}/notes", methods=["GET"])
async def api_notes_list(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = knowledge.list_notes(conn, pid)
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/projects/{pid:int}/notes", methods=["POST"])
async def api_notes_create(request: Request) -> JSONResponse:
    pid = request.path_params["pid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        nid = knowledge.create_note(
            conn,
            actor,
            pid,
            data.get("title", ""),
            body=data.get("body", ""),
            tags=data.get("tags", ""),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": nid}, status_code=201)


@mcp.custom_route("/api/notes/{nid:int}", methods=["GET"])
async def api_note_get(request: Request) -> JSONResponse:
    nid = request.path_params["nid"]
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        note = dict(knowledge.get_note(conn, nid))
        revs = [dict(r) for r in knowledge.note_revisions(conn, nid)]
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse({"note": note, "revisions": revs})


@mcp.custom_route("/api/notes/{nid:int}", methods=["PATCH"])
async def api_note_edit(request: Request) -> JSONResponse:
    nid = request.path_params["nid"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        row = knowledge.edit_note(
            conn,
            actor,
            nid,
            title=data.get("title"),
            body=data.get("body"),
            tags=data.get("tags"),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(row))


@mcp.custom_route("/api/wiki", methods=["GET"])
async def api_wiki_list(request: Request) -> JSONResponse:
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = knowledge.list_wiki(conn)
    finally:
        conn.close()
    return JSONResponse([dict(r) for r in rows])


@mcp.custom_route("/api/wiki", methods=["POST"])
async def api_wiki_create(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        wid = knowledge.create_wiki(
            conn,
            actor,
            data.get("slug", ""),
            data.get("title", ""),
            body=data.get("body", ""),
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse({"id": wid}, status_code=201)


@mcp.custom_route("/api/wiki/{slug}", methods=["GET"])
async def api_wiki_get(request: Request) -> JSONResponse:
    slug = request.path_params["slug"]
    _actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        page = dict(knowledge.get_wiki(conn, slug))
        revs = [dict(r) for r in knowledge.wiki_revisions(conn, slug)]
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse({"page": page, "revisions": revs})


@mcp.custom_route("/api/wiki/{slug}", methods=["PATCH"])
async def api_wiki_edit(request: Request) -> JSONResponse:
    slug = request.path_params["slug"]
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        row = knowledge.edit_wiki(
            conn, actor, slug, title=data.get("title"), body=data.get("body")
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse(dict(row))


@mcp.custom_route("/api/claims", methods=["GET"])
async def api_claims_check(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    qp = request.query_params
    conn = _db()
    try:
        if qp.get("path"):
            rows = claims.check_claims(conn, qp["path"], actor)
        else:
            rows = [dict(r) for r in claims.list_my_claims(conn, actor)]
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse(rows)


@mcp.custom_route("/api/claims", methods=["POST"])
async def api_claims_set(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        count = claims.set_claims(
            conn, actor, data.get("paths"), note=data.get("note", "")
        )
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse({"claims": count}, status_code=201)


@mcp.custom_route("/api/claims/release", methods=["POST"])
async def api_claims_release(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        count = claims.release_claims(conn, actor, data.get("paths"))
    except Exception as exc:  # noqa: BLE001 — typed mapping below
        return _know_error(exc)
    finally:
        conn.close()
    return JSONResponse({"claims": count})


def _qint(qp, name):
    v = qp.get(name)
    return int(v) if v and v.isdigit() else None


@mcp.custom_route("/api/stream", methods=["GET"])
async def api_stream(request: Request) -> StreamingResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    qp = request.query_params
    f = realtime.Filters(
        obj_type=qp.get("type") or None,
        obj_id=_qint(qp, "id"),
        project_id=_qint(qp, "project"),
        verb=qp.get("verb") or None,
        to_identity_id=actor["id"] if qp.get("to_me") == "1" else _qint(qp, "to"),
    )
    since = _qint(qp, "since") or 0

    async def gen():
        from app.bus import bus as live_bus
        from app.bus import sse_frame

        conn = _db()
        try:
            backlog = events.feed(
                conn,
                since=since,
                project_id=f.project_id,
                obj_type=f.obj_type,
                obj_id=f.obj_id,
                to_identity_id=f.to_identity_id,
                limit=500,
            )
        finally:
            conn.close()
        last = since
        for ev in backlog:
            if f.matches(ev):
                last = max(last, ev["id"])
                yield sse_frame(ev)
        q = live_bus.subscribe()
        try:
            import asyncio as _a

            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await _a.wait_for(q.get(), realtime.STREAM_HEARTBEAT)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if ev["id"] <= last or not f.matches(ev):
                    continue
                last = ev["id"]
                yield sse_frame(ev)
        finally:
            live_bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@mcp.custom_route("/api/wait", methods=["GET"])
async def api_wait(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    qp = request.query_params
    try:
        timeout = float(qp.get("timeout", "10"))
    except ValueError:
        timeout = 10.0
    rows = await realtime.wait_for(
        actor["id"],
        obj_type=qp.get("type") or None,
        obj_id=_qint(qp, "id"),
        project_id=_qint(qp, "project"),
        verb=qp.get("verb") or None,
        to_me=qp.get("to_me") == "1",
        timeout=timeout,
        since=_qint(qp, "since") or 0,
        db_path=db.db_path(),
    )
    return JSONResponse({"events": rows})


@mcp.custom_route("/api/inbox", methods=["GET"])
async def api_inbox(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    conn = _db()
    try:
        rows = events.unread_for(
            conn, actor["id"], obj_type=request.query_params.get("type"), limit=200
        )
        for r in rows:
            r["read_at"] = None
    finally:
        conn.close()
    return JSONResponse(rows)


@mcp.custom_route("/api/inbox/read", methods=["POST"])
async def api_inbox_read(request: Request) -> JSONResponse:
    actor, err = _require_actor(request)
    if err:
        return err
    try:
        data = await _json_body(request)
    except _BadBody as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    conn = _db()
    try:
        ids = [int(i) for i in data.get("event_ids", []) if str(i).isdigit()][:500]
        n = events.mark_read(conn, actor["id"], ids)
    finally:
        conn.close()
    return JSONResponse({"read": n})


@mcp.custom_route("/api/events", methods=["GET"])
async def api_events_feed(request: Request) -> JSONResponse:
    _actor, err = _require_actor(request)
    if err:
        return err
    qp = request.query_params
    conn = _db()
    try:
        if qp.get("desc") == "1":
            # newest-first page for list UIs (feed/since stays ascending)
            rows = events.feed_recent(
                conn,
                project_id=_qint(qp, "project"),
                obj_type=qp.get("type") or None,
                obj_id=_qint(qp, "id"),
                to_identity_id=_qint(qp, "to"),
                limit=_qint(qp, "limit") or 100,
            )
        else:
            rows = events.feed(
                conn,
                since=_qint(qp, "since") or 0,
                project_id=_qint(qp, "project"),
                obj_type=qp.get("type") or None,
                obj_id=_qint(qp, "id"),
                to_identity_id=_qint(qp, "to"),
                limit=_qint(qp, "limit") or 100,
            )
    finally:
        conn.close()
    return JSONResponse(rows)


asgi_app = mcp.http_app(
    path="/mcp", middleware=[Middleware(IngressPath), Middleware(BearerIdentity)]
)


def main() -> None:
    """Entrypoint for the add-on (run.sh execs python3 -m app.main)."""
    bootstrap.ensure(db.db_path())
    host = os.environ.get("SLOPCLANKER_HOST", "0.0.0.0")  # nosec B104
    port = int(os.environ.get("SLOPCLANKER_PORT", "8090"))
    uvicorn.run(asgi_app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
