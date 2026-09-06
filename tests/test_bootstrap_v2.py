"""Bootstrap: fresh create, legacy rename aside, idempotence."""

import sqlite3

from app import bootstrap, db


def _make_legacy(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE agents(name TEXT PRIMARY KEY, last_seen REAL NOT NULL);"
        "INSERT INTO agents VALUES('builder', 1.0);"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO meta VALUES('schema_version', '1');"
    )
    conn.commit()
    conn.close()


def test_fresh_creates_v2(tmp_path):
    target = tmp_path / "slopclanker.db"
    result = bootstrap.ensure(target)
    assert result == target
    assert db.is_v2(target)
    assert not list(tmp_path.glob("slopclanker-legacy*"))


def test_idempotent_second_call(tmp_path):
    target = tmp_path / "slopclanker.db"
    bootstrap.ensure(target)
    conn = db.connect(bootstrap.ensure(target))
    conn.execute("INSERT INTO meta(key, value) VALUES ('marker', '1')")
    conn.commit()
    conn.close()
    bootstrap.ensure(target)
    conn = db.connect(target)
    assert (
        conn.execute("SELECT value FROM meta WHERE key='marker'").fetchone()[0] == "1"
    )
    conn.close()
    assert not list(tmp_path.glob("slopclanker-legacy*"))


def test_legacy_db_renamed_aside(tmp_path):
    target = tmp_path / "slopclanker.db"
    _make_legacy(target)
    bootstrap.ensure(target)
    legacy = tmp_path / "slopclanker-legacy.db"
    assert legacy.exists()
    old = sqlite3.connect(str(legacy))
    assert old.execute("SELECT name FROM agents").fetchall() == [("builder",)]
    old.close()
    assert db.is_v2(target)


def test_legacy_name_taken_gets_suffix(tmp_path):
    (tmp_path / "slopclanker-legacy.db").write_bytes(b"")
    target = tmp_path / "slopclanker.db"
    _make_legacy(target)
    bootstrap.ensure(target)
    assert (tmp_path / "slopclanker-legacy-2.db").exists()
    assert db.is_v2(target)


def test_garbage_file_renamed_aside(tmp_path):
    target = tmp_path / "slopclanker.db"
    target.write_bytes(b"definitely not sqlite")
    bootstrap.ensure(target)
    assert (tmp_path / "slopclanker-legacy.db").read_bytes() == b"definitely not sqlite"
    assert db.is_v2(target)


def test_v2_db_left_alone(tmp_path):
    target = tmp_path / "slopclanker.db"
    bootstrap.ensure(target)
    conn = db.connect(target)
    conn.execute("INSERT INTO meta(key, value) VALUES ('keep', 'me')")
    conn.commit()
    conn.close()
    bootstrap.ensure(target)
    conn = db.connect(target)
    assert conn.execute("SELECT value FROM meta WHERE key='keep'").fetchone()
    conn.close()
    assert not list(tmp_path.glob("slopclanker-legacy*"))


def test_legacy_claiming_two_renamed_aside(tmp_path):
    """Regression: legacy 0.x stamped meta.schema_version='2' in an
    incompatible schema — the marker alone must never pass is_v2."""
    target = tmp_path / "slopclanker.db"
    conn = sqlite3.connect(str(target))
    conn.executescript(
        "CREATE TABLE agents(name TEXT PRIMARY KEY, last_seen REAL NOT NULL);"
        "INSERT INTO agents VALUES('primus', 1.0);"
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO meta VALUES('schema_version', '2');"
    )
    conn.commit()
    conn.close()
    assert not db.is_v2(target)
    bootstrap.ensure(target)
    legacy = tmp_path / "slopclanker-legacy.db"
    assert legacy.exists()
    old = sqlite3.connect(str(legacy))
    assert old.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
    old.close()
    assert db.is_v2(target)
    fresh = sqlite3.connect(str(target))
    assert (
        fresh.execute(
            "SELECT name FROM sqlite_master WHERE name='credentials'"
        ).fetchone()
        is not None
    )
    fresh.close()
