"""
crud/products.py — Product CRUD with schema-safe column probing.

Uses a cached column set (_products_columns) to avoid PGRST204 errors
on deployments that have not run the latest migration.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.db import supabase
from crud._helpers import _now, _one

log = logging.getLogger(__name__)

# ── Products column cache ─────────────────────────────────────────────────────

_products_columns: set | None = None


def _get_products_columns() -> set:
    """
    Discover which columns exist in the products table.
    Reads one row and caches the result for the process lifetime.
    Falls back to the guaranteed-safe minimal set on any error.
    """
    global _products_columns
    if _products_columns is not None:
        return _products_columns

    MINIMAL = {"id", "business_id", "name", "price", "created_at"}

    try:
        res = supabase.table("products").select("*").limit(1).execute()
        if res.data:
            _products_columns = set(res.data[0].keys())
            log.info("products columns discovered: %s", sorted(_products_columns))
        else:
            _products_columns = MINIMAL
            log.info("products table empty — using minimal columns: %s", sorted(_products_columns))
    except Exception as exc:
        log.warning("products column probe failed (%s) — using minimal set", exc)
        _products_columns = MINIMAL

    return _products_columns


def _has_product_col(col: str) -> bool:
    return col in _get_products_columns()


def _invalidate_products_column_cache() -> None:
    global _products_columns
    _products_columns = None


def create_product(business_id: int, product) -> dict:
    """
    Insert a new product row into Supabase.

    Validates name and price. Only inserts columns that actually exist in the
    products table — safe on both old schemas (no stock/image_url columns) and
    new schemas (all columns present). Raises ValueError on invalid input.
    """
    name  = (getattr(product, "name", "") or "").strip()
    price = getattr(product, "price", None)

    if not name:
        raise ValueError("Product name is required")
    if price is None or float(price) < 0:
        raise ValueError("Product price must be a non-negative number")

    row: dict = {
        "business_id": int(business_id),
        "name":        name,
        "price":       float(price),
    }

    if _has_product_col("image_url"):
        row["image_url"] = getattr(product, "image_url", None) or None

    if _has_product_col("stock"):
        row["stock"] = int(getattr(product, "stock", 0) or 0)

    if _has_product_col("low_stock_threshold"):
        row["low_stock_threshold"] = int(getattr(product, "low_stock_threshold", 5) or 5)

    log.info("create_product  business_id=%s  name=%r  price=%s  columns=%s",
             business_id, name, price, sorted(row.keys()))

    try:
        res = supabase.table("products").insert(row).execute()
    except Exception as exc:
        _invalidate_products_column_cache()
        log.error("create_product Supabase error  business_id=%s  name=%r  exc=%s",
                  business_id, name, exc)
        raise RuntimeError(f"Database error creating product: {exc}") from exc

    p = _one("products", res)
    if not p:
        log.error("create_product: insert returned no data  row=%s", row)
        raise RuntimeError("Product insert returned no data — check Supabase RLS policies")

    log.info("create_product OK  id=%s  name=%r  business_id=%s", p["id"], name, business_id)
    return p


def get_products(business_id: int) -> list[dict]:
    res = (
        supabase.table("products")
        .select("*")
        .eq("business_id", business_id)
        .execute()
    )
    return res.data or []


def get_product_by_id(product_id: int, business_id: int) -> Optional[dict]:
    res = (
        supabase.table("products")
        .select("*")
        .eq("id", product_id)
        .eq("business_id", business_id)
        .limit(1)
        .execute()
    )
    return _one("products", res)


def get_product_by_name(business_id: int, name: str) -> Optional[dict]:
    """
    Case-insensitive product lookup by name within a business.
    Used by ai.py to get a fresh stock-accurate product row before adding to cart.
    """
    res = (
        supabase.table("products")
        .select("*")
        .eq("business_id", business_id)
        .ilike("name", name)
        .limit(1)
        .execute()
    )
    return _one("products", res)


def get_product_price(business_id: int, name: str) -> float:
    res = (
        supabase.table("products")
        .select("price")
        .eq("business_id", business_id)
        .ilike("name", name)
        .limit(1)
        .execute()
    )
    row = _one("products", res)
    return float(row["price"]) if row and row.get("price") is not None else 0.0


def update_product(product_id: int, business_id: int, data: dict) -> Optional[dict]:
    """
    Update specific fields of a product.
    Silently drops any keys that don't exist as columns in the schema.
    """
    safe = {k: v for k, v in data.items() if _has_product_col(k)}
    if not safe:
        log.warning("update_product: no valid columns in data keys=%s", list(data.keys()))
        return get_product_by_id(product_id, business_id)
    try:
        res = (
            supabase.table("products")
            .update(safe)
            .eq("id", product_id)
            .eq("business_id", business_id)
            .execute()
        )
        return _one("products", res)
    except Exception as exc:
        _invalidate_products_column_cache()
        log.error("update_product error  id=%s  exc=%s", product_id, exc)
        raise RuntimeError(f"Database error updating product: {exc}") from exc


def delete_product(product_id: int, business_id: int) -> Optional[dict]:
    """
    Delete a product by id. Returns the deleted row or None if not found.
    Scoped to business_id to prevent cross-tenant deletion.
    """
    existing = get_product_by_id(product_id, business_id)
    if not existing:
        log.warning("delete_product: id=%s not found for business_id=%s", product_id, business_id)
        return None

    try:
        res = (
            supabase.table("products")
            .delete()
            .eq("id", product_id)
            .eq("business_id", business_id)
            .execute()
        )
        deleted = _one("products", res)
        if deleted:
            log.info("delete_product OK  id=%s  name=%r  business_id=%s",
                     product_id, deleted.get("name"), business_id)
        else:
            log.info("delete_product OK (empty response)  id=%s  business_id=%s",
                     product_id, business_id)
            return existing
        return deleted
    except Exception as exc:
        log.error("delete_product error  id=%s  business_id=%s  exc=%s",
                  product_id, business_id, exc)
        raise RuntimeError(f"Database error deleting product: {exc}") from exc
