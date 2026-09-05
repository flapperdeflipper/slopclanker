"""SQLite storage: schema, connection factory, migrations.

One connection per operation (WAL allows concurrent readers; writes queue
behind busy_timeout). All timestamps are epoch seconds (float).

History: v1 shipped threads/messages/todos(scope,session). v2 adds projects,
posts with a body column, self-referencing comments (max depth enforced in
store), titled/priority/tag todos, notes, wiki pages, chat and an events
log. v1 databases are migrated in place on connect().
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS agents(
    name       TEXT PRIMARY KEY,
    session_id TEXT,
    note       TEXT,
    role       TEXT,
    contact    TEXT,
    started_at REAL NOT NULL,
    last_seen  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS projects(
    id          INTEGER PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    archived    INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS posts(
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL DEFAULT 1 REFERENCES projects(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'info',
    audience   TEXT NOT NULL DEFAULT 'all',
    status     TEXT NOT NULL DEFAULT 'open',
    outcome    TEXT,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    closed_at  REAL
);

CREATE TABLE IF NOT EXISTS comments(
    id         INTEGER PRIMARY KEY,
    post_id    INTEGER NOT NULL REFERENCES posts(id),
    parent_id  INTEGER REFERENCES comments(id),
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS todos(
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL DEFAULT '',
    done        INTEGER NOT NULL DEFAULT 0,
    priority    TEXT NOT NULL DEFAULT 'medium',
    tags        TEXT NOT NULL DEFAULT '',
    assignee    TEXT,
    project_id  INTEGER NOT NULL DEFAULT 1 REFERENCES projects(id),
    archived    INTEGER NOT NULL DEFAULT 0,
    scope       TEXT NOT NULL DEFAULT 'shared',
    session_key TEXT,
    created_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    done_at     REAL
);

CREATE TABLE IF NOT EXISTS notes(
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL DEFAULT 1 REFERENCES projects(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki(
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL DEFAULT 1 REFERENCES projects(id),
    slug       TEXT UNIQUE NOT NULL,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS chat(
    id         INTEGER PRIMARY KEY,
    channel    TEXT NOT NULL DEFAULT 'general',
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
    id         INTEGER PRIMARY KEY,
    ts         REAL NOT NULL,
    actor      TEXT NOT NULL,
    verb       TEXT NOT NULL,
    obj_type   TEXT NOT NULL,
    obj_id     TEXT NOT NULL DEFAULT '',
    summary    TEXT NOT NULL DEFAULT '',
    project_id INTEGER
);

CREATE TABLE IF NOT EXISTS claims(
    agent      TEXT NOT NULL,
    path       TEXT NOT NULL,
    note       TEXT,
    claimed_at REAL NOT NULL,
    PRIMARY KEY(agent, path)
);
"""

SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_comments_post    ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_comments_created ON comments(created_at);
CREATE INDEX IF NOT EXISTS idx_posts_created    ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_todos_project    ON todos(project_id);
CREATE INDEX IF NOT EXISTS idx_chat_channel     ON chat(channel, created_at);
CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts);
CREATE INDEX IF NOT EXISTS idx_notes_project    ON notes(project_id);
CREATE INDEX IF NOT EXISTS idx_wiki_project     ON wiki(project_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and create/migrate) the database at ``path``. Idempotent."""
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    legacy = "threads" in _tables(conn)
    conn.executescript(SCHEMA)
    _migrate(conn, legacy)
    conn.executescript(SCHEMA_INDEXES)
    conn.commit()
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    if col not in _cols(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _migrate(conn: sqlite3.Connection, legacy: bool) -> None:
    if legacy:
        # SCHEMA just created empty posts/comments; drop them so the legacy
        # tables can take their names, then rename.
        conn.execute("DROP TABLE IF EXISTS comments")
        conn.execute("DROP TABLE IF EXISTS posts")
        conn.execute("ALTER TABLE threads RENAME TO posts")
        conn.execute("ALTER TABLE messages RENAME TO comments")
        comment_cols = _cols(conn, "comments")
        if "thread_id" in comment_cols and "post_id" not in comment_cols:
            conn.execute("ALTER TABLE comments RENAME COLUMN thread_id TO post_id")

    _add_column(conn, "agents", "role", "TEXT")
    _add_column(conn, "agents", "contact", "TEXT")

    # Seed the default project everything else defaults to.
    conn.execute(
        """
        INSERT OR IGNORE INTO projects(id, slug, name, description, created_by, created_at)
        VALUES(1, 'general', 'General', 'Uncategorised things', 'system', 0)
        """
    )

    _add_column(conn, "posts", "project_id", "INTEGER NOT NULL DEFAULT 1")
    _add_column(conn, "posts", "body", "TEXT NOT NULL DEFAULT ''")
    _add_column(conn, "comments", "parent_id", "INTEGER")

    if legacy:
        # v1 stored the post body as the first message; move it into posts.body
        conn.execute(
            """
            UPDATE posts SET body = (
                SELECT c.body FROM comments c
                WHERE c.post_id = posts.id AND c.parent_id IS NULL
                ORDER BY c.created_at, c.id LIMIT 1
            ) WHERE body = ''
            """
        )
        conn.execute(
            """
            DELETE FROM comments WHERE id IN (
                SELECT MIN(c.id) FROM comments c
                JOIN posts p ON p.id = c.post_id
                WHERE c.parent_id IS NULL AND c.author = p.created_by
                GROUP BY c.post_id
            )
            """
        )

    for col, decl in (
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("body", "TEXT NOT NULL DEFAULT ''"),
        ("priority", "TEXT NOT NULL DEFAULT 'medium'"),
        ("tags", "TEXT NOT NULL DEFAULT ''"),
        ("project_id", "INTEGER NOT NULL DEFAULT 1"),
        ("archived", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_column(conn, "todos", col, decl)
    conn.execute(
        "UPDATE todos SET title = substr(body, 1, 60) WHERE title = '' AND body != ''"
    )
    conn.execute("UPDATE todos SET title = 'untitled' WHERE title = ''")

    _add_column(conn, "events", "project_id", "INTEGER")

    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '2')"
    )
