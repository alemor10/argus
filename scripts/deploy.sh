#!/usr/bin/env bash
# Deploy Argus to the box: push local secrets, pull latest code, sync deps.
#
#   ./scripts/deploy.sh [user@host]
#
# Secrets are the source of truth on YOUR machine and are pushed over SSH —
# they never travel through git (this repo is public). Code travels through
# git; secrets do not. The two are deliberately separate channels.
#
# Secrets file:  $ARGUS_SECRETS_FILE, else ~/.config/argus/argus.env
set -euo pipefail

HOST="${1:-root@46.225.170.24}"
SECRETS="${ARGUS_SECRETS_FILE:-$HOME/.config/argus/argus.env}"
REMOTE_STATE="/root/argus-data"
REMOTE_CODE="/root/argus"
UV="/root/.local/bin/uv"

if [ ! -f "$SECRETS" ]; then
  echo "No secrets file at: $SECRETS" >&2
  echo "Create it first:" >&2
  echo "  mkdir -p \"\$(dirname \"$SECRETS\")\" && cp argus.env.example \"$SECRETS\" && \$EDITOR \"$SECRETS\"" >&2
  exit 1
fi

echo "→ Secrets   $SECRETS  →  $HOST:$REMOTE_STATE/argus.env"
scp -q "$SECRETS" "$HOST:$REMOTE_STATE/argus.env"
ssh "$HOST" "chmod 600 $REMOTE_STATE/argus.env"

echo "→ Code      git pull + uv sync on $HOST"
ssh "$HOST" "cd $REMOTE_CODE && git pull --quiet && $UV sync --quiet"

echo "✓ Deployed. Cron uses the new secrets + code on the next scheduled run."
echo "  Run now:  ssh $HOST '. $REMOTE_STATE/argus.env && cd $REMOTE_CODE && $UV run argus scout --root $REMOTE_STATE'"
