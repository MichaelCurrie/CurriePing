#!/usr/bin/env bash
# Pull CurriePing from origin and rebuild containers only when HEAD moved.
# Intended for cron on the deploy host (see README → Auto-update).
#
# The host tracks origin exactly: a real update does `git reset --hard` to
# origin/<branch>. Untracked files (notably `.env`) are left alone.

set -euo pipefail

PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LOG_FILE="${CURRIEPING_UPDATE_LOG:-/var/log/currieping-auto-update.log}"
BRANCH="${CURRIEPING_BRANCH:-main}"

# Prefer an explicit dir; otherwise accept either spelling used in the wild.
if [ -n "${CURRIEPING_DIR:-}" ]; then
  APP_DIR="$CURRIEPING_DIR"
elif [ -d /opt/currieping ]; then
  APP_DIR=/opt/currieping
elif [ -d /opt/curieping ]; then
  APP_DIR=/opt/curieping
else
  echo "CurriePing directory not found (set CURRIEPING_DIR)" >&2
  exit 1
fi

log() {
  # Fall back to /tmp if /var/log is not writable (shouldn't happen under root cron).
  if ! echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG_FILE" 2>/dev/null; then
    LOG_FILE=/tmp/currieping-auto-update.log
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $*" >>"$LOG_FILE"
  fi
}

cd "$APP_DIR"

git fetch --quiet origin "$BRANCH"

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

log "updating $APP_DIR $LOCAL -> $REMOTE"
git reset --hard "origin/$BRANCH"
docker compose up -d --build --remove-orphans
log "update complete ($(git rev-parse --short HEAD))"
