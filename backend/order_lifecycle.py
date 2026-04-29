# order_lifecycle.py
"""
Order lifecycle — Supabase-first, production-safe, atomic-friendly.

Responsibilities:
- Create orders
- Validate stock via inventory module
- Update order status
- Fetch orders

NO AI logic, NO API logic, NO WhatsApp logic here.
"""

import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional

from db import supabase
from inventory import reduce_stock_by_name

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

VALID_STATUSES = {"pending", "confirmed", "paid", "delivered"}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_cart(cart: List[Dict]) -> None:
    if not cart:
        raise ValueError("Cart is empty")

    for item in cart:
        if "name" not in item or "qty" not in item or "price" not in item:
            raise ValueError(f"Invalid cart item format: {item}")

        if int(item["qty"]) <= 0:
            raise ValueError(f"Invalid quantity for {item['name']}")


# ─────────────────────────────────────────────
# CORE: CREATE ORDER
# ─────────────────────────────────────────────

def create_order(
    business_id: int,
    customer_phone: str,
    cart: List[Dict],   # [{name, qty, price}]
) -> Dict:
    """
    Create an order and reduce inventory safely.

    Raises:
        ValueError: invalid cart or stock issues
    """

    _validate_cart(cart)

    total = 0.0
    items_detail = []

    # ── Step 1: Validate + build order ──
    for item in cart:
        name = item["name"].strip().lower()
        qty = int(item["qty"])
        price = float(item["price"])

        # IMPORTANT: stock is reduced here (single source of truth)
        reduce_stock_by_name(business_id, name, qty)

        subtotal = price * qty
        total += subtotal

        items_detail.append({
            "name": name,
            "qty": qty,
            "price": price,
            "subtotal": round(subtotal, 2),
        })

    # ── Step 2: Build DB row ──
    row = {
        "business_id": business_id,
        "customer_phone": customer_phone,
        "items": json.dumps(items_detail),
        "total_price": round(total, 2),
        "status": "pending",
        "created_at": _now(),
    }

    # ── Step 3: Insert order ──
    try:
        res = supabase.table("orders").insert(row).execute()
        order = res.data[0]
    except Exception as exc:
        log.exception("❌ Order insert failed")
        raise RuntimeError("Failed to create order") from exc

    log.info(
        "✅ Order created | business=%s | phone=%s | total=%.2f",
        business_id, customer_phone, total
    )

    return order


# ─────────────────────────────────────────────
# UPDATE ORDER STATUS
# ─────────────────────────────────────────────

def update_order(order_id: int, status: str) -> Dict:
    """
    Update order status safely.
    """

    status = status.lower().strip()

    if status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Valid: {list(VALID_STATUSES)}"
        )

    try:
        res = (
            supabase.table("orders")
            .update({"status": status})
            .eq("id", order_id)
            .execute()
        )
    except Exception as exc:
        log.exception("❌ Failed to update order")
        raise RuntimeError("Order update failed") from exc

    if not res.data:
        raise ValueError(f"Order id={order_id} not found")

    log.info("📦 Order updated | id=%s → %s", order_id, status)

    return res.data[0]


# ─────────────────────────────────────────────
# GET ORDER
# ─────────────────────────────────────────────

def get_order(order_id: int) -> Optional[Dict]:
    """
    Fetch a single order by ID.
    """

    try:
        res = (
            supabase.table("orders")
            .select("*")
            .eq("id", order_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        log.exception("❌ Failed to fetch order")
        raise RuntimeError("Order fetch failed") from exc

    return res.data[0] if res.data else None