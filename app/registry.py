"""Clanker registration pipeline: request -> human approval -> token.

Two delivery paths, both human-gated: the live registering process polls
with its one-time claim-secret and receives the token instantly; or the
approver hands the one-time enrollment code to the agent out-of-band.
Enrollment rotates any prior agent token (single-token policy).
"""

import hashlib
import sqlite3
import time

from app import auth, notify, setup

CODE_TTL = 3600.0
PENDING_TTL = 14 * 86400.0


class RegistryError(ValueError):
    """Base registration failure."""


class NameTaken(RegistryError):
    """Name exists as identity or live registration."""


class NotFound(RegistryError):
    """Unknown request/code."""


class WrongClaim(RegistryError):
    """Claim secret does not match the registering process."""


class InvalidState(RegistryError):
    """Request is not in a state that allows this action."""


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _lazy_expire(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE registrations SET status = 'expired'"
        " WHERE status = 'pending' AND created_at < ?",
        (time.time() - PENDING_TTL,),
    )


def register_request(
    conn: sqlite3.Connection,
    name: str,
    note: str,
    claim_secret: str,
    ip: str | None,
    user_agent: str | None,
) -> int:
    """Queue a pending registration; notifies admins."""
    if not setup.NAME_RE.match(name or ""):
        raise setup.InvalidName("name must match [a-z0-9][a-z0-9_-]{1,31}")
    if not isinstance(claim_secret, str) or len(claim_secret) < 16:
        raise RegistryError("claim_secret must be at least 16 characters")
    if conn.execute("SELECT 1 FROM identities WHERE name = ?", (name,)).fetchone():
        raise NameTaken(f"'{name}' already exists as an identity")
    live = conn.execute(
        "SELECT 1 FROM registrations WHERE name = ?"
        " AND status IN ('pending','approved')",
        (name,),
    ).fetchone()
    if live:
        raise NameTaken(f"'{name}' already has a live registration")
    _lazy_expire(conn)
    cur = conn.execute(
        "INSERT INTO registrations"
        "(name, note, claim_hash, ip, user_agent, status, created_at)"
        " VALUES (?,?,?,?,?,'pending',?)",
        (name, note or "", _sha256(claim_secret), ip, user_agent, time.time()),
    )
    conn.commit()
    notify.notify(
        conn,
        "registration_pending",
        f"clanker '{name}' requests enrollment (from {ip or 'unknown'})",
    )
    return cur.lastrowid


def poll(
    conn: sqlite3.Connection,
    request_id: int,
    claim_secret: str,
    ip: str | None = None,
) -> dict:
    """Poll own request; delivers the token once when approved live."""
    _lazy_expire(conn)
    row = conn.execute(
        "SELECT * FROM registrations WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise NotFound("no such registration")
    if row["claim_hash"] != _sha256(claim_secret or ""):
        raise WrongClaim("claim secret mismatch")
    if row["status"] == "approved" and row["delivered_at"] is None:
        token = auth.mint_agent_token(
            conn, row["identity_id"], row["decided_by"], label=f"reg-{row['id']}"
        )
        now = time.time()
        conn.execute(
            "UPDATE registrations SET status = 'delivered', delivered_at = ?,"
            " delivered_ip = ? WHERE id = ?",
            (now, ip, row["id"]),
        )
        conn.commit()
        return {"status": "delivered", "name": row["name"], "token": token}
    return {"status": row["status"], "name": row["name"], "token": None}  # nosec B105 - sentinel None, not a password


def approve(conn: sqlite3.Connection, registration_id: int, approver_id: int) -> dict:
    """Approve a pending request: creates the identity + enrollment code."""
    _lazy_expire(conn)
    row = conn.execute(
        "SELECT * FROM registrations WHERE id = ?", (registration_id,)
    ).fetchone()
    if row is None:
        raise NotFound("no such registration")
    if row["status"] != "pending":
        raise InvalidState(f"registration is {row['status']}, not pending")
    now = time.time()
    with conn:
        cur = conn.execute(
            "INSERT INTO identities"
            "(name, kind, status, note, created_at, reg_ip, reg_user_agent,"
            " approved_by, approved_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row["name"],
                "clanker",
                "active",
                row["note"],
                now,
                row["ip"],
                row["user_agent"],
                approver_id,
                now,
            ),
        )
        identity_id = cur.lastrowid
        conn.execute(
            "UPDATE registrations SET status = 'approved', identity_id = ?,"
            " decided_by = ?, decided_at = ? WHERE id = ?",
            (identity_id, approver_id, now, registration_id),
        )
    code, expires = issue_code(conn, identity_id, approver_id)
    return {
        "identity_id": identity_id,
        "name": row["name"],
        "code": code,
        "expires_at": expires,
    }


def reject(conn: sqlite3.Connection, registration_id: int, rejector_id: int) -> None:
    row = conn.execute(
        "SELECT status FROM registrations WHERE id = ?", (registration_id,)
    ).fetchone()
    if row is None:
        raise NotFound("no such registration")
    if row["status"] != "pending":
        raise InvalidState(f"registration is {row['status']}, not pending")
    with conn:
        conn.execute(
            "UPDATE registrations SET status = 'rejected', decided_by = ?,"
            " decided_at = ? WHERE id = ?",
            (rejector_id, time.time(), registration_id),
        )


def issue_code(
    conn: sqlite3.Connection, identity_id: int, issued_by: int
) -> tuple[str, float]:
    """Fresh one-time enrollment code; outstanding ones are invalidated."""
    now = time.time()
    with conn:
        conn.execute(
            "UPDATE enrollment_codes SET used_at = ? WHERE identity_id = ?"
            " AND used_at IS NULL",
            (now, identity_id),
        )
        code = auth.new_secret(24)
        expires = now + CODE_TTL
        conn.execute(
            "INSERT INTO enrollment_codes"
            "(code_hash, identity_id, issued_by, created_at, expires_at)"
            " VALUES (?,?,?,?,?)",
            (_sha256(code), identity_id, issued_by, now, expires),
        )
    return code, expires


def enroll(conn: sqlite3.Connection, code: str, ip: str | None = None) -> str:
    """Redeem a one-time enrollment code for the identity's agent token."""
    if not isinstance(code, str) or not code:
        raise InvalidState("code required")
    row = conn.execute(
        "SELECT * FROM enrollment_codes WHERE code_hash = ? AND used_at IS NULL",
        (_sha256(code),),
    ).fetchone()
    if row is None:
        raise InvalidState("invalid or already-used code")
    if row["expires_at"] < time.time():
        raise InvalidState("code expired")
    ident = conn.execute(
        "SELECT * FROM identities WHERE id = ?", (row["identity_id"],)
    ).fetchone()
    if ident is None or ident["status"] != "active":
        raise InvalidState("identity is not active")
    token = auth.mint_agent_token(
        conn, ident["id"], row["issued_by"], label=f"code-{row['id']}"
    )
    now = time.time()
    with conn:
        conn.execute(
            "UPDATE enrollment_codes SET used_at = ?, used_ip = ? WHERE id = ?",
            (now, ip, row["id"]),
        )
        conn.execute(
            "UPDATE registrations SET status = 'delivered', delivered_at = ?"
            " WHERE identity_id = ? AND status = 'approved'",
            (now, ident["id"]),
        )
    return token


def reenroll_request(conn: sqlite3.Connection, name: str, ip: str | None) -> None:
    """A keyless agent asks for a fresh code; admins get a notification."""
    row = conn.execute(
        "SELECT * FROM identities WHERE name = ? AND kind = 'clanker'"
        " AND status = 'active'",
        (name,),
    ).fetchone()
    if row is not None:
        notify.notify(
            conn,
            "reenroll_request",
            f"clanker '{name}' lost its token and requests re-enrollment"
            f" (from {ip or 'unknown'})",
        )
