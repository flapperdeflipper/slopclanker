"""Auth core: tokens, authenticate+stamping, login, accounts, proxy trust."""

import pytest
from helpers_ids import PW

from app import auth, db, setup
from app.middleware import client_ip


def _conn(tmp_path):
    return db.init_db(tmp_path / "t.db")


def test_agent_token_rotation(tmp_path):
    conn = _conn(tmp_path)
    setup.create_superadmin(conn, "boss", PW)
    conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('clanker-x','clanker','active',1.0)"
    )
    t1 = auth.mint_agent_token(conn, 2, 1)
    t2 = auth.mint_agent_token(conn, 2, 1)
    assert t1 != t2
    assert auth.authenticate(conn, t1) is None
    row = auth.authenticate(conn, t2)
    assert row["name"] == "clanker-x"
    conn.close()


def test_authenticate_stamps_usage(tmp_path):
    conn = _conn(tmp_path)
    setup.create_superadmin(conn, "boss", PW)
    token, _ = auth.mint_ui_session(conn, 1, ip="10.1.1.1", user_agent="pytest")
    auth.authenticate(conn, token, ip="10.1.1.1", user_agent="pytest")
    cred = conn.execute(
        "SELECT last_ip, last_user_agent, call_count FROM credentials"
        " WHERE kind = 'ui_session'"
    ).fetchone()
    assert cred["last_ip"] == "10.1.1.1"
    assert cred["call_count"] == 1
    conn.close()


def test_authenticate_rejects_bad_states(tmp_path):
    conn = _conn(tmp_path)
    setup.create_superadmin(conn, "boss", PW)
    conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('clanker-x','clanker','active',1.0)"
    )
    token = auth.mint_agent_token(conn, 2, 1)
    assert auth.authenticate(conn, "garbage-token") is None
    assert auth.authenticate(conn, "") is None
    conn.execute("UPDATE identities SET status='revoked' WHERE id=2")
    conn.commit()
    assert auth.authenticate(conn, token) is None
    conn.execute("UPDATE identities SET status='active' WHERE id=2")
    conn.execute("UPDATE credentials SET revoked_at=1.0 WHERE id=2")
    conn.commit()
    assert auth.authenticate(conn, token) is None
    conn.close()


def test_ui_session_expiry(tmp_path):
    conn = _conn(tmp_path)
    setup.create_superadmin(conn, "boss", PW)
    token, expires = auth.mint_ui_session(conn, 1)
    assert auth.authenticate(conn, token) is not None
    conn.execute("UPDATE credentials SET expires_at = 1.0 WHERE kind='ui_session'")
    conn.commit()
    assert auth.authenticate(conn, token) is None
    assert expires > 1.0
    conn.close()


def test_login_and_wrong_password(tmp_path):
    conn = _conn(tmp_path)
    setup.create_superadmin(conn, "boss", PW)
    row, token, _ = auth.login(conn, "boss", PW)
    assert row["name"] == "boss"
    assert auth.authenticate(conn, token)["name"] == "boss"
    with pytest.raises(auth.AuthError):
        auth.login(conn, "boss", "wrong-password")
    with pytest.raises(auth.AuthError):
        auth.login(conn, "ghost", PW)
    conn.close()


def test_create_human_guards(tmp_path):
    conn = _conn(tmp_path)
    boss = setup.create_superadmin(conn, "boss", PW)
    row = auth.create_human(conn, "helper", "anotherlongone", "user", boss["id"])
    assert row["role"] == "user"
    with pytest.raises(setup.SetupError):
        auth.create_human(conn, "helper2", "short", "user", boss["id"])
    with pytest.raises(setup.SetupError):
        auth.create_human(conn, "Bad!", "anotherlongone", "user", boss["id"])
    with pytest.raises(setup.SetupError):
        auth.create_human(conn, "helper3", "anotherlongone", "wizard", boss["id"])
    conn.close()


def test_revoke_identity_kills_credentials(tmp_path):
    conn = _conn(tmp_path)
    setup.create_superadmin(conn, "boss", PW)
    conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('clanker-x','clanker','active',1.0)"
    )
    token = auth.mint_agent_token(conn, 2, 1)
    auth.revoke_identity(conn, 2)
    assert auth.authenticate(conn, token) is None
    status = conn.execute("SELECT status FROM identities WHERE id=2").fetchone()[0]
    assert status == "revoked"
    conn.close()


def test_client_ip_proxy_trust():
    assert client_ip("10.20.0.5", "1.2.3.4") == "10.20.0.5"
    import os

    old = os.environ.get("SLOPCLANKER_TRUSTED_PROXY")
    try:
        os.environ["SLOPCLANKER_TRUSTED_PROXY"] = "172.30.32.0/24"
        assert client_ip("172.30.32.1", "203.0.113.9, 172.30.32.1") == "203.0.113.9"
        assert client_ip("10.20.0.5", "1.2.3.4") == "10.20.0.5"
    finally:
        if old is None:
            os.environ.pop("SLOPCLANKER_TRUSTED_PROXY", None)
        else:
            os.environ["SLOPCLANKER_TRUSTED_PROXY"] = old
