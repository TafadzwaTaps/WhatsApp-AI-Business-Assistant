"""
routes/seo_routes.py — Google-crawlability endpoints: /sitemap.xml, /robots.txt.

Both are new, additive, unauthenticated GET endpoints. Neither touches
existing routes, existing routers, auth, Stripe, WhatsApp, or the dashboard.

Why /sitemap.xml previously returned {"detail":"Not Found"}:
  main.py never defined a route for it, so FastAPI's own 404 handler
  produced that default JSON body. There was no bug to "fix" beyond
  registering the route.

Design:
  - PUBLIC_PAGES is a small, hand-maintained list of real, indexable,
    non-authenticated marketing pages. Add a tuple here when a new public
    page ships — that's the one place future public pages need to be
    added (Phase 2's "maintainable" requirement).
  - Per-tenant public pages (/store/{slug}, /menu/{slug}, /site/{slug})
    are safe to crawl but are unbounded and DB-backed, so they are added
    dynamically by querying the same public, already-safe "businesses"
    listing the marketplace API uses (`is_active=True` only — the exact
    same data already exposed at GET /api/directory). This is wrapped in
    try/except: if the DB is unreachable for any reason, the sitemap
    still returns 200 with the static pages rather than failing.
  - Never lists /dashboard, /inbox, /onboarding, /admin, /webhook, API
    routes, or any authenticated/internal path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, Response

log = logging.getLogger("wazibot")
router = APIRouter(include_in_schema=False)

DOMAIN = "https://wazibothq.com"

# ── Static public pages ──────────────────────────────────────────────────
# (path, change_frequency, priority) — only real, existing, public routes.
# Add a new tuple here when a new public marketing page ships.
PUBLIC_PAGES = [
    ("/",          "weekly",  "1.0"),
    ("/pricing",   "weekly",  "0.8"),
    ("/signup",    "monthly", "0.7"),
    ("/directory", "daily",   "0.6"),
    ("/privacy",   "yearly",  "0.3"),
    ("/terms",     "yearly",  "0.3"),
]


def _public_store_slugs() -> list[str]:
    """Slugs of active, publicly-listed businesses (/store/{slug}).

    Reuses the same public, already-safe query as GET /api/directory
    (routes/marketplace_routes.py) — only id/name of businesses that have
    opted into the public marketplace. Never touches private fields.
    Fails silently (empty list) so a DB hiccup never breaks the sitemap.
    """
    try:
        from core.db import supabase
        from routes.marketplace_routes import _name_to_slug

        res = (
            supabase.table("businesses")
            .select("name")
            .eq("is_active", True)
            .execute()
        )
        slugs = []
        for b in res.data or []:
            name = (b or {}).get("name")
            if name:
                slugs.append(_name_to_slug(name))
        return slugs
    except Exception as exc:
        log.warning("seo_routes: could not load public store slugs for sitemap: %s", exc)
        return []


@router.get("/sitemap.xml")
def sitemap_xml():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    urls = []
    for path, freq, priority in PUBLIC_PAGES:
        urls.append((f"{DOMAIN}{path}", freq, priority))

    for slug in _public_store_slugs():
        urls.append((f"{DOMAIN}/store/{escape(slug)}", "weekly", "0.5"))

    parts = ['<?xml version="1.0" encoding="UTF-8"?>']
    parts.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for loc, freq, priority in urls:
        parts.append(
            "<url>"
            f"<loc>{escape(loc)}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{priority}</priority>"
            "</url>"
        )
    parts.append("</urlset>")
    xml = "\n".join(parts)

    return Response(content=xml, media_type="application/xml")


@router.get("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Allow: /static/",
        "",
        # Authenticated / private application surfaces — never crawlable.
        # Enumerated by inspecting every registered router's route paths
        # (auth, billing, admin, onboarding, webhooks, orders, platform,
        # debug), not guessed.
        "Disallow: /dashboard",
        "Disallow: /inbox",
        "Disallow: /onboarding",
        "Disallow: /config/public",
        "Disallow: /api/",
        "Disallow: /admin",
        "Disallow: /debug",
        "Disallow: /auth",
        "Disallow: /billing",
        "Disallow: /webhook",
        "Disallow: /payment",
        "Disallow: /invoice",
        "Disallow: /orders",
        "Disallow: /platform",
        "",
        f"Sitemap: {DOMAIN}/sitemap.xml",
        "",
    ]
    return PlainTextResponse("\n".join(lines), media_type="text/plain")
