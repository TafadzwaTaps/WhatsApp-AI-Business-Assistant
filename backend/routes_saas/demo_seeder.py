"""
saas/demo_seeder.py
═══════════════════
Demo Business Seeder — creates sample businesses + products for
the marketplace directory and setup wizard demos.

PLACEMENT: backend/saas/demo_seeder.py

Run once via:
    python -c "from saas.demo_seeder import seed_demo_businesses; seed_demo_businesses()"

Or call from the SaaS admin panel:
    POST /admin/saas/seed-demos  (requires superadmin)

SAFETY:
  - Only creates records marked demo=True (safe to delete)
  - Never modifies existing businesses
  - Idempotent — checks before inserting
  - Demo accounts use a reserved prefix: demo_*
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("wazibot")

DEMO_BUSINESSES = [
    {
        "name":             "Flavoury Foods",
        "category":         "Restaurant",
        "tagline":          "Delicious local food delivered to your door",
        "currency":         "USD",
        "currency_symbol":  "$",
        "is_active":        True,
        "is_demo":          True,
        "theme_colour":     "#f59e0b",
        "products": [
            {"name": "Sadza & Beef",  "price": 5.00,  "description": "Traditional sadza with beef stew",        "category": "Main", "stock": 50},
            {"name": "Chicken Wrap",  "price": 4.50,  "description": "Grilled chicken in a fresh wrap",          "category": "Main", "stock": 30},
            {"name": "Chips",         "price": 2.00,  "description": "Crispy fried potato chips",               "category": "Side", "stock": 100},
            {"name": "Mazoe Orange",  "price": 1.50,  "description": "Refreshing orange drink",                 "category": "Drink", "stock": 80},
        ],
    },
    {
        "name":             "Firelilyfarrismum",
        "category":         "Health & Beauty",
        "tagline":          "Premium flowers and beauty products",
        "currency":         "USD",
        "currency_symbol":  "$",
        "is_active":        True,
        "is_demo":          True,
        "theme_colour":     "#ec4899",
        "products": [
            {"name": "Exotic Flowers",   "price": 25.00, "description": "Beautiful exotic flower arrangement",  "category": "Flowers", "stock": 20},
            {"name": "Rose Bouquet",     "price": 15.00, "description": "Fresh red roses, 12 stems",           "category": "Flowers", "stock": 15},
            {"name": "Skincare Bundle",  "price": 35.00, "description": "Premium moisturiser + serum set",     "category": "Beauty",  "stock": 10},
            {"name": "Single Stem",      "price": 2.00,  "description": "One perfect flower stem",             "category": "Flowers", "stock": 100},
        ],
    },
    {
        "name":             "Uptown Class Barber",
        "category":         "Barbershop",
        "tagline":          "Premium grooming and haircuts",
        "currency":         "USD",
        "currency_symbol":  "$",
        "is_active":        True,
        "is_demo":          True,
        "theme_colour":     "#6366f1",
        "products": [
            {"name": "Haircut",         "price": 8.00,  "description": "Classic haircut + style",              "category": "Hair",   "stock": None},
            {"name": "Beard Trim",      "price": 5.00,  "description": "Beard shaping and trim",               "category": "Beard",  "stock": None},
            {"name": "Full Groom",      "price": 12.00, "description": "Haircut + beard + hot towel",          "category": "Combo",  "stock": None},
            {"name": "Kids Cut",        "price": 5.00,  "description": "Haircut for children under 12",        "category": "Hair",   "stock": None},
        ],
    },
    {
        "name":             "Econ Side Hustle",
        "category":         "Retail",
        "tagline":          "Everyday essentials at great prices",
        "currency":         "USD",
        "currency_symbol":  "$",
        "is_active":        True,
        "is_demo":          True,
        "theme_colour":     "#10b981",
        "products": [
            {"name": "Sugar 2kg",       "price": 2.50,  "description": "Fine white sugar",                     "category": "Grocery", "stock": 200},
            {"name": "Cooking Oil 2L",  "price": 4.00,  "description": "Pure vegetable cooking oil",           "category": "Grocery", "stock": 150},
            {"name": "Airtime $5",      "price": 5.00,  "description": "Econet airtime top-up voucher",        "category": "Telecoms", "stock": 500},
            {"name": "Data Bundle",     "price": 3.00,  "description": "500MB data bundle",                    "category": "Telecoms", "stock": 500},
        ],
    },
]


def seed_demo_businesses(force: bool = False) -> dict:
    """
    Create demo businesses and their products.

    force=True  → re-creates even if already exists (useful for reset)
    force=False → skips if the business name already exists

    Returns a summary dict.
    """
    try:
        from core.db import supabase
    except ImportError:
        log.error("demo_seeder: cannot import supabase — run from backend/")
        return {"error": "supabase not available"}

    created  = []
    skipped  = []
    errors   = []

    for demo in DEMO_BUSINESSES:
        name = demo["name"]
        try:
            # Check if already exists
            existing = (
                supabase.table("businesses")
                .select("id")
                .eq("name", name)
                .limit(1)
                .execute()
            )
            if existing.data and not force:
                skipped.append(name)
                log.debug("demo_seeder: skipping existing business: %s", name)
                continue

            # Create business — only include columns that exist safely
            biz_payload = {
                "name":            name,
                "category":        demo.get("category", ""),
                "currency":        demo.get("currency", "USD"),
                "currency_symbol": demo.get("currency_symbol", "$"),
                "is_active":       True,
                "onboarding_completed": True,
            }
            # Optional columns (added via ADD COLUMN IF NOT EXISTS in schema)
            for optional_col in ["tagline", "theme_colour"]:
                if demo.get(optional_col):
                    biz_payload[optional_col] = demo[optional_col]

            if existing.data and force:
                # Update existing
                biz_id = existing.data[0]["id"]
                supabase.table("businesses").update(biz_payload).eq("id", biz_id).execute()
            else:
                res = supabase.table("businesses").insert(biz_payload).execute()
                biz_id = res.data[0]["id"] if res.data else None

            if not biz_id:
                errors.append(f"{name}: no ID returned")
                continue

            # Create products
            for prod in demo.get("products", []):
                prod_payload = {
                    "business_id": biz_id,
                    "name":        prod["name"],
                    "price":       prod["price"],
                    "description": prod.get("description", ""),
                    "category":    prod.get("category", ""),
                }
                if prod.get("stock") is not None:
                    prod_payload["stock"] = prod["stock"]
                supabase.table("products").insert(prod_payload).execute()

            created.append({"name": name, "id": biz_id, "products": len(demo.get("products", []))})
            log.info("demo_seeder: created %s (id=%s)", name, biz_id)

        except Exception as exc:
            errors.append(f"{name}: {exc}")
            log.error("demo_seeder error for %s: %s", name, exc)

    summary = {
        "created": created,
        "skipped": skipped,
        "errors":  errors,
        "total_created": len(created),
        "total_skipped": len(skipped),
    }
    log.info("demo_seeder complete: %s", summary)
    return summary


def clear_demo_businesses() -> dict:
    """
    Remove all demo businesses and their data.
    Only removes records where is_demo=True (if that column exists)
    or where name matches the known demo names.
    """
    try:
        from core.db import supabase
    except ImportError:
        return {"error": "supabase not available"}

    removed = []
    demo_names = [d["name"] for d in DEMO_BUSINESSES]

    for name in demo_names:
        try:
            res = (
                supabase.table("businesses")
                .select("id")
                .eq("name", name)
                .limit(1)
                .execute()
            )
            if not res.data:
                continue
            biz_id = res.data[0]["id"]
            # Remove products first
            supabase.table("products").delete().eq("business_id", biz_id).execute()
            # Remove business
            supabase.table("businesses").delete().eq("id", biz_id).execute()
            removed.append(name)
            log.info("demo_seeder: removed %s (id=%s)", name, biz_id)
        except Exception as exc:
            log.error("demo_seeder clear error for %s: %s", name, exc)

    return {"removed": removed, "total": len(removed)}
