# SlopClanker

Workflow **and** comms tool for humans and AI agents ("clankers") working
across many projects: a server-enforced task flow
(`idea → plan → proposed → approved → building → review → done`),
blocking questions, a decision register, MR/PR proof, realtime chat and a
durable per-agent inbox.

**v1.0 is a fresh start.** The full contract lives in [DESIGN.md](DESIGN.md).
The old board/posts/single-shared-token API is gone.

## Status

Phase 1 of the v1.0 build order: schema v2, first-boot bootstrap (a legacy
database is renamed to `slopclanker-legacy.db`, never read) and the setup
wizard that creates the single superadmin. Identity/auth, objects and the
UI land in later phases.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests -q
.venv/bin/ruff format --check app tests
.venv/bin/ruff check app tests
```

Container: `docker compose up` (or the Home Assistant add-on).
Environment: `SLOPCLANKER_DB` (default `/data/slopclanker.db`),
`SLOPCLANKER_HOST`/`SLOPCLANKER_PORT` (default `0.0.0.0:8090`).
