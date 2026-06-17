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

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

log    = logging.getLogger("wazibot")
router = APIRouter()


def _find_static_dir() -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(base, "static"),
        os.path.join(base, "..", "static"),
        os.path.join(base, "..", "..", "static"),
    ]
    for c in candidates:
        p = os.path.abspath(c)
        if os.path.isdir(p):
            return p
    return os.path.abspath(os.path.join(base, "..", "static"))

STATIC_DIR = _find_static_dir()

# Public-safe fields only — never exposes credentials/tokens
_BIZ_PUBLIC_FIELDS = (
    "id,name,category,tagline,logo_url,theme_colour,"
    "currency,currency_symbol,onboarding_completed"
)

_PRODUCT_PUBLIC_FIELDS = "id,name,price,description,category,image_url,stock"


def _slug_to_name(slug: str) -> str:
    """Convert URL slug back to a business name pattern for lookup."""
    return slug.replace("-", " ")


def _name_to_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _safe_biz(b: dict) -> dict:
    """Return only public-safe business fields."""
    return {
        "id":           b.get("id"),
        "name":         b.get("name", ""),
        "category":     b.get("category", ""),
        "tagline":      b.get("tagline", ""),
        "logo_url":     b.get("logo_url", ""),
        "theme_colour": b.get("theme_colour", "#00c853"),
        "currency":     b.get("currency", "USD"),
        "currency_sym": b.get("currency_symbol", "$"),
        "slug":         _name_to_slug(b.get("name", "")),
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
def api_directory():
    """JSON: list of all publicly visible active businesses."""
    try:
        from core.db import supabase
        res = (
            supabase.table("businesses")
            .select(_BIZ_PUBLIC_FIELDS)
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
def api_store(slug: str):
    """JSON: public store data for one business."""
    try:
        from core.db import supabase
        name_pattern = _slug_to_name(slug)   # e.g. "flavoury-foods" → "flavoury foods"

        # M5/M8: try exact name match first (case-insensitive) to avoid matching
        # wrong business with similar name; fall back to ilike contains-match
        res = (
            supabase.table("businesses")
            .select(_BIZ_PUBLIC_FIELDS)
            .eq("is_active", True)
            .ilike("name", name_pattern)       # exact ci match: "flavoury foods"
            .limit(1)
            .execute()
        )
        if not res.data:
            # Fallback 1: contains match on space-expanded slug
            res = (
                supabase.table("businesses")
                .select(_BIZ_PUBLIC_FIELDS)
                .eq("is_active", True)
                .ilike("name", f"%{name_pattern}%")
                .limit(1)
                .execute()
            )
        if not res.data:
            # Fallback 2: try matching against the raw slug characters (no space expansion)
            # Handles single-word names like "Firelilyfarrismum" whose slug is
            # "firelilyfarrismum" — different from the space-expanded form
            slug_compact = slug.replace("-", "")
            res = (
                supabase.table("businesses")
                .select(_BIZ_PUBLIC_FIELDS)
                .eq("is_active", True)
                .ilike("name", f"%{slug_compact}%")
                .limit(1)
                .execute()
            )
        if not res.data:
            raise HTTPException(404, f"Business '{slug}' not found")
        biz = _safe_biz(res.data[0])

        prod_res = (
            supabase.table("products")
            .select(_PRODUCT_PUBLIC_FIELDS)
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

def _serve_static(filename: str) -> HTMLResponse:
    path = os.path.join(STATIC_DIR, filename)
    if os.path.exists(path):
        return HTMLResponse(open(path).read())
    return HTMLResponse(f"<html><body><p>File not found: {filename}</p></body></html>", status_code=404)


@router.get("/directory", response_class=HTMLResponse, include_in_schema=False)
async def directory_page():
    """Public marketplace directory SPA."""
    return _serve_static("marketplace.html")


# /store/{slug} and /menu/{slug} are served by main.py fallback routes.
# Only /site/{slug} is unique to this router.
@router.get("/site/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def site_page(slug: str):
    """AI-generated website for a business."""
    try:
        from services.site_generator import generate_site_html
        html = generate_site_html(slug)
        return HTMLResponse(html)
    except Exception as exc:
        log.warning("site_page error: %s", exc)
        return _serve_static("store.html")
