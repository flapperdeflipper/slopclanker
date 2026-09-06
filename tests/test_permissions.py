"""Permission engine: the DESIGN §4 matrix, negatives proven."""

from helpers_ids import PW

from app import auth, db, setup
from app.permissions import (
    APPROVE_REGISTRATION,
    CREATE_USER,
    ISSUE_CODE,
    REJECT_REGISTRATION,
    REVOKE_CREDENTIAL,
    REVOKE_IDENTITY,
    VIEW_IDENTITIES,
    can,
)

ALL_ADMIN_ACTIONS = (
    APPROVE_REGISTRATION,
    REJECT_REGISTRATION,
    VIEW_IDENTITIES,
    REVOKE_IDENTITY,
    ISSUE_CODE,
    CREATE_USER,
    REVOKE_CREDENTIAL,
)


def _conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


def _human(conn, name, role):
    founder = conn.execute("SELECT id FROM identities WHERE kind='human'").fetchone()
    if founder is None:
        if role == "superadmin":
            return setup.create_superadmin(conn, name, PW)
        boss = setup.create_superadmin(conn, "founder", PW)
        return auth.create_human(conn, name, PW, role, boss["id"])
    if role == "superadmin":
        raise AssertionError("superadmin already exists in this db")
    return auth.create_human(conn, name, PW, role, founder["id"])


def _clanker(conn):
    cur = conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('clanker-x','clanker','active',1.0)"
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM identities WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


def test_clanker_cannot_do_any_admin_action(tmp_path):
    conn = _conn(tmp_path)
    actor = _clanker(conn)
    for action in ALL_ADMIN_ACTIONS:
        assert can(actor, action) is False
    conn.close()


def test_regular_user_cannot_admin(tmp_path):
    conn = _conn(tmp_path)
    actor = _human(conn, "plainuser", "user")
    for action in ALL_ADMIN_ACTIONS:
        if action != REVOKE_CREDENTIAL:
            assert can(actor, action) is False
    assert can(actor, REVOKE_CREDENTIAL, {"identity_id": actor["id"] + 99}) is False
    assert can(actor, REVOKE_CREDENTIAL, {"identity_id": actor["id"]}) is True
    conn.close()


def test_admin_matrix(tmp_path):
    conn = _conn(tmp_path)
    actor = _human(conn, "theadmin", "admin")
    assert can(actor, APPROVE_REGISTRATION) is True
    assert can(actor, REJECT_REGISTRATION) is True
    assert can(actor, VIEW_IDENTITIES) is True
    assert can(actor, ISSUE_CODE) is True
    assert can(actor, REVOKE_CREDENTIAL, {"identity_id": 12345}) is True
    assert can(actor, CREATE_USER, {"role": "user"}) is True
    assert can(actor, CREATE_USER, {"role": "admin"}) is False
    assert can(actor, REVOKE_IDENTITY, {"target_kind": "clanker"}) is True
    assert can(actor, REVOKE_IDENTITY, {"target_kind": "human"}) is False
    conn.close()


def test_superadmin_matrix(tmp_path):
    conn = _conn(tmp_path)
    actor = _human(conn, "root", "superadmin")
    assert can(actor, CREATE_USER, {"role": "admin"}) is True
    assert can(actor, REVOKE_IDENTITY, {"target_kind": "human"}) is True
    assert can(actor, REVOKE_IDENTITY, {"target_role": "superadmin"}) is False
    conn.close()


def test_revoked_and_none_actors_rejected(tmp_path):
    conn = _conn(tmp_path)
    actor = _human(conn, "theadmin", "admin")
    conn.execute("UPDATE identities SET status='revoked' WHERE id=?", (actor["id"],))
    conn.commit()
    fresh = conn.execute(
        "SELECT * FROM identities WHERE id = ?", (actor["id"],)
    ).fetchone()
    assert can(fresh, APPROVE_REGISTRATION) is False
    assert can(None, APPROVE_REGISTRATION) is False
    conn.close()
