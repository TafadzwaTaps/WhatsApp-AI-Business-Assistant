# inventory.py
"""
Inventory management — pure Supabase, no SQLAlchemy.
"""

import logging
from core.db import supabase

log = logging.getLogger(__name__)


def get_product(product_id: int) -> dict | None:
    res = (
        supabase.table("products")
        .select("*")
        .eq("id", product_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def get_product_by_name(business_id: int, name: str) -> dict | None:
    res = (
        supabase.table("products")
        .select("*")
        .eq("business_id", business_id)
        .ilike("name", name)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def reduce_stock(product_id: int, quantity: int, is_service_business: bool = False) -> dict:
    """
    Atomically reduce stock for a product.
    Raises ValueError if product not found or insufficient stock.
    Logs a warning when stock falls at or below low_stock_threshold.

    is_service_business: when True, stock is never checked or reduced —
    services (haircuts, consultations, bookings) don't have inventory in
    the same sense a physical product does. This was previously the ONLY
    stock-enforcement point in the whole booking flow that had no concept
    of service businesses at all: the AI's own checks (menu display,
    add-to-cart) had already been fixed to skip stock for services, but
    order CREATION independently re-validated stock here and blocked the
    booking anyway, after the customer had already gone through checkout.
    """
    if quantity is None or int(quantity) <= 0:
        raise ValueError("Quantity must be a positive number")
    product = get_product(product_id)

    if not product:
        raise ValueError(f"Product id={product_id} not found")

    if is_service_business:
        log.info("reduce_stock: service business — skipping stock check for '%s'", product.get("name"))
        return product

    current = product.get("stock")
    if current is None:
        # stock column not set — treat as unlimited, skip reduction
        log.info("reduce_stock: product '%s' has no stock field — skipping reduction", product.get("name"))
        return product

    if current < quantity:
        raise ValueError(
            f"Insufficient stock for '{product['name']}': "
            f"requested {quantity}, available {current}"
        )

    # Compare-and-set: only write if stock is still what we read. A concurrent order that
    # changed it makes the update match no row; re-read and re-check instead of
    # overwriting it (the old read-then-write let two orders both "take" the same unit).
    new_stock = None
    for _attempt in range(5):
        new_stock = current - quantity
        q = supabase.table("products").update({"stock": new_stock}).eq("id", product_id).eq("stock", current)
        if product.get("business_id") is not None:
            q = q.eq("business_id", product["business_id"])
        if q.execute().data:
            break
        fresh = get_product(product_id)
        current = (fresh or {}).get("stock")
        if fresh is None or current is None:
            raise ValueError(f"Product id={product_id} changed while ordering - please try again")
        if current < quantity:
            raise ValueError(
                f"Insufficient stock for '{product['name']}': "
                f"requested {quantity}, available {current}"
            )
        product = fresh
        new_stock = None
    if new_stock is None:
        raise ValueError(f"Stock for '{product.get('name')}' is changing too quickly - please try again")

    threshold = product.get("low_stock_threshold") or 5
    if new_stock <= threshold:
        log.warning(
            "⚠️  LOW STOCK — product='%s' id=%s remaining=%d threshold=%d",
            product["name"], product_id, new_stock, threshold,
        )

    product["stock"] = new_stock
    return product


def reduce_stock_by_name(business_id: int, name: str, quantity: int, is_service_business: bool = False) -> dict:
    """Reduce stock by product name within a business. Raises ValueError on failure."""
    product = get_product_by_name(business_id, name)
    if not product:
        raise ValueError(f"Product '{name}' not found in business id={business_id}")
    return reduce_stock(product["id"], quantity, is_service_business)


def restore_stock(product_id: int, quantity: int) -> bool:
    """Give back `quantity` units taken by reduce_stock when an order could not be completed.
    Compare-and-set like reduce_stock; products with no stock tracking (NULL) are left alone.
    Returns True when restored (or nothing to restore). Never raises."""
    try:
        for _ in range(5):
            product = get_product(product_id)
            if not product or product.get("stock") is None:
                return True
            cur = product["stock"]
            q = supabase.table("products").update({"stock": cur + quantity}).eq("id", product_id).eq("stock", cur)
            if product.get("business_id") is not None:
                q = q.eq("business_id", product["business_id"])
            if q.execute().data:
                log.info("restore_stock: product=%s +%d", product_id, quantity)
                return True
    except Exception as exc:
        log.error("restore_stock failed product=%s qty=%s: %s", product_id, quantity, exc)
        return False
    log.error("restore_stock gave up (contention) product=%s qty=%s", product_id, quantity)
    return False


def restock_product(product_id: int, quantity: int) -> dict:
    """Add stock to a product."""
    product = get_product(product_id)
    if not product:
        raise ValueError(f"Product id={product_id} not found")
    new_stock = (product.get("stock") or 0) + quantity
    supabase.table("products").update({"stock": new_stock}).eq("id", product_id).execute()
    product["stock"] = new_stock
    log.info("restock_product: id=%s +%d → %d", product_id, quantity, new_stock)
    return product


def check_stock(business_id: int, name: str, quantity: int) -> tuple[bool, int]:
    """
    Check if enough stock is available without reducing it.
    Returns (ok: bool, available: int).
    """
    product = get_product_by_name(business_id, name)
    if not product:
        return False, 0
    available = product.get("stock")
    if available is None:
        return True, 9999  # unlimited
    return available >= quantity, available
