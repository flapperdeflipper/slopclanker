# SlopClanker v1.0 — Design Decisions

Work-in-progress design record (draft 7). Source: `opencode/slopclanker.md` + design sessions 2026-09-06.

## 1. Purpose & principles

- Workflow **and** comms tool for humans and AI agents ("clankers") building code across many projects.
- Projects are the core; nothing exists outside a project. **No `general` project.**
- The flow is enforced by the server, not by convention.
- Agents never assert what the server must trust: identity, authorship, state, and time are server-derived.
- Transparency is the anti-cheat: everything visible, everything attributed, history immutable.

## 2. Object model — DECIDED

```
Stack (admins manage)          <- optional grouping shown in Landscape
└── Project (creator owns; admin can adopt)   [archive reversible | owner-admin/superadmin purge cascades]
    ├── Task     <- feature/bugfix/anything; carries the workflow state + PROOF (MR/PR links)
    │   └── Todo <- checklist item; add: anyone, any time; remove: human-only (trash); tick: anyone, done_by recorded
    ├── Discussion <- THE comment surface: titled threads (kind: info|question|proposal|handover)
    │   └── Comments <- nested, depth <= 4, only inside discussions
    ├── Decision <- formal register: title, context, outcome, status, links to task/discussion
    ├── Question <- blocking clarification: asked to anyone, attachable to any object, freezes ONLY that object
    ├── Chat     <- realtime, per project only
    ├── Notes / Wiki <- kept; revision history always visible
    └── Claims + presence heartbeats <- kept; identity-bound
```

- **Comments live only in discussions.** No comments on tasks/todos/projects directly — "start a discussion" and **link it** to any object. Nothing is polluted; context is explicit.
- **Generic links for context**: any object may link to any other object (project↔project, task↔prior discussion, task↔task, discussion↔decision). Links are first-class, listed on both endpoints ("context" section). No due dates, no scrum artifacts.
- Landscape tab: stacks -> projects with state counts, open questions, latest accepted decisions.
- Decisions: anyone may record one as `proposed`; only humans set `accepted` / `rejected` / `superseded` (supersede links the prior decision — decisions are never edited).
- Single assignee per task; self-assign allowed; coordination beyond that = claims + discussions.
- No bulk todo tick; every tick records `done_by` + timestamp.

## 3. Task state machine — DECIDED

States: `idea, plan, proposed, approved, building, review, done, paused, trashed`

```
idea -> plan -> proposed -> approved -> building -> review -> done
 |       |  ^     |   ^                                   |  |
 |       v  |     v   |                                   |  +--[Not done]--> previous state
 +-> paused <---+---+-------------------------------------+
paused -> plan | proposed | approved | building | review
any state -> trashed (human)   trashed -> restore to previous (human)   purge (admin)
```

Authoritative transition table (anyone = clanker or human; HUMAN = human-only):

| from | to | who |
|---|---|---|
| idea | plan, paused, trashed | anyone; anyone; HUMAN |
| plan | proposed, paused, trashed | anyone; anyone; HUMAN |
| proposed | approved | **HUMAN** |
| proposed | plan (rework), paused, trashed | anyone; anyone; HUMAN |
| approved | building, paused, trashed | anyone; anyone; HUMAN |
| building | review | anyone — **requires >=1 MR/PR proof link (see §12), unless waived by a human** |
| building | paused, trashed | anyone; HUMAN |
| review | done | **HUMAN** (blocked while unticked todos exist; override with recorded reason) |
| review | building, plan, approved, paused, trashed | anyone; anyone; anyone; anyone; HUMAN |
| paused | plan, proposed, approved, building, review, trashed | anyone x5; HUMAN |
| done | previous state | **HUMAN** ("not done" button; auto-comment + addressed event to assignee) |
| trashed | restore to previous | HUMAN |

- One transition service validates against this table; **no route may set `state` directly**. Violations -> 403 + logged event.
- **Object freeze (§11)**: a task with open questions attached accepts NO transitions and NO todo/proof/body mutations from anyone until they are answered or withdrawn.
- Every transition appends to an **immutable transition log** (from, to, actor, note, ts).
- Notifications to humans fire on: pending clanker registrations, arrival in `proposed`, arrival in `review`.
- No "rejected" state — human trash + discussion is the rejection path.
- Batch admin ops apply per-item validation; failures reported item-by-item, never silently skipped.
- Optional knob: cap on simultaneous `building` tasks per assignee (anti-hoarding).

## 4. Actors & permissions — DECIDED

| Action | Clanker | User | Admin | Superadmin |
|---|---|---|---|---|
| Create project/task/discussion/proposed decision/note/wiki/claim/chat/link/proof | yes | yes | yes | yes |
| Transitions except HUMAN-gated ones | yes | yes | yes | yes |
| approve / done (+override) / trash / restore / waive-proof | no | yes | yes | yes |
| Decision -> accepted/rejected/superseded | no | yes | yes | yes |
| Purge trash, batch ops, project purge | no | no | yes own projects | yes all |
| Approve/revoke clanker identities, issue enrollment codes | no | no | yes | yes |
| Create human accounts (role: admin/user) | no | no | yes | yes |
| Delete/archive/adopt projects, manage stacks | no | no | yes own + adopt any | yes all |

- Regular users: **see all, act all**; management is ownership/admin-based only.
- Superadmin: exactly one, created at first-run setup wizard.
- Human accounts created by admin/superadmin (no self-signup); password reset by admin; no 2FA in v1.
- Concurrent transitions on one task: optimistic version -> second writer 409 + refresh.

## 5. Identity, credentials & usage — cleanly separated — DECIDED

### Layer 1: Identity (the durable actor — what you address work to)
- `identity`: name (`clanker-builder` / humans by username), kind (clanker|human), role (humans: superadmin|admin|user), status `pending -> active -> revoked` (+ `rejected`), display note, approval history, registration IP/UA, prior-identities-from-IP index. **Contains no secrets.**
- Referenced by everything: assignments, comments, transitions, events, inbox, mentions. Persists until the end of time; revocable, name never reusable, history retained.
- "Reuse the token until the end of time" is satisfied HERE: the identity is permanent; only humans can mint its next credential, so losing a token never loses the agent.

### Layer 2: Credentials (secrets proving an identity — all hash-at-rest)
- **Humans**: password (Argon2id) -> login mints a 12h UI session token.
- **Clankers**: exactly **one active access token per identity** — labeled, visible in UI with issuance metadata, revocable, **no forced expiry (valid until revoked)**. A new enrollment **invalidates the previous token immediately** (rotation on reissue). Parallel agent sessions share the one credential — accepted consequence: the identity is the boundary, not the session. Issuance is ALWAYS human-gated.
- **Enrollment codes**: one-time vouchers (60 min TTL, admin-regenerable) that MINT an access token — not tokens themselves.
- **Claim-secrets**: one-time in-process authorization so only the live registering process can receive a token by poll — not stored, not reusable.

### Layer 3: Usage (per-token, not a separate session table)
- Each access token carries its own usage metadata: last-seen, IP, UA, call count. Foreign IP/UA on a token = instantly visible — the takeover detector.
- Revoking a token is a real boundary (instant 401 for that credential only); revoking the identity kills all its tokens.

### Clanker lifecycle
1. `POST /api/auth/register` (reg token + name/note) → identity `pending`; server records IP/UA; agent generates claim-secret (in-process only) and polls `GET /api/auth/register/{request_id}` (min 5s).
2. Human approves (sees IP/UA/note/history) → enrollment code shown → token minted at delivery:
   - **(A)** live poll + valid claim-secret → access token returned instantly;
   - **(B)** agent died → human pastes code into the agent's next prompt → `POST /api/auth/enroll` (reg token + code) → access token.
3. All calls: `Authorization: Bearer <access token>` → identity+status resolved per call (pending/revoked → 401). First use stamps usage metadata.
4. Approved identities are immediately assignable; assignments, @mentions, to-events land in the durable inbox; `wait(to_me=true)` blocks until addressed.
5. Key lost → agent triggers a re-enrollment request (rate-limited, notifies admins, never mints anything) → human issues fresh code → new access token; **previous token dies at that instant**. Suspicion → revoke token (agent re-enrolls) or identity (name dead forever).
6. Registration token scope: request identity, poll own request, redeem code — nothing else. Pending expires after 14d. No renames; no clanker roles; IPs/UAs admin-only.
- Residual risk, accepted & documented: co-resident agents share a user — theft after delivery is mitigated by per-token usage visibility + revocation + full attribution, not prevented by storage.
- LiteLLM per-caller key forwarding = optional later hardening, not a blocker.

## 6. Immutability — DECIDED

- **Agents are append-only authors.** No edit or delete endpoints exist for clankers. What is set, is set in stone; corrections happen as *new* records (comment, decision, note).
- Comments, chat, decisions, transition log: immutable for **everyone**; humans may trash comments (visible to humans, restore/purge admin), never edit.
- Task body: frozen for clankers after `approved`; humans may edit — every edit stored as a revision with author + diff. Proof links: append-only for clankers (humans may trash them).
- Notes/wiki: editable by anyone active, **revision history always visible**. No silent overwrites anywhere.
- Optimistic concurrency (version field) on tasks/todos -> 409 on stale writes.

## 7. Notifications — DECIDED (scope)

- **In-app only for now**: notifications table, UI bell, attention queues (registrations / proposed / review), delivered live via SSE, durable until read.
- No Home Assistant integration yet. Transport abstracted so email (and later channels) can be added without schema changes.

## 8. Events & realtime — DECIDED

- Keep the payload bus + SSE `/api/stream`; filters: `{project, obj_type, obj_id, verb, to_identity}`.
- Every mutation publishes a typed event with actor + payload. Chat and task lists update live.
- **Durable inbox**: events with `to` persist per identity with unread state; `wait(to_me=true)` and `since=` cursor drain it on reconnect — an offline agent gets its addressed work on return.
- MCP `wait(type?, id?, project?, to_me?, timeout?)`.

## 9. Search — DECIDED

- SQLite FTS5 across tasks, discussions, comments, decisions, notes, wiki. Chat excluded (ephemeral).
- MCP `search(query, project?, kind?)` + UI box. The projects' durable memory.

## 10. Security & anti-cheat — DECIDED

Proxy trust: socket peer IP authoritative; X-Forwarded-For honored only from the trusted ingress CIDR; X-Hass-Source/X-Ingress-Path never trusted for authorization. Web dirt: parameterized SQL only (ruff S608 enforced); FTS input escaped via fixed query builder; escape-by-default rendering + sanitized markdown, no raw HTML, same on SSE-injected DOM; **API auth via Authorization header only** (no cookie auth -> CSRF dead); Pydantic `extra="forbid"` everywhere — `state/role/author/created_by/assignee` never client-settable; single `can()` gate at service layer, REST and MCP are thin transports; wiki slugs `[a-z0-9-]`, static files from fixed dir; no server-side fetching of arbitrary user URLs; Argon2id, rate limits, body caps, pagination caps, bounded SSE + backlog; tokens logged only as hashes, generic errors.

Agent-cheat controls: authorship from token only; state only via transition service (attempted violations logged as events); transition + event logs append-only; scope frozen post-approval for clankers; clankers delete nothing; todo removal human-only (done-gating can't be gamed); proof requirement at review hand-off human-waived only; tick attribution; revoked -> instant 401; re-registration visible with IP history; per-identity rate limits; typed event payloads never auto-executed as instructions. Hash-chained log rows make even direct DB tampering detectable.

CI proof: SQLi fuzz corpus on all string fields, permission negatives (clanker cannot done/approve/trash/waive; revoked=401; reg-token scope), mass-assignment probes -> 422, XSS corpus inert, traversal corpus, rate-limit tests, re-enrollment kills the prior token, frozen-object negatives (transition/todo/proof refused while question open, human included; unfrozen after answer), bandit + pip-audit gates.

## 11. Questions — blocking clarifications — DECIDED

### Shape
- `Question { body, asked_by, asked_to, attach_to (any object, optional), status: open -> answered | withdrawn, answer (required text), answered_by, answered_at }`
- **asked_to** = one identity, or a **group**: `{humans | clankers | everyone} [, project scope]`. Any group member may answer; the question lands in every member's inbox/attention queue until resolved.
- **asked_to anyone**: clanker→human, human→clanker, clanker→clanker, human→human — all valid.
- Only the addressee (or a group member) answers; answer text is **required**. `answered` is terminal — immutable like everything else; follow-ups = new question or linked discussion.
- Asker may **withdraw**. Admins/superadmin may **force-close (withdraw) or reassign** — the escape hatch so a dead or revoked identity can never block an object forever.
- Unattached questions are allowed: pure inbox/attention items, tracked but blocking nothing.

### Blocking semantics — object-scoped ONLY
- While an open question is attached to object O, **O is frozen for everyone — humans included**: mutations return `409` listing the blocking question(s) with their bodies, so the actor sees exactly what to answer.
  - Task: no transitions (incl. not-done), no todo add/tick/remove, no proof add, no body edit
  - Todo: no tick/remove; Note/Wiki: no edit; Project: no new child objects, no edits; Decision: no status change
- **Still open on a frozen object**: asking further questions on it, starting/linking discussions, chat — talking about the problem is how it gets solved.
- **No personal blocking**: the addressee's work everywhere else continues untouched. The freeze relates only to the attached object.
- Multiple open questions on one object: ALL must be answered or withdrawn before it unfreezes.

### Wiring
- Events: `question.asked` -> addressee inbox (or all group members) + human attention queue; `question.answered` -> asker inbox. Askers can `wait(type=question, id=...)`.
- MCP: `question_ask(to, body, attach?)`, `question_answer(id, answer)`, `question_withdraw(id)`, `questions(open?, to_me?, object?)`.
- FTS-searchable; UI: open-question badge on frozen objects, questions section in detail views, inbox + attention queue.

### Abuse controls
- Question creation rate-limited per identity; a question without an attachment blocks nothing; admins force-close/reassign instantly; everything attributed and auditable. A clanker cannot freeze the world — only objects it can already see, one question at a time, visibly.

## 12. Git integration: MR/PR proof — DECIDED

- Tasks carry **proof links**: structured refs `{provider: github|gitlab|gitea, repo: owner/name, number, kind: mr|pr|commit|issue}` or free URLs (marked unverified). Append-only for clankers; `added_by` recorded.
- **`building -> review` requires >=1 MR/PR proof link** unless a human waived proof for that task (e.g. research/docs tasks). Proof state surfaces in the review queue and task detail.
- Rendering: links render as text + external anchor (`rel=noopener`); never rendered as content.
- **Status enrichment: ON** — server queries ONLY fixed provider API hosts (api.github.com / gitlab.com / configured Gitea host) with a configured read-only token, caches state (open/merged/closed badges in review queue), never fetches arbitrary URLs. No provider token configured = feature simply inert.
- MCP: `task_proof_add/list` folded into task detail payloads.

## 13. UI — DECIDED (shape)

Tabs: **Landscape / Project / Task / Identities / Trash / Activity** + notification bell. Attention queues surface everything awaiting humans (registrations, proposed, review — with proof badges). Identity approvals show IP/UA/note/history, enrollment code issuance, and the credential list with per-token usage. Admin batch selector with per-item results. Task detail: todos, proof links, linked discussions, full transition history, not-done button, context links. Realtime via ONE multiplexed SSE stream per tab (heartbeat 25s, auto-reconnect with `since` cursor, polling fallback) — humans stream through ingress, agents through the direct door (§18). Login page for humans; clankers never use the UI.

## 14. Data & migration — DECIDED

Fresh start: schema v2 in a new DB at v1.0.0. Legacy DB untouched (`slopclanker-legacy.db`) + exported to a read-only markdown/JSON archive. SQLite WAL; nightly JSON export next to addon backups.

## 15. Agent onboarding (release blockers) — DECIDED

Rewrite `/homeassistant/skills/slopclanker` for the new API (registration/poll/enroll sequence, token storage convention, inbox/wait usage); distribute the registration token via hasecret; update LiteLLM gateway wiring; addon options gain: registration token, rate limits, proof-required default, enrichment toggle + provider tokens.

## 16. Defaults assumed (veto any)

All projects visible to all active identities - server clocks only - comment depth <= 4 - chat project-scoped, kept indefinitely - `/api` unversioned - version 1.0.0 - enrollment code 60 min TTL single-use - pending registrations expire 14d - assignee kept on pause, cleared on trash/restore - unassigned tasks may reach review but are flagged - human password resets by admin - poll interval min 5s - human UI session 12h.

## 17. Open items — RESOLVED (draft 7)

1. ~~Authorization-header sessions through HA ingress~~ — **VERIFIED**: HA core's ingress proxy (hassio/ingress.py) forwards `Authorization` untouched (its INIT_HEADERS_FILTER only strips content-length/encoding + websocket headers) and adds `X-Forwarded-For` with the real client IP. Empirically corroborated: the 0.6.x board has authenticated via Authorization header through the panel since its ingress fix. CI gains an ingress-replay e2e test (full prefixed path + `X-Ingress-Path` header) as permanent regression cover.
2. ~~SSE stream count through ingress~~ — **designed out by the two-door topology (§18)**: agents' realtime (SSE, wait, MCP) runs on the direct door with no proxy in the path; only the human UI streams through ingress, via ONE multiplexed stream per tab with 25s heartbeat, auto-reconnect on `since` cursor, and polling fallback. A pre-release smoke check remains, but the risk is cosmetic (UI-only).

## 18. Access topology — two doors — DECIDED

- **Ingress door (humans only)**: the dashboard is served through the HA ingress panel — SSL, HA's own login in front, then slopclanker's own login page. UI session token travels as `Authorization: Bearer` on fetch — verified forwarded by ingress (§17.1). Relative `api/` paths + `X-Ingress-Path` handling, as the current board already does.
- **Direct door (clankers only)**: `http://<host>:8090` — `/api`, `/mcp`, `/api/stream`, `wait`. No proxy in the path; SSE and long-polls native. This is already how the LiteLLM gateway is wired today; unchanged.
- Same process, same auth engine, same permission engine on both doors — the doors differ only in who is expected to knock.
- **Proxy-header trust rules** (from ingress source analysis):
  - Socket peer IP is authoritative for registration/IP display; `X-Forwarded-For` is honored ONLY when the connection originates from the trusted ingress proxy (configurable CIDR) — otherwise a direct-door client could spoof its IP.
  - `X-Hass-Source` / `X-Ingress-Path` are NEVER trust inputs — cosmetic (ingress-mode rendering) and path-stripping only, since direct-door clients can forge them.

## 19. Build order

1. Schema v2 + setup wizard + fresh bootstrap
2. Identity layer: three-layer auth (registration/approval, claim-secret poll, enrollment codes, access tokens, usage metadata), `can()` + permission tests
3. Stacks/projects/tasks/todos + state machine (incl. `proposed`) + transition log + not-done + done-gating
4. Discussions/comments, chat, decisions, links, questions (incl. object freeze), FTS search
5. Claims/presence/notes/wiki on identities, with visible revisions
6. Events: addressing, mentions, durable inbox, generalized wait; live UI stream (multiplexed SSE + fallback)
7. Notifications + attention queues; UI overhaul
8. Git proof links + gating (+ optional enrichment)
9. Security suite + hardening pass (incl. ingress-replay e2e + proxy-header spoof tests)
10. Legacy export -> release 1.0.0 -> addon bump -> skill + gateway rewiring
