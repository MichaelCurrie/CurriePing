"""Flask app: serves the status page and its JSON API, and owns the checker."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import cast
from urllib.parse import quote, urljoin

from flask import (
    Flask,
    Response,
    abort,
    has_request_context,
    jsonify,
    render_template,
    request,
)

from . import checker, config, export, favicons, store

app = Flask(__name__)

store.init(config.DB_PATH)

_STATUS_LABELS = {
    "operational": "Operational",
    "down": "Down",
    "degraded": "Degraded",
    "unknown": "Unknown",
}


def _overall(components: list[dict[str, object]]) -> str:
    states = {c["status"] for c in components}
    if not components or states == {"unknown"}:
        return "unknown"
    if "down" in states:
        return "down"
    # Only today's UTC bucket signals current degradation (buckets are sparse).
    today_key = datetime.now(timezone.utc).date().isoformat()
    for component in components:
        buckets = component.get("buckets")
        if not isinstance(buckets, list):
            continue
        for raw in buckets:
            if not isinstance(raw, dict):
                continue
            bucket = cast(dict[str, object], raw)
            if bucket.get("date") == today_key and bucket.get("state") == "partial":
                return "degraded"
    if "unknown" in states and states != {"operational", "unknown"}:
        return "degraded"
    return "operational"


def _build_status() -> dict[str, object]:
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
        entry: dict[str, object] = {
            "name": target.name,
            "url": target.url,
            "urls": list(target.urls),
            **data,
        }
        if icon is not None:
            entry["icon"] = icon
        components.append(entry)
    return {
        "title": config.TITLE,
        "version": config.VERSION,
        "generated_at": int(time.time()),
        "history_days": config.HISTORY_DAYS,
        "check_interval_seconds": config.CHECK_INTERVAL_SECONDS,
        "check_ipv4": config.CHECK_IPV4,
        "check_ipv6": config.CHECK_IPV6,
        "overall": _overall(components),
        "components": components,
    }


def _page_url() -> str:
    """Canonical status-page origin for structured data (trailing slash)."""
    if config.STATUS_PAGE_URL:
        return config.STATUS_PAGE_URL.rstrip("/") + "/"
    if has_request_context():
        return request.url_root
    # Static export / no request: relative root so the snapshot is relocatable.
    return "/"


def _api_status_url() -> str:
    """Primary JSON URL advertised to agents / <link rel=alternate>."""
    # Offline/static render advertises the on-disk file; live requests use Flask.
    if not has_request_context():
        return urljoin(_page_url(), "api/status.json")
    return urljoin(_page_url(), "api/status")


def _api_status_candidates() -> list[str]:
    """URLs the open page polls for live updates (first success wins).

    Bootstrap is first paint only — the tab keeps fetching so status stays
    current after later check cycles. Prefer the live API when serving from
    Flask; static snapshots try status.json first, then /api/status.
    """
    page = _page_url()
    live = urljoin(page, "api/status")
    static = urljoin(page, "api/status.json")
    if not has_request_context():
        return [static, live]
    return [live, static]


def _status_label(code: object) -> str:
    if isinstance(code, str) and code in _STATUS_LABELS:
        return _STATUS_LABELS[code]
    return "Unknown"


def _meta_description(status: dict[str, object]) -> str:
    """One-line summary for search engines / agents that only read <meta>."""
    overall = _status_label(status.get("overall"))
    components = status.get("components")
    n = len(components) if isinstance(components, list) else 0
    days = status.get("history_days", config.HISTORY_DAYS)
    return (
        f"{config.TITLE}: overall {overall}. "
        f"Monitoring {n} service{'s' if n != 1 else ''} "
        f"over the last {days} days. "
        f"Machine-readable JSON at {_api_status_url()}."
    )


def _json_ld(status: dict[str, object]) -> dict[str, object]:
    """schema.org WebPage + ItemList so agents need not scrape the JS UI."""
    page = _page_url()
    api_url = _api_status_url()
    generated = status.get("generated_at")
    modified = (
        datetime.fromtimestamp(int(generated), tz=timezone.utc).isoformat()
        if isinstance(generated, int)
        else None
    )
    components = status.get("components")
    items: list[dict[str, object]] = []
    if isinstance(components, list):
        for i, raw in enumerate(components, start=1):
            if not isinstance(raw, dict):
                continue
            # Bare isinstance(dict) narrows to dict[Never, Never] under ty.
            component = cast(dict[str, object], raw)
            name = component.get("name")
            url = component.get("url")
            props: list[dict[str, object]] = [
                {
                    "@type": "PropertyValue",
                    "name": "status",
                    "value": component.get("status") or "unknown",
                },
                {
                    "@type": "PropertyValue",
                    "name": "statusLabel",
                    "value": component.get("status_label")
                    or _status_label(component.get("status")),
                },
            ]
            uptime = component.get("uptime")
            if isinstance(uptime, (int, float)):
                props.append(
                    {
                        "@type": "PropertyValue",
                        "name": "uptimePercent",
                        "value": uptime,
                        "unitText": "percent",
                        "description": f"{config.HISTORY_DAYS}-day uptime",
                    }
                )
            service: dict[str, object] = {
                "@type": "Service",
                "name": name if isinstance(name, str) else "service",
                "additionalProperty": props,
            }
            if isinstance(url, str) and url:
                service["url"] = url
            items.append(
                {
                    "@type": "ListItem",
                    "position": i,
                    "item": service,
                }
            )

    overall = status.get("overall") or "unknown"
    doc: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "@id": page,
        "url": page,
        "name": config.TITLE,
        "description": _meta_description(status),
        "inLanguage": "en",
        "about": {
            "@type": "Thing",
            "name": "Service availability",
            "description": f"Overall status: {_status_label(overall)}",
            "additionalProperty": [
                {
                    "@type": "PropertyValue",
                    "name": "overallStatus",
                    "value": overall,
                },
                {
                    "@type": "PropertyValue",
                    "name": "checkIntervalSeconds",
                    "value": status.get("check_interval_seconds"),
                },
                {
                    "@type": "PropertyValue",
                    "name": "checkIPv4",
                    "value": bool(status.get("check_ipv4")),
                },
                {
                    "@type": "PropertyValue",
                    "name": "checkIPv6",
                    "value": status.get("check_ipv6") is not False,
                },
                {
                    "@type": "PropertyValue",
                    "name": "historyDays",
                    "value": status.get("history_days"),
                },
            ],
        },
        "mainEntity": {
            "@type": "ItemList",
            "name": "Monitored services",
            "numberOfItems": len(items),
            "itemListElement": items,
        },
        "significantLink": api_url,
        "subjectOf": {
            "@type": "DataDownload",
            "name": "CurriePing status API",
            "description": (
                "Authoritative machine-readable status (same fields as this page)."
            ),
            "encodingFormat": "application/json",
            "contentUrl": api_url,
        },
        "provider": {
            "@type": "SoftwareApplication",
            "name": "CurriePing",
            "softwareVersion": config.VERSION,
            "url": "https://github.com/MichaelCurrie/CurriePing",
            "license": "https://unlicense.org/",
        },
    }
    if modified:
        doc["dateModified"] = modified
    return doc


# Public status surface is meant to be crawled (search engines, AI agents).
# Never emit noindex/nofollow here.
_CRAWL_ROBOTS = "index, follow, max-snippet:-1, max-image-preview:large"


@app.after_request
def _crawlable_headers(response: Response) -> Response:
    # Header wins over missing/conflicting meta for many bots.
    if "X-Robots-Tag" not in response.headers:
        response.headers["X-Robots-Tag"] = _CRAWL_ROBOTS
    return response


def _noscript_rows(status: dict[str, object]) -> list[dict[str, object]]:
    components = status.get("components")
    rows: list[dict[str, object]] = []
    if not isinstance(components, list):
        return rows
    for raw in components:
        if not isinstance(raw, dict):
            continue
        component = cast(dict[str, object], raw)
        rows.append(
            {
                "name": component.get("name"),
                "url": component.get("url"),
                "status": component.get("status"),
                "status_label": component.get("status_label")
                or _status_label(component.get("status")),
                "uptime": component.get("uptime"),
            }
        )
    return rows


def _render_index(status: dict[str, object]) -> str:
    """Render the status page HTML for a live response or static export."""
    noscript_rows = _noscript_rows(status)
    return render_template(
        "index.html",
        title=config.TITLE,
        history_days=config.HISTORY_DAYS,
        version=config.VERSION,
        check_ipv4=config.CHECK_IPV4,
        check_ipv6=config.CHECK_IPV6,
        check_interval_seconds=config.CHECK_INTERVAL_SECONDS,
        page_url=_page_url(),
        api_status_url=_api_status_url(),
        api_status_candidates=_api_status_candidates(),
        meta_description=_meta_description(status),
        robots=_CRAWL_ROBOTS,
        overall=status.get("overall") or "unknown",
        overall_label=_status_label(status.get("overall")),
        generated_at=status.get("generated_at"),
        json_ld=_json_ld(status),
        noscript_components=noscript_rows,
        status_bootstrap=status,
        component_count=len(noscript_rows),
    )


def _export_static() -> None:
    """Write a complete static snapshot under config.EXPORT_DIR."""
    with app.app_context():
        status = _build_status()
        html = _render_index(status)
        export.write(status, html)


@app.route("/")
def index():
    return _render_index(_build_status())


@app.route("/robots.txt")
def robots_txt() -> Response:
    """Explicit allow-all — status pages should be fully crawlable."""
    page = _page_url()
    body = f"User-agent: *\nAllow: /\n\nSitemap: {urljoin(page, 'sitemap.xml')}\n"
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/sitemap.xml")
def sitemap_xml() -> Response:
    page = _page_url().rstrip("/") + "/"
    api = urljoin(page, "api/status")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>{page}</loc><changefreq>always</changefreq>"
        "<priority>1.0</priority></url>\n"
        f"  <url><loc>{api}</loc><changefreq>always</changefreq>"
        "<priority>0.8</priority></url>\n"
        "</urlset>\n"
    )
    return Response(body, mimetype="application/xml; charset=utf-8")


@app.route("/api/status")
@app.route("/api/status.json")
def api_status():
    # status.json alias keeps exported pages polling when Flask is still origin.
    return jsonify(_build_status())


@app.route("/icon/<name>")
def icon(name: str) -> Response:
    fav = store.get_favicon(name)
    if fav is None:
        abort(404)
        raise AssertionError("abort() does not return")
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
        {
            "ok": True,
            "version": config.VERSION,
            "targets": len(config.TARGETS),
            "check_ipv4": config.CHECK_IPV4,
            "check_ipv6": config.CHECK_IPV6,
        }
    )


# Register export before starting the checker so the first cycle cannot miss it.
checker.set_after_cycle(_export_static)
checker.start()
favicons.start()
# Populate EXPORT_DIR before the first check finishes (empty history is fine).
try:
    _export_static()
except Exception:
    pass
