"""Account management: change/reset password, role changes, prefs landing."""

import pytest
from helpers_ids import PW

from app import auth, bootstrap, db, objects, setup


def _db2(tmp_path):
    path = tmp_path / "t.db"
    bootstrap.ensure(path)
    return db.connect(path)


def _boss(conn):
    return setup.create_superadmin(conn, "root", PW)


def test_change_password_roundtrip(tmp_path):
    conn = _db2(tmp_path)
    boss = _boss(conn)
    me = dict(boss)
    with pytest.raises(auth.AuthError):
        auth.change_password(conn, me, "wrong", "newpassword99")
    auth.change_password(conn, me, PW, "newpassword99")
    row, tok, _ = auth.login(conn, "root", "newpassword99")
    assert row["id"] == boss["id"] and tok
    conn.close()


def test_clanker_cannot_change_password(tmp_path):
    conn = _db2(tmp_path)
    cur = conn.execute(
        "INSERT INTO identities(name, kind, status, created_at)"
        " VALUES ('bot', 'clanker', 'active', 1.0)"
    )
    conn.commit()
    with pytest.raises(auth.AuthError):
        auth.change_password(conn, {"id": cur.lastrowid}, "x", "y" * 20)
    conn.close()


def test_reset_password_permissions(tmp_path):
    conn = _db2(tmp_path)
    boss = _boss(conn)
    admin = auth.create_human(conn, "admin1", PW, "admin", boss["id"])
    user = auth.create_human(conn, "user1", PW, "user", boss["id"])
    # admin resets user: ok
    auth.reset_password(conn, dict(admin), user["id"], "reset-password-9")
    _, tok, _ = auth.login(conn, "user1", "reset-password-9")
    assert tok
    # admin cannot reset the superadmin
    with pytest.raises(auth.AuthError):
        auth.reset_password(conn, dict(admin), boss["id"], "hack-password-99")
    # weak password rejected
    with pytest.raises(setup.WeakPassword):
        auth.reset_password(conn, dict(boss), user["id"], "short")
    conn.close()


def test_role_changes_and_single_superadmin(tmp_path):
    conn = _db2(tmp_path)
    boss = _boss(conn)
    admin = auth.create_human(conn, "admin1", PW, "admin", boss["id"])
    user = auth.create_human(conn, "user1", PW, "user", boss["id"])
    # nobody can grant superadmin (schema: exactly one)
    with pytest.raises(auth.AuthError):
        auth.set_role(conn, dict(boss), admin["id"], "superadmin")
    # the superadmin role itself never changes
    with pytest.raises(auth.AuthError):
        auth.set_role(conn, dict(boss), boss["id"], "user")
    # superadmin moves users between user/admin
    r = auth.set_role(conn, dict(boss), user["id"], "admin")
    assert r["role"] == "admin"
    auth.set_role(conn, dict(boss), user["id"], "user")
    conn.close()


def test_default_project_pref_beats_stack(tmp_path):
    conn = _db2(tmp_path)
    boss = _boss(conn)
    sid = objects.create_stack(conn, boss, "S1")
    objects.create_project(conn, boss, name="One", stack_id=sid)
    p2 = objects.create_project(conn, boss, name="Two", stack_id=sid)
    from app import prefs

    prefs.set(conn, boss["id"], "default_stack", str(sid))
    prefs.set(conn, boss["id"], "default_project", str(p2))
    allp = prefs.get_all(conn, boss["id"])
    assert allp["default_project"] == str(p2)
    assert int(allp["default_project"]) == p2
    conn.close()
