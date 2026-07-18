"""Favicon fetching and caching.

Each target's favicon is downloaded and stored in SQLite (bytes + content-type)
so the status page can show it without hotlinking the origin on every render.
A daemon thread sweeps the cache and re-fetches hourly (and once at startup).

Resolution order per site, first hit wins:
  1. <link rel="...icon..."> declared in the page HTML
  2. /favicon.ico at the site root
  3. Google's favicon service (normalized PNG) as a last-resort fallback
"""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import urljoin, urlparse

import requests

from . import config, store

REFRESH_INTERVAL_SECONDS = 3600

# Magic-byte signatures so we can accept favicons served with a wrong/missing
# Content-Type (common for /favicon.ico).
_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),  # WEBP starts with RIFF....WEBP
)


def _sniff(data: bytes) -> str | None:
    """Return an image content-type from magic bytes, or None if not an image."""
    if not data:
        return None
    for sig, ctype in _SIGNATURES:
        if data.startswith(sig):
            return ctype
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "image/svg+xml"
    return None


def _icon_links_from_html(html: str, base_url: str) -> list[str]:
    links = []
    for tag in re.findall(r"<link\b[^>]*>", html, flags=re.IGNORECASE):
        if not re.search(r'rel\s*=\s*["\']?[^"\'>]*icon', tag, flags=re.IGNORECASE):
            continue
        href = re.search(r'href\s*=\s*["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
        if href:
            links.append(urljoin(base_url, href.group(1)))
    return links


def _download(url: str) -> tuple[bytes, str] | None:
    try:
        r = requests.get(
            url,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
        )
    except requests.RequestException:
        return None
    if not r.ok or not r.content:
        return None
    declared = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    sniffed = _sniff(r.content)
    if declared.startswith("image/"):
        return r.content, (sniffed or declared)
    if sniffed:
        return r.content, sniffed
    return None


def fetch_favicon(url: str) -> tuple[bytes, str] | None:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates: list[str] = []

    try:
        page = requests.get(
            url,
            timeout=config.REQUEST_TIMEOUT_SECONDS,
            allow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
        )
        if page.ok and page.text:
            candidates.extend(_icon_links_from_html(page.text, page.url))
    except requests.RequestException:
        pass

    candidates.append(urljoin(origin + "/", "favicon.ico"))
    candidates.append(
        f"https://www.google.com/s2/favicons?sz=64&domain={parsed.netloc}"
    )

    # Prefer raster (ICO/PNG) over SVG: SVG favicons render unreliably inside an
    # <img> (currentColor / prefers-color-scheme tricks can make them invisible),
    # so try any raster candidate first and fall back to .svg only if that's all
    # a site offers. Stable sort keeps the site's declared order within each group.
    candidates.sort(key=lambda u: u.split("?", 1)[0].lower().endswith(".svg"))

    seen = set()
    for icon_url in candidates:
        if icon_url in seen:
            continue
        seen.add(icon_url)
        result = _download(icon_url)
        if result:
            return result
    return None


def refresh_all() -> None:
    # Wipe first so a previously cached bad icon cannot outlive a failed re-fetch
    # for that target; the status page simply omits the img until the next success.
    store.clear_favicons()
    for target in config.TARGETS:
        result = fetch_favicon(target.url)
        if result:
            data, ctype = result
            store.save_favicon(target.name, data, ctype)


def _run() -> None:
    while True:
        try:
            refresh_all()
        except Exception:
            pass  # never let a fetch error kill the refresh loop
        time.sleep(REFRESH_INTERVAL_SECONDS)


def start() -> None:
    thread = threading.Thread(target=_run, name="favicon-refresh", daemon=True)
    thread.start()
