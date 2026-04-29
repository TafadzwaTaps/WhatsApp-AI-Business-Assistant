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


# inventory.py

def reduce_stock(supabase, product_id: int, quantity: int):
    res = supabase.table("products").select("*").eq("id", product_id).execute()

    if not res.data:
        raise Exception("Product not found")

    product = res.data[0]

    stock = product.get("stock", 0)

    if stock < quantity:
        raise Exception(f"Not enough stock for {product['name']}")

    new_stock = stock - quantity

    supabase.table("products").update({
        "stock": new_stock
    }).eq("id", product_id).execute()

    # Low stock alert
    if product.get("low_stock_threshold") and new_stock <= product["low_stock_threshold"]:
        print(f"[LOW STOCK] {product['name']} → {new_stock} left")

    return True


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
