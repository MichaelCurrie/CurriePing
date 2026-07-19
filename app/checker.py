"""Background availability checker.

A daemon thread wakes every CHECK_INTERVAL_SECONDS, probes every target
concurrently, records each result, and prunes anything older than the display
window.

Each probe is an HTTP GET forced onto a specific address family (IPv6 always;
IPv4 only when CHECK_IPV4 is enabled). A family is "up" when it returns any
HTTP status below 400 within the timeout. The target is up only when every
probed family is up.

Probe failures are stored as short labels (e.g. "connection refused"), not the
raw urllib3/requests exception text — those strings are for the status page and
ntfy alerts, not for debugging the HTTP client.
"""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests
import urllib3.util.connection as urllib3_connection

from . import alerts, config, store

_started = False
_lock = threading.Lock()

# Per-thread address-family pin for urllib3's getaddrinfo. Thread-local so the
# check pool can probe IPv4 and IPv6 concurrently without races.
_tls = threading.local()
_orig_allowed_gai_family = urllib3_connection.allowed_gai_family


def _allowed_gai_family() -> socket.AddressFamily:
    forced = getattr(_tls, "family", None)
    if forced is not None:
        return socket.AddressFamily(int(forced))
    return _orig_allowed_gai_family()


# setattr: intentional monkeypatch; a direct attribute assign trips ty's
# invalid-assignment (implicit function shadowing).
setattr(urllib3_connection, "allowed_gai_family", _allowed_gai_family)

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


@dataclass(frozen=True)
class _FamilyResult:
    ok: bool
    status_code: int | None
    error: str | None
    latency_ms: float


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
            if (
                isinstance(arg, OSError)
                and isinstance(arg.errno, int)
                and errno is None
            ):
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
            "no address associated with hostname",
            "gaierror",
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


def _probe_family(url: str, family: int) -> _FamilyResult:
    """HTTP GET forced onto AF_INET or AF_INET6 via urllib3's gai family hook."""
    start = time.monotonic()
    _tls.family = family
    try:
        resp = requests.get(
            url,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
            stream=True,
        )
        status_code = resp.status_code
        ok = status_code < 400
        error = None if ok else f"HTTP {status_code}"
        resp.close()
        return _FamilyResult(
            ok=ok,
            status_code=status_code,
            error=error,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )
    except requests.exceptions.SSLError as exc:
        return _FamilyResult(
            ok=False,
            status_code=None,
            error=_short_error(exc),
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )
    except requests.exceptions.Timeout:
        return _FamilyResult(
            ok=False,
            status_code=None,
            error=f"timeout after {config.REQUEST_TIMEOUT_SECONDS}s",
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )
    except requests.exceptions.RequestException as exc:
        return _FamilyResult(
            ok=False,
            status_code=None,
            error=_short_error(exc),
            latency_ms=round((time.monotonic() - start) * 1000, 1),
        )
    finally:
        _tls.family = None


def _combined_error(
    ipv6: _FamilyResult | None, ipv4: _FamilyResult | None
) -> str | None:
    """Compact multi-family failure label for legacy `error` column / alerts."""
    parts: list[str] = []
    if ipv6 is not None and not ipv6.ok:
        parts.append(f"IPv6: {ipv6.error or 'down'}")
    if ipv4 is not None and not ipv4.ok:
        parts.append(f"IPv4: {ipv4.error or 'down'}")
    if not parts:
        return None
    return "; ".join(parts)


def check_one(target: config.Target) -> dict[str, object]:
    ipv6 = _probe_family(target.url, socket.AF_INET6) if config.CHECK_IPV6 else None
    ipv4 = _probe_family(target.url, socket.AF_INET) if config.CHECK_IPV4 else None

    probed = [r for r in (ipv6, ipv4) if r is not None]
    ok = all(r.ok for r in probed) if probed else False
    latencies = [r.latency_ms for r in probed]
    latency_ms = max(latencies) if latencies else 0.0
    # Prefer IPv6 status code for the summary column when both ran.
    status_code = None
    for r in (ipv6, ipv4):
        if r is not None and r.status_code is not None:
            status_code = r.status_code
            if r is ipv6:
                break
    error = _combined_error(ipv6, ipv4)

    store.record(
        target.name,
        ok,
        status_code,
        latency_ms,
        error,
        ipv4_ok=None if ipv4 is None else ipv4.ok,
        ipv6_ok=None if ipv6 is None else ipv6.ok,
        ipv4_error=None if ipv4 is None else ipv4.error,
        ipv6_error=None if ipv6 is None else ipv6.error,
        ipv4_status_code=None if ipv4 is None else ipv4.status_code,
        ipv6_status_code=None if ipv6 is None else ipv6.status_code,
    )
    return {
        "target": target.name,
        "url": target.url,
        "ok": ok,
        "status_code": status_code,
        "error": error,
        "ipv4_ok": None if ipv4 is None else ipv4.ok,
        "ipv6_ok": None if ipv6 is None else ipv6.ok,
        "ts": int(time.time()),
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
