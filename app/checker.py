"""Background availability checker.

A daemon thread wakes every CHECK_INTERVAL_SECONDS, probes every target
concurrently, records each result, and prunes anything older than the display
window. A target is "up" when it returns any HTTP status below 400 within the
timeout; connection errors, TLS failures, and 4xx/5xx count as down.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import alerts, config, store

_started = False
_lock = threading.Lock()


def check_one(target: config.Target) -> dict[str, object]:
    start = time.monotonic()
    status_code: int | None = None
    error: str | None = None
    ok = False
    try:
        resp = requests.get(
            target.url,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
            stream=True,
        )
        status_code = resp.status_code
        ok = status_code < 400
        if not ok:
            error = f"HTTP {status_code}"
        resp.close()
    except requests.exceptions.SSLError as exc:
        error = f"TLS error: {exc}"[:300]
    except requests.exceptions.Timeout:
        error = f"timeout after {config.REQUEST_TIMEOUT_SECONDS}s"
    except requests.exceptions.RequestException as exc:
        error = str(exc)[:300]
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    ts = int(time.time())
    store.record(target.name, ok, status_code, latency_ms, error)
    return {
        "target": target.name,
        "url": target.url,
        "ok": ok,
        "status_code": status_code,
        "error": error,
        "ts": ts,
    }


def _run() -> None:
    # A generous worker pool so one slow/timing-out target never delays others.
    workers = max(4, len(config.TARGETS))
    while True:
        cycle_start = time.monotonic()
        if config.TARGETS:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(check_one, config.TARGETS))
            try:
                alerts.process(results)
            except Exception:
                pass  # alerting must never kill the check loop
        try:
            store.prune(config.HISTORY_DAYS + 1)
        except Exception:
            pass  # pruning is best-effort; never kill the loop over it
        elapsed = time.monotonic() - cycle_start
        time.sleep(max(1.0, config.CHECK_INTERVAL_SECONDS - elapsed))


def start() -> None:
    """Start the checker exactly once for this process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(target=_run, name="status-checker", daemon=True)
    thread.start()
