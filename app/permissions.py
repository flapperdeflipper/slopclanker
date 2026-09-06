"""The permission engine: can(actor, action, obj) — DESIGN §4 matrix.

REST and MCP are both thin transports over this one gate. The action
catalog grows with the build phases; identity-level actions only here.
"""

import sqlite3
from typing import Any

APPROVE_REGISTRATION = "identities.approve"
REJECT_REGISTRATION = "identities.reject"
VIEW_IDENTITIES = "identities.view_admin"
REVOKE_IDENTITY = "identities.revoke"
ISSUE_CODE = "identities.issue_code"
CREATE_USER = "users.create"
REVOKE_CREDENTIAL = "credentials.revoke"
STACKS_MANAGE = "stacks.manage"
PROJECTS_CREATE = "projects.create"
PROJECT_EDIT = "projects.edit"
PROJECT_ARCHIVE = "projects.archive"
PROJECT_PURGE = "projects.purge"
PROJECT_ADOPT = "projects.adopt"
TASKS_CREATE = "tasks.create"
TASKS_ASSIGN = "tasks.assign"
TODOS_ADD = "todos.add"
TODOS_TICK = "todos.tick"

_ANY_ACTIVE = {
    PROJECTS_CREATE,
    TASKS_CREATE,
    TASKS_ASSIGN,
    TODOS_ADD,
    TODOS_TICK,
}

_ADMIN_ACTIONS = {
    APPROVE_REGISTRATION,
    REJECT_REGISTRATION,
    VIEW_IDENTITIES,
    ISSUE_CODE,
    REVOKE_CREDENTIAL,
}


def is_admin(actor: sqlite3.Row | dict | None) -> bool:
    return (
        actor is not None
        and actor["kind"] == "human"
        and actor["role"] in ("admin", "superadmin")
    )


def can(
    actor: sqlite3.Row | dict | None, action: str, obj: dict[str, Any] | None = None
) -> bool:
    """May this actor perform this action on this object?"""
    if actor is None or actor["status"] != "active":
        return False
    if action in _ANY_ACTIVE:
        return True
    if action == PROJECT_EDIT:
        if obj is None:
            return False
        if obj.get("owner_id") == actor["id"]:
            return True
        return actor["kind"] == "human" and actor["role"] == "superadmin"
    if actor["kind"] != "human":
        return False
    if action == REVOKE_CREDENTIAL and obj and obj.get("identity_id") == actor["id"]:
        return True
    role = actor["role"]
    if role == "superadmin":
        return not (
            action == REVOKE_IDENTITY and obj and obj.get("target_role") == "superadmin"
        )
    if role == "admin":
        if action == CREATE_USER:
            return bool(obj) and obj.get("role") == "user"
        if action == REVOKE_IDENTITY:
            return not (obj and obj.get("target_kind") == "human")
        if action in (PROJECT_ARCHIVE, PROJECT_PURGE):
            return obj is not None and obj.get("owner_id") == actor["id"]
        if action == STACKS_MANAGE or action == PROJECT_ADOPT:
            return True
        return action in _ADMIN_ACTIONS
    return False
