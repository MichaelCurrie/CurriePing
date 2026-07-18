"""Down/recovery alerting via ntfy.sh.

The checker calls `process()` once per cycle with each target's result. We track
per-target state in memory and publish a notification only on transitions:
  - "down": after ALERT_FAIL_THRESHOLD consecutive failures (debounces blips)
  - "recovered": on the first success after a "down" alert was sent

Delivery is a plain POST to NTFY_URL (a full ntfy topic URL); subscribe to that
topic in the ntfy phone app. No auth — the topic name is the shared secret.
"""

from __future__ import annotations

import threading

import requests

from . import config

_lock = threading.Lock()
_consec_fail: dict[str, int] = {}
_alerted_down: dict[str, bool] = {}


def _enabled() -> bool:
    return bool(config.NTFY_URL)


def _notify(event: str, result: dict) -> None:
    site = result.get("target") or "a site"
    down = event != "recovered"
    if down:
        status_code = result.get("status_code")
        error = result.get("error")
        detail = error or (
            f"HTTP {status_code}" if status_code is not None else "unreachable"
        )
        title = f"{site} is DOWN"
        body = f"{site} failed its check: {detail}"
        tags = "rotating_light"
        priority = "urgent"
    else:
        title = f"{site} recovered"
        body = f"{site} is back up."
        tags = "white_check_mark"
        priority = "default"

    # ntfy headers must be ASCII; emoji come from the Tags header (shortcodes).
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
    }
    if config.STATUS_PAGE_URL:
        headers["Click"] = config.STATUS_PAGE_URL

    try:
        requests.post(
            config.NTFY_URL,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        # A failed alert must never break the checker; a persistent outage will
        # keep the target "down" until a recovery fires, so no state is lost.
        pass


def process(results: list[dict]) -> None:
    """Evaluate a cycle's results and publish transition notifications."""
    if not _enabled():
        return
    with _lock:
        for r in results:
            if not r:
                continue
            name = r.get("target")
            if name is None:
                continue
            if r.get("ok"):
                _consec_fail[name] = 0
                if _alerted_down.get(name):
                    _alerted_down[name] = False
                    _notify("recovered", r)
            else:
                _consec_fail[name] = _consec_fail.get(name, 0) + 1
                if _consec_fail[name] >= config.ALERT_FAIL_THRESHOLD and not (
                    _alerted_down.get(name)
                ):
                    _alerted_down[name] = True
                    _notify("down", r)
