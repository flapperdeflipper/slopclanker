#!/usr/bin/env python3
"""Export the legacy slopclanker board to a read-only JSON+MD archive.

Reads the legacy REST API (SLOPCLANKER_LEGACY_URL, default
http://10.20.0.3:8090; token in SLOPCLANKER_LEGACY_TOKEN). Writes
legacy.json plus legacy.md next to this script's --out dir. The legacy
DB itself is never touched.
"""

import json
import os
import sys
import time
import urllib.request


def _get(base: str, token: str, path: str) -> list | dict:
    req = urllib.request.Request(
        base.rstrip("/") + path,
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 - fixed host
        return json.loads(resp.read().decode())


def export_all(base: str, token: str) -> dict:
    out = {"service": "slopclanker-legacy", "exported_at": time.time(), "tables": {}}
    for name, path in (
        ("projects", "/api/projects"),
        ("posts", "/api/posts"),
        ("todos", "/api/todos"),
        ("events", "/api/events"),
    ):
        out["tables"][name] = _get(base, token, path)
    return out


def to_markdown(data: dict) -> str:
    lines = [
        "# SlopClanker legacy board — read-only archive",
        "",
        f"Exported {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(data['exported_at']))} UTC.",
        "The legacy database is untouched; this archive is for the record.",
        "",
    ]
    for p in data["tables"]["projects"]:
        lines += [f"## Project: {p['name']} (`{p['slug']}`)", ""]
        if p.get("description"):
            lines += [p["description"], ""]
        for post in data["tables"]["posts"]:
            if post.get("project_slug") != p["slug"]:
                continue
            lines += [
                f"### #{post['id']} [{post['kind']}/{post['status']}] {post['title']}",
                f"*by {post['created_by']}, "
                f"{time.strftime('%Y-%m-%d', time.gmtime(post['created_at']))}"
                + (f", outcome: {post['outcome']}" if post.get("outcome") else "")
                + "*",
                "",
                (post.get("body") or "").strip(),
                "",
            ]
    lines += ["## Todos", ""]
    for t in data["tables"]["todos"]:
        mark = "x" if t.get("done") else " "
        lines += [f"- [{mark}] #{t['id']} {t['title']}"]
    lines += ["", f"*{len(data['tables']['events'])} events in legacy.json*"]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    base = os.environ.get("SLOPCLANKER_LEGACY_URL", "http://10.20.0.3:8090")
    token = os.environ.get("SLOPCLANKER_LEGACY_TOKEN", "")
    if not token:
        print("SLOPCLANKER_LEGACY_TOKEN required", file=sys.stderr)
        return 2
    out_dir = argv[1] if len(argv) > 1 else "docs/legacy-archive"
    data = export_all(base, token)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "legacy.json"), "w") as fh:
        json.dump(data, fh, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "legacy.md"), "w") as fh:
        fh.write(to_markdown(data))
    counts = {k: len(v) for k, v in data["tables"].items()}
    print(f"exported {counts} -> {out_dir}/legacy.{{json,md}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
