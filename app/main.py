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
    components = []
    for target in config.TARGETS:
        data = store.component(target.name, config.HISTORY_DAYS)
        has_icon = store.has_favicon(target.name)
        icon = "/icon/" + quote(target.name, safe="") if has_icon else None
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
    )


@app.route("/api/status")
def api_status():
    return jsonify(_build_status())


@app.route("/icon/<name>")
def icon(name):
    fav = store.get_favicon(name)
    if fav is None:
        abort(404)
    data, content_type = fav
    return Response(
        data,
        mimetype=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "targets": len(config.TARGETS)})
