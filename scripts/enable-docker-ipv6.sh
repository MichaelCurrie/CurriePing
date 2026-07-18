#!/usr/bin/env bash
# Configure the Docker daemon so published ports (Caddy :80/:443) listen on
# IPv6 as well as IPv4. Required for an IPv6-only public EC2 host — without
# this, AAAA traffic never reaches the proxy. Safe to re-run (idempotent).

set -euo pipefail

DAEMON_JSON=/etc/docker/daemon.json
MARKER_CIDR='fd00:dead:beef:1::/64'

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo $0)" >&2
  exit 1
fi

mkdir -p /etc/docker

if [ -f "$DAEMON_JSON" ] && grep -q "$MARKER_CIDR" "$DAEMON_JSON"; then
  echo "Docker IPv6 already configured in $DAEMON_JSON"
else
  if [ -f "$DAEMON_JSON" ]; then
    cp -a "$DAEMON_JSON" "${DAEMON_JSON}.bak.$(date -u +%Y%m%d%H%M%S)"
  fi
  cat >"$DAEMON_JSON" <<'EOF'
{
  "ipv6": true,
  "fixed-cidr-v6": "fd00:dead:beef:1::/64",
  "ip6tables": true
}
EOF
  echo "Wrote $DAEMON_JSON"
fi

systemctl enable docker
systemctl restart docker
echo "Docker restarted with IPv6 enabled."
