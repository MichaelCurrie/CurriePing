"""Environment-driven configuration.

Everything the monitor needs to know at runtime comes from environment
variables (populated from a `.env` file via docker-compose's `env_file`).
The only thing an operator has to set is `TARGETS`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Target:
    name: str
    url: str


def _read_version() -> str:
    """Semantic version from the repo-root VERSION file (copied into the image)."""
    path = Path(__file__).resolve().parent.parent / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


VERSION = _read_version()


def _parse_targets(raw: str) -> list[Target]:
    """Parse a `Name=URL,Name=URL` string into Target objects.

    Whitespace around entries is ignored. Entries without an `=` are treated
    as a bare URL and the host becomes the display name. URLs never contain a
    comma, so a comma is always an entry separator.
    """
    targets: list[Target] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk:
            name, url = chunk.split("=", 1)
            name, url = name.strip(), url.strip()
        else:
            url = chunk
            name = url.split("//", 1)[-1].split("/", 1)[0]
        if url:
            targets.append(Target(name=name or url, url=url))
    return targets


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


TITLE = os.environ.get("STATUS_TITLE", "Service Status").strip() or "Service Status"
CHECK_INTERVAL_SECONDS = _int("CHECK_INTERVAL_SECONDS", 60)
REQUEST_TIMEOUT_SECONDS = _int("REQUEST_TIMEOUT_SECONDS", 10)
HISTORY_DAYS = max(1, min(_int("HISTORY_DAYS", 90), 365))
DB_PATH = (
    os.environ.get("STATUS_DB_PATH", "/data/status.db").strip() or "/data/status.db"
)
USER_AGENT = os.environ.get(
    "STATUS_USER_AGENT",
    "status-monitor/1.0 (+https://github.com)",
).strip()

TARGETS = _parse_targets(os.environ.get("TARGETS", ""))

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
