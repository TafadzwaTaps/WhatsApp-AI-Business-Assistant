"""
crud.py — All database operations via Supabase (no SQLAlchemy).

Return convention:
  • Single-row lookups return a dict or None.
  • Multi-row lookups return a list of dicts.
  • Create / update operations return the created/updated dict.
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
    return datetime.now(timezone.utc).isoformat()


def _one(table: str, res) -> Optional[dict]:
    data = res.data
    return data[0] if data else None


# ── Business ──────────────────────────────────────────────────────────────────

def create_business(data) -> dict:
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
    if not business or not business.get("whatsapp_token"):
        return ""
    return decrypt_token(business["whatsapp_token"])


def update_business(business_id: int, data) -> Optional[dict]:
    update_dict = data.dict(exclude_none=True) if hasattr(data, "dict") else dict(data)

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
        "business_id":         business_id,
        "name":                product.name,
        "price":               product.price,
        "image_url":           getattr(product, "image_url", None),
        "stock":               getattr(product, "stock", 0) or 0,
        "low_stock_threshold": getattr(product, "low_stock_threshold", 5) or 5,
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


def get_product_by_id(product_id: int, business_id: int) -> Optional[dict]:
    res = (
        supabase.table("products")
        .select("*")
        .eq("id", product_id)
        .eq("business_id", business_id)
        .limit(1)
        .execute()
    )
    return _one("products", res)


def get_product_by_name(business_id: int, name: str) -> Optional[dict]:
    """
    Case-insensitive product lookup by name within a business.
    Used by ai.py to get a fresh stock-accurate product row before adding to cart.
    Returns None if not found.
    """
    res = (
        supabase.table("products")
        .select("*")
        .eq("business_id", business_id)
        .ilike("name", name)
        .limit(1)
        .execute()
    )
    return _one("products", res)


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


def update_product(product_id: int, business_id: int, data: dict) -> Optional[dict]:
    res = (
        supabase.table("products")
        .update(data)
        .eq("id", product_id)
        .eq("business_id", business_id)
        .execute()
    )
    return _one("products", res)


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
    """
    Simple single-item order creation (legacy webhook path).
    For full cart checkout use order_lifecycle.create_order_supabase().
    """
    total = order.quantity * get_product_price(business_id, order.product_name)
    row = {
        "business_id":    business_id,
        "customer_phone": order.customer_phone,
        "product_name":   order.product_name,
        "quantity":       order.quantity,
        "total_price":    round(float(total), 2),
        "status":         "pending",
        "payment_status": "pending",
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


def get_order_by_id(order_id: int, business_id: int) -> Optional[dict]:
    res = (
        supabase.table("orders")
        .select("*")
        .eq("id", order_id)
        .eq("business_id", business_id)
        .limit(1)
        .execute()
    )
    return _one("orders", res)


def update_order_status(order_id: int, business_id: int, status: str) -> Optional[dict]:
    res = (
        supabase.table("orders")
        .update({"status": status})
        .eq("id", order_id)
        .eq("business_id", business_id)
        .execute()
    )
    return _one("orders", res)


# ── Dashboard analytics ───────────────────────────────────────────────────────

def get_dashboard_stats(business_id: int) -> dict:
    """
    Returns analytics data for the business dashboard:
    total_revenue, total_orders, total_customers,
    orders_per_day (last 30 days), top_products, recent_orders,
    revenue_per_day, orders_by_status.
    """
    from collections import defaultdict, Counter

    orders = get_orders(business_id)
    customers_res = (
        supabase.table("customers")
        .select("id,created_at")
        .eq("business_id", business_id)
        .execute()
    )
    customers = customers_res.data or []

    total_revenue = round(sum(float(o.get("total_price") or 0) for o in orders), 2)
    total_orders  = len(orders)
    total_customers = len(customers)

    # Orders per day (last 30 days)
    orders_per_day: dict[str, int] = defaultdict(int)
    revenue_per_day: dict[str, float] = defaultdict(float)
    product_counter: Counter = Counter()
    status_counter: Counter = Counter()

    for o in orders:
        raw_date = o.get("created_at", "")
        day = raw_date[:10] if raw_date else "unknown"
        orders_per_day[day] += 1
        revenue_per_day[day] += float(o.get("total_price") or 0)
        status_counter[o.get("status", "pending")] += 1

        # Count products from product_name field
        names = o.get("product_name", "")
        if names:
            for name in [n.strip() for n in names.split(",")]:
                if name:
                    product_counter[name] += 1

    # Sort days
    sorted_days = sorted(orders_per_day.keys())[-30:]
    orders_by_day   = {d: orders_per_day[d] for d in sorted_days}
    revenue_by_day  = {d: round(revenue_per_day[d], 2) for d in sorted_days}

    top_products = product_counter.most_common(8)

    # New customers per day
    customers_per_day: dict[str, int] = defaultdict(int)
    for c in customers:
        raw = c.get("created_at", "")
        day = raw[:10] if raw else "unknown"
        customers_per_day[day] += 1

    sorted_cust_days = sorted(customers_per_day.keys())[-30:]
    customers_by_day = {d: customers_per_day[d] for d in sorted_cust_days}

    # Recent orders (last 10)
    recent_orders = [
        {
            "id":      o.get("id"),
            "phone":   o.get("customer_phone", ""),
            "total":   float(o.get("total_price") or 0),
            "status":  o.get("status", "pending"),
            "payment": o.get("payment_status", "pending"),
            "date":    (o.get("created_at") or "")[:16],
        }
        for o in orders[:10]
    ]

    return {
        "total_revenue":     total_revenue,
        "total_orders":      total_orders,
        "total_customers":   total_customers,
        "orders_per_day":    orders_by_day,
        "revenue_per_day":   revenue_by_day,
        "top_products":      top_products,
        "orders_by_status":  dict(status_counter),
        "customers_per_day": customers_by_day,
        "recent_orders":     recent_orders,
    }


# ── ChatMessage (legacy) ──────────────────────────────────────────────────────

def log_message(business_id: int, phone: str, direction: str, message: str) -> None:
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


# ── Message (CRM) ─────────────────────────────────────────────────────────────

def message_exists(wa_message_id: str) -> bool:
    if not wa_message_id:
        return False
    res = (
        supabase.table("messages")
        .select("id")
        .eq("wa_message_id", wa_message_id)
        .limit(1)
        .execute()
    )
    exists = bool(res.data)
    if exists:
        log.warning("⚠️  Duplicate WA message skipped  wa_id=%s", wa_message_id)
    return exists


def create_message(
    customer_id: int,
    business_id: int,
    text: str,
    direction: str,
    wa_message_id: str | None = None,
) -> dict:
    is_read = direction == "outgoing"
    row: dict = {
        "customer_id": customer_id,
        "business_id": business_id,
        "text":        text,
        "direction":   direction,
        "is_read":     is_read,
        "status":      "sent",
        "created_at":  _now(),
    }
    if wa_message_id:
        row["wa_message_id"] = wa_message_id

    res = supabase.table("messages").insert(row).execute()
    msg = _one("messages", res)

    if direction == "incoming":
        try:
            supabase.rpc("increment_unread", {"p_customer_id": customer_id}).execute()
        except Exception as exc:
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
    supabase.table("messages").update({
        "is_read": True,
        "status":  "read",
    }).eq("customer_id", customer_id)\
      .eq("business_id", business_id)\
      .eq("direction", "incoming")\
      .eq("is_read", False)\
      .execute()

    supabase.table("customers").update({"unread_count": 0}).eq("id", customer_id).execute()


def get_chat_conversations(business_id: int, filter_unread: bool = False) -> list[dict]:
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

    msgs_res = (
        supabase.table("messages")
        .select("*")
        .eq("business_id", business_id)
        .in_("customer_id", customer_ids)
        .order("id", desc=True)
        .limit(len(customer_ids) * 5)
        .execute()
    )
    msgs = msgs_res.data or []

    latest: dict[int, dict] = {}
    for m in msgs:
        cid = m["customer_id"]
        if cid not in latest:
            latest[cid] = m

    result = []
    for c in customers:
        last = latest.get(c["id"], {})
        result.append({
            "customer_id":     c["id"],
            "phone":           c["phone"],
            "customer_since":  c.get("created_at"),
            "last_seen":       c.get("last_seen"),
            "unread_count":    c.get("unread_count") or 0,
            "last_message":    last.get("text", ""),
            "last_direction":  last.get("direction", ""),
            "last_message_at": last.get("created_at"),
            "last_status":     last.get("status", "sent"),
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


# ── CARTS ─────────────────────────────────────────────────────────────────────

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
        # Filter: only valid cart items
        return [i for i in items if isinstance(i, dict) and "name" in i and "price" in i]
    except Exception as exc:
        log.error("get_cart error  phone=%s  biz=%s  exc=%s", phone, business_id, exc)
        return []


def save_cart(phone: str, business_id: int, items: list) -> Optional[dict]:
    """
    Upsert cart for phone+business. items must be a list of {name, qty, price}.
    Returns the saved row or None on error.
    """
    # Sanitise: ensure all items are dicts with required keys
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
    """Delete the cart row for this phone+business."""
    try:
        supabase.table("carts")\
            .delete()\
            .eq("phone", phone)\
            .eq("business_id", business_id)\
            .execute()
        log.debug("clear_cart OK  phone=%s", phone)
    except Exception as exc:
        log.error("clear_cart error  phone=%s  exc=%s", phone, exc)


# ── AI MEMORY ─────────────────────────────────────────────────────────────────

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
    res = (
        supabase.table("user_memory")
        .upsert(
            {
                "phone":          phone,
                "business_id":    business_id,
                "frequent_items": memory.get("frequent_items", {}),
                "last_orders":    memory.get("last_orders", []),
                "updated_at":     _now(),
            },
            on_conflict="phone,business_id",
        )
        .execute()
    )
    return _one("user_memory", res)


# ── Message delete / clear ────────────────────────────────────────────────────

def delete_message(message_id: int, business_id: int) -> bool:
    """
    Soft-delete a single message by id.
    Only deletes messages belonging to the given business.
    Returns True if a row was deleted.
    """
    try:
        res = (
            supabase.table("messages")
            .delete()
            .eq("id", message_id)
            .eq("business_id", business_id)
            .execute()
        )
        deleted = bool(res.data)
        if deleted:
            log.info("delete_message OK  id=%s  business=%s", message_id, business_id)
        return deleted
    except Exception as exc:
        log.error("delete_message error  id=%s  exc=%s", message_id, exc)
        return False


def clear_customer_messages(customer_id: int, business_id: int) -> int:
    """
    Delete all messages for a customer within a business.
    Also resets the customer's unread_count to 0.
    Returns the number of rows deleted.
    """
    try:
        res = (
            supabase.table("messages")
            .delete()
            .eq("customer_id", customer_id)
            .eq("business_id", business_id)
            .execute()
        )
        count = len(res.data) if res.data else 0

        # Also clear legacy chat_messages table for same phone
        try:
            cust = get_customer_by_id(customer_id, business_id)
            if cust:
                supabase.table("chat_messages") \
                    .delete() \
                    .eq("business_id", business_id) \
                    .eq("phone", cust["phone"]) \
                    .execute()
        except Exception:
            pass

        # Reset unread badge
        supabase.table("customers") \
            .update({"unread_count": 0}) \
            .eq("id", customer_id) \
            .execute()

        log.info("clear_customer_messages  customer=%s  deleted=%d", customer_id, count)
        return count
    except Exception as exc:
        log.error("clear_customer_messages error  customer=%s  exc=%s", customer_id, exc)
        return 0


# ── Payment update ────────────────────────────────────────────────────────────

def update_order_payment(order_id: int, business_id: int, data: dict) -> Optional[dict]:
    """
    Update payment-related fields on an order.
    Only updates columns that exist in the schema (safe for old deployments).
    data keys: payment_method, payment_status, payment_reference, payment_url
    """
    from order_lifecycle import _has_col

    safe: dict = {}
    for col in ("payment_method", "payment_status", "payment_reference", "payment_url"):
        if col in data and _has_col(col):
            safe[col] = data[col]

    if not safe:
        log.debug("update_order_payment: no valid columns to update")
        return None

    try:
        res = (
            supabase.table("orders")
            .update(safe)
            .eq("id", order_id)
            .eq("business_id", business_id)
            .execute()
        )
        log.info("update_order_payment  order=%s  fields=%s", order_id, list(safe.keys()))
        return _one("orders", res)
    except Exception as exc:
        log.error("update_order_payment error  order=%s  exc=%s", order_id, exc)
        return None
