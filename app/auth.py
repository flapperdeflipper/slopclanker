"""Credential services: tokens, authentication, usage stamping, accounts.

Tokens are >=256-bit url-safe secrets; only their SHA-256 hashes are
stored. Agent tokens: one active per identity (rotation on mint).
Human UI sessions: 12h expiry. Passwords: Argon2id (via app.setup).
"""

import hashlib
import secrets
import sqlite3
import time

from app import setup

UI_SESSION_TTL = 12 * 3600
AGENT_TOKEN_KINDS = ("agent_token", "ui_session")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def mint_ui_session(
    conn: sqlite3.Connection,
    identity_id: int,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, float]:
    """Mint a 12h UI session token; expired sessions are reaped first."""
    now = time.time()
    conn.execute(
        "UPDATE credentials SET revoked_at = ?"
        " WHERE identity_id = ? AND kind = 'ui_session'"
        " AND revoked_at IS NULL AND expires_at IS NOT NULL AND expires_at < ?",
        (now, identity_id, now),
    )
    token = new_secret()
    expires = now + UI_SESSION_TTL
    conn.execute(
        "INSERT INTO credentials"
        "(identity_id, kind, label, secret_hash, issued_by, issued_at, expires_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (identity_id, "ui_session", "web", _sha256(token), identity_id, now, expires),
    )
    conn.commit()
    return token, expires


def mint_agent_token(
    conn: sqlite3.Connection,
    identity_id: int,
    issued_by: int,
    label: str = "primary",
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """Mint the identity's agent token; single-token policy rotates any prior."""
    now = time.time()
    conn.execute(
        "UPDATE credentials SET revoked_at = ?"
        " WHERE identity_id = ? AND kind = 'agent_token' AND revoked_at IS NULL",
        (now, identity_id),
    )
    token = new_secret()
    conn.execute(
        "INSERT INTO credentials"
        "(identity_id, kind, label, secret_hash, issued_by, issued_at)"
        " VALUES (?,?,?,?,?,?)",
        (identity_id, "agent_token", label, _sha256(token), issued_by, now),
    )
    conn.commit()
    return token


def authenticate(
    conn: sqlite3.Connection,
    token: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> sqlite3.Row | None:
    """Resolve a bearer token to an active identity; stamps credential usage."""
    if not token or len(token) > 512:
        return None
    row = conn.execute(
        "SELECT i.id, i.name, i.kind, i.role, i.status, c.id AS cred_id,"
        "       c.kind AS cred_kind, c.expires_at AS cred_expires"
        "  FROM credentials c JOIN identities i ON i.id = c.identity_id"
        " WHERE c.kind IN ('agent_token','ui_session')"
        "   AND c.revoked_at IS NULL AND c.secret_hash = ?",
        (_sha256(token),),
    ).fetchone()
    now = time.time()
    if row is None or row["status"] != "active":
        return None
    if row["cred_expires"] is not None and row["cred_expires"] < now:
        return None
    conn.execute(
        "UPDATE credentials SET last_seen_at = ?, last_ip = ?,"
        " last_user_agent = ?, call_count = call_count + 1 WHERE id = ?",
        (now, ip, user_agent, row["cred_id"]),
    )
    conn.commit()
    return row


def login(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[sqlite3.Row, str, float]:
    """Password login for humans; raises AuthError on failure."""
    row = conn.execute(
        "SELECT i.*, c.secret_hash FROM identities i"
        " JOIN credentials c ON c.identity_id = i.id"
        " WHERE i.name = ? AND i.kind = 'human' AND i.status = 'active'"
        "   AND c.kind = 'password' AND c.revoked_at IS NULL",
        (username,),
    ).fetchone()
    if row is None or not setup.verify_password(row["secret_hash"], password):
        raise AuthError("invalid credentials")
    token, expires = mint_ui_session(conn, row["id"], ip, user_agent)
    return row, token, expires


class AuthError(ValueError):
    """Login or account failure."""


def create_human(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    created_by: int,
) -> sqlite3.Row:
    """Admin-created human account (role user|admin)."""
    if role not in ("admin", "user"):
        raise setup.SetupError("role must be 'admin' or 'user'")
    if not setup.NAME_RE.match(username or ""):
        raise setup.InvalidName("username must match [a-z0-9][a-z0-9_-]{1,31}")
    if not isinstance(password, str) or len(password) < setup.MIN_PASSWORD_LEN:
        raise setup.WeakPassword(
            f"password must be at least {setup.MIN_PASSWORD_LEN} chars"
        )
    now = time.time()
    with conn:
        cur = conn.execute(
            "INSERT INTO identities"
            "(name, kind, role, status, created_by, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (username, "human", role, "active", created_by, now),
        )
        uid = cur.lastrowid
        conn.execute(
            "INSERT INTO credentials"
            "(identity_id, kind, label, secret_hash, issued_by, issued_at)"
            " VALUES (?,?,?,?,?,?)",
            (
                uid,
                "password",
                "account",
                setup.hash_password(password),
                created_by,
                now,
            ),
        )
    return conn.execute("SELECT * FROM identities WHERE id = ?", (uid,)).fetchone()


def revoke_identity(conn: sqlite3.Connection, identity_id: int) -> None:
    """Revoke an identity and every credential it holds."""
    now = time.time()
    with conn:
        conn.execute(
            "UPDATE identities SET status = 'revoked' WHERE id = ?", (identity_id,)
        )
        conn.execute(
            "UPDATE credentials SET revoked_at = ? WHERE identity_id = ?"
            " AND revoked_at IS NULL",
            (now, identity_id),
        )


def revoke_credential(conn: sqlite3.Connection, credential_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE credentials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (time.time(), credential_id),
        )
