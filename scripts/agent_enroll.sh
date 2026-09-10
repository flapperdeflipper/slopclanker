#!/usr/bin/env bash
# Secret-free clanker enrollment (device flow).
#
# Registers a clanker identity request, waits for the human approval, and
# writes the delivered identity token straight to a 0600 file. No secret is
# ever printed, logged, or handed to a calling process — the claim secret and
# the token live only inside this script's process and the output file.
#
# Usage: agent_enroll.sh <api-base> <name> <token-file> [note]
#   e.g. scripts/agent_enroll.sh http://10.20.0.3:8090 clanker-x \
#          /data/.config/opencode/slopclanker.token "opencode add-on agent"
set -euo pipefail

base=${1:?api base required} name=${2:?name required} outfile=${3:?token file required}
note=${4:-enrolled via device flow}

if [ -s "$outfile" ]; then
  echo "token file already exists ($(wc -c <"$outfile") bytes) — nothing to do"; exit 0
fi

claim=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
body=$(python3 - "$name" "$note" "$claim" <<'PY'
import json, sys
print(json.dumps({"name": sys.argv[1], "note": sys.argv[2], "claim_secret": sys.argv[3]}))
PY
)
# register — retry while a stale live registration for the same name blocks
# the queue (an operator rejecting it unblocks us); abort on real collisions.
rid=""
for _ in $(seq 1 90); do
  resp=$(curl -sS -w "\n%{http_code}" -X POST "$base/api/auth/register" \
    -H "Content-Type: application/json" -d "$body") || { sleep 10; continue; }
  code=$(printf '%s' "$resp" | tail -n1)
  payload=$(printf '%s' "$resp" | sed '$d')
  if [ "$code" = "201" ]; then
    rid=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin)["request_id"])')
    break
  fi
  if printf '%s' "$payload" | grep -q "already exists as an identity"; then
    echo "identity '$name' already exists — nothing to enroll" >&2; exit 1
  fi
  echo "register: HTTP $code $(printf '%s' "$payload" | head -c 120) — retrying in 10s" >&2
  sleep 10
done
[ -n "$rid" ] || { echo "could not queue registration after 15 minutes" >&2; exit 1; }
echo "registration #$rid queued for '$name' — waiting for human approval (polling every 5s)…"

for _ in $(seq 1 120); do
  sleep 5
  status=$(curl -fsS -X POST "$base/api/auth/register/$rid/poll" \
    -H "Content-Type: application/json" -d "{\"claim_secret\":\"$claim\"}" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("status") or ""); import os; os.write(3, (d.get("token") or "").encode())' 3>/tmp/.enroll_tok.$$ 2>/dev/null || echo retry)
  if [ "$status" = "delivered" ]; then
    umask 077
    { cat /tmp/.enroll_tok.$$; printf '\n'; } > "$outfile.tmp.$$" && mv "$outfile.tmp.$$" "$outfile"
    rm -f /tmp/.enroll_tok.$$
    echo "enrolled: token written to $outfile ($(wc -c <"$outfile") bytes, mode 600)"
    exit 0
  fi
  if [ "$status" = "rejected" ]; then
    rm -f /tmp/.enroll_tok.$$
    echo "registration #$rid was REJECTED by the operator" >&2; exit 1
  fi
done
rm -f /tmp/.enroll_tok.$$
echo "timed out after 10 minutes — registration #$rid still pending" >&2; exit 1
