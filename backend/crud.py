"""
crud.py — All database operations via Supabase (no SQLAlchemy).

Every public function has the same name and return shape as the old
SQLAlchemy version so main.py needs minimal changes.

Return convention:
  • Single-row lookups return a dict or None.
  • Multi-row lookups return a list of dicts.
  • Create / update operations return the created/updated dict.

The `db` parameter is gone — all functions use the module-level
`supabase` client directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from db import supabase
from crypto import encrypt_token, decrypt_token, TokenDecryptionError

log = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    """UTC timestamp in ISO-8601 format for Supabase TIMESTAMPTZ columns."""
    return datetime.now(timezone.utc).isoformat()


def _one(table: str, res) -> Optional[dict]:
    """Return first row from a Supabase response or None."""
    data = res.data
    return data[0] if data else None


# ── Business ──────────────────────────────────────────────────────────────────

def create_business(data) -> dict:
    """
    Insert a new business row.
    `data` must have: name, owner_username, owner_password,
                      whatsapp_phone_id (optional), whatsapp_token (optional)
    """
    raw_token = (data.whatsapp_token or "").strip()
    row = {
        "name":               data.name,
        "owner_username":     data.owner_username,
        "owner_password":     data.owner_password,
        "whatsapp_phone_id":  data.whatsapp_phone_id or None,
        "whatsapp_token":     encrypt_token(raw_token) if raw_token else None,
        "is_active":          True,
        "created_at":         _now(),
    }
    res = supabase.table("businesses").insert(row).execute()
    biz = _one("businesses", res)
    log.info("create_business OK  id=%s  name=%r", biz["id"], biz["name"])
    return biz


def get_business_by_username(username: str) -> Optional[dict]:
    res = (
        supabase.table("businesses")
        .select("*")
        .eq("owner_username", username)
        .limit(1)
        .execute()
    )
    return _one("businesses", res)


def get_business_by_phone_id(phone_id: str) -> Optional[dict]:
    res = (
        supabase.table("businesses")
        .select("*")
        .eq("whatsapp_phone_id", phone_id)
        .limit(1)
        .execute()
    )
    return _one("businesses", res)


def get_all_businesses() -> list[dict]:
    res = supabase.table("businesses").select("*").order("id").execute()
    return res.data or []


def get_business_by_id(business_id: int) -> Optional[dict]:
    res = (
        supabase.table("businesses")
        .select("*")
        .eq("id", business_id)
        .limit(1)
        .execute()
    )
    return _one("businesses", res)


def get_decrypted_token(business: dict) -> str:
    """
    Decrypt and return the WhatsApp access token for *business* (dict).

    Returns "" when no token is configured.
    Raises TokenDecryptionError if decryption fails (key mismatch etc).
    """
    if not business or not business.get("whatsapp_token"):
        return ""
    return decrypt_token(business["whatsapp_token"])


def update_business(business_id: int, data) -> Optional[dict]:
    """
    PATCH a business row. `data` is a Pydantic model with optional fields.
    Handles token encryption, blocks is_active changes from the business itself
    (caller must strip that field before calling if needed).
    """
    update_dict = data.dict(exclude_none=True) if hasattr(data, "dict") else dict(data)

    # Encrypt token before saving — idempotent guard is inside encrypt_token()
    if update_dict.get("whatsapp_token"):
        new_token = update_dict["whatsapp_token"].strip()
        if new_token:
            update_dict["whatsapp_token"] = encrypt_token(new_token)
        else:
            del update_dict["whatsapp_token"]

    if not update_dict:
        return get_business_by_id(business_id)

    res = (
        supabase.table("businesses")
        .update(update_dict)
        .eq("id", business_id)
        .execute()
    )
    return _one("businesses", res)


def delete_business(business_id: int) -> Optional[dict]:
    res = (
        supabase.table("businesses")
        .delete()
        .eq("id", business_id)
        .execute()
    )
    return _one("businesses", res)


# ── Products ──────────────────────────────────────────────────────────────────

def create_product(business_id: int, product) -> dict:
    row = {
        "business_id": business_id,
        "name":        product.name,
        "price":       product.price,
        "image_url":   getattr(product, "image_url", None),
    }
    res = supabase.table("products").insert(row).execute()
    p = _one("products", res)
    log.info("create_product OK  id=%s  business_id=%s", p["id"], business_id)
    return p


def get_products(business_id: int) -> list[dict]:
    res = (
        supabase.table("products")
        .select("*")
        .eq("business_id", business_id)
        .execute()
    )
    return res.data or []


def get_product_price(business_id: int, name: str) -> float:
    res = (
        supabase.table("products")
        .select("price")
        .eq("business_id", business_id)
        .ilike("name", name)
        .limit(1)
        .execute()
    )
    row = _one("products", res)
    return float(row["price"]) if row and row.get("price") is not None else 0.0


def delete_product(product_id: int, business_id: int) -> Optional[dict]:
    res = (
        supabase.table("products")
        .delete()
        .eq("id", product_id)
        .eq("business_id", business_id)
        .execute()
    )
    return _one("products", res)


# ── Orders ────────────────────────────────────────────────────────────────────

def create_order(business_id: int, order) -> dict:
    total = order.quantity * get_product_price(business_id, order.product_name)
    row = {
        "business_id":    business_id,
        "customer_phone": order.customer_phone,
        "product_name":   order.product_name,
        "quantity":       order.quantity,
        "total_price":    total,
        "status":         "pending",
        "created_at":     _now(),
    }
    res = supabase.table("orders").insert(row).execute()
    return _one("orders", res)


def get_orders(business_id: int) -> list[dict]:
    res = (
        supabase.table("orders")
        .select("*")
        .eq("business_id", business_id)
        .order("id", desc=True)
        .execute()
    )
    return res.data or []


# ── ChatMessage (legacy) ──────────────────────────────────────────────────────

def log_message(business_id: int, phone: str, direction: str, message: str) -> None:
    """Insert a legacy chat_messages row. Fire-and-forget — errors are logged."""
    try:
        supabase.table("chat_messages").insert({
            "business_id": business_id,
            "phone":       phone,
            "direction":   direction,
            "message":     message,
            "created_at":  _now(),
        }).execute()
    except Exception as exc:
        log.error("log_message failed: %s", exc)


def get_conversations(business_id: int) -> list[dict]:
    """
    One row per phone number — the most recent message for each.
    Mirrors the SQLAlchemy subquery logic using Supabase's RPC or
    a window-function approach via raw PostgREST view.

    We implement it with two queries to avoid raw SQL dependency:
      1. Fetch all messages for the business ordered by id desc.
      2. Deduplicate by phone in Python (keeps first = latest seen).
    This is correct and fast enough for dashboard use (not webhook hot path).
    """
    res = (
        supabase.table("chat_messages")
        .select("*")
        .eq("business_id", business_id)
        .order("id", desc=True)
        .execute()
    )
    rows = res.data or []
    seen: set[str] = set()
    latest: list[dict] = []
    for row in rows:
        if row["phone"] not in seen:
            seen.add(row["phone"])
            latest.append(row)
    return latest


def get_messages_for_phone(business_id: int, phone: str) -> list[dict]:
    res = (
        supabase.table("chat_messages")
        .select("*")
        .eq("business_id", business_id)
        .eq("phone", phone)
        .order("created_at")
        .execute()
    )
    return res.data or []


def get_all_customer_phones(business_id: int) -> list[str]:
    """All distinct phones that have sent at least one inbound message."""
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


# ── Customer (CRM) ────────────────────────────────────────────────────────────

def get_or_create_customer(phone: str, business_id: int) -> dict:
    """
    Upsert a customer row using Supabase's on_conflict parameter.
    Updates last_seen on every call.
    """
    now = _now()
    res = (
        supabase.table("customers")
        .upsert(
            {
                "phone":        phone,
                "business_id":  business_id,
                "last_seen":    now,
            },
            on_conflict="phone,business_id",   # matches the UNIQUE constraint
        )
        .execute()
    )
    customer = _one("customers", res)

    # Upsert doesn't increment unread_count — we handle that in create_message.
    # But we need the full row (including id) for the caller.
    if not customer:
        # Fallback: fetch the existing row
        customer = (
            supabase.table("customers")
            .select("*")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
            .data[0]
        )
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


# ── Message (CRM) ─────────────────────────────────────────────────────────────

def create_message(
    customer_id: int,
    business_id: int,
    text: str,
    direction: str,
) -> dict:
    """
    Insert a message row and increment unread_count for incoming messages.
    Returns the inserted row dict.
    """
    is_read = direction == "outgoing"
    row = {
        "customer_id": customer_id,
        "business_id": business_id,
        "text":        text,
        "direction":   direction,
        "is_read":     is_read,
        "status":      "sent",
        "created_at":  _now(),
    }
    res = supabase.table("messages").insert(row).execute()
    msg = _one("messages", res)

    # Increment unread_count on the customer row for incoming messages
    if direction == "incoming":
        try:
            # Supabase doesn't support atomic increment via the REST API directly,
            # so we use rpc() with a custom SQL function — OR we fetch + update.
            # We use rpc so it's a single round-trip and race-condition safe.
            supabase.rpc(
                "increment_unread",
                {"p_customer_id": customer_id},
            ).execute()
        except Exception as exc:
            # Non-fatal — the message is already saved
            log.warning("increment_unread rpc failed (customer %s): %s", customer_id, exc)

    return msg


def get_messages_by_customer(
    customer_id: int,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    res = (
        supabase.table("messages")
        .select("*")
        .eq("customer_id", customer_id)
        .order("created_at")
        .range(offset, offset + limit - 1)
        .execute()
    )
    return res.data or []


def mark_messages_read(customer_id: int, business_id: int) -> None:
    """Mark all incoming messages as read and reset unread_count."""
    # Update messages
    supabase.table("messages").update({
        "is_read": True,
        "status":  "read",
    }).eq("customer_id", customer_id)\
      .eq("business_id", business_id)\
      .eq("direction", "incoming")\
      .eq("is_read", False)\
      .execute()

    # Reset unread_count on the customer
    supabase.table("customers").update({
        "unread_count": 0,
    }).eq("id", customer_id).execute()


def get_chat_conversations(business_id: int, filter_unread: bool = False) -> list[dict]:
    """
    Inbox view: one row per customer with last message details + unread count.

    Implemented with two queries:
      1. Fetch all customers for the business.
      2. Fetch the latest message per customer in a single bulk query,
         then join in Python.
    This avoids raw SQL while keeping round-trips to two.
    """
    # 1. Customers
    cust_q = (
        supabase.table("customers")
        .select("*")
        .eq("business_id", business_id)
        .order("last_seen", desc=True)
    )
    if filter_unread:
        cust_q = cust_q.gt("unread_count", 0)
    customers = cust_q.execute().data or []

    if not customers:
        return []

    customer_ids = [c["id"] for c in customers]

    # 2. Latest message per customer — fetch recent messages, dedupe in Python
    msgs_res = (
        supabase.table("messages")
        .select("*")
        .eq("business_id", business_id)
        .in_("customer_id", customer_ids)
        .order("id", desc=True)
        .limit(len(customer_ids) * 5)   # enough to cover each customer at least once
        .execute()
    )
    msgs = msgs_res.data or []

    # Build a map: customer_id → latest message
    latest: dict[int, dict] = {}
    for m in msgs:
        cid = m["customer_id"]
        if cid not in latest:
            latest[cid] = m

    result = []
    for c in customers:
        last = latest.get(c["id"], {})
        result.append({
            "customer_id":    c["id"],
            "phone":          c["phone"],
            "customer_since": c.get("created_at"),
            "last_seen":      c.get("last_seen"),
            "unread_count":   c.get("unread_count") or 0,
            "last_message":   last.get("text", ""),
            "last_direction": last.get("direction", ""),
            "last_message_at": last.get("created_at"),
            "last_status":    last.get("status", "sent"),
        })
    return result


# ── Admin stats ───────────────────────────────────────────────────────────────

def get_admin_stats() -> dict:
    businesses = get_all_businesses()
    orders_res = supabase.table("orders").select("total_price").execute()
    orders = orders_res.data or []
    return {
        "businesses":        len(businesses),
        "active_businesses": sum(1 for b in businesses if b.get("is_active")),
        "total_orders":      len(orders),
        "total_revenue":     round(sum(float(o.get("total_price") or 0) for o in orders), 2),
    }
