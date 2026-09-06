"""Registration pipeline: request -> approve -> poll/enroll delivery."""

import pytest
from helpers_ids import PW

from app import auth, db, registry, setup


def _admin(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    boss = setup.create_superadmin(conn, "boss", PW)
    return conn, boss


CLAIM = "claim-secret-0123456789-abcdef"


def test_register_and_duplicate_names(tmp_path):
    conn, _boss = _admin(tmp_path)
    rid = registry.register_request(
        conn, "clanker-a", "builds", CLAIM, "10.0.0.9", "ua"
    )
    assert rid == 1
    with pytest.raises(registry.NameTaken):
        registry.register_request(conn, "clanker-a", "", "x" * 20, "10.0.0.9", "ua")
    with pytest.raises(setup.InvalidName):
        registry.register_request(conn, "Bad Name", "", CLAIM, None, None)
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 1
    conn.close()


def test_approve_creates_identity_and_code(tmp_path):
    conn, boss = _admin(tmp_path)
    rid = registry.register_request(conn, "clanker-a", "note", CLAIM, "10.0.0.9", "ua")
    result = registry.approve(conn, rid, boss["id"])
    ident = conn.execute("SELECT * FROM identities WHERE name='clanker-a'").fetchone()
    assert ident["kind"] == "clanker"
    assert ident["status"] == "active"
    assert ident["reg_ip"] == "10.0.0.9"
    assert ident["approved_by"] == boss["id"]
    assert result["identity_id"] == ident["id"]
    token = registry.enroll(conn, result["code"], ip="10.0.0.9")
    assert auth.authenticate(conn, token)["name"] == "clanker-a"
    conn.close()


def test_approve_only_pending(tmp_path):
    conn, boss = _admin(tmp_path)
    rid = registry.register_request(conn, "clanker-a", "", CLAIM, None, None)
    registry.approve(conn, rid, boss["id"])
    with pytest.raises(registry.InvalidState):
        registry.approve(conn, rid, boss["id"])
    rid2 = registry.register_request(conn, "clanker-b", "", CLAIM, None, None)
    registry.reject(conn, rid2, boss["id"])
    with pytest.raises(registry.InvalidState):
        registry.approve(conn, rid2, boss["id"])
    conn.close()


def test_poll_delivery_once(tmp_path):
    conn, boss = _admin(tmp_path)
    rid = registry.register_request(conn, "clanker-a", "", CLAIM, "10.0.0.9", "ua")
    assert registry.poll(conn, rid, CLAIM)["status"] == "pending"
    with pytest.raises(registry.WrongClaim):
        registry.poll(conn, rid, "wrong-claim-secret")
    registry.approve(conn, rid, boss["id"])
    first = registry.poll(conn, rid, CLAIM)
    assert first["status"] == "delivered"
    assert auth.authenticate(conn, first["token"])["name"] == "clanker-a"
    second = registry.poll(conn, rid, CLAIM)
    assert second["status"] == "delivered"
    assert second["token"] is None
    conn.close()


def test_pending_expires(tmp_path):
    conn, boss = _admin(tmp_path)
    rid = registry.register_request(conn, "clanker-a", "", CLAIM, None, None)
    conn.execute(
        "UPDATE registrations SET created_at = created_at - 15*86400 WHERE id = ?",
        (rid,),
    )
    conn.commit()
    assert registry.poll(conn, rid, CLAIM)["status"] == "expired"
    with pytest.raises(registry.InvalidState):
        registry.approve(conn, rid, boss["id"])
    conn.close()


def test_enroll_code_single_use_and_expiry(tmp_path):
    conn, boss = _admin(tmp_path)
    rid = registry.register_request(conn, "clanker-a", "", CLAIM, None, None)
    result = registry.approve(conn, rid, boss["id"])
    token1 = registry.enroll(conn, result["code"])
    with pytest.raises(registry.InvalidState):
        registry.enroll(conn, result["code"])
    code2, _ = registry.issue_code(conn, result["identity_id"], boss["id"])
    conn.execute("UPDATE enrollment_codes SET expires_at = 1.0 WHERE used_at IS NULL")
    conn.commit()
    with pytest.raises(registry.InvalidState):
        registry.enroll(conn, code2)
    assert auth.authenticate(conn, token1)["name"] == "clanker-a"
    conn.close()


def test_enroll_rotates_prior_token(tmp_path):
    conn, boss = _admin(tmp_path)
    rid = registry.register_request(conn, "clanker-a", "", CLAIM, None, None)
    result = registry.approve(conn, rid, boss["id"])
    token1 = registry.enroll(conn, result["code"])
    assert auth.authenticate(conn, token1) is not None
    code2, _ = registry.issue_code(conn, result["identity_id"], boss["id"])
    token2 = registry.enroll(conn, code2)
    assert auth.authenticate(conn, token1) is None
    assert auth.authenticate(conn, token2)["name"] == "clanker-a"
    conn.close()


def test_reenroll_request_notifies(tmp_path):
    conn, boss = _admin(tmp_path)
    rid = registry.register_request(conn, "clanker-a", "", CLAIM, None, None)
    registry.approve(conn, rid, boss["id"])
    registry.reenroll_request(conn, "clanker-a", "10.0.0.9")
    rows = conn.execute(
        "SELECT kind FROM notifications WHERE kind='reenroll_request'"
    ).fetchall()
    assert len(rows) == 1
    registry.reenroll_request(conn, "ghost", "10.0.0.9")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE kind='reenroll_request'"
        ).fetchone()[0]
        == 1
    )
    conn.close()
