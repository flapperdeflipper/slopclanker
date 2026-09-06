"""Schema v2: tables, pragmas, constraints, FTS trigger round-trips."""

import sqlite3

import pytest

from app import db

EXPECTED_TABLES = {
    "meta",
    "identities",
    "credentials",
    "registrations",
    "enrollment_codes",
    "stacks",
    "projects",
    "tasks",
    "todos",
    "transitions",
    "task_revisions",
    "discussions",
    "comments",
    "decisions",
    "questions",
    "chat",
    "notes",
    "note_revisions",
    "wiki",
    "wiki_revisions",
    "claims",
    "links",
    "proofs",
    "events",
    "inbox",
    "notifications",
    "search_docs",
    "search_fts",
}


def _identity(conn, name="founder"):
    cur = conn.execute(
        "INSERT INTO identities(name, kind, role, status, created_at)"
        " VALUES (?,?,?,?,?)",
        (name, "human", "user", "active", 1.0),
    )
    return cur.lastrowid


def _project(conn, uid):
    cur = conn.execute(
        "INSERT INTO projects(slug, name, owner_id, created_by, created_at)"
        " VALUES (?,?,?,?,?)",
        ("proj", "Proj", uid, uid, 1.0),
    )
    return cur.lastrowid


def _seed(conn):
    uid = _identity(conn)
    return uid, _project(conn, uid)


def test_init_creates_all_tables_and_version(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    names = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert EXPECTED_TABLES <= names
    version = conn.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert version == "2"
    conn.close()


def test_wal_journal_mode(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()


def test_foreign_keys_enforced(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks(project_id, title, created_by, created_at)"
            " VALUES (999, 'x', 999, 1.0)"
        )
    conn.close()


def test_task_state_check_constraint(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    uid, pid = _seed(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tasks(project_id, title, state, created_by, created_at)"
            " VALUES (?,?,?,?,?)",
            (pid, "x", "shipped", uid, 1.0),
        )
    conn.close()


def test_question_target_xor(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    uid, pid = _seed(conn)
    base = (pid, "why?", uid, 1.0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO questions(project_id, body, asked_by, created_at)"
            " VALUES (?,?,?,?)",
            base,
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO questions(project_id, body, asked_by, asked_to_id,"
            " asked_to_group, created_at) VALUES (?,?,?,?,?,?)",
            (pid, "why?", uid, uid, "everyone", 1.0),
        )
    conn.execute(
        "INSERT INTO questions(project_id, body, asked_by, asked_to_group,"
        " created_at) VALUES (?,?,?,?,?)",
        (pid, "why?", uid, "everyone", 1.0),
    )
    conn.execute(
        "INSERT INTO questions(project_id, body, asked_by, asked_to_id,"
        " created_at) VALUES (?,?,?,?,?)",
        (pid, "why?", uid, uid, 1.0),
    )
    conn.close()


def test_registration_live_name_unique(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    conn.execute(
        "INSERT INTO registrations(name, claim_hash, status, created_at)"
        " VALUES ('clanker-x','h1','pending',1.0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO registrations(name, claim_hash, status, created_at)"
            " VALUES ('clanker-x','h2','pending',2.0)"
        )
    conn.execute("UPDATE registrations SET status='expired' WHERE name='clanker-x'")
    conn.execute(
        "INSERT INTO registrations(name, claim_hash, status, created_at)"
        " VALUES ('clanker-x','h2','pending',3.0)"
    )
    conn.close()


def test_single_superadmin_unique(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    _identity(conn, "boss")
    conn.execute(
        "INSERT INTO identities(name, kind, role, status, created_at)"
        " VALUES ('boss2','human','superadmin','active',2.0)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO identities(name, kind, role, status, created_at)"
            " VALUES ('boss3','human','superadmin','active',3.0)"
        )
    conn.close()


def _search_rows(conn, match):
    return conn.execute(
        "SELECT d.kind, d.obj_id, d.project_id, d.title, d.body FROM search_fts f"
        " JOIN search_docs d ON d.id = f.rowid WHERE search_fts MATCH ?",
        (match,),
    ).fetchall()


def test_fts_task_lifecycle(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    uid, pid = _seed(conn)
    conn.execute(
        "INSERT INTO tasks(project_id, title, body, created_by, created_at)"
        " VALUES (?,?,?,?,?)",
        (pid, "Fix the flux capacitor", "overheats on monday", uid, 1.0),
    )
    assert _search_rows(conn, "flux")[0]["kind"] == "task"
    assert len(_search_rows(conn, "capacitor")) == 1

    conn.execute("UPDATE tasks SET title = 'Fix the warp coil' WHERE id = 1")
    assert len(_search_rows(conn, "capacitor")) == 0
    assert _search_rows(conn, "warp")[0]["title"] == "Fix the warp coil"

    conn.execute("UPDATE tasks SET state = 'plan' WHERE id = 1")
    assert len(_search_rows(conn, "warp")) == 1

    conn.execute("DELETE FROM tasks WHERE id = 1")
    assert len(_search_rows(conn, "warp")) == 0
    conn.close()


def test_fts_comment_via_discussion_project(tmp_path):
    conn = db.init_db(tmp_path / "t.db")
    uid, pid = _seed(conn)
    conn.execute(
        "INSERT INTO discussions(project_id, title, created_by, created_at)"
        " VALUES (?,?,?,?)",
        (pid, "How to flux", uid, 1.0),
    )
    conn.execute(
        "INSERT INTO comments(discussion_id, author_id, body, created_at)"
        " VALUES (?,?,?,?)",
        (1, uid, "reverse the polarity", 2.0),
    )
    rows = _search_rows(conn, "polarity")
    assert rows[0]["kind"] == "comment"
    assert rows[0]["project_id"] == pid
    conn.close()
