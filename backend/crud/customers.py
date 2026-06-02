"""
crud/customers.py — Customer records, cart state, and AI user memory.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.db import supabase
from crud._helpers import _now, _one

log = logging.getLogger(__name__)


# ── Customer ──────────────────────────────────────────────────────────────────

def get_all_customer_phones(business_id: int) -> list[str]:
    res = (
        supabase.table("chat_messages")
        .select("phone")
        .eq("business_id", business_id)
        .eq("direction", "in")
        .execute()
    )
    seen: set[str] = set()
    phones: list[str] = []
    for row in (res.data or []):
        p = row.get("phone")
        if p and p not in seen:
            seen.add(p)
            phones.append(p)
    return phones


def get_or_create_customer(phone: str, business_id: int) -> dict:
    now = _now()

    try:
        res = (
            supabase.table("customers")
            .upsert(
                {"phone": phone, "business_id": business_id, "last_seen": now},
                on_conflict="phone,business_id",
            )
            .execute()
        )
        customer = _one("customers", res)
        if customer:
            return customer
    except Exception as exc:
        log.warning("get_or_create_customer: upsert failed (%s) — trying select", exc)

    try:
        res2 = (
            supabase.table("customers")
            .select("*")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        if res2.data:
            customer = res2.data[0]
            supabase.table("customers").update({"last_seen": now}).eq("id", customer["id"]).execute()
            return customer
    except Exception as exc:
        log.warning("get_or_create_customer: select failed (%s) — trying insert", exc)

    res3 = (
        supabase.table("customers")
        .insert({
            "phone":        phone,
            "business_id":  business_id,
            "last_seen":    now,
            "unread_count": 0,
            "created_at":   now,
        })
        .execute()
    )
    customer = _one("customers", res3)
    log.info("get_or_create_customer: new  id=%s  phone=%s", customer["id"], phone)
    return customer


def get_customers_for_business(business_id: int, search: Optional[str] = None) -> list[dict]:
    q = (
        supabase.table("customers")
        .select("*")
        .eq("business_id", business_id)
        .order("last_seen", desc=True)
    )
    if search:
        q = q.ilike("phone", f"%{search}%")
    return q.execute().data or []


def get_customer_by_id(customer_id: int, business_id: int) -> Optional[dict]:
    res = (
        supabase.table("customers")
        .select("*")
        .eq("id", customer_id)
        .eq("business_id", business_id)
        .limit(1)
        .execute()
    )
    return _one("customers", res)


# ── Carts ─────────────────────────────────────────────────────────────────────

def get_cart(phone: str, business_id: int) -> list:
    """Returns the cart as a list of {name, qty, price} dicts. Always safe."""
    try:
        res = (
            supabase.table("carts")
            .select("*")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        row = _one("carts", res)
        if not row:
            return []
        items = row.get("items") or []
        if isinstance(items, dict):
            items = list(items.values())
        return [i for i in items if isinstance(i, dict) and "name" in i and "price" in i]
    except Exception as exc:
        log.error("get_cart error  phone=%s  biz=%s  exc=%s", phone, business_id, exc)
        return []


def save_cart(phone: str, business_id: int, items: list) -> Optional[dict]:
    """
    Upsert cart for phone+business. items must be a list of {name, qty, price}.
    Returns the saved row or None on error.
    """
    clean = [
        {
            "name":  str(i.get("name", "")).strip(),
            "qty":   max(1, int(i.get("qty", 1))),
            "price": float(i.get("price", 0)),
        }
        for i in items
        if isinstance(i, dict) and i.get("name") and i.get("price") is not None
    ]
    try:
        res = (
            supabase.table("carts")
            .upsert(
                {
                    "phone":       phone,
                    "business_id": business_id,
                    "items":       clean,
                    "updated_at":  _now(),
                },
                on_conflict="phone,business_id",
            )
            .execute()
        )
        log.debug("save_cart OK  phone=%s  items=%d", phone, len(clean))
        return _one("carts", res)
    except Exception as exc:
        log.error("save_cart error  phone=%s  exc=%s", phone, exc)
        return None


def clear_cart(phone: str, business_id: int) -> None:
    """
    Clear cart items WITHOUT deleting the row.
    We UPDATE items to [] and preserve state_data (which holds
    pending_payment / awaiting_payment state) so the "paid" reply
    still works after checkout.

    IMPORTANT: Do NOT use DELETE here — it would wipe state_data
    and break the awaiting_payment → paid confirmation flow.
    """
    try:
        supabase.table("carts").upsert(
            {
                "phone":       phone,
                "business_id": business_id,
                "items":       [],
                "updated_at":  _now(),
            },
            on_conflict="phone,business_id",
        ).execute()
        log.debug("clear_cart OK (items cleared, state preserved)  phone=%s", phone)
    except Exception as exc:
        log.error("clear_cart error  phone=%s  exc=%s", phone, exc)


# ── AI Memory ─────────────────────────────────────────────────────────────────

def get_user_memory(phone: str, business_id: int) -> dict:
    res = (
        supabase.table("user_memory")
        .select("*")
        .eq("phone", phone)
        .eq("business_id", business_id)
        .limit(1)
        .execute()
    )
    row = _one("user_memory", res)
    if not row:
        return {
            "phone":          phone,
            "business_id":    business_id,
            "frequent_items": {},
            "last_orders":    [],
        }
    return row


def save_user_memory(phone: str, business_id: int, memory: dict) -> dict:
    """
    Persist full customer memory. Only sends columns that exist in the schema
    (safe on both old and new deployments via the _has_memory_col pattern).
    """
    row: dict = {
        "phone":          phone,
        "business_id":    business_id,
        "frequent_items": memory.get("frequent_items", {}),
        "last_orders":    memory.get("last_orders", []),
        "updated_at":     _now(),
    }
    _MEMORY_EXTENDED = {
        "customer_name":   memory.get("customer_name", ""),
        "total_spent":     float(memory.get("total_spent", 0) or 0),
        "order_count":     int(memory.get("order_count", 0) or 0),
        "last_seen":       memory.get("last_seen") or _now(),
        "last_rating":     memory.get("last_rating", ""),
        "last_suggestion": memory.get("last_suggestion", ""),
    }
    for col, val in _MEMORY_EXTENDED.items():
        if _has_memory_col(col):
            row[col] = val

    res = (
        supabase.table("user_memory")
        .upsert(row, on_conflict="phone,business_id")
        .execute()
    )
    return _one("user_memory", res)


# Cache for user_memory columns
_memory_columns: set | None = None


def _has_memory_col(col: str) -> bool:
    global _memory_columns
    if _memory_columns is None:
        try:
            res = supabase.table("user_memory").select("*").limit(1).execute()
            if res.data:
                _memory_columns = set(res.data[0].keys())
            else:
                _memory_columns = {"id", "phone", "business_id",
                                   "frequent_items", "last_orders", "updated_at"}
        except Exception:
            _memory_columns = set()
    return col in _memory_columns
