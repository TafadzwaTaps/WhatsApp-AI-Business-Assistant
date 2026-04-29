# order_lifecycle.py
"""
Order lifecycle management.

SQLAlchemy path is used by the legacy /orders REST endpoint.
Supabase path (create_order_supabase) is used by the AI/webhook flow.
"""

import json
import logging
from sqlalchemy.orm import Session
from models import Order
from inventory import reduce_stock

log = logging.getLogger(__name__)

VALID_STATUSES = ["pending", "confirmed", "paid", "delivered"]


# ─── SQLAlchemy path ──────────────────────────────────────────────────────────

def create_order(db: Session, business_id: int, items: list) -> Order:
    """
    Create an order from a list of cart items.
    Each item: {"product_id": int, "name": str, "quantity": int, "price": float}
    Reduces stock for each item before committing.
    Raises ValueError on stock failure — caller must catch and surface.
    """
    total = 0.0

    for item in items:
        reduce_stock(db, item["product_id"], item["quantity"])
        total += float(item["price"]) * int(item["quantity"])

    order = Order(
        business_id=business_id,
        customer_phone=items[0].get("customer_phone", ""),
        product_name=", ".join(i["name"] for i in items),
        quantity=sum(i["quantity"] for i in items),
        items=json.dumps(items),
        total_price=round(total, 2),
        status="pending",
    )

    db.add(order)
    db.commit()
    db.refresh(order)

    log.info(
        "✅ Order created (SQLAlchemy)  id=%s  business=%s  total=%.2f",
        order.id, business_id, total,
    )
    return order


def update_order_status(db: Session, order_id: int, status: str) -> Order:
    order = db.query(Order).filter(Order.id == order_id).first()

    if not order:
        raise ValueError(f"Order id={order_id} not found")

    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Valid values: {VALID_STATUSES}"
        )

    order.status = status
    db.commit()
    log.info("📦 Order %s status → %s", order_id, status)
    return order


def get_order(db: Session, order_id: int) -> Order | None:
    return db.query(Order).filter(Order.id == order_id).first()


# ─── Supabase path (used by AI webhook flow) ─────────────────────────────────

def create_order_supabase(
    business_id: int,
    customer_phone: str,
    cart: list,          # list of {"name": str, "qty": int, "price": float}
) -> dict:
    """
    Create an order row in Supabase and reduce inventory.
    Returns the created order dict.
    Raises ValueError on stock issues — ai.py converts this to a WA message.
    """
    from db import supabase
    from datetime import datetime, timezone
    from inventory import reduce_stock_supabase

    total = 0.0
    items_detail = []

    for item in cart:
        name = item["name"]
        qty = int(item["qty"])
        price = float(item["price"])

        # Reduce stock — raises ValueError if insufficient
        reduce_stock_supabase(business_id, name, qty)

        subtotal = price * qty
        total += subtotal
        items_detail.append({
            "name": name,
            "qty": qty,
            "price": price,
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
        "created_at":     datetime.now(timezone.utc).isoformat(),
    }

    res = supabase.table("orders").insert(row).execute()
    order = res.data[0] if res.data else row

    log.info(
        "✅ Order created (Supabase)  business=%s  phone=%s  total=%.2f",
        business_id, customer_phone, total,
    )
    return order


def update_order_status_supabase(order_id: int, status: str) -> dict:
    from db import supabase

    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Valid values: {VALID_STATUSES}"
        )

    res = (
        supabase.table("orders")
        .update({"status": status})
        .eq("id", order_id)
        .execute()
    )
    if not res.data:
        raise ValueError(f"Order id={order_id} not found")

    log.info("📦 Order %s status → %s (Supabase)", order_id, status)
    return res.data[0]
