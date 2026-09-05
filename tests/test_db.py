"""DB layer: schema, idempotent open, WAL, v1 -> v2 migration."""

import sqlite3

from app.db import connect

TABLES = {
    "agents",
    "projects",
    "posts",
    "comments",
    "todos",
    "notes",
    "wiki",
    "chat",
    "events",
    "claims",
    "meta",
}

V1_SCHEMA = """
CREATE TABLE agents(
    name TEXT PRIMARY KEY, session_id TEXT, note TEXT,
    started_at REAL NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE threads(
    id INTEGER PRIMARY KEY, title TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'info', audience TEXT NOT NULL DEFAULT 'all',
    status TEXT NOT NULL DEFAULT 'open', outcome TEXT,
    created_by TEXT NOT NULL, created_at REAL NOT NULL, closed_at REAL);
CREATE TABLE messages(
    id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
    author TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE todos(
    id INTEGER PRIMARY KEY, body TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0, scope TEXT NOT NULL DEFAULT 'shared',
    session_key TEXT, assignee TEXT, created_by TEXT NOT NULL,
    created_at REAL NOT NULL, done_at REAL);
CREATE TABLE claims(
    agent TEXT NOT NULL, path TEXT NOT NULL, note TEXT,
    claimed_at REAL NOT NULL, PRIMARY KEY(agent, path));
"""


def test_connect_creates_all_tables(tmp_path):
    conn = connect(tmp_path / "sc.db")
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert TABLES <= {row["name"] for row in rows}


def test_connect_is_idempotent(tmp_path):
    connect(tmp_path / "sc.db")
    conn = connect(tmp_path / "sc.db")
    assert conn.execute("SELECT COUNT(*) AS c FROM agents").fetchone()["c"] == 0


def test_wal_mode_enabled(tmp_path):
    conn = connect(tmp_path / "sc.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def _make_v1_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO agents(name, started_at, last_seen) VALUES('legacy', 1, 2)"
    )
    conn.execute(
        "INSERT INTO threads(id, title, kind, created_by, created_at) VALUES(7, 'old', 'info', 'legacy', 3)"
    )
    conn.execute(
        "INSERT INTO messages(id, thread_id, author, body, created_at) VALUES(1, 7, 'legacy', 'the body', 3)"
    )
    conn.execute(
        "INSERT INTO messages(id, thread_id, author, body, created_at) VALUES(2, 7, 'other', 'a reply', 4)"
    )
    conn.execute(
        "INSERT INTO todos(body, scope, created_by, created_at) VALUES('old todo body', 'shared', 'legacy', 3)"
    )
    conn.commit()
    conn.close()


def test_v1_db_migrated_in_place(tmp_path):
    db = tmp_path / "legacy.db"
    _make_v1_db(db)
    conn = connect(db)

    # threads/messages renamed, body moved into posts, replies kept as comments
    post = conn.execute("SELECT * FROM posts WHERE id = 7").fetchone()
    assert post is not None
    assert post["body"] == "the body"
    assert post["project_id"] == 1
    comments = conn.execute("SELECT * FROM comments WHERE post_id = 7").fetchall()
    assert [c["body"] for c in comments] == ["a reply"]

    # todos got titles derived from their bodies + new fields
    todo = conn.execute("SELECT * FROM todos").fetchone()
    assert todo["title"] == "old todo body"
    assert todo["priority"] == "medium"
    assert todo["archived"] == 0

    # agents gained profile columns with legacy data intact
    agent = conn.execute("SELECT * FROM agents WHERE name = 'legacy'").fetchone()
    assert agent["role"] is None and agent["last_seen"] == 2

    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert TABLES <= tables
    version = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()["value"]
    assert version == "2"

    # reconnect is stable
    conn.close()
    connect(db).execute("SELECT 1")
