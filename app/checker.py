"""Background availability checker.

A daemon thread wakes every CHECK_INTERVAL_SECONDS, probes every target URL
concurrently, records one aggregated result per target group, and prunes
anything older than the display window.

Each probe is an HTTP GET forced onto a specific address family (IPv6 always;
IPv4 only when CHECK_IPV4 is enabled). A family is "up" when it returns any
HTTP status below 400 within the timeout. A multi-URL group is up only when
every URL is up on every probed family and every URL's final host (after
redirects) matches the group's canonical host (from the first URL).

Probe failures are stored as short labels (e.g. "connection refused"), not the
raw urllib3/requests exception text — those strings are for the status page and
ntfy alerts, not for debugging the HTTP client.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
import urllib3.util.connection as urllib3_connection

from . import alerts, config, store

_started = False
_lock = threading.Lock()
# Optional hook (e.g. static HTML export) run once per cycle after records/alerts.
_after_cycle: Callable[[], None] | None = None

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
    final_url: str | None


@dataclass(frozen=True)
class _UrlProbe:
    url: str
    host: str
    ipv6: _FamilyResult | None
    ipv4: _FamilyResult | None


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


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


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
        final_url = resp.url
        resp.close()
        return _FamilyResult(
            ok=ok,
            status_code=status_code,
            error=error,
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            final_url=final_url,
        )
    except requests.exceptions.SSLError as exc:
        return _FamilyResult(
            ok=False,
            status_code=None,
            error=_short_error(exc),
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            final_url=None,
        )
    except requests.exceptions.Timeout:
        return _FamilyResult(
            ok=False,
            status_code=None,
            error=f"timeout after {config.REQUEST_TIMEOUT_SECONDS}s",
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            final_url=None,
        )
    except requests.exceptions.RequestException as exc:
        return _FamilyResult(
            ok=False,
            status_code=None,
            error=_short_error(exc),
            latency_ms=round((time.monotonic() - start) * 1000, 1),
            final_url=None,
        )
    finally:
        _tls.family = None


def _probe_url(url: str) -> _UrlProbe:
    ipv6 = _probe_family(url, socket.AF_INET6) if config.CHECK_IPV6 else None
    ipv4 = _probe_family(url, socket.AF_INET) if config.CHECK_IPV4 else None
    return _UrlProbe(url=url, host=_host_of(url), ipv6=ipv6, ipv4=ipv4)


def _final_host(result: _FamilyResult | None) -> str | None:
    if result is None or not result.ok or not result.final_url:
        return None
    return _host_of(result.final_url)


def _url_failure(probe: _UrlProbe, expected_host: str) -> str | None:
    """Return a short failure label for this URL, or None if it passes."""
    probed = [r for r in (probe.ipv6, probe.ipv4) if r is not None]
    if not probed:
        return "no probe configured"
    family_fail: list[str] = []
    if probe.ipv6 is not None and not probe.ipv6.ok:
        family_fail.append(f"IPv6 {probe.ipv6.error or 'down'}")
    if probe.ipv4 is not None and not probe.ipv4.ok:
        family_fail.append(f"IPv4 {probe.ipv4.error or 'down'}")
    if family_fail:
        return " · ".join(family_fail)

    # Reachable: every successful family must land on the canonical host.
    landed = {
        h for h in (_final_host(probe.ipv6), _final_host(probe.ipv4)) if h is not None
    }
    if not landed:
        return "redirect unknown"
    bad = sorted(h for h in landed if h != expected_host)
    if bad:
        got = bad[0]
        return f"redirects to {got}, want {expected_host}"
    return None


def _combine_family(
    probes: list[_UrlProbe], attr: str
) -> tuple[bool | None, str | None, int | None]:
    """AND family results across member URLs; pick a representative error/code."""
    results: list[_FamilyResult] = []
    for probe in probes:
        r = getattr(probe, attr)
        if isinstance(r, _FamilyResult):
            results.append(r)
    if not results:
        return None, None, None
    ok = all(r.ok for r in results)
    status_code = None
    error = None
    for r in results:
        if r.status_code is not None and status_code is None:
            status_code = r.status_code
        if not r.ok and error is None:
            error = r.error
    return ok, error, status_code


def check_one(target: config.Target, probes: list[_UrlProbe]) -> dict[str, object]:
    expected_host = _host_of(target.url)
    failures: list[str] = []
    for probe in probes:
        why = _url_failure(probe, expected_host)
        if why:
            failures.append(f"{probe.host}: {why}")

    ok = not failures
    ipv6_ok, ipv6_error, ipv6_status = _combine_family(probes, "ipv6")
    ipv4_ok, ipv4_error, ipv4_status = _combine_family(probes, "ipv4")

    latencies = [
        r.latency_ms
        for probe in probes
        for r in (probe.ipv6, probe.ipv4)
        if r is not None
    ]
    latency_ms = max(latencies) if latencies else 0.0

    status_code = ipv6_status if ipv6_status is not None else ipv4_status
    error = "; ".join(failures) if failures else None
    # When only families fail (single URL), keep the legacy combined label.
    if error is None and not ok:
        parts: list[str] = []
        if ipv6_ok is False:
            parts.append(f"IPv6: {ipv6_error or 'down'}")
        if ipv4_ok is False:
            parts.append(f"IPv4: {ipv4_error or 'down'}")
        error = "; ".join(parts) or "down"

    store.record(
        target.name,
        ok,
        status_code,
        latency_ms,
        error,
        ipv4_ok=ipv4_ok,
        ipv6_ok=ipv6_ok,
        ipv4_error=ipv4_error,
        ipv6_error=ipv6_error,
        ipv4_status_code=ipv4_status,
        ipv6_status_code=ipv6_status,
    )
    return {
        "target": target.name,
        "url": target.url,
        "urls": list(target.urls),
        "ok": ok,
        "status_code": status_code,
        "error": error,
        "ipv4_ok": ipv4_ok,
        "ipv6_ok": ipv6_ok,
        "ts": int(time.time()),
    }


def _run() -> None:
    # One worker per member URL so a slow alias never delays the whole cycle.
    members = [(t, url) for t in config.TARGETS for url in t.urls]
    workers = max(4, len(members))
    while True:
        cycle_start = time.monotonic()
        if config.TARGETS:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                probes = list(pool.map(lambda pair: _probe_url(pair[1]), members))
            by_url: dict[str, _UrlProbe] = {
                members[i][1]: probes[i] for i in range(len(members))
            }
            results: list[dict[str, object]] = []
            for target in config.TARGETS:
                target_probes = [by_url[u] for u in target.urls]
                results.append(check_one(target, target_probes))
            try:
                alerts.process(results)
            except Exception:
                pass  # alerting must never kill the check loop
        try:
            store.prune(config.HISTORY_DAYS + 1)
        except Exception:
            pass  # pruning is best-effort; never kill the loop over it
        if _after_cycle is not None:
            try:
                _after_cycle()
            except Exception:
                pass  # export/hooks must never kill the check loop
        elapsed = time.monotonic() - cycle_start
        time.sleep(max(1.0, config.CHECK_INTERVAL_SECONDS - elapsed))


def set_after_cycle(callback: Callable[[], None] | None) -> None:
    """Register a zero-arg callback invoked after each check cycle completes."""
    global _after_cycle
    _after_cycle = callback


def start() -> None:
    """Start the checker exactly once for this process."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    thread = threading.Thread(target=_run, name="status-checker", daemon=True)
    thread.start()
