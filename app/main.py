"""Flask app: serves the status page and its JSON API, and owns the checker."""

from __future__ import annotations

import time

from flask import Flask, jsonify, render_template

from . import checker, config, store

app = Flask(__name__)

store.init(config.DB_PATH)
checker.start()


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
        components.append({"name": target.name, "url": target.url, **data})
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


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "targets": len(config.TARGETS)})
