# Releasing SlopClanker

## How a release flows

1. `VERSION` (single source of truth) is bumped, with a matching entry
   in `CHANGELOG.md`, and merged to `master`.
2. **CI** runs ruff + pytest + a docker build on every PR; master pushes
   run the same tests as the gate of the release job.
3. **Release** (`.github/workflows/release.yml`) — only on `master`,
   only after tests are green:
   - reads `VERSION`; if tag `v<VERSION>` already exists, it stops
     (idempotent — doc-only pushes re-run safely),
   - builds and pushes `ghcr.io/flapperdeflipper/slopclanker:<VERSION>`
     and `:latest`,
   - creates the GitHub release with generated notes.
4. **Addon sync** (`.github/workflows/addons-sync.yml`) — on release
   published, opens a PR against
   [flapperdeflipper/addons](https://github.com/flapperdeflipper/addons)
   pinning the add-on image to the new tag and bumping its `config.yaml`
   version.

Images go to **GHCR**, not Docker Hub: the release pipeline needs zero
configured credentials (it authenticates with the workflow's own
`GITHUB_TOKEN`).

## The sync token

Cross-repo PRs need a PAT with `repo` scope stored as the
`SLOPCLANKER_SYNC_TOKEN` secret in this repository — the built-in
`GITHUB_TOKEN` cannot open PRs elsewhere. Until that secret exists the
sync workflow succeeds with a notice and the bump is manual:

1. Branch off `master` in `flapperdeflipper/addons`.
2. In `slopclanker/Dockerfile`, pin `FROM
   ghcr.io/flapperdeflipper/slopclanker:<version>` (strict pin, never
   `latest`).
3. Set `version:` in `slopclanker/config.yaml` to the same version.
4. Add a `slopclanker/CHANGELOG.md` entry pointing at the release.
5. PR, wait for the two checks, merge, update the add-on in Home
   Assistant.

## Versioning rules

- Versions align across the split: `VERSION` here = container tag =
  add-on `config.yaml` version.
- The add-on never floats: it pins an exact released tag.
- Breaking changes to the HTTP API, MCP tools or the SQLite schema
  bump the minor version; note migrations in `CHANGELOG.md`.
