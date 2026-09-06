"""FTS search over the durable memory — fixed query builder, no injection.

Chat is excluded by schema design (ephemeral). Query strings are split
into tokens, stripped of FTS operators and quoted — user input can never
become FTS syntax.
"""

import re
import sqlite3

_TOKEN = re.compile(r"[\w-]{2,}")
KINDS = ("task", "discussion", "comment", "decision", "question", "note", "wiki")


def build_query(q: str) -> str:
    tokens = _TOKEN.findall(q or "")
    return " ".join(f'"{t}"' for t in tokens[:16])


def search(
    conn: sqlite3.Connection,
    q: str,
    *,
    project_id: int | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict]:
    if kind is not None and kind not in KINDS:
        raise ValueError("kind must be " + "|".join(KINDS))
    match = build_query(q)
    if not match:
        return []
    sql = (
        "SELECT d.kind, d.obj_id, d.project_id, d.title,"
        " snippet(search_fts, 1, '[', ']', '…', 12) AS snip"
        " FROM search_fts f JOIN search_docs d ON d.id = f.rowid"
        " WHERE search_fts MATCH ?"
    )
    args: list = [match]
    if project_id is not None:
        sql += " AND d.project_id = ?"
        args.append(project_id)
    if kind is not None:
        sql += " AND d.kind = ?"
        args.append(kind)
    sql += " ORDER BY rank LIMIT ?"
    args.append(max(1, min(limit, 100)))
    return [dict(r) for r in conn.execute(sql, args)]
