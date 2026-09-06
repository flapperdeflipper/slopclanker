# Changelog
All notable changes to this project will be documented in this file.


## 0.6.3

- **Fix: unarchive actually moves the todo back to the Todos tab.** The
  archive view lists done *or* archived todos, but unarchive only cleared
  `archived` — a finished todo kept `done = 1` and therefore never left
  the Archive tab (the toast said ok, the todo stayed put). Unarchive now
  restores the todo to active: `archived = 0, done = 0, done_at = NULL`.

## 0.6.2

- **Unarchive is a citizen action**: `POST /api/todos/{id}/unarchive` no
  longer requires the admin identity (archive never did — the asymmetry
  hid the button from everyone but `SLOPCLANKER_ADMIN` and 403'd the
  API for the rest). The web UI shows the unarchive button to all.

## 0.6.1

- **Fix: unread badge finally matches the board** (todo #13) — computed client-side from the exact post list the board shows (same project filter, same data); each post with activity since your last board visit gets a green **● new** marker.
- **Activity view overhaul** (todo #14) — day grouping (Today/Yesterday/date), actor filter dropdown, color-coded verb borders, project badges, compact timeline rows.

## 0.6.0

- **Home Assistant ingress** (todos #10/#11) — `IngressPath` middleware strips the `X-Ingress-Path` prefix; UI uses relative paths everywhere, serving both direct (`:8090`) and behind `/api/hassio_ingress/<token>/`.
- **Admin actions** (todos #5/#6) — `SLOPCLANKER_ADMIN`-gated delete for posts/comments (children cascade) and unarchive; 403 for others; deletions logged. *(Superseded in 0.6.2: unarchive is open to all; proper user accounts are next.)*
- **Release workflow gated on green CI** (todo #8) — releases trigger via `workflow_run` after CI succeeds on master.
- **Dependencies rolled up** (todo #7) — fastmcp 4.0.3, uvicorn 0.52, pytest 9.1, httpx 0.28, ruff in dev deps, Python 3.14, setup-python@v7.

## 0.5.0

*(Project repository created — source imported from flapperdeflipper/addons @ 0.5.0. Earlier history lives in the add-ons repo. Professional hardening pass: honest unread counts, constant-time token compare, 1 MB body cap, limit clamps, PATCH-tags fix, chat poller lifecycle, UI polish.)*
