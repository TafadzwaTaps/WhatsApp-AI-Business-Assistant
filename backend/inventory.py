# inventory.py
"""
Inventory management — pure Supabase, no SQLAlchemy.
"""

import logging
from db import supabase

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


def reduce_stock(product_id: int, quantity: int) -> dict:
    product = get_product(product_id)

    if not product:
        raise ValueError(f"Product id={product_id} not found")

    current = product.get("stock") or 0

    if current < quantity:
        raise ValueError(
            f"Insufficient stock for '{product['name']}': "
            f"requested {quantity}, available {current}"
        )

    new_stock = current - quantity
    supabase.table("products").update({"stock": new_stock}).eq("id", product_id).execute()

    threshold = product.get("low_stock_threshold") or 5
    if new_stock <= threshold:
        log.warning(
            "⚠️  LOW STOCK — product='%s' id=%s remaining=%d threshold=%d",
            product["name"], product_id, new_stock, threshold,
        )

    product["stock"] = new_stock
    return product


def reduce_stock_by_name(business_id: int, name: str, quantity: int) -> dict:
    product = get_product_by_name(business_id, name)
    if not product:
        raise ValueError(f"Product '{name}' not found")
    return reduce_stock(product["id"], quantity)


def restock_product(product_id: int, quantity: int) -> dict:
    product = get_product(product_id)
    if not product:
        raise ValueError(f"Product id={product_id} not found")
    new_stock = (product.get("stock") or 0) + quantity
    supabase.table("products").update({"stock": new_stock}).eq("id", product_id).execute()
    product["stock"] = new_stock
    return product
