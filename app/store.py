"""SQLite persistence for check results.

One row per check. Aggregation into daily "bars" happens at read time so the
raw data stays flexible. A single connection guarded by a lock is plenty for
the low write volume (a handful of targets every minute) and keeps the whole
thing single-process-simple.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta, timezone

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_db_path = "/data/status.db"


def _ensure_family_columns(conn: sqlite3.Connection) -> None:
    """Add per-family columns on older DBs that predate dual-stack probes."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(checks)")}
    for name, decl in (
        ("ipv4_ok", "INTEGER"),
        ("ipv6_ok", "INTEGER"),
        ("ipv4_error", "TEXT"),
        ("ipv6_error", "TEXT"),
        ("ipv4_status_code", "INTEGER"),
        ("ipv6_status_code", "INTEGER"),
    ):
        if name not in existing:
            conn.execute(f"ALTER TABLE checks ADD COLUMN {name} {decl}")


def init(db_path: str) -> None:
    global _conn, _db_path
    _db_path = db_path
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS checks (
            target      TEXT    NOT NULL,
            ts          INTEGER NOT NULL,
            ok          INTEGER NOT NULL,
            status_code INTEGER,
            latency_ms  REAL,
            error       TEXT
        )
        """
    )
    _ensure_family_columns(_conn)
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checks_target_ts ON checks(target, ts)"
    )
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS favicons (
            target       TEXT    PRIMARY KEY,
            data         BLOB    NOT NULL,
            content_type TEXT    NOT NULL,
            fetched_at   INTEGER NOT NULL
        )
        """
    )
    _conn.commit()


def record(
    target: str,
    ok: bool,
    status_code: int | None,
    latency_ms: float | None,
    error: str | None,
    *,
    ipv4_ok: bool | None = None,
    ipv6_ok: bool | None = None,
    ipv4_error: str | None = None,
    ipv6_error: str | None = None,
    ipv4_status_code: int | None = None,
    ipv6_status_code: int | None = None,
) -> None:
    assert _conn is not None, "store.init() not called"
    with _lock:
        _conn.execute(
            "INSERT INTO checks ("
            "target, ts, ok, status_code, latency_ms, error, "
            "ipv4_ok, ipv6_ok, ipv4_error, ipv6_error, "
            "ipv4_status_code, ipv6_status_code"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                target,
                int(time.time()),
                1 if ok else 0,
                status_code,
                latency_ms,
                error,
                None if ipv4_ok is None else (1 if ipv4_ok else 0),
                None if ipv6_ok is None else (1 if ipv6_ok else 0),
                ipv4_error,
                ipv6_error,
                ipv4_status_code,
                ipv6_status_code,
            ),
        )
        _conn.commit()


def prune(older_than_days: int) -> None:
    assert _conn is not None
    cutoff = int(time.time()) - older_than_days * 86400
    with _lock:
        _conn.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
        _conn.commit()


def save_favicon(target: str, data: bytes, content_type: str) -> None:
    assert _conn is not None
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO favicons (target, data, content_type, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            (target, sqlite3.Binary(data), content_type, int(time.time())),
        )
        _conn.commit()


def clear_favicons() -> None:
    """Drop every cached favicon so the next refresh cannot serve stale bytes."""
    assert _conn is not None
    with _lock:
        _conn.execute("DELETE FROM favicons")
        _conn.commit()


def get_favicon(target: str) -> tuple[bytes, str, int] | None:
    assert _conn is not None
    with _lock:
        row = _conn.execute(
            "SELECT data, content_type, fetched_at FROM favicons WHERE target = ?",
            (target,),
        ).fetchone()
    if row is None:
        return None
    return bytes(row[0]), row[1], int(row[2])


def favicon_fetched_at(target: str) -> int | None:
    assert _conn is not None
    with _lock:
        row = _conn.execute(
            "SELECT fetched_at FROM favicons WHERE target = ?", (target,)
        ).fetchone()
    return int(row[0]) if row else None


def _tri_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _omit_none(payload: dict[str, object]) -> dict[str, object]:
    """Drop null fields so the public JSON stays dense (wire + curl-friendly)."""
    return {key: value for key, value in payload.items() if value is not None}


def _shorten_stored_error(error: object) -> str | None:
    """Legacy rows may still hold raw urllib3 text; never emit that on the API."""
    if not isinstance(error, str) or not error:
        return None
    if len(error) <= 80 and "HTTPSConnectionPool" not in error:
        return error
    lower = error.lower()
    if "name or service not known" in lower or "getaddrinfo" in lower:
        return "DNS lookup failed"
    if "connection refused" in lower:
        return "connection refused"
    if "timed out" in lower or "timeout" in lower:
        return "connection timed out"
    if "network is unreachable" in lower or "network unreachable" in lower:
        return "network unreachable"
    if "host is unreachable" in lower or "no route to host" in lower:
        return "host unreachable"
    if "certificate" in lower or "ssl" in lower or "tls" in lower:
        return "TLS error"
    if "HTTP " in error[:8]:
        return error.split(":", 1)[0][:40]
    return "connection failed"


def _status_label(
    ok: bool,
    ipv4_ok: bool | None,
    ipv6_ok: bool | None,
    error: str | None = None,
) -> str:
    """LIVE indicator text: Operational, or a short failure reason.

    Prefer a concrete `error` (e.g. `nematode.io: redirects to …`) over bare
    IPv4/IPv6 flags so multi-URL groups stay readable when one alias fails.
    """
    if ok:
        return "Operational"
    if isinstance(error, str) and error.strip():
        first = error.split(";", 1)[0].strip()
        return first if len(first) <= 72 else first[:69] + "..."
    parts: list[str] = []
    if ipv6_ok is not None:
        parts.append(f"IPv6 {'up' if ipv6_ok else 'down'}")
    if ipv4_ok is not None:
        parts.append(f"IPv4 {'up' if ipv4_ok else 'down'}")
    if parts:
        return " · ".join(parts)
    return "Down"


def _latest(target: str) -> dict[str, object] | None:
    assert _conn is not None
    row = _conn.execute(
        "SELECT ts, ok, status_code, latency_ms, error, "
        "ipv4_ok, ipv6_ok, ipv4_error, ipv6_error, "
        "ipv4_status_code, ipv6_status_code "
        "FROM checks WHERE target = ? ORDER BY ts DESC LIMIT 1",
        (target,),
    ).fetchone()
    if row is None:
        return None
    ipv4_ok = _tri_bool(row[5])
    ipv6_ok = _tri_bool(row[6])
    ok = bool(row[1])
    return _omit_none(
        {
            "ts": row[0],
            "ok": ok,
            "status_code": row[2],
            "latency_ms": row[3],
            "error": _shorten_stored_error(row[4]),
            "ipv4_ok": ipv4_ok,
            "ipv6_ok": ipv6_ok,
            "ipv4_error": _shorten_stored_error(row[7]),
            "ipv6_error": _shorten_stored_error(row[8]),
            "ipv4_status_code": row[9],
            "ipv6_status_code": row[10],
            "status_label": _status_label(
                ok, ipv4_ok, ipv6_ok, _shorten_stored_error(row[4])
            ),
        }
    )


def _recent_pings(target: str, count: int) -> list[dict[str, object]]:
    """Newest `count` checks, oldest → newest. No empty left-pad (UI pads)."""
    assert _conn is not None
    rows = _conn.execute(
        "SELECT ts, ok, status_code, latency_ms, error, "
        "ipv4_ok, ipv6_ok, ipv4_error, ipv6_error "
        "FROM checks WHERE target = ? ORDER BY ts DESC LIMIT ?",
        (target, count),
    ).fetchall()
    pings: list[dict[str, object]] = []
    for (
        ts,
        ok,
        status_code,
        latency_ms,
        error,
        ipv4_ok_raw,
        ipv6_ok_raw,
        ipv4_error,
        ipv6_error,
    ) in reversed(rows):
        ipv4_ok = _tri_bool(ipv4_ok_raw)
        ipv6_ok = _tri_bool(ipv6_ok_raw)
        pings.append(
            _omit_none(
                {
                    "ts": ts,
                    "ok": bool(ok),
                    "state": "up" if ok else "down",
                    "status_code": status_code,
                    "latency_ms": latency_ms,
                    "error": _shorten_stored_error(error),
                    "ipv4_ok": ipv4_ok,
                    "ipv6_ok": ipv6_ok,
                    "ipv4_error": _shorten_stored_error(ipv4_error),
                    "ipv6_error": _shorten_stored_error(ipv6_error),
                    "status_label": _status_label(
                        bool(ok), ipv4_ok, ipv6_ok, _shorten_stored_error(error)
                    ),
                }
            )
        )
    return pings


def component(
    target: str, days: int, recent_count: int, check_interval_seconds: int
) -> dict[str, object]:
    """Return daily buckets, recent per-ping bars, uptime %, and latest result.

    Daily buckets: only days with at least one check (UI expands to `days`).
    Recent: up to `recent_count` real checks; UI left-pads empty slots to match
    the daily bar count. Window label = interval × slot count.
    """
    assert _conn is not None
    with _lock:
        since = int(time.time()) - days * 86400
        rows = _conn.execute(
            "SELECT strftime('%Y-%m-%d', ts, 'unixepoch') AS d, "
            "SUM(ok) AS up, COUNT(*) AS total FROM checks "
            "WHERE target = ? AND ts >= ? GROUP BY d",
            (target, since),
        ).fetchall()
        by_day = {d: (up, total) for d, up, total in rows}
        recent = _recent_pings(target, recent_count)
        latest = _latest(target)

    today = datetime.now(timezone.utc).date()
    buckets: list[dict[str, object]] = []
    up_sum = 0
    total_sum = 0
    for i in range(days - 1, -1, -1):
        day: date = today - timedelta(days=i)
        key = day.isoformat()
        up, total = by_day.get(key, (0, 0))
        up_sum += up
        total_sum += total
        if total == 0:
            # Sparse API: empty days are reconstructed client-side from history_days.
            continue
        ratio = up / total
        state = "up" if ratio >= 0.999 else ("down" if ratio == 0 else "partial")
        buckets.append(
            {
                "date": key,
                "state": state,
                "ratio": round(ratio, 4),
                "up": up,
                "total": total,
            }
        )

    uptime = round(100.0 * up_sum / total_sum, 3) if total_sum else None

    recent_up = sum(1 for p in recent if p.get("ok"))
    recent_total = len(recent)
    recent_uptime = round(100.0 * recent_up / recent_total, 3) if recent_total else None
    # Window length the recent row represents when full (interval × slot count).
    recent_window_minutes = max(1, (recent_count * check_interval_seconds + 59) // 60)

    if latest is None:
        status = "unknown"
        status_label = "No data yet"
    else:
        status = "operational" if latest["ok"] else "down"
        label = latest.get("status_label")
        status_label = (
            label
            if isinstance(label, str)
            else ("Operational" if latest["ok"] else "Down")
        )

    return _omit_none(
        {
            "status": status,
            "status_label": status_label,
            "uptime": uptime,
            "recent_uptime": recent_uptime,
            "recent_window_minutes": recent_window_minutes,
            "latest": latest,
            "buckets": buckets,
            "recent": recent,
        }
    )
