"""Environment-driven configuration.

Everything the monitor needs to know at runtime comes from environment
variables (populated from a `.env` file via docker-compose's `env_file`).
The only required operator setting is `TARGETS`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Target:
    """One status-page row: a display name plus one or more URLs to probe.

    The first URL is the canonical link (favicon, click-through). Extra URLs
    are usually www / redirect aliases; the checker requires every URL to be
    reachable and to land on the canonical host after redirects.
    """

    name: str
    urls: tuple[str, ...]

    @property
    def url(self) -> str:
        return self.urls[0]


def _read_version() -> str:
    """Semantic version from the repo-root VERSION file (copied into the image)."""
    path = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


VERSION = _read_version()


def _parse_targets(raw: str) -> list[Target]:
    """Parse `Name=URL|URL,Name=URL` into grouped Target objects.

    - Comma separates groups (URLs never contain commas).
    - Within a group, `|` lists multiple URLs that share one status row.
    - The same Name repeated merges URLs (first-seen order, deduped).
    - Bare URL entries use the host as the display name.
    """
    order: list[str] = []
    by_name: dict[str, list[str]] = {}

    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, url_part = chunk.split("=", 1)
            name, url_part = name.strip(), url_part.strip()
        else:
            url_part = chunk
            name = url_part.split("//", 1)[-1].split("/", 1)[0].split("|", 1)[0]

        urls = [u.strip() for u in url_part.split("|") if u.strip()]
        if not name or not urls:
            continue

        if name not in by_name:
            order.append(name)
            by_name[name] = []
        seen = set(by_name[name])
        for url in urls:
            if url not in seen:
                by_name[name].append(url)
                seen.add(url)

    return [Target(name=n, urls=tuple(by_name[n])) for n in order]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str) -> bool:
    """Require env var exactly `True` or `False` (KeyError if unset)."""
    raw = os.environ[name]
    if raw == "True":
        return True
    if raw == "False":
        return False
    raise ValueError(f"{name} must be exactly True or False, got {raw!r}")


TITLE = os.environ.get("STATUS_TITLE", "Service Status").strip() or "Service Status"
CHECK_INTERVAL_SECONDS = _int("CHECK_INTERVAL_SECONDS", 60)
REQUEST_TIMEOUT_SECONDS = _int("REQUEST_TIMEOUT_SECONDS", 10)
HISTORY_DAYS = max(1, min(_int("HISTORY_DAYS", 90), 365))
DB_PATH = (
    os.environ.get("STATUS_DB_PATH", "/data/status.db").strip() or "/data/status.db"
)
# Always rewrite a static status tree here after each check cycle (index.html,
# api/status.json, icons). Sibling of the DB so the Docker /data volume covers it.
EXPORT_DIR = str(Path(DB_PATH).expanduser().parent / "www")
USER_AGENT = os.environ.get(
    "STATUS_USER_AGENT",
    "status-monitor/1.0 (+https://github.com)",
).strip()

TARGETS = _parse_targets(os.environ.get("TARGETS", ""))

# Probe address families. IPv6 is always on. IPv4 needs public IPv4 egress on
# the host (e.g. an AWS Elastic IP, ~$3.65/mo); set False on IPv6-only EC2.
# Required: CHECK_IPV4 must be exactly True or False.
CHECK_IPV6 = True
CHECK_IPV4 = _bool("CHECK_IPV4")

# Alerting via ntfy.sh: when a target goes down (or recovers), POST a
# notification to NTFY_URL (a full topic URL, e.g. https://ntfy.sh/my-topic).
# Subscribe to that topic in the ntfy phone app. The topic name IS the secret,
# so pick an unguessable one. Empty NTFY_URL -> alerting disabled. A target
# must fail ALERT_FAIL_THRESHOLD consecutive checks before a "down" alert fires
# (debounces blips); recovery fires on the first success after a down alert.
NTFY_URL = os.environ.get("NTFY_URL", "").strip()
ALERT_FAIL_THRESHOLD = max(1, _int("ALERT_FAIL_THRESHOLD", 2))

# Deep link put on the notification (tapping it opens the status page).
STATUS_PAGE_URL = (
    f"https://{STATUS_DOMAIN}"
    if (STATUS_DOMAIN := os.environ.get("STATUS_DOMAIN", "").strip())
    and not STATUS_DOMAIN.startswith(":")
    else ""
)
