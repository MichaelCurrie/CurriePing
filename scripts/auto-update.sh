#!/usr/bin/env bash
# Pull CurriePing from origin and rebuild containers only when HEAD moved.
# Intended for cron on the deploy host (see INSTALL.md → auto-update).
#
# The host tracks origin exactly: a real update does `git reset --hard` to
# origin/<branch>. Untracked files (notably `.env`) are left alone; missing
# keys from `.env.example` are appended so new required settings (e.g.
# CHECK_IPV4) appear without clobbering local values.
#
# IPv6-only hosts: github.com often has no AAAA from AWS DNS, so `git fetch`
# over HTTPS fails. Point origin at an IPv6-reachable mirror (or a same-VPC
# host that can reach GitHub), or set CURRIEPING_GIT_JUMP / use a local
# mirror — see INSTALL.md.

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

merge_env_example() {
  # Append keys present in .env.example but missing from .env.
  local example="$APP_DIR/.env.example"
  local envf="$APP_DIR/.env"
  [ -f "$example" ] && [ -f "$envf" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      '' | \#*) continue ;;
    esac
    local key=${line%%=*}
    [[ "$key" == "$line" ]] && continue
    if ! grep -q "^${key}=" "$envf" 2>/dev/null; then
      echo "$line" >>"$envf"
      log "added missing .env key $key from .env.example"
    fi
  done <"$example"
}

cd "$APP_DIR"

# Cron often runs as root while the tree may be owned by another user (or the
# reverse after a manual ssh deploy). Git 2.35+ refuses that with "dubious
# ownership" and the fetch never runs — pin this path as safe for the
# invoking user so 15-minute updates keep working.
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

if ! git fetch --quiet origin "$BRANCH" 2>/tmp/currieping-git-fetch.err; then
  log "git fetch failed (IPv6-only hosts often cannot reach github.com — no AAAA). $(tr '\n' ' ' </tmp/currieping-git-fetch.err)"
  exit 1
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

log "updating $APP_DIR $LOCAL -> $REMOTE"
git reset --hard "origin/$BRANCH"
merge_env_example
docker compose up -d --build --remove-orphans
log "update complete ($(git rev-parse --short HEAD) VERSION=$(tr -d '\n' <VERSION))"
