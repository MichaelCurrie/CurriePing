"""Flask app: serves the status page and its JSON API, and owns the checker."""

from __future__ import annotations

import time
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, render_template

from . import checker, config, favicons, store

app = Flask(__name__)

store.init(config.DB_PATH)
checker.start()
favicons.start()


def _overall(components: list[dict]) -> str:
    states = {c["status"] for c in components}
    if not components or states == {"unknown"}:
        return "unknown"
    if "down" in states:
        return "down"
    if any(
        b["state"] == "partial"
        for c in components
        for b in c["buckets"][-1:]  # only today's bucket signals current degradation
    ):
        return "degraded"
    if "unknown" in states and states != {"operational", "unknown"}:
        return "degraded"
    return "operational"


def _build_status() -> dict:
    # Recent row uses the same bar count as the daily row so the two stacks align.
    recent_count = config.HISTORY_DAYS
    components = []
    for target in config.TARGETS:
        data = store.component(
            target.name,
            config.HISTORY_DAYS,
            recent_count,
            config.CHECK_INTERVAL_SECONDS,
        )
        fetched_at = store.favicon_fetched_at(target.name)
        # ?v= busts browser caches when the hourly sweep replaces the bytes.
        icon = (
            f"/icon/{quote(target.name, safe='')}?v={fetched_at}"
            if fetched_at is not None
            else None
        )
        components.append(
            {
                "name": target.name,
                "url": target.url,
                "icon": icon,
                **data,
            }
        )
    return {
        "title": config.TITLE,
        "version": config.VERSION,
        "generated_at": int(time.time()),
        "history_days": config.HISTORY_DAYS,
        "check_interval_seconds": config.CHECK_INTERVAL_SECONDS,
        "overall": _overall(components),
        "components": components,
    }


@app.route("/")
def index():
    return render_template(
        "index.html",
        title=config.TITLE,
        history_days=config.HISTORY_DAYS,
        version=config.VERSION,
    )


@app.route("/api/status")
def api_status():
    return jsonify(_build_status())


@app.route("/icon/<name>")
def icon(name):
    fav = store.get_favicon(name)
    if fav is None:
        abort(404)
    data, content_type, fetched_at = fav
    return Response(
        data,
        mimetype=content_type,
        headers={
            # Align with the hourly favicon sweep; ?v=fetched_at also busts caches.
            "Cache-Control": "public, max-age=3600",
            "ETag": f'"{fetched_at}"',
        },
    )


@app.route("/healthz")
def healthz():
    return jsonify(
        {"ok": True, "version": config.VERSION, "targets": len(config.TARGETS)}
    )
