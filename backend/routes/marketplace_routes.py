"""
routes/marketplace_routes.py
════════════════════════════
Public marketplace — read-only, no auth required.

PLACEMENT: backend/routes/marketplace_routes.py

Public endpoints:
  GET /directory             — marketplace listing of all active businesses
  GET /store/{slug}          — public store page for one business
  GET /menu/{slug}           — public menu page (same data, menu-focused layout)
  GET /api/directory         — JSON API for directory data
  GET /api/store/{slug}      — JSON API for store data

All routes are READ-ONLY. No writes. No auth.
All data is already public (businesses choose to list publicly).
Never exposes private business fields (EcoCash numbers, tokens, etc.).
"""
from __future__ import annotations

import logging
import os
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

log    = logging.getLogger("wazibot")
router = APIRouter()


# Static files served exclusively via app.mount("/static", StaticFiles(...))
# All page routes below use RedirectResponse — no manual file I/O, no path issues.

# Public-safe fields only — never exposes credentials/tokens.
#
# IMPORTANT: this list is built dynamically (see _get_biz_public_fields below)
# rather than hardcoded. tagline, logo_url, and theme_colour were added to
# this constant in an earlier session assuming a migration had been run —
# it hadn't. Supabase rejects a .select() that names a column which doesn't
# exist on the table with a single error for the WHOLE query (PGRST204-style),
# which the broad except Exception here silently swallowed, returning an
# empty directory/store every time even though the businesses existed and
# were active. Probing the schema first means every endpoint below keeps
# working today, and automatically picks up the optional columns the moment
# their migration is run — no further code change needed.
_ALWAYS_SAFE_BIZ_FIELDS = "id,name,category,currency,currency_symbol,onboarding_completed"
_OPTIONAL_BIZ_FIELDS    = ("tagline", "logo_url", "theme_colour", "is_service_business")

_ALWAYS_SAFE_PRODUCT_FIELDS = "id,name,price"
_OPTIONAL_PRODUCT_FIELDS    = ("description", "category", "image_url", "stock")

_biz_columns_cache: set | None = None
_product_columns_cache: set | None = None


def _get_biz_columns() -> set:
    """Probe the live businesses table schema once, cache the result."""
    global _biz_columns_cache
    if _biz_columns_cache is None:
        try:
            from core.db import supabase
            res = supabase.table("businesses").select("*").limit(1).execute()
            if res.data:
                _biz_columns_cache = set(res.data[0].keys())
            else:
                _biz_columns_cache = set(_ALWAYS_SAFE_BIZ_FIELDS.split(","))
        except Exception:
            _biz_columns_cache = set()
    return _biz_columns_cache


def _get_biz_public_fields() -> str:
    """Build the select() field list from columns confirmed to exist."""
    cols = _get_biz_columns()
    extra = [f for f in _OPTIONAL_BIZ_FIELDS if f in cols]
    fields = _ALWAYS_SAFE_BIZ_FIELDS.split(",") + extra
    return ",".join(fields)


def _get_product_columns() -> set:
    """Probe the live products table schema once, cache the result."""
    global _product_columns_cache
    if _product_columns_cache is None:
        try:
            from core.db import supabase
            res = supabase.table("products").select("*").limit(1).execute()
            if res.data:
                _product_columns_cache = set(res.data[0].keys())
            else:
                _product_columns_cache = set(_ALWAYS_SAFE_PRODUCT_FIELDS.split(","))
        except Exception:
            _product_columns_cache = set()
    return _product_columns_cache


def _get_product_public_fields() -> str:
    """
    Build the select() field list from columns confirmed to exist.

    Fix: "description" was hardcoded here even though it doesn't exist on
    the live products table yet. Supabase rejects a .select() naming any
    unknown column for the WHOLE query — this took down /api/store/{slug}
    entirely (500 error, "Store not found" shown to customers) even though
    the business and its products were real and active. Same root cause
    and fix pattern as _get_biz_public_fields above.
    """
    cols  = _get_product_columns()
    extra = [f for f in _OPTIONAL_PRODUCT_FIELDS if f in cols]
    fields = _ALWAYS_SAFE_PRODUCT_FIELDS.split(",") + extra
    return ",".join(fields)


def _slug_to_name(slug: str) -> str:
    """Convert URL slug back to a business name pattern for lookup."""
    return slug.replace("-", " ")


def _name_to_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _safe_biz(b: dict) -> dict:
    """Return only public-safe business fields."""
    slug = _name_to_slug(b.get("name", ""))
    return {
        "id":           b.get("id"),
        "name":         b.get("name", ""),
        "category":     b.get("category", ""),
        "tagline":      b.get("tagline", ""),
        "logo_url":     b.get("logo_url", ""),
        "theme_colour": b.get("theme_colour", "#00c853"),
        "currency":     b.get("currency", "USD"),
        "currency_sym": b.get("currency_symbol", "$"),
        "slug":         slug,
        # Only present when the business actually has bookings enabled —
        # the directory should show no "Book Now" button otherwise.
        "booking_url":  f"/book/{slug}" if b.get("is_service_business") else None,
    }


def _safe_product(p: dict) -> dict:
    stock = p.get("stock")
    return {
        "id":          p.get("id"),
        "name":        p.get("name", ""),
        "price":       float(p.get("price") or 0),
        "description": p.get("description", ""),
        "category":    p.get("category", ""),
        "image_url":   p.get("image_url", ""),
        "available":   stock is None or stock > 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# JSON API
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/directory")
def api_directory(request: Request):
    """JSON: list of all publicly visible active businesses."""
    from services.security import check as _public_rl
    _public_rl("public_read", request)
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select(_get_biz_public_fields())
            .eq("is_active", True)
            .order("display_order", desc=False, nullsfirst=True)
            .order("id", desc=False)
            .execute()
        )
        businesses = [_safe_biz(b) for b in (res.data or [])]
        return {"businesses": businesses, "total": len(businesses)}
    except Exception as exc:
        log.warning("api_directory error: %s", exc)
        return {"businesses": [], "total": 0}


@router.get("/api/store/{slug}")
def api_store(slug: str, request: Request):
    """JSON: public store data for one business."""
    from services.security import check as _public_rl
    _public_rl("public_read", request)
    try:
        from core.db import supabase
        name_pattern = _slug_to_name(slug)   # e.g. "flavoury-foods" → "flavoury foods"

        # M5/M8: try exact name match first (case-insensitive) to avoid matching
        # wrong business with similar name; fall back to ilike contains-match
        # Exact slug match; only an unambiguous loose match is allowed as a
        # fallback, so one business's link can never open another's store.
        from core.slug import find_active_business_by_slug
        _row = find_active_business_by_slug(supabase, slug, _get_biz_public_fields())
        if not _row:
            raise HTTPException(404, f"Business '{slug}' not found")
        class _Res:  # keeps the code below (res.data[0]) unchanged
            data = [_row]
        res = _Res()
        biz = _safe_biz(res.data[0])

        prod_res = (
            supabase.table("products")
            .select(_get_product_public_fields())
            .eq("business_id", res.data[0]["id"])
            .execute()
        )
        products = [_safe_product(p) for p in (prod_res.data or [])]
        return {"business": biz, "products": products, "total_products": len(products)}
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("api_store error: %s", exc)
        raise HTTPException(500, "Could not load store")


# ─────────────────────────────────────────────────────────────────────────────
# HTML pages (serve SPAs from static/)
# ─────────────────────────────────────────────────────────────────────────────

# _serve_static removed — use RedirectResponse to /static/ instead


# /directory page route is registered in main.py (more reliable — main.py
# is always deployed). This router handles only /api/directory and /api/store/* API routes.


# /store/{slug} and /menu/{slug} are served by main.py fallback routes.
# Only /site/{slug} is unique to this router.
@router.get("/site/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def site_page(slug: str, request: Request):
    """AI-generated website for a business."""
    from services.security import check as _public_rl
    _public_rl("public_read", request)   # RateLimitExceeded -> 429 via the app handler
    # SuperAdmin 2.0 emergency control — platform-wide generated-site pause.
    # This is the ONLY rendering path for these sites in this codebase, so
    # this is genuinely the whole "website-gen" kill switch — pausing it
    # takes every business's generated site offline, not just new ones.
    # Fails open (is_paused() returns False on any error), and only takes
    # effect when a SuperAdmin explicitly flips it during an incident.
    try:
        from core.platform_controls import is_paused
        if is_paused("website_gen"):
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;text-align:center;padding:60px;'>"
                "<h2>Temporarily unavailable</h2><p>This site is undergoing brief maintenance. Please check back shortly.</p>"
                "</body></html>", status_code=503,
            )
    except Exception as _swallowed_exc:
        from core.safelog import swallow as _swallow; _swallow(__name__, _swallowed_exc)

    try:
        from services.site_generator import generate_site_html
        html = generate_site_html(slug)
        return HTMLResponse(html)
    except Exception as exc:
        log.warning("site_page error: %s", exc)
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/static/store.html", status_code=302)
