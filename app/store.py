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
    _conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_checks_target_ts ON checks(target, ts)"
    )
    _conn.commit()


def record(
    target: str,
    ok: bool,
    status_code: int | None,
    latency_ms: float | None,
    error: str | None,
) -> None:
    assert _conn is not None, "store.init() not called"
    with _lock:
        _conn.execute(
            "INSERT INTO checks (target, ts, ok, status_code, latency_ms, error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (target, int(time.time()), 1 if ok else 0, status_code, latency_ms, error),
        )
        _conn.commit()


def prune(older_than_days: int) -> None:
    assert _conn is not None
    cutoff = int(time.time()) - older_than_days * 86400
    with _lock:
        _conn.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
        _conn.commit()


def _latest(target: str) -> dict | None:
    assert _conn is not None
    row = _conn.execute(
        "SELECT ts, ok, status_code, latency_ms, error FROM checks "
        "WHERE target = ? ORDER BY ts DESC LIMIT 1",
        (target,),
    ).fetchone()
    if row is None:
        return None
    return {
        "ts": row[0],
        "ok": bool(row[1]),
        "status_code": row[2],
        "latency_ms": row[3],
        "error": row[4],
    }


def component(target: str, days: int) -> dict:
    """Return daily buckets (oldest -> newest), uptime %, and latest result."""
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
        latest = _latest(target)

    today = datetime.now(timezone.utc).date()
    buckets: list[dict] = []
    up_sum = 0
    total_sum = 0
    for i in range(days - 1, -1, -1):
        day: date = today - timedelta(days=i)
        key = day.isoformat()
        up, total = by_day.get(key, (0, 0))
        up_sum += up
        total_sum += total
        if total == 0:
            state = "none"
            ratio = None
        else:
            ratio = up / total
            state = "up" if ratio >= 0.999 else ("down" if ratio == 0 else "partial")
        buckets.append(
            {
                "date": key,
                "state": state,
                "ratio": ratio,
                "up": up,
                "total": total,
            }
        )

    uptime = round(100.0 * up_sum / total_sum, 3) if total_sum else None

    if latest is None:
        status = "unknown"
    else:
        status = "operational" if latest["ok"] else "down"

    return {
        "status": status,
        "uptime": uptime,
        "latest": latest,
        "buckets": buckets,
    }
