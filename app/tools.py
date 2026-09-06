"""MCP tools — thin transports over the service layer.

The actor ALWAYS comes from the bearer token (get_http_request ->
request.state.identity); no tool accepts an author/agent argument.
The single can() gate stays in the services.
"""

import json

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request


def _actor() -> dict:
    request = get_http_request()
    if request is None:
        raise RuntimeError("MCP tools require the HTTP transport")
    return request.state.identity


def register(mcp: FastMCP) -> None:
    from app import (
        claims,
        comms,
        db,
        decisions,
        events,
        knowledge,
        links,
        objects,
        realtime,
        statemachine,
    )
    from app import (
        proofs as proofs_mod,
    )
    from app import (
        questions as questions_mod,
    )
    from app import (
        search as search_mod,
    )

    def _conn():
        return db.connect(db.db_path())

    def _payload(ev):
        if isinstance(ev.get("payload"), str):
            try:
                ev["payload"] = json.loads(ev["payload"])
            except ValueError:
                pass
        return ev

    @mcp.tool
    def hello(note: str = "") -> dict:
        """Check in: who you are, unread inbox, open questions for you."""
        actor = _actor()
        conn = _conn()
        try:
            unread = events.unread_for(conn, actor["id"])
            mine = questions_mod.list_questions(conn, open_only=True, to_actor=actor)
            return {
                "identity": {
                    k: actor[k] for k in ("id", "name", "kind", "role", "status")
                },
                "note": note[:500],
                "unread": [_payload(dict(r)) for r in unread],
                "open_questions": [dict(r) for r in mine],
            }
        finally:
            conn.close()

    @mcp.tool
    def projects(stack: int | None = None) -> dict:
        """List projects (optionally by stack)."""
        _actor()
        conn = _conn()
        try:
            rows = objects.list_projects(conn, stack_id=stack, include_archived=False)
            return {"projects": [dict(r) for r in rows]}
        finally:
            conn.close()

    @mcp.tool
    def project_get(project_id: int) -> dict:
        """Project detail with its tasks."""
        _actor()
        conn = _conn()
        try:
            proj = dict(objects.get_project(conn, project_id))
            tasks = [dict(t) for t in objects.list_tasks(conn, project_id=project_id)]
            return {"project": proj, "tasks": tasks}
        finally:
            conn.close()

    @mcp.tool
    def task_create(
        project_id: int,
        title: str,
        body: str = "",
        priority: str = "medium",
        tags: str = "",
        assignee_id: int | None = None,
    ) -> dict:
        """Create a task (starts in idea)."""
        actor = _actor()
        conn = _conn()
        try:
            tid = objects.create_task(
                conn,
                actor,
                project_id,
                title,
                body=body,
                priority=priority,
                tags=tags,
                assignee_id=assignee_id,
            )
            return {"id": tid}
        finally:
            conn.close()

    @mcp.tool
    def task_get(task_id: int) -> dict:
        """Task detail: todos, transition history, proofs, questions."""
        _actor()
        conn = _conn()
        try:
            task = dict(objects.get_task(conn, task_id))
            todos = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM todos WHERE task_id = ? AND trashed_at IS NULL"
                    " ORDER BY sort, id",
                    (task_id,),
                )
            ]
            trans = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM transitions WHERE task_id = ? ORDER BY id",
                    (task_id,),
                )
            ]
            proofs = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM proofs WHERE task_id = ? AND trashed_at IS NULL",
                    (task_id,),
                )
            ]
            qs = questions_mod.list_questions(
                conn, attach_type="task", attach_id=task_id
            )
            return {
                "task": task,
                "todos": todos,
                "transitions": trans,
                "proofs": proofs,
                "questions": [dict(q) for q in qs],
            }
        finally:
            conn.close()

    @mcp.tool
    def tasks(
        project_id: int | None = None,
        state: str | None = None,
        assignee_id: int | None = None,
    ) -> dict:
        """List tasks, filterable by project/state/assignee."""
        _actor()
        conn = _conn()
        try:
            rows = objects.list_tasks(
                conn, project_id=project_id, state=state, assignee_id=assignee_id
            )
            return {"tasks": [dict(r) for r in rows]}
        finally:
            conn.close()

    @mcp.tool
    def task_edit(
        task_id: int,
        body: str | None = None,
        title: str | None = None,
        priority: str | None = None,
        tags: str | None = None,
        assignee_id: int | None = None,
        version: int | None = None,
    ) -> dict:
        """Edit your own task fields (agents: body frozen after approval)."""
        actor = _actor()
        conn = _conn()
        try:
            row = objects.edit_task(
                conn,
                actor,
                task_id,
                body=body,
                title=title,
                priority=priority,
                tags=tags,
                assignee_id=(objects.UNSET if assignee_id is None else assignee_id),
                version=version,
            )
            return dict(row)
        finally:
            conn.close()

    @mcp.tool
    def task_transition(
        task_id: int, to: str, note: str = "", version: int | None = None
    ) -> dict:
        """Move a task through its states. approve/done/trash/not-done are
        human-only; `to` may be `previous` for not-done/restore."""
        actor = _actor()
        conn = _conn()
        try:
            row = statemachine.transition(
                conn, task_id, to, actor, note=note, version=version
            )
            return dict(row)
        finally:
            conn.close()

    @mcp.tool
    def todo_add(task_id: int, title: str) -> dict:
        """Add a checklist item to a task (anyone, any time)."""
        actor = _actor()
        conn = _conn()
        try:
            return {"id": objects.add_todo(conn, actor, task_id, title)}
        finally:
            conn.close()

    @mcp.tool
    def todo_tick(todo_id: int, done: bool = True, version: int | None = None) -> dict:
        """Tick/untick a todo; your identity is recorded."""
        actor = _actor()
        conn = _conn()
        try:
            return dict(objects.tick_todo(conn, actor, todo_id, done, version=version))
        finally:
            conn.close()

    @mcp.tool
    def task_proof_add(
        task_id: int,
        url: str,
        kind: str | None = None,
        provider: str | None = None,
        repo: str | None = None,
        number: str | None = None,
    ) -> dict:
        """Attach an MR/PR/commit/issue proof link to a task.

        A forge URL (github/gitlab pull, merge_request, commit, issue) is
        parsed into a structured ref; other URLs are stored unverified and
        do NOT satisfy the building->review gate. Append-only — only humans
        can trash proofs.
        """
        actor = _actor()
        conn = _conn()
        try:
            return proofs_mod.add_proof(
                conn,
                actor,
                task_id,
                url,
                provider=provider,
                repo=repo,
                number=number,
                kind=kind,
            )
        finally:
            conn.close()

    @mcp.tool
    def task_proof_list(task_id: int) -> dict:
        """List a task's proof links (with cached provider state)."""
        _actor()
        conn = _conn()
        try:
            return {"proofs": proofs_mod.list_proofs(conn, task_id)}
        finally:
            conn.close()

    @mcp.tool
    def task_proof_check(task_id: int) -> dict:
        """Refresh provider state for a task's MR/PR proofs.

        Queries only fixed provider API hosts, and only when a read-only
        provider token is configured; without one this is inert.
        """
        _actor()
        conn = _conn()
        try:
            return {"proofs": proofs_mod.check_task(conn, task_id)}
        finally:
            conn.close()

    @mcp.tool
    def discussion_start(
        project_id: int, title: str, kind: str = "info", body: str = ""
    ) -> dict:
        """Start a discussion — THE comment surface. Link it for context."""
        actor = _actor()
        conn = _conn()
        try:
            return {
                "id": comms.create_discussion(
                    conn, actor, project_id, title, kind=kind, body=body
                )
            }
        finally:
            conn.close()

    @mcp.tool
    def discussions(project_id: int) -> dict:
        """List a project's discussions."""
        _actor()
        conn = _conn()
        try:
            return {
                "discussions": [
                    dict(r) for r in comms.list_discussions(conn, project_id)
                ]
            }
        finally:
            conn.close()

    @mcp.tool
    def discussion_get(discussion_id: int) -> dict:
        """Discussion with its comments (trashed hidden from agents)."""
        actor = _actor()
        conn = _conn()
        try:
            return {
                "comments": [
                    dict(r) for r in comms.list_comments(conn, discussion_id, actor)
                ]
            }
        finally:
            conn.close()

    @mcp.tool
    def comment_add(
        discussion_id: int, body: str, parent_id: int | None = None
    ) -> dict:
        """Comment inside a discussion (nesting up to depth 4)."""
        actor = _actor()
        conn = _conn()
        try:
            return {
                "id": comms.add_comment(
                    conn, actor, discussion_id, body, parent_id=parent_id
                )
            }
        finally:
            conn.close()

    @mcp.tool
    def chat_say(project_id: int, body: str) -> dict:
        """Say something in the project chat."""
        actor = _actor()
        conn = _conn()
        try:
            return {"id": comms.post_chat(conn, actor, project_id, body)}
        finally:
            conn.close()

    @mcp.tool
    def chat_read(project_id: int, since_id: int = 0) -> dict:
        """Read project chat after `since_id`."""
        _actor()
        conn = _conn()
        try:
            return {
                "messages": [
                    dict(r)
                    for r in comms.list_chat(conn, project_id, since_id=since_id)
                ]
            }
        finally:
            conn.close()

    @mcp.tool
    def question_ask(
        project_id: int,
        body: str,
        to_identity_id: int | None = None,
        to_group: str | None = None,
        attach_type: str | None = None,
        attach_id: int | None = None,
    ) -> dict:
        """Ask a blocking question (one of to_identity_id / to_group).
        Attaching freezes that object for everyone until answered."""
        actor = _actor()
        conn = _conn()
        try:
            qid = questions_mod.ask(
                conn,
                actor,
                project_id,
                body,
                to_identity_id=to_identity_id,
                to_group=to_group,
                attach_type=attach_type,
                attach_id=attach_id,
            )
            return {"id": qid}
        finally:
            conn.close()

    @mcp.tool
    def question_answer(question_id: int, answer: str) -> dict:
        """Answer (addressee or group member only); unfreezes the object."""
        actor = _actor()
        conn = _conn()
        try:
            return dict(questions_mod.answer(conn, actor, question_id, answer))
        finally:
            conn.close()

    @mcp.tool
    def question_withdraw(question_id: int) -> dict:
        """Withdraw your own question (admins may force-withdraw)."""
        actor = _actor()
        conn = _conn()
        try:
            return dict(questions_mod.withdraw(conn, actor, question_id))
        finally:
            conn.close()

    @mcp.tool
    def questions(
        open_only: bool = True,
        to_me: bool = False,
        attach_type: str | None = None,
        attach_id: int | None = None,
    ) -> dict:
        """List questions (optionally open only, addressed to you, attached
        to one object)."""
        actor = _actor()
        conn = _conn()
        try:
            rows = questions_mod.list_questions(
                conn,
                open_only=open_only,
                to_actor=actor if to_me else None,
                attach_type=attach_type,
                attach_id=attach_id,
            )
            return {"questions": [dict(r) for r in rows]}
        finally:
            conn.close()

    @mcp.tool
    def decision_record(project_id: int, title: str, context: str = "") -> dict:
        """Record a proposed decision (humans accept/reject/supersede)."""
        actor = _actor()
        conn = _conn()
        try:
            return {
                "id": decisions.create(conn, actor, project_id, title, context=context)
            }
        finally:
            conn.close()

    @mcp.tool
    def note_save(project_id: int, title: str, body: str = "", tags: str = "") -> dict:
        """Create or update a project note by exact title (revision kept)."""
        actor = _actor()
        conn = _conn()
        try:
            existing = conn.execute(
                "SELECT id FROM notes WHERE project_id = ? AND title = ?"
                " ORDER BY updated_at DESC LIMIT 1",
                (project_id, title.strip()),
            ).fetchone()
            if existing:
                row = knowledge.edit_note(
                    conn, actor, existing["id"], body=body, tags=tags
                )
                return {"id": existing["id"], "note": dict(row)}
            nid = knowledge.create_note(
                conn, actor, project_id, title, body=body, tags=tags
            )
            return {"id": nid}
        finally:
            conn.close()

    @mcp.tool
    def wiki_save(slug: str, title: str, body: str = "") -> dict:
        """Create or update a global wiki page by slug (revision kept)."""
        actor = _actor()
        conn = _conn()
        try:
            page = conn.execute(
                "SELECT id FROM wiki WHERE slug = ?", (slug,)
            ).fetchone()
            if page:
                row = knowledge.edit_wiki(conn, actor, slug, title=title, body=body)
                return {"id": page["id"], "page": dict(row)}
            wid = knowledge.create_wiki(conn, actor, slug, title, body=body)
            return {"id": wid}
        finally:
            conn.close()

    @mcp.tool
    def wiki_get(slug: str) -> dict:
        """Read a wiki page with its revision history."""
        _actor()
        conn = _conn()
        try:
            return {
                "page": dict(knowledge.get_wiki(conn, slug)),
                "revisions": [dict(r) for r in knowledge.wiki_revisions(conn, slug)],
            }
        finally:
            conn.close()

    @mcp.tool
    def claims_set(paths: list[str], note: str = "") -> dict:
        """Claim the paths you are about to work on; re-claim refreshes."""
        actor = _actor()
        conn = _conn()
        try:
            return {"claims": claims.set_claims(conn, actor, paths, note=note)}
        finally:
            conn.close()

    @mcp.tool
    def claims_check(path: str) -> dict:
        """Who else has claimed this path (or parents/children)? Stale
        claims are marked; coordinate via a discussion before touching."""
        actor = _actor()
        conn = _conn()
        try:
            return {"claims": claims.check_claims(conn, path, actor)}
        finally:
            conn.close()

    @mcp.tool
    def claims_release(paths: list[str]) -> dict:
        """Release your claims on these paths."""
        actor = _actor()
        conn = _conn()
        try:
            return {"claims": claims.release_claims(conn, actor, paths)}
        finally:
            conn.close()

    @mcp.tool
    def link_add(from_type: str, from_id: int, to_type: str, to_id: int) -> dict:
        """Link any two objects (first-class context, listed both ways)."""
        actor = _actor()
        conn = _conn()
        try:
            return {"id": links.create(conn, actor, from_type, from_id, to_type, to_id)}
        finally:
            conn.close()

    @mcp.tool
    def context_get(obj_type: str, obj_id: int) -> dict:
        """Links to/from an object, both directions."""
        _actor()
        conn = _conn()
        try:
            return {"links": links.context_for(conn, obj_type, obj_id)}
        finally:
            conn.close()

    @mcp.tool
    def events_feed(
        since: int = 0, project_id: int | None = None, limit: int = 100
    ) -> dict:
        """The append-only event log (hash-chained)."""
        _actor()
        conn = _conn()
        try:
            return {
                "events": events.feed(
                    conn, since=since, project_id=project_id, limit=limit
                )
            }
        finally:
            conn.close()

    @mcp.tool
    async def wait(
        obj_type: str | None = None,
        obj_id: int | None = None,
        project_id: int | None = None,
        to_me: bool = True,
        timeout: float = 30.0,
    ) -> dict:
        """Block until a matching event exists. to_me drains your durable
        inbox (addressed work finds you even after downtime)."""
        actor = _actor()
        rows = await realtime.wait_for(
            actor["id"],
            obj_type=obj_type,
            obj_id=obj_id,
            project_id=project_id,
            to_me=to_me,
            timeout=timeout,
            db_path=db.db_path(),
        )
        return {"events": rows}

    @mcp.tool
    def search(
        query: str, project_id: int | None = None, kind: str | None = None
    ) -> dict:
        """FTS search across tasks, discussions, comments, decisions,
        questions, notes, wiki."""
        _actor()
        conn = _conn()
        try:
            return {
                "hits": search_mod.search(conn, query, project_id=project_id, kind=kind)
            }
        finally:
            conn.close()
