"""Write a static status snapshot to disk after each check cycle.

Every cycle CurriePing renders the same HTML/JSON the live app serves and
atomically replaces files under config.EXPORT_DIR. A plain file server can
host that tree without hitting Flask — fast for humans and fully readable by
bots (no client-side Loading wait on first paint).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from . import config, store

log = logging.getLogger(__name__)

_STATIC_FAVICON = Path(__file__).resolve().parent / "static" / "favicon.ico"


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace `path` with `data` via same-directory temp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write(status: dict[str, object], html: str | bytes) -> None:
    """Atomically write index.html, api/status.json, icons, and crawl helpers."""
    root = Path(config.EXPORT_DIR)
    html_bytes = html.encode("utf-8") if isinstance(html, str) else html

    _atomic_write(root / "index.html", html_bytes)
    _atomic_write(
        root / "api" / "status.json",
        (json.dumps(status, separators=(",", ":"), ensure_ascii=False) + "\n").encode(
            "utf-8"
        ),
    )

    page = config.STATUS_PAGE_URL.rstrip("/") + "/" if config.STATUS_PAGE_URL else "/"
    _atomic_write(
        root / "robots.txt",
        f"User-agent: *\nAllow: /\n\nSitemap: {page}sitemap.xml\n".encode("utf-8"),
    )

    page_loc = page if page != "/" else "/"
    api_loc = f"{page}api/status.json" if page != "/" else "/api/status.json"
    _atomic_write(
        root / "sitemap.xml",
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"  <url><loc>{page_loc}</loc>"
            "<changefreq>always</changefreq><priority>1.0</priority></url>\n"
            f"  <url><loc>{api_loc}</loc>"
            "<changefreq>always</changefreq><priority>0.8</priority></url>\n"
            "</urlset>\n"
        ).encode("utf-8"),
    )

    if _STATIC_FAVICON.is_file():
        _atomic_write(root / "static" / "favicon.ico", _STATIC_FAVICON.read_bytes())

    for target in config.TARGETS:
        fav = store.get_favicon(target.name)
        if fav is None:
            continue
        data, _content_type, _fetched_at = fav
        # Match live /icon/<name> paths (query string is cache-bust only).
        _atomic_write(root / "icon" / quote(target.name, safe=""), data)

    log.info("Wrote static status snapshot to %s", root)
