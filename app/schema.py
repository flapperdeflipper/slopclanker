"""SlopClanker schema v2 — the full DESIGN.md data model (draft 7).

Fresh start: no v1 tables survive here. Everything hangs off projects
(no default project); every actor reference is an identity row; secrets
live only in credentials. Append-only logs (transitions, events) carry
hash-chain columns filled by the services that write them.
"""

# 3, not 2: the legacy 0.x line already stamped meta.schema_version='2'
# in its (incompatible) schema. A fresh v1 database is '3'.
SCHEMA_VERSION = 3

SCHEMA_V2 = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identities(
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL CHECK (kind IN ('human','clanker')),
    role          TEXT CHECK (role IN ('superadmin','admin','user')),
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','active','revoked','rejected')),
    note          TEXT NOT NULL DEFAULT '',
    contact       TEXT NOT NULL DEFAULT '',
    created_by    INTEGER REFERENCES identities(id),
    created_at    REAL NOT NULL,
    reg_ip        TEXT,
    reg_user_agent TEXT,
    approved_by   INTEGER REFERENCES identities(id),
    approved_at   REAL,
    CHECK ((kind='human' AND role IS NOT NULL) OR (kind='clanker' AND role IS NULL))
);

CREATE TABLE IF NOT EXISTS credentials(
    id              INTEGER PRIMARY KEY,
    identity_id     INTEGER NOT NULL REFERENCES identities(id),
    kind            TEXT NOT NULL
                    CHECK (kind IN ('password','ui_session','agent_token')),
    label           TEXT NOT NULL DEFAULT '',
    secret_hash     TEXT NOT NULL,
    issued_by       INTEGER REFERENCES identities(id),
    issued_at       REAL NOT NULL,
    expires_at      REAL,
    revoked_at      REAL,
    last_seen_at    REAL,
    last_ip         TEXT,
    last_user_agent TEXT,
    call_count      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS registrations(
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    claim_hash   TEXT NOT NULL,
    ip           TEXT,
    user_agent   TEXT,
    status       TEXT NOT NULL
                 CHECK (status IN ('pending','approved','rejected','expired','delivered')),
    identity_id  INTEGER REFERENCES identities(id),
    created_at   REAL NOT NULL,
    decided_by   INTEGER REFERENCES identities(id),
    decided_at   REAL,
    delivered_at REAL,
    delivered_ip TEXT
);

CREATE TABLE IF NOT EXISTS enrollment_codes(
    id          INTEGER PRIMARY KEY,
    code_hash   TEXT NOT NULL UNIQUE,
    identity_id INTEGER NOT NULL REFERENCES identities(id),
    issued_by   INTEGER NOT NULL REFERENCES identities(id),
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL,
    used_ip     TEXT
);

CREATE TABLE IF NOT EXISTS stacks(
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by  INTEGER NOT NULL REFERENCES identities(id),
    created_at  REAL NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects(
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    stack_id    INTEGER REFERENCES stacks(id),
    owner_id    INTEGER NOT NULL REFERENCES identities(id),
    archived    INTEGER NOT NULL DEFAULT 0,
    created_by  INTEGER NOT NULL REFERENCES identities(id),
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks(
    id               INTEGER PRIMARY KEY,
    project_id       INTEGER NOT NULL REFERENCES projects(id),
    title            TEXT NOT NULL,
    body             TEXT NOT NULL DEFAULT '',
    state            TEXT NOT NULL DEFAULT 'idea'
                     CHECK (state IN
                     ('idea','plan','proposed','approved','building','review',
                      'done','paused','trashed')),
    priority         TEXT NOT NULL DEFAULT 'medium'
                     CHECK (priority IN ('low','medium','high','urgent')),
    tags             TEXT NOT NULL DEFAULT '',
    assignee_id      INTEGER REFERENCES identities(id),
    previous_state   TEXT,
    state_changed_by INTEGER REFERENCES identities(id),
    state_changed_at REAL,
    proof_waived     INTEGER NOT NULL DEFAULT 0,
    proof_waived_by  INTEGER REFERENCES identities(id),
    version          INTEGER NOT NULL DEFAULT 1,
    created_by       INTEGER NOT NULL REFERENCES identities(id),
    created_at       REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS todos(
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    title      TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    done_by    INTEGER REFERENCES identities(id),
    done_at    REAL,
    sort       INTEGER NOT NULL DEFAULT 0,
    version    INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL REFERENCES identities(id),
    created_at REAL NOT NULL,
    trashed_at REAL,
    trashed_by INTEGER REFERENCES identities(id)
);

CREATE TABLE IF NOT EXISTS transitions(
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    from_state TEXT,
    to_state   TEXT NOT NULL,
    actor_id   INTEGER NOT NULL REFERENCES identities(id),
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    prev_hash  TEXT,
    row_hash   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_revisions(
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    edited_by  INTEGER NOT NULL REFERENCES identities(id),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS discussions(
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'info'
               CHECK (kind IN ('info','question','proposal','handover')),
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    outcome    TEXT,
    created_by INTEGER NOT NULL REFERENCES identities(id),
    created_at REAL NOT NULL,
    closed_by  INTEGER REFERENCES identities(id),
    closed_at  REAL
);

CREATE TABLE IF NOT EXISTS comments(
    id            INTEGER PRIMARY KEY,
    discussion_id INTEGER NOT NULL REFERENCES discussions(id),
    parent_id     INTEGER REFERENCES comments(id),
    author_id     INTEGER NOT NULL REFERENCES identities(id),
    body          TEXT NOT NULL,
    created_at    REAL NOT NULL,
    trashed_at    REAL,
    trashed_by    INTEGER REFERENCES identities(id)
);

CREATE TABLE IF NOT EXISTS decisions(
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id),
    title        TEXT NOT NULL,
    context      TEXT NOT NULL DEFAULT '',
    outcome      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'proposed'
                 CHECK (status IN ('proposed','accepted','rejected','superseded')),
    decided_by   INTEGER REFERENCES identities(id),
    decided_at   REAL,
    supersedes_id INTEGER REFERENCES decisions(id),
    created_by   INTEGER NOT NULL REFERENCES identities(id),
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS questions(
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id),
    body            TEXT NOT NULL,
    asked_by        INTEGER NOT NULL REFERENCES identities(id),
    asked_to_id     INTEGER REFERENCES identities(id),
    asked_to_group  TEXT CHECK (asked_to_group IN ('humans','clankers','everyone')),
    attach_type     TEXT,
    attach_id       INTEGER,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','answered','withdrawn')),
    answer          TEXT,
    answered_by     INTEGER REFERENCES identities(id),
    answered_at     REAL,
    withdrawn_by    INTEGER REFERENCES identities(id),
    withdrawn_at    REAL,
    created_at      REAL NOT NULL,
    CHECK ((asked_to_id IS NULL) + (asked_to_group IS NULL) = 1)
);

CREATE TABLE IF NOT EXISTS chat(
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    author_id  INTEGER NOT NULL REFERENCES identities(id),
    body       TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS notes(
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES identities(id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS note_revisions(
    id         INTEGER PRIMARY KEY,
    note_id    INTEGER NOT NULL REFERENCES notes(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    edited_by  INTEGER NOT NULL REFERENCES identities(id),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki(
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES identities(id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS wiki_revisions(
    id         INTEGER PRIMARY KEY,
    wiki_id    INTEGER NOT NULL REFERENCES wiki(id),
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    edited_by  INTEGER NOT NULL REFERENCES identities(id),
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS claims(
    identity_id INTEGER NOT NULL REFERENCES identities(id),
    path        TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    claimed_at  REAL NOT NULL,
    PRIMARY KEY (identity_id, path)
);

CREATE TABLE IF NOT EXISTS links(
    id         INTEGER PRIMARY KEY,
    from_type  TEXT NOT NULL,
    from_id    INTEGER NOT NULL,
    to_type    TEXT NOT NULL,
    to_id      INTEGER NOT NULL,
    created_by INTEGER NOT NULL REFERENCES identities(id),
    created_at REAL NOT NULL,
    UNIQUE (from_type, from_id, to_type, to_id)
);

CREATE TABLE IF NOT EXISTS proofs(
    id                INTEGER PRIMARY KEY,
    task_id           INTEGER NOT NULL REFERENCES tasks(id),
    provider          TEXT NOT NULL
                      CHECK (provider IN ('github','gitlab','gitea','other')),
    repo              TEXT NOT NULL DEFAULT '',
    number            TEXT NOT NULL DEFAULT '',
    kind              TEXT NOT NULL
                      CHECK (kind IN ('mr','pr','commit','issue','other')),
    url               TEXT NOT NULL,
    state             TEXT,
    state_checked_at  REAL,
    added_by          INTEGER NOT NULL REFERENCES identities(id),
    added_at          REAL NOT NULL,
    trashed_at        REAL
);

CREATE TABLE IF NOT EXISTS events(
    id              INTEGER PRIMARY KEY,
    ts              REAL NOT NULL,
    actor_id        INTEGER NOT NULL REFERENCES identities(id),
    verb            TEXT NOT NULL,
    obj_type        TEXT NOT NULL,
    obj_id          INTEGER NOT NULL DEFAULT 0,
    project_id      INTEGER,
    to_identity_id  INTEGER REFERENCES identities(id),
    payload         TEXT NOT NULL DEFAULT '{}',
    prev_hash       TEXT,
    row_hash        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox(
    identity_id INTEGER NOT NULL REFERENCES identities(id),
    event_id    INTEGER NOT NULL REFERENCES events(id),
    read_at     REAL,
    PRIMARY KEY (identity_id, event_id)
);

CREATE TABLE IF NOT EXISTS notifications(
    id          INTEGER PRIMARY KEY,
    identity_id INTEGER REFERENCES identities(id),
    kind        TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    read_at     REAL
);

CREATE TABLE IF NOT EXISTS search_docs(
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    obj_id     INTEGER NOT NULL,
    project_id INTEGER,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    UNIQUE (kind, obj_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    title, body, content='search_docs', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS search_docs_ai AFTER INSERT ON search_docs BEGIN
    INSERT INTO search_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS search_docs_ad AFTER DELETE ON search_docs BEGIN
    INSERT INTO search_fts(search_fts, rowid, title, body)
        VALUES ('delete', old.id, old.title, old.body);
END;
CREATE TRIGGER IF NOT EXISTS search_docs_au AFTER UPDATE ON search_docs BEGIN
    INSERT INTO search_fts(search_fts, rowid, title, body)
        VALUES ('delete', old.id, old.title, old.body);
    INSERT INTO search_fts(rowid, title, body) VALUES (new.id, new.title, new.body);
END;

CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_superadmin ON identities(role)
    WHERE role = 'superadmin';

CREATE UNIQUE INDEX IF NOT EXISTS idx_registrations_live ON registrations(name)
    WHERE status IN ('pending','approved');


CREATE TRIGGER IF NOT EXISTS fts_tasks_ai AFTER INSERT ON tasks BEGIN
    INSERT INTO search_docs(kind, obj_id, project_id, title, body)
        VALUES ('task', new.id, new.project_id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS fts_tasks_au AFTER UPDATE OF title, body, project_id
    ON tasks BEGIN
    UPDATE search_docs
        SET project_id = new.project_id, title = new.title, body = new.body
        WHERE kind = 'task' AND obj_id = new.id;
END;
CREATE TRIGGER IF NOT EXISTS fts_tasks_ad AFTER DELETE ON tasks BEGIN
    DELETE FROM search_docs WHERE kind = 'task' AND obj_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS fts_discussions_ai AFTER INSERT ON discussions BEGIN
    INSERT INTO search_docs(kind, obj_id, project_id, title, body)
        VALUES ('discussion', new.id, new.project_id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS fts_discussions_au
    AFTER UPDATE OF title, body, project_id ON discussions BEGIN
    UPDATE search_docs
        SET project_id = new.project_id, title = new.title, body = new.body
        WHERE kind = 'discussion' AND obj_id = new.id;
END;
CREATE TRIGGER IF NOT EXISTS fts_discussions_ad AFTER DELETE ON discussions BEGIN
    DELETE FROM search_docs WHERE kind = 'discussion' AND obj_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS fts_comments_ai AFTER INSERT ON comments BEGIN
    INSERT INTO search_docs(kind, obj_id, project_id, title, body)
        SELECT 'comment', new.id, d.project_id, '', new.body
        FROM discussions d WHERE d.id = new.discussion_id;
END;
CREATE TRIGGER IF NOT EXISTS fts_comments_ad AFTER DELETE ON comments BEGIN
    DELETE FROM search_docs WHERE kind = 'comment' AND obj_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS fts_decisions_ai AFTER INSERT ON decisions BEGIN
    INSERT INTO search_docs(kind, obj_id, project_id, title, body)
        VALUES ('decision', new.id, new.project_id, new.title,
                new.context || ' ' || new.outcome);
END;
CREATE TRIGGER IF NOT EXISTS fts_decisions_au
    AFTER UPDATE OF title, context, outcome, project_id ON decisions BEGIN
    UPDATE search_docs
        SET project_id = new.project_id, title = new.title,
            body = new.context || ' ' || new.outcome
        WHERE kind = 'decision' AND obj_id = new.id;
END;
CREATE TRIGGER IF NOT EXISTS fts_decisions_ad AFTER DELETE ON decisions BEGIN
    DELETE FROM search_docs WHERE kind = 'decision' AND obj_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS fts_questions_ai AFTER INSERT ON questions BEGIN
    INSERT INTO search_docs(kind, obj_id, project_id, title, body)
        VALUES ('question', new.id, new.project_id, '', new.body);
END;
CREATE TRIGGER IF NOT EXISTS fts_questions_au
    AFTER UPDATE OF body, project_id ON questions BEGIN
    UPDATE search_docs
        SET project_id = new.project_id, body = new.body
        WHERE kind = 'question' AND obj_id = new.id;
END;
CREATE TRIGGER IF NOT EXISTS fts_questions_ad AFTER DELETE ON questions BEGIN
    DELETE FROM search_docs WHERE kind = 'question' AND obj_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS fts_notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO search_docs(kind, obj_id, project_id, title, body)
        VALUES ('note', new.id, new.project_id, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS fts_notes_au AFTER UPDATE OF title, body, project_id
    ON notes BEGIN
    UPDATE search_docs
        SET project_id = new.project_id, title = new.title, body = new.body
        WHERE kind = 'note' AND obj_id = new.id;
END;
CREATE TRIGGER IF NOT EXISTS fts_notes_ad AFTER DELETE ON notes BEGIN
    DELETE FROM search_docs WHERE kind = 'note' AND obj_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS fts_wiki_ai AFTER INSERT ON wiki BEGIN
    INSERT INTO search_docs(kind, obj_id, project_id, title, body)
        VALUES ('wiki', new.id, NULL, new.title, new.body);
END;
CREATE TRIGGER IF NOT EXISTS fts_wiki_au AFTER UPDATE OF title, body ON wiki BEGIN
    UPDATE search_docs
        SET title = new.title, body = new.body
        WHERE kind = 'wiki' AND obj_id = new.id;
END;
CREATE TRIGGER IF NOT EXISTS fts_wiki_ad AFTER DELETE ON wiki BEGIN
    DELETE FROM search_docs WHERE kind = 'wiki' AND obj_id = old.id;
END;

CREATE INDEX IF NOT EXISTS idx_tasks_project      ON tasks(project_id, state);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee    ON tasks(assignee_id)
    WHERE assignee_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_todos_task        ON todos(task_id);
CREATE INDEX IF NOT EXISTS idx_transitions_task  ON transitions(task_id);
CREATE INDEX IF NOT EXISTS idx_comments_disc     ON comments(discussion_id);
CREATE INDEX IF NOT EXISTS idx_discussions_proj  ON discussions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_decisions_proj    ON decisions(project_id, status);
CREATE INDEX IF NOT EXISTS idx_questions_attach  ON questions(attach_type, attach_id)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_questions_target  ON questions(asked_to_id)
    WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_chat_project      ON chat(project_id, id);
CREATE INDEX IF NOT EXISTS idx_events_obj        ON events(obj_type, obj_id);
CREATE INDEX IF NOT EXISTS idx_events_ts         ON events(ts);
CREATE INDEX IF NOT EXISTS idx_inbox_unread      ON inbox(identity_id, read_at);
CREATE INDEX IF NOT EXISTS idx_creds_identity    ON credentials(identity_id, kind);
CREATE INDEX IF NOT EXISTS idx_notif_unread      ON notifications(identity_id, read_at);
CREATE INDEX IF NOT EXISTS idx_projects_stack    ON projects(stack_id);
CREATE INDEX IF NOT EXISTS idx_registrations_st  ON registrations(status);
"""
