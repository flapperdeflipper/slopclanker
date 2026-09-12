#!/usr/bin/env bash
# Redeem a one-time enrollment code into an agent token file (device flow,
# existing-identity path: re-issue a code in the web UI, then run this).
#
# The code is typed by the human running this script; the delivered token is
# written straight to a 0600 file and never printed.
#
# Usage: redeem_code.sh <code> <token-file> [api-base]
#   e.g. scripts/redeem_code.sh AB12-CD34 /home-assistant/apps/data/.../slopclanker.token
set -euo pipefail

code=${1:?one-time code required (from Admin → People & enrollment)}
outfile=${2:?token file path required}
base=${3:-http://10.20.0.3:8090}

if [ -s "$outfile" ]; then
  echo "token file already exists ($(wc -c <"$outfile") bytes) — nothing to do"; exit 0
fi

umask 077
resp=$(curl -sS -w "\n%{http_code}" -X POST "$base/api/auth/enroll" \
  -H "Content-Type: application/json" -d "{\"code\":\"$code\"}")
code_http=$(printf '%s' "$resp" | tail -n1)
payload=$(printf '%s' "$resp" | sed '$d')
if [ "$code_http" != "200" ]; then
  echo "redemption failed (HTTP $code_http): $(printf '%s' "$payload" | head -c 200)" >&2
  exit 1
fi
printf '%s' "$payload" | python3 -c 'import json,sys
tok = json.load(sys.stdin).get("token") or ""
if not tok:
    sys.exit("no token in response")
import os
fd = os.open(sys.argv[1] + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(tok)
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
' "$outfile"
echo "token written to $outfile ($(wc -c <"$outfile") bytes, mode 600)"
