"""Aggregated down/recovery alerting via ntfy.sh.

The checker calls `process()` once per cycle with each target's result. We track
the *set* of currently-down sites and notify only when that set CHANGES:

  - a site is "down" once it fails ALERT_FAIL_THRESHOLD consecutive checks
    (debounces blips)
  - when the down-set grows or shrinks we send ONE notification listing every
    site currently down (never one-per-site, never repeated while unchanged)
  - when the last site recovers we send a single "all clear"

Delivery is a plain POST to NTFY_URL (a full ntfy topic URL); subscribe to that
topic in the ntfy phone app. No auth — the topic name is the shared secret.
"""

from __future__ import annotations

import threading

import requests

from . import config

_lock = threading.Lock()
_consec_fail: dict[str, int] = {}
_down: set[str] = set()  # sites currently in the alerted-down state


def _enabled() -> bool:
    return bool(config.NTFY_URL)


def _detail(result: dict) -> str:
    status_code = result.get("status_code")
    error = result.get("error")
    return error or (f"HTTP {status_code}" if status_code is not None else "unreachable")


def _post(title: str, body: str, tags: str, priority: str) -> None:
    headers = {"Title": title, "Priority": priority, "Tags": tags}
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
        # A failed alert must never break the checker. The down-set is only
        # advanced after a successful-or-attempted post; a persistent change
        # will resend next cycle because _down wasn't updated on exception.
        raise


def process(results: list[dict]) -> None:
    """Evaluate a cycle's results; notify only when the down-set changes."""
    if not _enabled():
        return
    with _lock:
        detail_by_site: dict[str, dict] = {}
        for r in results:
            if not r:
                continue
            name = r.get("target")
            if name is None:
                continue
            detail_by_site[name] = r
            if r.get("ok"):
                _consec_fail[name] = 0
            else:
                _consec_fail[name] = _consec_fail.get(name, 0) + 1

        # Sites at/over the failure threshold are considered down.
        current_down = {
            name
            for name in detail_by_site
            if _consec_fail.get(name, 0) >= config.ALERT_FAIL_THRESHOLD
        }

        if current_down == _down:
            return  # nothing changed -> stay quiet

        if current_down:
            sites = sorted(current_down)
            n = len(sites)
            title = f"{n} site{'s' if n != 1 else ''} down"
            body = "\n".join(f"{s}: {_detail(detail_by_site[s])}" for s in sites)
            tags, priority = "rotating_light", "urgent"
        else:
            title = "All sites recovered"
            body = "All monitored sites are back up."
            tags, priority = "white_check_mark", "default"

        try:
            _post(title, body, tags, priority)
        except requests.RequestException:
            return  # leave _down unchanged so the change is retried next cycle
        _down.clear()
        _down.update(current_down)
