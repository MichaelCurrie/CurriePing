"""Background availability checker.

A daemon thread wakes every CHECK_INTERVAL_SECONDS, probes every target
concurrently, records each result, and prunes anything older than the display
window. A target is "up" when it returns any HTTP status below 400 within the
timeout; connection errors, TLS failures, and 4xx/5xx count as down.

Probe failures are stored as short labels (e.g. "connection refused"), not the
raw urllib3/requests exception text — those strings are for the status page and
ntfy alerts, not for debugging the HTTP client.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from . import alerts, config, store

_started = False
_lock = threading.Lock()

# Linux / common POSIX errnos that show up under requests ConnectionError.
_ERRNO_LABELS: dict[int, str] = {
    111: "connection refused",
    110: "connection timed out",
    104: "connection reset",
    101: "network unreachable",
    113: "host unreachable",
    8: "DNS lookup failed",  # EAI_NONAME on some platforms
    -2: "DNS lookup failed",
    -3: "DNS lookup failed",
}


def _short_error(exc: BaseException) -> str:
    """Turn a requests/urllib3 failure into a short alert/UI label."""
    errno: int | None = None
    parts: list[str] = []
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(str(cur))
        err = getattr(cur, "errno", None)
        if isinstance(err, int) and errno is None:
            errno = err
        for arg in getattr(cur, "args", ()):
            if isinstance(arg, OSError) and isinstance(arg.errno, int) and errno is None:
                errno = arg.errno
        cur = cur.__cause__ or cur.__context__

    if errno in _ERRNO_LABELS:
        return _ERRNO_LABELS[errno]

    blob = " ".join(parts).lower()
    if any(
        s in blob
        for s in (
            "name or service not known",
            "getaddrinfo failed",
            "nodename nor servname",
            "name resolution",
            "temporary failure in name resolution",
        )
    ):
        return "DNS lookup failed"
    if "connection refused" in blob:
        return "connection refused"
    if "timed out" in blob or "timeout" in blob:
        return "connection timed out"
    if "connection reset" in blob or "reset by peer" in blob:
        return "connection reset"
    if "network is unreachable" in blob or "network unreachable" in blob:
        return "network unreachable"
    if "host is unreachable" in blob or "no route to host" in blob:
        return "host unreachable"
    if "certificate" in blob or "ssl" in blob or "tls" in blob:
        return "TLS error"
    return "connection failed"


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
        error = _short_error(exc)
    except requests.exceptions.Timeout:
        error = f"timeout after {config.REQUEST_TIMEOUT_SECONDS}s"
    except requests.exceptions.RequestException as exc:
        error = _short_error(exc)
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
