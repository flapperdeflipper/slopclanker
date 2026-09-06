"""MR/PR proof links — DESIGN §12.

Append-only for clankers, trashable by humans. `building -> review`
requires >=1 MR/PR link (kind mr/pr) unless a human waived it.
Status enrichment queries ONLY fixed provider API hosts with a
configured read-only token; no token = inert.
"""

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

from app import events, statemachine

PROVIDERS = ("github", "gitlab", "gitea", "other")
KINDS = ("mr", "pr", "commit", "issue", "other")

_GH_PR = re.compile(r"^https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?$")
_GH_ISSUE = re.compile(r"^https?://github\.com/([\w.-]+/[\w.-]+)/issues/(\d+)/?$")
_GH_COMMIT = re.compile(
    r"^https?://github\.com/([\w.-]+/[\w.-]+)/commit/([0-9a-f]{7,40})/?$"
)
_GL_MR = re.compile(
    r"^https?://gitlab\.com/([\w.-]+/[\w.-]+)/-/merge_requests/(\d+)/?$"
)
_GL_ISSUE = re.compile(r"^https?://gitlab\.com/([\w.-]+/[\w.-]+)/-/issues/(\d+)/?$")
_GL_COMMIT = re.compile(
    r"^https?://gitlab\.com/([\w.-]+/[\w.-]+)/-/commit/([0-9a-f]{7,40})/?$"
)


class ProofError(ValueError):
    """Proof validation/state failure."""


def parse_url(url: str) -> dict:
    """Map a forge URL to a structured ref; anything else = unverified."""
    u = (url or "").strip()
    for rx, provider, kind in (
        (_GH_PR, "github", "pr"),
        (_GH_ISSUE, "github", "issue"),
        (_GH_COMMIT, "github", "commit"),
        (_GL_MR, "gitlab", "mr"),
        (_GL_ISSUE, "gitlab", "issue"),
        (_GL_COMMIT, "gitlab", "commit"),
    ):
        m = rx.match(u)
        if m:
            return {
                "provider": provider,
                "repo": m.group(1),
                "number": m.group(2),
                "kind": kind,
            }
    return {"provider": "other", "repo": "", "number": "", "kind": "other"}


def _task(conn: sqlite3.Connection, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ProofError("no such task")
    return row


def add_proof(
    conn: sqlite3.Connection,
    actor: dict,
    task_id: int,
    url: str,
    *,
    provider: str | None = None,
    repo: str | None = None,
    number: str | None = None,
    kind: str | None = None,
) -> dict:
    """Attach a proof link. Explicit structured fields override URL parsing."""
    task = _task(conn, task_id)
    if statemachine.open_questions_on(conn, "task", task_id):
        raise ProofError("task is frozen by open questions")
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise ProofError("url required (max 2048 chars)")
    parsed = parse_url(url)
    out = {
        "provider": provider or parsed["provider"],
        "repo": repo if repo is not None else parsed["repo"],
        "number": number if number is not None else parsed["number"],
        "kind": kind or parsed["kind"],
    }
    if out["provider"] not in PROVIDERS or out["kind"] not in KINDS:
        raise ProofError("invalid provider or kind")
    if out["kind"] in ("mr", "pr", "issue", "commit") and out["provider"] == "other":
        raise ProofError("structured kind needs a provider")
    now = time.time()
    with conn:
        cur = conn.execute(
            "INSERT INTO proofs(task_id, provider, repo, number, kind, url,"
            " added_by, added_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                task_id,
                out["provider"],
                out["repo"],
                str(out["number"]),
                out["kind"],
                url.strip(),
                actor["id"],
                now,
            ),
        )
        pid = cur.lastrowid
        events.emit(
            conn,
            actor["id"],
            "proof.added",
            "proof",
            pid,
            project_id=task["project_id"],
            payload={"task_id": task_id, **out, "url": url.strip()},
        )
    return get_proof(conn, pid)


def get_proof(conn: sqlite3.Connection, proof_id: int) -> dict:
    row = conn.execute("SELECT * FROM proofs WHERE id = ?", (proof_id,)).fetchone()
    if row is None:
        raise ProofError("no such proof")
    return dict(row)


def list_proofs(conn: sqlite3.Connection, task_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM proofs WHERE task_id = ? AND trashed_at IS NULL ORDER BY id",
            (task_id,),
        )
    ]


def trash_proof(conn: sqlite3.Connection, actor: dict, proof_id: int) -> dict:
    """Humans only — clankers append, never remove."""
    row = conn.execute(
        "SELECT * FROM proofs WHERE id = ? AND trashed_at IS NULL", (proof_id,)
    ).fetchone()
    if row is None:
        raise ProofError("no such proof")
    if actor["kind"] != "human":
        raise ProofError("proof trash is human-only")
    task = _task(conn, row["task_id"])
    with conn:
        conn.execute(
            "UPDATE proofs SET trashed_at = ? WHERE id = ?",
            (time.time(), proof_id),
        )
        events.emit(
            conn,
            actor["id"],
            "proof.trashed",
            "proof",
            proof_id,
            project_id=task["project_id"],
            payload={"task_id": row["task_id"], "url": row["url"]},
        )
    return {**dict(row), "trashed_at": time.time()}


def _gitea_host() -> str:
    host = os.environ.get("SLOPCLANKER_GITEA_HOST", "").strip()
    if not host:
        raise ProofError("no Gitea host configured")
    return host.rstrip("/")


def _token(provider: str) -> str:
    tok = os.environ.get(f"SLOPCLANKER_{provider.upper()}_TOKEN", "").strip()
    if not tok:
        raise ProofError(f"no {provider} token configured — enrichment inert")
    return tok


def api_url(provider: str, repo: str, number: str, kind: str) -> str:
    """Fixed API hosts ONLY — never derived from user input (§12)."""
    if provider == "github":
        what = "issues" if kind == "issue" else "pulls"
        return f"https://api.github.com/repos/{repo}/{what}/{number}"
    if provider == "gitlab":
        proj = urllib.parse.quote(repo, safe="")
        what = "issues" if kind == "issue" else "merge_requests"
        return f"https://gitlab.com/api/v4/projects/{proj}/{what}/{number}"
    if provider == "gitea":
        what = "issues" if kind == "issue" else "pulls"
        return f"{_gitea_host()}/api/v1/repos/{repo}/{what}/{number}"
    raise ProofError("unverified proof — nothing to check")


def _http_json(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "slopclanker",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
        return json.loads(resp.read().decode())


def _map_state(provider: str, kind: str, data: dict) -> str:
    if kind == "commit":
        return "commit"
    if provider == "github":
        if kind == "pr" and data.get("merged"):
            return "merged"
        return str(data.get("state") or "unknown")
    if provider == "gitlab":
        s = str(data.get("state") or "unknown")
        return "open" if s == "opened" else s
    s = str(data.get("state") or "unknown")
    return "open" if s == "opened" else s


def check_proof(conn: sqlite3.Connection, proof_id: int, *, fetch=None) -> dict:
    """Refresh one proof's state from its provider. Inert without a token."""
    row = conn.execute(
        "SELECT * FROM proofs WHERE id = ? AND trashed_at IS NULL", (proof_id,)
    ).fetchone()
    if row is None:
        raise ProofError("no such proof")
    if row["provider"] == "other" or row["kind"] not in ("mr", "pr", "issue"):
        raise ProofError("nothing to check for this proof")
    url = api_url(row["provider"], row["repo"], row["number"], row["kind"])
    token = "" if fetch is not None else _token(row["provider"])
    try:
        data = (fetch or _http_json)(url, token)
    except urllib.error.URLError as exc:
        raise ProofError(f"provider unreachable: {exc}") from exc
    state = _map_state(row["provider"], row["kind"], data)
    now = time.time()
    task = _task(conn, row["task_id"])
    with conn:
        conn.execute(
            "UPDATE proofs SET state = ?, state_checked_at = ? WHERE id = ?",
            (state, now, proof_id),
        )
        events.emit(
            conn,
            _system_id(conn),
            "proof.checked",
            "proof",
            proof_id,
            project_id=task["project_id"],
            payload={"task_id": row["task_id"], "state": state},
        )
    return {**dict(row), "state": state, "state_checked_at": now}


def check_task(conn: sqlite3.Connection, task_id: int, *, fetch=None) -> list[dict]:
    """Check every checkable proof on a task; skip inert/invalid ones."""
    out = []
    for p in list_proofs(conn, task_id):
        if p["provider"] == "other" or p["kind"] not in ("mr", "pr", "issue"):
            continue
        try:
            out.append(check_proof(conn, p["id"], fetch=fetch))
        except ProofError:
            out.append(p)
    return out


def _system_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM identities WHERE role = 'superadmin' ORDER BY id LIMIT 1"
    ).fetchone()
    return row["id"] if row else 0
