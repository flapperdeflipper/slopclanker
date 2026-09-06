"""First-run setup: creating the single superadmin account.

The wizard is open exactly until one human identity exists; after that
it is locked forever (meta.setup_done). Passwords hash with Argon2id.
"""

import re
import sqlite3
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
MIN_PASSWORD_LEN = 10

_ph = PasswordHasher()


class SetupError(ValueError):
    """Base setup failure."""


class SetupComplete(SetupError):
    """The superadmin already exists; the wizard is closed."""


class InvalidName(SetupError):
    """Username fails the naming rules."""


class WeakPassword(SetupError):
    """Password below the minimum length."""


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(secret_hash: str, password: str) -> bool:
    try:
        return _ph.verify(secret_hash, password)
    except (InvalidHashError, VerificationError):
        return False


def humans_exist(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT 1 FROM identities WHERE kind='human' LIMIT 1").fetchone()
    return row is not None


def setup_required(conn: sqlite3.Connection) -> bool:
    return not humans_exist(conn)


def create_superadmin(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> sqlite3.Row:
    """Create the one superadmin; single transaction; raises SetupError."""
    if humans_exist(conn):
        raise SetupComplete("setup already done")
    if not isinstance(username, str) or not NAME_RE.match(username):
        raise InvalidName("username must match [a-z0-9][a-z0-9_-]{1,31}")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise WeakPassword(f"password must be at least {MIN_PASSWORD_LEN} chars")

    now = time.time()
    with conn:
        cur = conn.execute(
            "INSERT INTO identities"
            "(name, kind, role, status, created_at, reg_ip, reg_user_agent)"
            " VALUES (?,?,?,?,?,?,?)",
            (username, "human", "superadmin", "active", now, ip, user_agent),
        )
        uid = cur.lastrowid
        conn.execute(
            "INSERT INTO credentials"
            "(identity_id, kind, label, secret_hash, issued_by, issued_at)"
            " VALUES (?,?,?,?,?,?)",
            (uid, "password", "setup", hash_password(password), uid, now),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('setup_done', ?)", (str(now),)
        )
    return conn.execute("SELECT * FROM identities WHERE id = ?", (uid,)).fetchone()
