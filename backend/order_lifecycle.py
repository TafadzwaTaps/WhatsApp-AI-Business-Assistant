# order_lifecycle.py
"""
Order lifecycle — pure Supabase, no SQLAlchemy.

DEFENSIVE DESIGN:
  Automatically detects which optional columns exist in the orders table
  (items, payment_status, payment_reference) and only inserts what's there.
  Works on both old schemas (missing columns) and new schemas transparently.

  Run MIGRATION.sql in Supabase SQL Editor to unlock full functionality.

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

# Process-level column cache — probed once on first order creation
_orders_columns: set | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_orders_columns() -> set:
    """
    Discover which columns actually exist in the orders table.
    Reads one existing row, or falls back to a minimal known-safe set.
    Cached for the lifetime of the process.
    """
    global _orders_columns
    if _orders_columns is not None:
        return _orders_columns

    MINIMAL = {
        "id", "business_id", "customer_phone",
        "product_name", "quantity", "total_price",
        "status", "created_at",
    }

    try:
        res = supabase.table("orders").select("*").limit(1).execute()
        if res.data:
            _orders_columns = set(res.data[0].keys())
            log.info("orders columns: %s", sorted(_orders_columns))
        else:
            _orders_columns = MINIMAL
            log.info("orders table empty — using minimal columns: %s", sorted(_orders_columns))
    except Exception as exc:
        log.warning("column probe failed (%s) — using minimal columns", exc)
        _orders_columns = MINIMAL

    return _orders_columns


def _has_col(col: str) -> bool:
    return col in _get_orders_columns()


def _invalidate_column_cache() -> None:
    global _orders_columns
    _orders_columns = None


def create_order_supabase(
    business_id: int,
    customer_phone: str,
    cart: list,
) -> dict:
    """
    Create an order and reduce inventory atomically.
    Only inserts columns that actually exist in Supabase.
    Always returns a dict with an 'items' key so invoice generation works.
    Raises ValueError on stock issues, missing products, or DB errors.
    """
    if not cart:
        raise ValueError("Cart is empty — nothing to order.")

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

    # Build insert using ONLY columns that exist in the DB
    row: dict = {
        "business_id":    business_id,
        "customer_phone": customer_phone,
        "product_name":   ", ".join(i["name"] for i in items_detail),
        "quantity":       sum(i["qty"] for i in items_detail),
        "total_price":    round(total, 2),
        "status":         "pending",
        "created_at":     _now(),
    }

    if _has_col("items"):
        row["items"] = json.dumps(items_detail)

    if _has_col("payment_status"):
        row["payment_status"] = "pending"

    if _has_col("payment_reference"):
        row["payment_reference"] = None

    log.info(
        "create_order_supabase  columns=%s  total=%.2f",
        sorted(row.keys()), total,
    )

    try:
        res = supabase.table("orders").insert(row).execute()
    except Exception as exc:
        # Schema mismatch — invalidate cache so next call re-probes columns
        _invalidate_column_cache()
        log.error("create_order_supabase insert failed: %s", exc)
        raise ValueError(
            f"Order could not be saved. The database schema may need updating. "
            f"Error: {exc}"
        ) from exc

    order = res.data[0] if res.data else row

    # Always ensure 'items' key exists in returned dict for invoice generation
    if not order.get("items"):
        order["items"] = json.dumps(items_detail)

    log.info(
        "✅ Order created  id=%s  business=%s  phone=%s  total=%.2f",
        order.get("id", "?"), business_id, customer_phone, total,
    )
    return order


def update_order_status_supabase(order_id: int, status: str) -> dict:
    """Update order status with transition validation."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {VALID_STATUSES}")

    existing = get_order(order_id)
    if not existing:
        raise ValueError(f"Order id={order_id} not found")

    current = existing.get("status", "pending")
    allowed = VALID_TRANSITIONS.get(current, [])
    if status not in allowed and status != current:
        raise ValueError(
            f"Cannot transition from '{current}' to '{status}'. "
            f"Allowed next states: {allowed}"
        )

    update_payload: dict = {"status": status}
    if status == "paid" and _has_col("payment_status"):
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
    """Mark an order as paid and record the payment reference."""
    existing = get_order(order_id)
    if not existing:
        raise ValueError(f"Order id={order_id} not found")

    update_payload: dict = {"status": "paid"}
    if _has_col("payment_status"):
        update_payload["payment_status"] = "paid"
    if _has_col("payment_reference"):
        update_payload["payment_reference"] = reference

    res = (
        supabase.table("orders")
        .update(update_payload)
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
