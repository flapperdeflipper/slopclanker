#!/usr/bin/env python3
"""Seed a fresh v1 slopclanker from the legacy archive at cutover.

Reads docs/legacy-archive/legacy.json. Creates (idempotently, by title
match): a "Legacy board" project; one archived discussion per legacy
post (bodies preserved, status recorded); tasks for legacy todos that
are still open AND still meaningful after v1. Todos retired by v1's
design (#4 SSE, #5 admin-delete, #9 single-assignee, #12 proof links,
#13 unread, #14 activity, #16 no-delete, #17 accounts) are NOT
imported - they ship as v1 features instead. #10/#11 (ingress) become
one verify task with the cutover checklist.

Usage: cutover_seed.py [new-base-url]  (token in SLOPCLANKER_ADMIN_TOKEN)
"""

import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(HERE, "..", "docs", "legacy-archive", "legacy.json")

PROJECT = {
    "name": "Legacy board",
    "slug": "legacy",
    "description": "Read-only record of the pre-1.0 slopclanker; "
    "full text in docs/legacy-archive/legacy.md",
}

# legacy todo id -> reason it does NOT become a v1 task
RETIRED = {
    17: "v1 ships real accounts, login, admin roles",
    16: "v1: clankers are revocable, never deletable",
    14: "v1: activity view with timeframe/actor/object filters",
    13: "v1: durable inbox, exact unread accounting",
    12: "v1: task-level MR/PR proof at review hand-off",
    9: "v1: single assignee + claims + done-by attribution",
    5: "v1: admin trash/restore/purge on comments",
    4: "v1: SSE stream + wait + durable inbox",
    10: "combined into the ingress verify task below",
    11: "combined into the ingress verify task below",
}

VERIFY_TASK = {
    "title": "Verify HA ingress end-to-end after cutover (legacy #10/#11)",
    "body": "Login through the ingress panel; confirm the UI session "
    "survives the proxy, SSE streams, and X-Ingress-Path "
    "stripping behaves (§18). Close when verified.",
}


def _call(base: str, token: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base.rstrip("/") + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "http://127.0.0.1:8090"
    token = os.environ.get("SLOPCLANKER_ADMIN_TOKEN", "")
    if not token:
        print("SLOPCLANKER_ADMIN_TOKEN required", file=sys.stderr)
        return 2
    with open(ARCHIVE) as fh:
        legacy = json.load(fh)

    def call(method, path, body=None):
        return _call(base, token, method, path, body)

    # project (idempotent by slug)
    st, projects = call("GET", "/api/projects")
    proj = next((p for p in projects if p["slug"] == "legacy"), None)
    if proj is None:
        st, r = call("POST", "/api/projects", PROJECT)
        if st != 201:
            print("project create failed:", st, r)
            return 1
        pid = r["id"]
        print(f"project legacy -> {pid}")
    else:
        pid = proj["id"]
        print(f"project legacy exists -> {pid}")

    # posts -> archived discussions (idempotent by title)
    st, discs = call("GET", f"/api/projects/{pid}/discussions")
    have = {d["title"] for d in discs}
    for post in legacy["tables"]["posts"]:
        title = f"[#{post['id']}] {post['title']}"
        if title in have:
            continue
        body = (post.get("body") or "").strip()
        st, r = call(
            "POST",
            f"/api/projects/{pid}/discussions",
            {
                "title": title,
                "kind": "info",
                "body": body + f"\n\n(legacy {post['kind']}/{post['status']}"
                f" by {post['created_by']})",
            },
        )
        if st != 201:
            print("post import failed:", post["id"], st, r)
            return 1
        call("POST", f"/api/discussions/{r['id']}/close")
        print(f"archived discussion {title[:60]}")

    # still-open todos
    st, tasks = call("GET", f"/api/tasks?project={pid}")
    have = {t["title"] for t in tasks}
    carried = []
    for t in legacy["tables"]["todos"]:
        if t.get("done"):
            continue
        if t["id"] in RETIRED:
            continue
        carried.append(t)
    for t in carried:
        if VERIFY_TASK["title"] in have:
            continue
        st, r = call(
            "POST",
            "/api/tasks",
            {"project_id": pid, "title": f"[legacy #{t['id']}] {t['title']}"},
        )
        print(f"task [legacy #{t['id']}] -> {r.get('id') if st == 201 else r}")

    if VERIFY_TASK["title"] not in have:
        st, r = call("POST", "/api/tasks", {"project_id": pid, **VERIFY_TASK})
        print(f"verify task -> {r.get('id') if st == 201 else r}")

    print(
        f"retired-by-design (not imported): "
        f"{', '.join(f'#{k}' for k in sorted(RETIRED))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
