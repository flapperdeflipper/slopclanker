"""Full JSON export — DESIGN §19: nightly dump next to addon backups.

Every table, verbatim, as one JSON document. Includes the hash chains
(transitions/events) so a tampered export is detectable via
chain.verify_chain on import. Never includes credential secrets —
only their hashes, exactly like the DB.
"""

import json
import sqlite3
import sys
import time

TABLES = (
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
)

# secrets_hash columns are hashes, not secrets — keep them; the export
# must let a restored DB re-verify tokens. No plaintext secrets exist
# anywhere in the schema.


def export_all(conn: sqlite3.Connection) -> dict:
    out = {
        "service": "slopclanker",
        "exported_at": time.time(),
        "tables": {},
    }
    for table in TABLES:
        rows = [
            dict(zip([c[0] for c in cur.description], row))
            for cur in [conn.execute(f"SELECT * FROM {table}")]  # noqa: S608  # nosec B608
            for row in cur.fetchall()
        ]
        out["tables"][table] = rows
    return out


def export_to_file(conn: sqlite3.Connection, path: str) -> dict:
    data = export_all(conn)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    return data


def main(argv: list[str]) -> int:
    from app import bootstrap, db

    db_path = argv[1] if len(argv) > 1 else db.db_path()
    out_path = argv[2] if len(argv) > 2 else f"{db_path}.export.{int(time.time())}.json"
    conn = db.connect(bootstrap.ensure(db_path))
    try:
        data = export_to_file(conn, out_path)
    finally:
        conn.close()
    counts = {k: len(v) for k, v in data["tables"].items() if v}
    total = sum(counts.values())
    print(f"exported {total} rows across {len(counts)} tables -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
