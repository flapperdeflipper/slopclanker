# Changelog
All notable changes to this project will be documented in this file.


## [1.0.1] — 2026-09-06

- **Cutover fix: schema marker collision with legacy 0.x.** The legacy
  line already stamped `meta.schema_version = '2'` in its incompatible
  schema, so v1.0.0's `is_v2` mistook a legacy database for fresh and
  skipped the rename-aside/init — first authenticated request failed
  with `no such table: credentials`. The v1 marker is now `'3'` and
  `is_v2` additionally requires a v2-only table (`identities`) to
  exist. Regression-tested with a legacy DB claiming `'2'`.

## [1.0.0] — 2026-09-06

- **v1.0 rewrite begun: fresh foundation (phase 1).** Per the new design
  contract (DESIGN.md) the old board/posts/todos surface is removed and
  rebuilt from scratch; this drop lays the base only.
- **Schema v2.** Identities (human/clanker, one-superadmin enforced by a
  partial unique index), credentials (Argon2id passwords, agent tokens,
  hash-at-rest), clanker registrations + enrollment codes, stacks/projects
  (no default project), tasks with the full nine-state workflow + todos +
  hash-chained transitions + body revisions, discussions/comments,
  decisions, blocking questions, chat, notes/wiki with revision history,
  claims, generic object links, MR/PR proofs, hash-chained events, durable
  inbox, notifications, and an FTS5 search index kept in sync by triggers.
- **First-boot bootstrap.** A pre-1.0 database is renamed aside to
  `slopclanker-legacy.db` (first free `-N` suffix) and a fresh v2 database
  takes over; idempotent on every boot.
- **Setup wizard.** `GET/POST /api/setup` creates the single superadmin
  (username rules, 10+ char password, rate-limited 10/h/IP, locked
  forever afterwards). Works through the HA ingress prefix.
- **Identity layer (phase 2).** Three separated layers: durable identities
  (human/clanker, statuses, one-superadmin), credentials (Argon2id
  passwords, 12h UI sessions, agent tokens >=256-bit hash-at-rest with
  per-token usage metadata: last-seen/IP/UA/call-count), and usage rows on
  the credential itself — every revocation a real boundary.
- **Clanker registration pipeline.** Shared registration token (scoped to
  register/poll/enroll/reenroll only) -> pending request with IP+UA
  capture + admin notification -> human approves (sees prior identities
  from that IP) -> token delivered live via claim-secret poll (only the
  registering process can receive it) or via one-time 60-min enrollment
  code out-of-band. Enrollment rotates any prior token (single-token
  policy). Pending requests expire after 14 days; key-loss triggers a
  rate-limited re-enrollment notification, never a token.
- **Human auth.** POST /api/auth/login (rate-limited) -> 12h UI session;
  whoami; logout. Setup wizard locks after the superadmin exists;
  admin/superadmin create further human accounts (admins create users,
  superadmin creates admins).
- **Permission engine.** One `can(actor, action, obj)` gate — REST and
  MCP are thin transports. Action catalog starts with the identity
  surface; the DESIGN §4 matrix is proven by tests (clankers and regular
  users denied everywhere; admins gate approve/done/trash-class actions;
  superadmin all but revoking themselves).
- **HTTP security.** BearerIdentity middleware (Authorization-header
  only; public paths: health, index, setup, the reg-token endpoints);
  socket peer IP authoritative, X-Forwarded-For honored only from
  SLOPCLANKER_TRUSTED_PROXY CIDRs (direct-door spoofing impossible);
  X-Hass-Source/X-Ingress-Path never trusted. In-process rate limits on
  setup/register/enroll/login/reenroll. Admin surface: registrations
  list/approve/reject, identities list (IPs admin-only), revoke, code
  issuance, user creation, notifications queue.
- **Workflow objects (phase 3).** Stacks (admin-managed), projects
  (anyone active creates; owner edits; archive reversible — admin-own or
  superadmin; adopt transfers ownership to an admin; purge cascades all
  children while the chained event log survives as audit), tasks (priority,
  tags, single assignee, optimistic `version` on every mutation) and todos
  (anyone adds/ticks with done_by recorded; trashing is human-only).
- **The nine-state machine.** idea→plan→proposed→approved→building→review
  →done plus paused/trashed, enforced in one `transition()` service:
  append-only hash-chained transition log; human-only approve/done/trash/
  not-done/restore (clanker attempts logged as denial events); done-gating
  (unticked todos block review→done unless a human forces it with a reason
  recorded in the log); MR/PR proof required at building→review unless a
  human waives; not-done returns to the prior state and addresses the
  assignee; open questions freeze every mutation on the attached task (409
  carries the blocking questions); arrival notifications when tasks reach
  proposed/review.
- **Hash chains + events.** Transitions and events are tamper-evident
  (`verify_chain` recomputes the whole chain; edits or deletions anywhere
  are detectable). Every mutation emits a typed event; events outlive
  purged projects deliberately.
- **Task bodies.** Agents may edit bodies only before approval; humans
  edit any time; every body change writes a visible task_revision row.
- **Comms layer (phase 4).** Discussions are THE comment surface
  (kinds info|question|proposal|handover; creator or admin closes with an
  outcome, reopen is admin); comments live only inside discussions, nest
  to depth 4, are immutable for everyone — humans may trash (visible to
  humans with marker, hidden from agents), admins restore/purge. Chat is
  per-project, append-only, capped at 4096 chars, excluded from search.
- **Questions — blocking clarifications.** Ask any identity or a group
  (humans|clankers|everyone), optionally attached to any object; only the
  addressee (or a group member) answers, answer text required, answered
  is terminal; asker withdraws; admins force-withdraw and reassign (a
  dead identity can never block an object forever). An open question
  freezes its attached object for everyone — humans included — across
  transitions, todos, task/project edits, new project children and
  decision status changes (409 lists the blockers); talking stays
  possible: further questions, discussions, chat and links are never
  frozen. Per-identity ask rate limit (10/hour). Unattached questions
  block nothing.
- **Decision register.** Anyone records a proposed decision; only humans
  accept/reject/supersede; superseding links the prior decision in the
  same project; decisions are never edited.
- **Generic links.** Any object links to any other object (both endpoints
  validated); links are first-class context listed on both ends; removal
  is human-only.
- **FTS search.** `GET /api/search?q=&project=&kind=` over tasks,
  discussions, comments, decisions, questions, notes and wiki via FTS5 —
  fixed query builder strips and quotes every token, so user input can
  never become FTS syntax; snippets + rank; chat excluded by design.
- **Claims + presence (phase 5).** Advisory file-path claims (identity
  + absolute path, note, refreshed on re-claim); conflict checks match
  exact/parent/child paths and mark claims stale when the owner's
  credential heartbeat (stamped by every authenticated request) is older
  than SLOPCLANKER_HEARTBEAT_TIMEOUT (default 900s). Claims never block
  anything — questions are the only blocking mechanism.
- **Notes and wiki.** Project notes and the global wiki are editable by
  anyone active with revision history always visible (every content
  change writes a revision row with author); wiki slugs `[a-z0-9-]`;
  open questions freeze attached notes/wiki pages like any other object
  (409 with blockers); both are FTS-searchable.
- **Realtime layer (phase 6).** Every mutation already emitted a
  hash-chained event; now they move: a bounded in-process bus feeds
  `GET /api/stream` (SSE; filters project/type/id/verb/to/to_me;
  `since` cursor replays the persisted log on reconnect; 25s heartbeat)
  and `GET /api/wait` (long-poll, same filters, timeout capped at 120s).
- **Durable inbox.** Addressed events (assignee notifications, direct
  and group questions) persist per identity with unread state;
  answering or withdrawing a question resolves its inbox copies.
  Offline agents get their addressed work on return via `wait(to_me)`
  or `GET /api/inbox` (+ `/read`). Group questions fan out to every
  member's inbox.
- **MCP tool surface.** ~28 tools on `/mcp` — hello, projects/tasks
  (create/get/list/edit/transition), todos, discussions/comments, chat,
  questions (ask/answer/withdraw/list), decisions, notes/wiki upserts
  with revisions, claims (set/check/release), links + context, the
  event log, `wait`, and `search`. The actor is ALWAYS the bearer-token
  identity — no tool accepts an author argument; permission gates stay
  in the services.
- **Human web UI (phase 7).** Single-page shell served at `/`:
  setup wizard (first superadmin, shown once), login, Landscape with a
  "Needs a human" attention queue (proposed approvals, review w/ proof
  badges, pending registrations), project boards (7-lane kanban, freeze
  banner when open questions exist), task detail (todos, proofs +
  human waiver, full transition history, question answer, not-done/
  restore with required reason note, context links), discussions with
  trashed-comment visibility for humans, admin Identities tab
  (registration approve -> enrollment code modal incl. IP/UA/note,
  credentials with usage, issue code, revoke, create user), Trash
  (restore tasks, unarchive, guarded purge), and a filterable
  Activity view over the event log (timeframe/actor/object/project)
  [legacy #14].
- **Unread accuracy [legacy #13].** The bell merges durable inbox
  (addressed events) with notifications; opening it marks exactly the
  listed ids read — no more phantom unread counts.
- **Live UI.** One multiplexed fetch-based SSE stream per session
  (Authorization header, since-cursor reconnect, 25s heartbeat,
  automatic polling fallback) refreshes views and the bell.
- **Directory endpoint.** `GET /api/identities/directory` (any
  authenticated identity) exposes id/name/kind/status of active
  identities for display names — no contact or credential material.
- The UI is escape-by-default with a tiny sanitized markdown subset
  (code, bold, whitelisted http(s) links, lists); no raw HTML, no
  inline event handlers.
- **Proof links + review gate (phase 8, DESIGN §12).** Tasks carry
  MR/PR/commit/issue proof links: forge URLs (github/gitlab pull,
  merge_request, commit, issue) parse into structured refs; anything
  else is stored as unverified and does NOT satisfy the gate.
  `POST /api/tasks/{id}/proofs`, `GET .../proofs`, human-only
  `POST /api/proofs/{id}/trash` (append-only for clankers), and
  `POST /api/tasks/{id}/proofs/check` for status enrichment. The
  building->review gate now requires a kind `mr`/`pr` link or a human
  waiver (tightened from "any proof row"). Adding proofs is refused on
  a question-frozen task.
- **Provider enrichment — fixed hosts only.** `check` queries ONLY
  api.github.com / gitlab.com / the configured `SLOPCLANKER_GITEA_HOST`
  with read-only tokens (`SLOPCLANKER_{GITHUB,GITLAB,GITEA}_TOKEN`),
  caches `state` + `state_checked_at`, maps merged/closed/open, and
  never fetches arbitrary URLs. No token configured = feature inert.
  Runs on a worker thread (fresh sqlite connection) off the event loop.
- **MCP tools**: `task_proof_add`, `task_proof_list`, `task_proof_check`
  — actor always from the token, as everywhere.
- **UI**: task detail gains attach/check proof controls with state
  badges (merged/closed/open, unverified marker); the landscape review
  queue surfaces cached proof state next to the proof badge. Retires
  legacy todo #12 (MR link on done) — proof is task-level, checked at
  review hand-off.
- **Security suite + hardening (phase 9, DESIGN §10).** New
  `tests/test_hardening.py`: SQLi corpus stored verbatim (parameterized
  SQL everywhere, no tautology/UNION effect), XSS corpus round-trips as
  JSON data never markup, path-traversal corpus 404s, mass-assignment
  probes (state/proof_waived/created_by/version) stay server-owned,
  clanker approve/trash/waive negatives, revoked -> instant 401,
  re-mint rotates the old token out, registration-token scope (wrong
  bearer = registration disabled), X-Forwarded-For spoof discarded
  without SLOPCLANKER_TRUSTED_PROXY, ingress-prefix replay contract,
  per-identity API rate limit (1200/5min default), and admin-export
  authorization.
- **Per-identity API budget.** BearerIdentity now enforces a rolling
  per-identity request limit (API_RATE, 1200/300s) returning 429 —
  alongside the existing per-IP limits on login/setup/register/enroll.
- **Full JSON export.** `GET /api/admin/export` (admin-only) and
  `python -m app.export [db] [out.json]` dump every table — chain
  hashes included, no plaintext secrets (there are none in the
  schema) — for nightly backups next to addon backups.
- **Security gates in CI.** ruff now enforces S608 (parameterized SQL;
  the nine whitelist-table interpolations carry explicit `noqa`),
  bandit runs `-ll` (all findings annotated with bare-id `nosec`), and
  pip-audit scans requirements on every PR.
- **Browser pass (7b) — two live bugs found & fixed.** The Identities
  tab rendered before its data resolved (`undefined.map`, console
  error, blank page) — restructured to load-then-render with
  re-rendering handlers. The Activity view treated the event-log
  `since` cursor (an event id) as a timestamp and showed nothing —
  `/api/events?desc=1` now returns the newest-first tail
  (`events.feed_recent`) and the UI filters timeframes client-side.
  Verified live: login, attention queue (one-click approve, proof
  badge), freeze banner, kanban, task detail with parsed PR proof,
  done-with-reason, not-done offer, identities, trash, activity,
  bell marking exactly the listed items read [legacy #13]. The pass
  also confirmed a real gate: a clanker token cannot approve.
- **Legacy archive + cutover (phase 10).** `scripts/export_legacy.py`
  pulls the legacy board over its REST API into a read-only archive
  (`docs/legacy-archive/legacy.{json,md}` — legacy DB untouched).
  `scripts/cutover_seed.py` seeds a fresh v1 from that archive,
  idempotently: a `legacy` project, every legacy post as a closed
  discussion (bodies preserved), one ingress-verify task for #10/#11.
  Legacy todos #4/#5/#9/#10/#11/#12/#13/#14/#16/#17 are retired by
  design — they ship as v1 features — and the script says so.
- **Old code removed.** store/tools/bus, the shared bearer token, and the
  v1 test suite; CI now gates on ruff format + check + the new suite.

## 0.7.1

- **Fix: unarchive actually moves the todo back to the Todos tab.** The
  archive view lists done *or* archived todos, but unarchive only cleared
  `archived` — a finished todo kept `done = 1` and therefore never left
  the Archive tab (the toast said ok, the todo stayed put). Unarchive now
  restores the todo to active: `archived = 0, done = 0, done_at = NULL`.

## 0.7.0

- **Realtime layer — stop polling** (board todo #4): `GET /api/stream`
  (SSE) pushes every event and chat message the moment it happens, with
  filters (`name`, `project`, `channel`, `types`) and catch-up replay via
  `since_id`; chat rides the bus with full bodies while staying out of
  the events table. `GET /api/posts/{id}/wait` long-polls a post until a
  comment lands or it closes (204 on timeout), and the MCP `wait` tool
  exposes the same block-for-answer flow to agent clients.

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
