"""wait() and the SSE stream — shared filter + long-poll core.

Durable: addressed events live in the inbox until read, so an offline
agent gets its work on return. Live: the in-process bus wakes waiting
subscribers instantly. SSE reconnects recover via the persisted `since`
cursor — the bus only carries live traffic.
"""

import asyncio

from app import db
from app.bus import bus as live_bus

MAX_TIMEOUT = 120.0
STREAM_HEARTBEAT = 25.0


class Filters:
    __slots__ = ("obj_id", "obj_type", "project_id", "to_identity_id", "verb")

    def __init__(
        self,
        *,
        obj_type=None,
        obj_id=None,
        project_id=None,
        verb=None,
        to_identity_id=None,
    ):
        self.obj_type = obj_type
        self.obj_id = obj_id
        self.project_id = project_id
        self.verb = verb
        self.to_identity_id = to_identity_id

    def matches(self, ev: dict) -> bool:
        if self.obj_type is not None and ev.get("obj_type") != self.obj_type:
            return False
        if self.obj_id is not None and ev.get("obj_id") != self.obj_id:
            return False
        if self.project_id is not None and ev.get("project_id") != self.project_id:
            return False
        if self.verb is not None and ev.get("verb") != self.verb:
            return False
        return not (
            self.to_identity_id is not None
            and ev.get("to_identity_id") != self.to_identity_id
        )


async def wait_for(
    identity_id: int,
    *,
    obj_type: str | None = None,
    obj_id: int | None = None,
    project_id: int | None = None,
    verb: str | None = None,
    to_me: bool = False,
    timeout: float = 10.0,
    since: int = 0,
    db_path=None,
) -> list[dict]:
    """Return matching events now, or block until one arrives / timeout."""
    timeout = max(0.0, min(float(timeout), MAX_TIMEOUT))
    f = Filters(
        obj_type=obj_type,
        obj_id=obj_id,
        project_id=project_id,
        verb=verb,
        to_identity_id=identity_id if to_me else None,
    )
    conn = db.connect(db_path or db.db_path())
    q = live_bus.subscribe()
    try:
        from app import events as ev_mod

        if to_me:
            rows = ev_mod.unread_for(
                conn, identity_id, obj_type=obj_type, obj_id=obj_id
            )
            if rows:
                ev_mod.mark_read(conn, identity_id, [r["id"] for r in rows])
                return rows
        else:
            rows = ev_mod.feed(
                conn,
                since=since,
                project_id=project_id,
                obj_type=obj_type,
                obj_id=obj_id,
            )
            rows = [r for r in rows if f.matches(r)]
            if rows:
                return rows
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return []
            try:
                ev = await asyncio.wait_for(q.get(), remaining)
            except TimeoutError:
                return []
            if f.matches(ev):
                if to_me:
                    ev_mod.mark_read(conn, identity_id, [ev["id"]])
                return [ev]
    finally:
        live_bus.unsubscribe(q)
        conn.close()
