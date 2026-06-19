"""
services/_ai_memory.py — Customer memory, cart I/O, and order history tracking.

Imported by ai.py. Do not import ai.py from here (circular import).
"""

import logging
from datetime import datetime, timezone

import crud

log = logging.getLogger(__name__)


# ── Memory ────────────────────────────────────────────────────────────────────

def _get_memory(phone: str, business_id: int) -> dict:
    """
    Return customer memory. Falls back to a safe default on any error.

    Fix: customers who message the bot but never complete an order were
    invisible in the CRM/Customers dashboard, because no user_memory row
    existed until _update_order_history() ran after a purchase. Now, on
    first contact (no existing row), a row is created immediately with
    order_count=0 so every customer who has ever messaged appears in the
    CRM segments, Customers page, and Conversations list consistently.
    """
    try:
        existing = crud.get_user_memory(phone, business_id)
        is_new   = not existing or not existing.get("updated_at")

        mem = existing or {}
        mem.setdefault("frequent_items", {})
        mem.setdefault("last_orders",    [])
        mem.setdefault("customer_name",  "")
        mem.setdefault("total_spent",    0.0)
        mem.setdefault("order_count",    0)
        mem.setdefault("last_seen",      "")
        mem.setdefault("last_rating",    "")

        if is_new:
            # First contact — create the row now so this customer is visible
            # in the CRM immediately, not only after their first order.
            mem["last_seen"] = datetime.now(timezone.utc).isoformat()
            try:
                crud.save_user_memory(phone, business_id, mem)
            except Exception as save_exc:
                log.warning("_get_memory: could not create row on first contact: %s", save_exc)

        return mem
    except Exception as exc:
        log.warning("_get_memory failed: %s", exc)
        return {"frequent_items": {}, "last_orders": [], "customer_name": "",
                "total_spent": 0.0, "order_count": 0}


def _update_order_history(phone: str, business_id: int, cart: list) -> None:
    """
    Update customer memory after a successful order.
    Tracks: frequent items, order history, total spent, order count, last seen.
    """
    try:
        mem = _get_memory(phone, business_id)

        for item in cart:
            name = item["name"]
            mem["frequent_items"][name] = mem["frequent_items"].get(name, 0) + item["qty"]

        mem["last_orders"].append([i["name"] for i in cart])
        mem["last_orders"] = mem["last_orders"][-10:]

        order_total = sum(i["qty"] * float(i["price"]) for i in cart)
        mem["total_spent"] = round(float(mem.get("total_spent", 0) or 0) + order_total, 2)
        mem["order_count"] = int(mem.get("order_count", 0) or 0) + 1
        mem["last_seen"]   = datetime.now(timezone.utc).isoformat()

        crud.save_user_memory(phone, business_id, mem)
        log.debug("_update_order_history  phone=%s  spent=%.2f  orders=%d",
                  phone, mem["total_spent"], mem["order_count"])
    except Exception as exc:
        log.warning("_update_order_history failed: %s", exc)


# ── Cart I/O ──────────────────────────────────────────────────────────────────

def _load_cart(phone: str, business_id: int) -> list:
    try:
        raw = crud.get_cart(phone, business_id)
    except Exception as exc:
        log.error("_load_cart error: %s", exc)
        return []
    if raw is None:
        return []
    if isinstance(raw, list):
        return [i for i in raw if isinstance(i, dict) and "name" in i and "price" in i]
    if isinstance(raw, dict):
        items = raw.get("items") or []
        if isinstance(items, dict):
            items = list(items.values())
        return [i for i in items if isinstance(i, dict) and "name" in i and "price" in i]
    return []


def _save_cart(phone: str, business_id: int, cart: list) -> None:
    try:
        crud.save_cart(phone, business_id, cart)
    except Exception as exc:
        log.error("_save_cart error: %s", exc)
