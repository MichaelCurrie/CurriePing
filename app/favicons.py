"""Server-side favicon fetch, SQLite cache, and static export paths.

The monitor process downloads each target's favicon and stores bytes +
content-type in SQLite. The status JSON points browsers at same-origin
`/icon/<name>.<ext>` URLs; the static export bakes those files under
`EXPORT_DIR/icon/` for Caddy. Clients never fetch icons from the targets.

A daemon thread sweeps the cache and re-fetches hourly (and once at startup).
Successful writes can trigger a static re-export so new icons appear without
waiting for the next uptime check cycle.

Resolution order per site, first hit wins:
  1. <link rel="...icon..."> declared in the page HTML
  2. /favicon.ico at the site root
  3. Google's favicon service (normalized PNG) as a last-resort fallback
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from urllib.parse import quote, urljoin, urlparse

import requests

from . import config, store

REFRESH_INTERVAL_SECONDS = 3600

# Extension used when baking `/icon/<name>.<ext>` for Caddy's file_server MIME map.
_CONTENT_TYPE_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/svg+xml": ".svg",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
# Longest-first order for stripping; keep in sync with _CONTENT_TYPE_EXT values.
_KNOWN_ICON_EXTS: tuple[str, ...] = (".png", ".ico", ".svg", ".gif", ".jpg", ".webp")


def icon_extension(content_type: str) -> str:
    """File extension for a cached favicon content-type (default `.bin`)."""
    base = content_type.split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_EXT.get(base, ".bin")


def icon_filename(target_name: str, content_type: str) -> str:
    """On-disk / URL basename: URL-encoded target name + type extension."""
    return f"{quote(target_name, safe='')}{icon_extension(content_type)}"


def icon_href(target_name: str, content_type: str, fetched_at: int) -> str:
    """Same-origin icon URL; `?v=` busts caches when the hourly sweep updates bytes."""
    return f"/icon/{icon_filename(target_name, content_type)}?v={fetched_at}"


def icon_lookup_name(path_name: str) -> str:
    """Strip a known image extension from an `/icon/…` path segment for DB lookup.

    Target names may contain dots (`datum.locker`); only trailing favicon
    extensions are removed.
    """
    lower = path_name.lower()
    for ext in _KNOWN_ICON_EXTS:
        if lower.endswith(ext):
            return path_name[: -len(ext)]
    return path_name


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


_after_save: Callable[[], None] | None = None


def set_after_save(callback: Callable[[], None] | None) -> None:
    """Register a zero-arg callback invoked after any favicon is newly written."""
    global _after_save
    _after_save = callback


def refresh_all() -> None:
    # Update per target only after a successful fetch. Do not wipe the table
    # first — on IPv6-only hosts many icon URLs fail, and a full clear would
    # blank every favicon until the next lucky sweep.
    saved = 0
    for target in config.TARGETS:
        result = fetch_favicon(target.url)
        if result:
            data, ctype = result
            store.save_favicon(target.name, data, ctype)
            saved += 1
    if saved and _after_save is not None:
        try:
            _after_save()
        except Exception:
            pass  # export/hooks must never kill the favicon loop


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
