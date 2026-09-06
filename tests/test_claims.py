"""Claims + presence: advisory coordination, staleness via heartbeats."""

import pytest
from helpers_ids import clanker, fresh_db, superadmin

from app import auth, claims


@pytest.fixture
def env(tmp_path):
    conn = fresh_db(tmp_path)
    boss = superadmin(conn)
    agent = clanker(conn)
    other = clanker(conn, "clanker-y")
    return conn, boss, agent, other


def _beat(conn, identity_id):
    conn.execute(
        "UPDATE credentials SET last_seen_at = strftime('%s','now')"
        " WHERE identity_id = ?",
        (identity_id,),
    )
    conn.commit()


def test_set_check_release(env):
    conn, _boss, agent, other = env
    _beat(conn, agent["id"])
    assert (
        claims.set_claims(conn, agent, ["/src/app.py", "/docs/"], note="refactor") == 2
    )
    assert claims.set_claims(conn, agent, ["/src/app.py"], note="refreshed") == 2
    row = conn.execute("SELECT note FROM claims WHERE path = '/src/app.py'").fetchone()
    assert row["note"] == "refreshed"
    claims.set_claims(conn, other, ["/src/deep/thing.py"])
    hits = claims.check_claims(conn, "/src/app.py", other)
    assert len(hits) == 1 and hits[0]["name"] == "clanker-x"
    assert claims.check_claims(conn, "/src/app.py", agent) == []
    parent = claims.check_claims(conn, "/src/deep", agent)
    assert len(parent) == 1 and parent[0]["path"] == "/src/deep/thing.py"
    sibling = claims.check_claims(conn, "/src/deep/x.py", agent)
    assert sibling == []
    assert claims.check_claims(conn, "/other", agent) == []
    assert claims.release_claims(conn, agent, ["/src/app.py"]) == 1
    assert claims.check_claims(conn, "/src/app.py", other) == []
    conn.close()


def test_staleness(env):
    conn, boss, agent, other = env
    auth.mint_agent_token(conn, agent["id"], boss["id"])
    claims.set_claims(conn, agent, ["/stale/path"])
    hits = claims.check_claims(conn, "/stale/path", other)
    assert len(hits) == 1 and hits[0]["stale"] is True
    _beat(conn, agent["id"])
    hits = claims.check_claims(conn, "/stale/path", other)
    assert hits[0]["stale"] is False
    conn.close()


def test_path_validation(env):
    conn, _boss, agent, _other = env
    with pytest.raises(claims.ClaimError):
        claims.set_claims(conn, agent, ["relative/path"])
    with pytest.raises(claims.ClaimError):
        claims.set_claims(conn, agent, [])
    with pytest.raises(claims.ClaimError):
        claims.set_claims(conn, agent, ["/ok"], note="x" * 600)
    conn.close()


def test_presence_from_credentials(env):
    conn, boss, agent, _other = env
    assert claims.presence(conn, agent["id"]) is None
    tok = auth.mint_agent_token(conn, agent["id"], boss["id"])
    auth.authenticate(conn, tok)
    assert claims.presence(conn, agent["id"]) is not None
    conn.close()
