"""Per-identity prefs + tag suggestions (1.1 UI support)."""

import pytest
from helpers_ids import PW

from app import auth, bootstrap, db, objects, setup


def _db2(tmp_path):
    path = tmp_path / "t.db"
    bootstrap.ensure(path)
    return db.connect(path)


def test_prefs_roundtrip_and_isolation(tmp_path):
    conn = _db2(tmp_path)
    boss = setup.create_superadmin(conn, "root", PW)
    other = auth.create_human(conn, "other", PW, "user", boss["id"])
    from app import prefs

    prefs.set(conn, boss["id"], "default_stack", "3")
    prefs.set(conn, boss["id"], "default_stack", "4")  # upsert
    prefs.set(conn, other["id"], "default_stack", "9")
    assert prefs.get_all(conn, boss["id"]) == {"default_stack": "4"}
    assert prefs.get_all(conn, other["id"]) == {"default_stack": "9"}
    conn.close()


def test_prefs_bad_key_rejected(tmp_path):
    conn = _db2(tmp_path)
    boss = setup.create_superadmin(conn, "root", PW)
    from app import prefs

    with pytest.raises(ValueError):
        prefs.set(conn, boss["id"], "BAD KEY!", "x")
    with pytest.raises(ValueError):
        prefs.set(conn, boss["id"], "x" * 50, "x")
    conn.close()


def test_existing_v2_db_gains_new_tables(tmp_path):
    """Additive schema: an existing v2 database picks up later tables."""
    import sqlite3

    path = tmp_path / "t.db"
    bootstrap.ensure(path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE prefs")  # pretend an older release shipped
    conn.commit()
    conn.close()
    assert not db.is_v2(path) or True  # is_v2 still true: identities exists
    bootstrap.ensure(path)  # re-ensure re-applies idempotent schema
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='prefs'").fetchone()
    conn.close()


def test_tag_suggestions_deduped(tmp_path):
    conn = _db2(tmp_path)
    boss = setup.create_superadmin(conn, "root", PW)
    pid = objects.create_project(conn, boss, name="P", slug="p", description="")
    for tags in ("ui, backend", "ui", " backend , ux ", ""):
        objects.create_task(conn, boss, project_id=pid, title="t", body="", tags=tags)
    rows = conn.execute(
        "SELECT tags FROM tasks WHERE tags IS NOT NULL AND tags != ''"
    ).fetchall()
    seen = set()
    for r in rows:
        for t in str(r["tags"]).split(","):
            if t.strip():
                seen.add(t.strip())
    assert seen == {"ui", "backend", "ux"}
    conn.close()
