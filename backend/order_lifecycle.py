# order_lifecycle.py
"""
Order lifecycle — pure Supabase, no SQLAlchemy.

Status flow: pending → confirmed → paid → delivered
"""

import json
import logging
from datetime import datetime, timezone
from db import supabase
from inventory import reduce_stock_by_name

log = logging.getLogger(__name__)

VALID_STATUSES = ["pending", "confirmed", "paid", "delivered"]

VALID_TRANSITIONS = {
    "pending":   ["confirmed", "paid", "delivered"],
    "confirmed": ["paid", "delivered"],
    "paid":      ["delivered"],
    "delivered": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_order_supabase(
    business_id: int,
    customer_phone: str,
    cart: list,   # [{name, qty, price}, ...]
) -> dict:
    """
    Create an order, reduce inventory atomically.
    Raises ValueError on stock issues or missing products.
    Returns the created order dict including id.
    """
    if not cart:
        raise ValueError("Cart is empty — nothing to order.")

    total = 0.0
    items_detail = []

    for item in cart:
        name  = item["name"]
        qty   = int(item["qty"])
        price = float(item["price"])

        # This raises ValueError if stock is insufficient
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
        "business_id":       business_id,
        "customer_phone":    customer_phone,
        "product_name":      ", ".join(i["name"] for i in items_detail),
        "quantity":          sum(i["qty"] for i in items_detail),
        "items":             json.dumps(items_detail),
        "total_price":       round(total, 2),
        "status":            "pending",
        "payment_status":    "pending",
        "payment_reference": None,
        "created_at":        _now(),
    }

    res = supabase.table("orders").insert(row).execute()
    order = res.data[0] if res.data else row

    log.info(
        "✅ Order created  id=%s  business=%s  phone=%s  total=%.2f",
        order.get("id", "?"), business_id, customer_phone, total,
    )
    return order


def update_order_status_supabase(order_id: int, status: str) -> dict:
    """
    Update order status. Validates transitions.
    Raises ValueError for invalid status or if order not found.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {VALID_STATUSES}")

    existing = get_order(order_id)
    if not existing:
        raise ValueError(f"Order id={order_id} not found")

    current = existing.get("status", "pending")
    allowed = VALID_TRANSITIONS.get(current, [])
    if status not in allowed and status != current:
        raise ValueError(
            f"Cannot transition order from '{current}' to '{status}'. "
            f"Allowed next states: {allowed}"
        )

    update_payload = {"status": status}
    if status == "paid":
        update_payload["payment_status"] = "paid"

    res = (
        supabase.table("orders")
        .update(update_payload)
        .eq("id", order_id)
        .execute()
    )
    if not res.data:
        raise ValueError(f"Order id={order_id} update failed")

    log.info("📦 Order %s → %s", order_id, status)
    return res.data[0]


def confirm_payment_supabase(order_id: int, reference: str) -> dict:
    """
    Mark an order as paid and set payment reference.
    Returns the updated order.
    """
    existing = get_order(order_id)
    if not existing:
        raise ValueError(f"Order id={order_id} not found")

    res = (
        supabase.table("orders")
        .update({
            "status":            "paid",
            "payment_status":    "paid",
            "payment_reference": reference,
        })
        .eq("id", order_id)
        .execute()
    )
    if not res.data:
        raise ValueError(f"Order id={order_id} payment confirmation failed")

    log.info("💳 Order %s marked PAID  ref=%s", order_id, reference)
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
