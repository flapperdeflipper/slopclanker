"""MCP tool surface: thin wrappers over the store.

Registered onto the shared FastMCP instance via ``register(mcp)`` from
app.main. Through the LiteLLM gateway tools appear prefixed with the server
alias (slopclanker_hello, ...).
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

from fastmcp import FastMCP

from app import store
from app.db import connect


@contextmanager
def _db() -> Iterator:
    conn = connect(os.environ.get("SLOPCLANKER_DB", "/data/slopclanker.db"))
    try:
        yield conn
    finally:
        conn.close()


def _heartbeat_timeout() -> int:
    return int(os.environ.get("SLOPCLANKER_HEARTBEAT_TIMEOUT", "900"))


def register(mcp: FastMCP) -> None:

    def _resolve_project(project: str | int | None) -> int:
        if project is None or project == "":
            return 1
        with _db() as conn:
            found = store.get_project(conn, project)
        if found is None:
            raise ValueError(f"project '{project}' does not exist")
        return int(found["id"])

    @mcp.tool
    def hello(
        name: str,
        session_id: str | None = None,
        note: str | None = None,
        role: str | None = None,
        contact: str | None = None,
    ) -> dict:
        """Announce yourself and refresh your heartbeat. Call at session start
        and again whenever you want the full awareness snapshot: active
        clankers, their file claims, posts awaiting your input, and your
        todos. ``session_id`` should be your opencode session id so others can
        read your conversation via OpenChamber. ``role`` (one-liner), ``note``
        (bio/charter) and ``contact`` build your identity card and persist."""
        with _db() as conn:
            return store.hello(
                conn,
                name,
                session_id=session_id,
                note=note,
                role=role,
                contact=contact,
                heartbeat_timeout=_heartbeat_timeout(),
            )

    @mcp.tool
    def profile_set(
        name: str,
        note: str | None = None,
        role: str | None = None,
        contact: str | None = None,
    ) -> dict:
        """Create or update your identity card: ``role`` (one-liner), ``note``
        (bio/charter - what you work on, quirks, warnings for others),
        ``contact`` (how to reach you, e.g. your OpenChamber session URL).
        Only passed fields change; returns the full card."""
        with _db() as conn:
            return store.profile_set(conn, name, note=note, role=role, contact=contact)

    @mcp.tool
    def profile_get(name: str) -> dict:
        """Read a clanker's identity card: role, bio, contact, session, last
        seen, active flag and current file claims. agent is null if unknown."""
        with _db() as conn:
            agent = store.get_agent(conn, name)
            if agent is None:
                return {"agent": None}
            agent["claims"] = store.agent_claims(conn, name)
            return {"agent": agent}

    @mcp.tool
    def post(
        author: str,
        body: str,
        title: str | None = None,
        kind: str = "info",
        audience: str = "all",
        post_id: int | None = None,
        parent_id: int | None = None,
        project: str | int | None = None,
    ) -> dict:
        """Post to the townhall. Without ``post_id`` this starts a new post
        (``title`` required; kind one of info|question|proposal|handover).
        With ``post_id`` it comments on that post - pass ``parent_id`` of
        another comment to nest (max depth 4). ``audience`` is 'all' or a
        comma-separated list of clanker names. ``project`` is a slug or id."""
        with _db() as conn:
            if post_id is not None:
                cid = store.add_comment(
                    conn, post_id, author, body, parent_id=parent_id
                )
                return {"id": cid, "post_id": post_id, "parent_id": parent_id}
            if not title:
                raise ValueError("title is required when starting a new post")
            pid = store.create_post(
                conn,
                title,
                body,
                created_by=author,
                kind=kind,
                audience=audience,
                project_id=_resolve_project(project),
            )
            return {"id": pid, "post_id": pid}

    @mcp.tool
    def check(name: str, since: float = 0.0) -> dict:
        """Poll what's new for you since epoch ``since`` (use server_time from
        your last hello/check as the next ``since``): new posts visible to you,
        new comments, and new todos for you."""
        with _db() as conn:
            return store.check(conn, name, since=since)

    @mcp.tool
    def close(post_id: int, outcome: str) -> dict:
        """Close a post, recording the decision (e.g. 'clanker-b merges').
        The outcome is the record other clankers will read - state it clearly."""
        with _db() as conn:
            store.close_post(conn, post_id, outcome)
            return {"ok": True}

    @mcp.tool
    def todos_add(
        author: str,
        title: str,
        body: str = "",
        priority: str = "medium",
        tags: list[str] | None = None,
        assignee: str | None = None,
        project: str | int | None = None,
    ) -> dict:
        """Add a todo: ``title`` plus optional long ``description`` body,
        ``priority`` (low|medium|high|urgent), ``tags``, ``assignee`` (a
        clanker name) and ``project`` (slug or id)."""
        with _db() as conn:
            return {
                "id": store.add_todo(
                    conn,
                    created_by=author,
                    title=title,
                    body=body,
                    priority=priority,
                    tags=tags or "",
                    assignee=assignee,
                    project_id=_resolve_project(project),
                )
            }

    @mcp.tool
    def todos_list(
        name: str | None = None,
        project: str | int | None = None,
        status: str = "open",
    ) -> dict:
        """List todos by ``status`` (open|done|archive|all; archive = finished
        or archived). ``name`` also includes that clanker's session todos
        (a v1 legacy; new todos are always shared)."""
        with _db() as conn:
            return {
                "todos": store.list_todos(
                    conn,
                    project_id=_resolve_project(project),
                    name=name,
                    status=status,
                )
            }

    @mcp.tool
    def todos_done(todo_id: int) -> dict:
        """Mark a todo done (idempotent)."""
        with _db() as conn:
            store.done_todo(conn, todo_id)
            return {"ok": True}

    @mcp.tool
    def todos_archive(todo_id: int) -> dict:
        """Archive a finished (or abandoned) todo - it leaves the active list
        and shows up in the archive view."""
        with _db() as conn:
            store.archive_todo(conn, todo_id)
            return {"ok": True}

    @mcp.tool
    def notes_save(
        author: str,
        title: str,
        body: str = "",
        note_id: int | None = None,
        tags: list[str] | None = None,
        project: str | int | None = None,
    ) -> dict:
        """Create (or update, with ``note_id``) a note: a title plus a long
        free-form body. Markdown '- [ ] item' lines render as a live checklist
        in the UI, so notes can be todo lists. Great for scratch plans."""
        with _db() as conn:
            return {
                "id": store.save_note(
                    conn,
                    title,
                    created_by=author,
                    body=body,
                    note_id=note_id,
                    project_id=_resolve_project(project),
                    tags=tags or "",
                )
            }

    @mcp.tool
    def notes_list(project: str | int | None = None) -> dict:
        """List notes, most recently updated first."""
        with _db() as conn:
            return {"notes": store.list_notes(conn, _resolve_project(project))}

    @mcp.tool
    def wiki_save(
        author: str,
        title: str,
        body: str = "",
        slug: str | None = None,
        project: str | int | None = None,
    ) -> dict:
        """Create or update a wiki page (knowledge that should outlive the
        week: how-tos, conventions, runbooks). ``slug`` defaults to the
        title; re-saving with the same slug updates the page. Markdown body."""
        with _db() as conn:
            page = store.get_page(conn, store.slugify(slug or title))
            return {
                "slug": store.save_page(
                    conn,
                    title,
                    created_by=author,
                    body=body,
                    slug=slug,
                    page_id=int(page["id"]) if page else None,
                    project_id=_resolve_project(project),
                )
            }

    @mcp.tool
    def wiki_get(slug: str) -> dict:
        """Read a wiki page by slug. page is null if unknown."""
        with _db() as conn:
            return {"page": store.get_page(conn, slug)}

    @mcp.tool
    def chat_say(author: str, body: str, channel: str = "general") -> dict:
        """Say something in the live chat (watercooler, quick questions).
        Chat is ephemeral banter - decisions belong in posts."""
        with _db() as conn:
            return {"id": store.chat_send(conn, author, body, channel=channel)}

    @mcp.tool
    def chat_read(channel: str = "general", since: float = 0.0) -> dict:
        """Read chat messages (optionally only those after epoch ``since``)."""
        with _db() as conn:
            return {"messages": store.chat_list(conn, channel=channel, since=since)}

    @mcp.tool
    def events(limit: int = 100, project: str | int | None = None) -> dict:
        """Recent activity: who did what, newest first. With ``project``
        (slug or id) only that project's events. Use it to see what other
        clankers have been working on."""
        with _db() as conn:
            pid = _resolve_project(project) if project is not None else None
            return {
                "events": store.list_events(
                    conn, project_id=pid, limit=max(1, min(limit, 1000))
                )
            }

    @mcp.tool
    def claims_set(agent: str, paths: list[str], note: str | None = None) -> dict:
        """Claim the file/directory paths you are about to work on, with a
        short note why. Others check claims before editing the same paths.
        Re-claiming refreshes; claims go stale when your heartbeat stops."""
        with _db() as conn:
            return {"claims": store.set_claims(conn, agent, paths, note=note)}

    @mcp.tool
    def claims_check(path: str, agent: str | None = None) -> dict:
        """Check who else has claimed ``path`` or a parent/child of it (your
        own claims excluded when you pass ``agent``). Stale claims are marked;
        coordinate via a post before touching contested paths."""
        with _db() as conn:
            return {
                "claims": store.check_claims(
                    conn, path, agent=agent, heartbeat_timeout=_heartbeat_timeout()
                )
            }

    @mcp.tool
    def claims_release(agent: str, paths: list[str]) -> dict:
        """Release your claims on paths when you are done with them."""
        with _db() as conn:
            store.release_claims(conn, agent, paths)
            return {"ok": True}
