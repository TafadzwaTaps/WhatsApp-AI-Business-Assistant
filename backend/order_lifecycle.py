# order_lifecycle.py
"""
Order lifecycle — pure Supabase, no SQLAlchemy.
"""

import json
import logging
from datetime import datetime, timezone
from db import supabase
from inventory import reduce_stock_by_name

log = logging.getLogger(__name__)

VALID_STATUSES = ["pending", "confirmed", "paid", "delivered"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_order_supabase(
    business_id: int,
    customer_phone: str,
    cart: list,   # [{name, qty, price}, ...]
) -> dict:
    """
    Create an order and reduce inventory.
    Raises ValueError on stock issues.
    """
    total = 0.0
    items_detail = []

    for item in cart:
        name  = item["name"]
        qty   = int(item["qty"])
        price = float(item["price"])

        reduce_stock_by_name(business_id, name, qty)

        subtotal = price * qty
        total += subtotal
        items_detail.append({
            "name":     name,
            "qty":      qty,
            "price":    price,
            "subtotal": round(subtotal, 2),
        })

    row = {
        "business_id":    business_id,
        "customer_phone": customer_phone,
        "product_name":   ", ".join(i["name"] for i in items_detail),
        "quantity":       sum(i["qty"] for i in items_detail),
        "items":          json.dumps(items_detail),
        "total_price":    round(total, 2),
        "status":         "pending",
        "created_at":     _now(),
    }

    res = supabase.table("orders").insert(row).execute()
    order = res.data[0] if res.data else row

    log.info(
        "✅ Order created  business=%s  phone=%s  total=%.2f",
        business_id, customer_phone, total,
    )
    return order


def update_order_status_supabase(order_id: int, status: str) -> dict:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {VALID_STATUSES}")

    res = (
        supabase.table("orders")
        .update({"status": status})
        .eq("id", order_id)
        .execute()
    )
    if not res.data:
        raise ValueError(f"Order id={order_id} not found")

    log.info("📦 Order %s → %s", order_id, status)
    return res.data[0]


def get_order(order_id: int) -> dict | None:
    res = (
        supabase.table("orders")
        .select("*")
        .eq("id", order_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None
