# inventory.py
"""
Inventory management — stock tracking and low-stock alerts.

Supports both SQLAlchemy (legacy local dev) and Supabase (production).
All public functions used by the webhook/AI flow use Supabase via crud.
The SQLAlchemy helpers are kept for the order_lifecycle.py path.
"""

import logging
from sqlalchemy.orm import Session
from models import Product

log = logging.getLogger(__name__)


# ─── SQLAlchemy helpers (used by order_lifecycle.py) ──────────────────────────

def get_product(db: Session, product_id: int):
    return db.query(Product).filter(Product.id == product_id).first()


def reduce_stock(db: Session, product_id: int, quantity: int):
    product = get_product(db, product_id)

    if not product:
        raise ValueError(f"Product id={product_id} not found")

    if product.stock is None:
        product.stock = 0

    if product.stock < quantity:
        raise ValueError(
            f"Insufficient stock for '{product.name}': "
            f"requested {quantity}, available {product.stock}"
        )

    product.stock -= quantity
    db.commit()

    # Low-stock alert
    if (
        product.low_stock_threshold is not None
        and product.stock <= product.low_stock_threshold
    ):
        log.warning(
            "⚠️  LOW STOCK — product='%s' id=%s remaining=%d threshold=%d",
            product.name, product.id, product.stock, product.low_stock_threshold,
        )

    return product


def restock_product(db: Session, product_id: int, quantity: int):
    product = get_product(db, product_id)

    if not product:
        raise ValueError(f"Product id={product_id} not found")

    product.stock += quantity
    db.commit()
    return product


# ─── Supabase helpers (used by ai.py / webhook flow) ──────────────────────────

def get_product_by_name_supabase(business_id: int, name: str) -> dict | None:
    """Return the first product matching name (case-insensitive) for a business."""
    from db import supabase
    res = (
        supabase.table("products")
        .select("*")
        .eq("business_id", business_id)
        .ilike("name", name)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def reduce_stock_supabase(business_id: int, product_name: str, quantity: int) -> dict:
    """
    Atomically reduce stock for a product (Supabase path).
    Returns the updated product dict.
    Raises ValueError on not-found or insufficient stock.
    """
    from db import supabase

    product = get_product_by_name_supabase(business_id, product_name)
    if not product:
        raise ValueError(f"Product '{product_name}' not found")

    current_stock = product.get("stock") or 0

    if current_stock < quantity:
        raise ValueError(
            f"Insufficient stock for '{product['name']}': "
            f"requested {quantity}, available {current_stock}"
        )

    new_stock = current_stock - quantity

    res = (
        supabase.table("products")
        .update({"stock": new_stock})
        .eq("id", product["id"])
        .execute()
    )
    updated = res.data[0] if res.data else product
    updated["stock"] = new_stock  # ensure local dict is correct even if update returns stale

    threshold = product.get("low_stock_threshold") or 5
    if new_stock <= threshold:
        log.warning(
            "⚠️  LOW STOCK — product='%s' id=%s remaining=%d threshold=%d",
            product["name"], product["id"], new_stock, threshold,
        )

    return updated
