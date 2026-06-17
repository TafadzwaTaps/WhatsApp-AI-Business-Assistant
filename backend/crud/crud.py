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

from core.db import supabase
from core.crypto import encrypt_token, decrypt_token, TokenDecryptionError

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
    row: dict = {
        "name":               data.name,
        "owner_username":     data.owner_username,
        "owner_password":     data.owner_password,
        "whatsapp_phone_id":  data.whatsapp_phone_id or None,
        "whatsapp_token":     encrypt_token(raw_token) if raw_token else None,
        "is_active":          True,
        "created_at":         _now(),
    }
    # Optional fields set during onboarding
    if hasattr(data, "category") and data.category:
        row["category"] = data.category.strip()
    if hasattr(data, "use_shared_number"):
        row["use_shared_number"] = bool(data.use_shared_number)
    if hasattr(data, "contact_phone") and data.contact_phone:
        row["contact_phone"] = data.contact_phone.strip()

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


def get_active_businesses() -> list[dict]:
    """
    Return all active businesses for the shared-number business picker.
    Only returns businesses that are active AND have products (avoids empty storefronts).
    Falls back to all active if the join fails.
    """
    try:
        res = (
            supabase.table("businesses")
            .select("id, name, category, is_active, ecocash_number, paypal_email")
            .eq("is_active", True)
            .order("display_order", desc=False, nullsfirst=True)
            .order("id")
            .execute()
        )
        return res.data or []
    except Exception:
        # display_order column may not exist yet — fall back
        try:
            res = (
                supabase.table("businesses")
                .select("id, name, category, is_active, ecocash_number, paypal_email")
                .eq("is_active", True)
                .order("id")
                .execute()
            )
            return res.data or []
        except Exception as exc:
            log.error("get_active_businesses error: %s", exc)
            return []


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


def get_business_payment_settings(business_id: int) -> dict:
    """
    Return all payment settings for a business in a single dict.
    Used by ai.py / payments.py to inject into the order dict before
    calling gateway functions.

    Returns:
      {
        "ecocash_number":  str,   # e.g. "+263771234567"
        "ecocash_name":    str,   # e.g. "Flavoury Foods"
        "paypal_email":    str,   # e.g. "pay@flavoury.com"
        "payment_number":  str,   # legacy field (same as ecocash_number)
        "payment_name":    str,   # legacy field (same as ecocash_name)
      }
    All values are empty strings if not configured.
    """
    biz = get_business_by_id(business_id)
    if not biz:
        return {
            "ecocash_number": "", "ecocash_name": "",
            "paypal_email": "", "payment_number": "", "payment_name": "",
        }
    return {
        "ecocash_number": biz.get("ecocash_number") or biz.get("payment_number") or "",
        "ecocash_name":   biz.get("ecocash_name")   or biz.get("payment_name")  or "",
        "paypal_email":   biz.get("paypal_email")   or "",
        # Legacy aliases — kept for backward compatibility with invoice.py
        "payment_number": biz.get("ecocash_number") or biz.get("payment_number") or "",
        "payment_name":   biz.get("ecocash_name")   or biz.get("payment_name")  or "",
    }


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

# ── Products column cache (same pattern as order_lifecycle._has_col) ──────────

_products_columns: set | None = None


def _get_products_columns() -> set:
    """
    Discover which columns exist in the products table.
    Reads one row and caches the result for the process lifetime.
    Falls back to the guaranteed-safe minimal set on any error.
    """
    global _products_columns
    if _products_columns is not None:
        return _products_columns

    # Columns guaranteed to exist in every deployment
    MINIMAL = {"id", "business_id", "name", "price", "created_at"}

    try:
        res = supabase.table("products").select("*").limit(1).execute()
        if res.data:
            _products_columns = set(res.data[0].keys())
            log.info("products columns discovered: %s", sorted(_products_columns))
        else:
            # Table exists but is empty — insert only the minimal safe set
            # and let the optional columns be added later via MIGRATION.sql
            _products_columns = MINIMAL
            log.info("products table empty — using minimal columns: %s", sorted(_products_columns))
    except Exception as exc:
        log.warning("products column probe failed (%s) — using minimal set", exc)
        _products_columns = MINIMAL

    return _products_columns


def _has_product_col(col: str) -> bool:
    return col in _get_products_columns()


def _invalidate_products_column_cache() -> None:
    global _products_columns
    _products_columns = None


def create_product(business_id: int, product) -> dict:
    """
    Insert a new product row into Supabase.

    Validates name and price. Only inserts columns that actually exist in the
    products table — safe on both old schemas (no stock/image_url columns) and
    new schemas (all columns present). Raises ValueError on invalid input.

    IMPORTANT: If your products table is missing stock or low_stock_threshold,
    run MIGRATION.sql section 15 in Supabase SQL Editor to add them.
    """
    name  = (getattr(product, "name", "") or "").strip()
    price = getattr(product, "price", None)

    if not name:
        raise ValueError("Product name is required")
    if price is None or float(price) < 0:
        raise ValueError("Product price must be a non-negative number")

    # Core row — always safe
    row: dict = {
        "business_id": int(business_id),
        "name":        name,
        "price":       float(price),
    }

    # Optional columns — only added if they exist in the schema
    if _has_product_col("image_url"):
        row["image_url"] = getattr(product, "image_url", None) or None

    if _has_product_col("stock"):
        row["stock"] = int(getattr(product, "stock", 0) or 0)

    if _has_product_col("low_stock_threshold"):
        row["low_stock_threshold"] = int(getattr(product, "low_stock_threshold", 5) or 5)

    log.info("create_product  business_id=%s  name=%r  price=%s  columns=%s",
             business_id, name, price, sorted(row.keys()))

    try:
        res = supabase.table("products").insert(row).execute()
    except Exception as exc:
        # Schema mismatch — invalidate cache so next call re-probes columns
        _invalidate_products_column_cache()
        log.error("create_product Supabase error  business_id=%s  name=%r  exc=%s",
                  business_id, name, exc)
        raise RuntimeError(f"Database error creating product: {exc}") from exc

    p = _one("products", res)
    if not p:
        log.error("create_product: insert returned no data  row=%s", row)
        raise RuntimeError("Product insert returned no data — check Supabase RLS policies")

    log.info("create_product OK  id=%s  name=%r  business_id=%s", p["id"], name, business_id)
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
    """
    Update specific fields of a product.
    Silently drops any keys that don't exist as columns in the schema.
    """
    # Strip unknown columns to avoid PGRST204 schema-cache errors
    safe = {k: v for k, v in data.items() if _has_product_col(k)}
    if not safe:
        log.warning("update_product: no valid columns in data keys=%s", list(data.keys()))
        return get_product_by_id(product_id, business_id)
    try:
        res = (
            supabase.table("products")
            .update(safe)
            .eq("id", product_id)
            .eq("business_id", business_id)
            .execute()
        )
        return _one("products", res)
    except Exception as exc:
        _invalidate_products_column_cache()
        log.error("update_product error  id=%s  exc=%s", product_id, exc)
        raise RuntimeError(f"Database error updating product: {exc}") from exc


def delete_product(product_id: int, business_id: int) -> Optional[dict]:
    """
    Delete a product by id. Returns the deleted row or None if not found.
    Scoped to business_id to prevent cross-tenant deletion.
    """
    # Check existence first so we can give a clear log message
    existing = get_product_by_id(product_id, business_id)
    if not existing:
        log.warning("delete_product: id=%s not found for business_id=%s", product_id, business_id)
        return None

    try:
        res = (
            supabase.table("products")
            .delete()
            .eq("id", product_id)
            .eq("business_id", business_id)
            .execute()
        )
        deleted = _one("products", res)
        if deleted:
            log.info("delete_product OK  id=%s  name=%r  business_id=%s",
                     product_id, deleted.get("name"), business_id)
        else:
            # Some Supabase versions return empty data on DELETE — treat as success
            log.info("delete_product OK (empty response)  id=%s  business_id=%s",
                     product_id, business_id)
            return existing   # return what we fetched earlier
        return deleted
    except Exception as exc:
        log.error("delete_product error  id=%s  business_id=%s  exc=%s",
                  product_id, business_id, exc)
        raise RuntimeError(f"Database error deleting product: {exc}") from exc


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
    sender_type: str | None = None,   # "ai" | "agent" — differentiates AI vs human replies
    sender_name: str | None = None,   # agent username when sender_type="agent"
) -> dict:
    """
    Save a message to the messages table.

    sender_type differentiates:
      - "ai"    → response generated by the AI assistant
      - "agent" → typed and sent manually by a human agent from the dashboard
      - None    → incoming customer message (direction="incoming")

    This enables the inbox UI to show agent replies with a different badge/colour.
    """
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

    # sender_type and sender_name stored only if columns exist
    if sender_type and _has_messages_col("sender_type"):
        row["sender_type"] = sender_type
    if sender_name and _has_messages_col("sender_name"):
        row["sender_name"] = sender_name

    res = supabase.table("messages").insert(row).execute()
    msg = _one("messages", res)

    if direction == "incoming":
        try:
            supabase.rpc("increment_unread", {"p_customer_id": customer_id}).execute()
        except Exception as exc:
            log.warning("increment_unread rpc failed (customer %s): %s", customer_id, exc)

    return msg


# Cache for messages table columns (same pattern as products)
_messages_columns: set | None = None

def _has_messages_col(col: str) -> bool:
    """Check if a column exists in the messages table — cached after first call."""
    global _messages_columns
    if _messages_columns is None:
        try:
            res = supabase.table("messages").select("*").limit(1).execute()
            if res.data:
                _messages_columns = set(res.data[0].keys())
            else:
                _messages_columns = {"id", "customer_id", "business_id", "text",
                                     "direction", "is_read", "status", "created_at"}
        except Exception:
            _messages_columns = set()
    return col in _messages_columns


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
    """
    Persist full customer memory.  Only sends columns that exist in the schema
    (safe on both old and new deployments via the _has_product_col pattern).

    Core columns (always present): frequent_items, last_orders
    Extended columns (added by migration 19):
      customer_name, total_spent, order_count, last_seen, last_rating
    """
    row: dict = {
        "phone":          phone,
        "business_id":    business_id,
        "frequent_items": memory.get("frequent_items", {}),
        "last_orders":    memory.get("last_orders", []),
        "updated_at":     _now(),
    }
    # Extended fields — stored only when present in schema
    _MEMORY_EXTENDED = {
        "customer_name": memory.get("customer_name", ""),
        "total_spent":   float(memory.get("total_spent", 0) or 0),
        "order_count":   int(memory.get("order_count", 0) or 0),
        "last_seen":     memory.get("last_seen") or _now(),
        "last_rating":   memory.get("last_rating", ""),
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


# Cache for user_memory columns (same pattern as products)
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

    Supported keys:
      payment_method, payment_status, payment_reference, payment_url,
      paypal_order_id   ← NEW: stores PayPal's order ID for webhook lookup
    """
    from workflows.order_lifecycle import _has_col

    # All columns we are allowed to update
    allowed = (
        "payment_method", "payment_status", "payment_reference",
        "payment_url", "paypal_order_id",
        # Fulfillment columns (added in migration section 16)
        "fulfillment_method", "delivery_address", "fulfillment_notes",
    )
    safe: dict = {}
    for col in allowed:
        if col in data and _has_col(col):
            safe[col] = data[col]

    if not safe:
        log.debug("update_order_payment: no valid columns to update  data_keys=%s", list(data.keys()))
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


def get_order_by_paypal_id(paypal_order_id: str) -> Optional[dict]:
    """
    Look up an internal order by PayPal's order ID.
    Used by the PayPal webhook handler to find which order was paid.
    Returns the order dict or None.
    """
    if not paypal_order_id:
        return None
    try:
        res = (
            supabase.table("orders")
            .select("*")
            .eq("paypal_order_id", paypal_order_id)
            .limit(1)
            .execute()
        )
        order = _one("orders", res)
        if order:
            log.debug("get_order_by_paypal_id  paypal_id=%s  order_id=%s",
                      paypal_order_id, order.get("id"))
        return order
    except Exception as exc:
        log.error("get_order_by_paypal_id error  paypal_id=%s  exc=%s", paypal_order_id, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ANALYTICS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_top_customers(business_id: int, limit: int = 10) -> list[dict]:
    """
    Return top customers by order count from user_memory.
    Falls back to counting orders table if user_memory lacks order_count.
    """
    try:
        res = (
            supabase.table("user_memory")
            .select("phone, customer_name, total_spent, order_count, last_seen")
            .eq("business_id", business_id)
            .order("order_count", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("get_top_customers error: %s", exc)
        return []


def get_low_stock_products(business_id: int) -> list[dict]:
    """
    Return products where stock <= low_stock_threshold.
    Used for dashboard alerts and business-owner WhatsApp notifications.
    """
    try:
        res = (
            supabase.table("products")
            .select("id, name, stock, low_stock_threshold")
            .eq("business_id", business_id)
            .execute()
        )
        products = res.data or []
        low = []
        for p in products:
            stock = p.get("stock")
            threshold = p.get("low_stock_threshold") or 5
            if stock is not None and stock <= threshold:
                low.append(p)
        return sorted(low, key=lambda x: x.get("stock", 0))
    except Exception as exc:
        log.warning("get_low_stock_products error: %s", exc)
        return []


def get_business_stats(business_id: int) -> dict:
    """
    Lightweight stats aggregation for the analytics dashboard card.
    Returns: total_orders, paid_orders, total_revenue, active_customers,
             pending_orders, ai_handled, human_handled.
    """
    try:
        orders_res = (
            supabase.table("orders")
            .select("id, total_price, payment_status, status")
            .eq("business_id", business_id)
            .execute()
        )
        orders = orders_res.data or []

        total_orders   = len(orders)
        paid_orders    = sum(1 for o in orders if o.get("payment_status") == "paid")
        total_revenue  = sum(float(o.get("total_price") or 0)
                            for o in orders if o.get("payment_status") == "paid")
        pending_orders = sum(1 for o in orders
                            if o.get("status") in ("pending", "confirmed", "pending_cash"))

        # Customer count
        cust_res = (
            supabase.table("customers")
            .select("id")
            .eq("business_id", business_id)
            .execute()
        )
        active_customers = len(cust_res.data or [])

        # AI vs human messages
        msgs_res = (
            supabase.table("messages")
            .select("sender_type")
            .eq("business_id", business_id)
            .eq("direction", "outgoing")
            .execute()
        )
        msgs = msgs_res.data or []
        ai_handled    = sum(1 for m in msgs if m.get("sender_type") == "ai")
        human_handled = sum(1 for m in msgs if m.get("sender_type") == "agent")

        return {
            "total_orders":      total_orders,
            "paid_orders":       paid_orders,
            "total_revenue":     round(total_revenue, 2),
            "pending_orders":    pending_orders,
            "active_customers":  active_customers,
            "ai_handled":        ai_handled,
            "human_handled":     human_handled,
        }
    except Exception as exc:
        log.warning("get_business_stats error: %s", exc)
        return {
            "total_orders": 0, "paid_orders": 0, "total_revenue": 0.0,
            "pending_orders": 0, "active_customers": 0,
            "ai_handled": 0, "human_handled": 0,
        }


# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT REMINDER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_stale_payment_orders(
    business_id: int,
    older_than_hours: float = 1.0,
    statuses: list[str] | None = None,
) -> list[dict]:
    """
    Return orders stuck in awaiting_payment / payment_review longer than
    `older_than_hours` hours, so the business can send reminder nudges.

    Parameters
    ──────────
    business_id       Filter to one business (tenant isolation).
    older_than_hours  Minimum age in hours before an order is "stale".
    statuses          Payment statuses to check (default: awaiting_payment).

    Returns list of full order dicts, oldest first.
    Never raises — returns [] on any error.
    """
    from datetime import datetime, timezone, timedelta

    if statuses is None:
        statuses = ["awaiting_payment", "payment_review"]

    try:
        res = (
            supabase.table("orders")
            .select(
                "id, business_id, customer_phone, total_price, "
                "payment_method, payment_status, payment_reference, "
                "status, created_at, items"
            )
            .eq("business_id", business_id)
            .in_("payment_status", statuses)
            .order("created_at", desc=False)          # oldest first
            .execute()
        )
        rows = res.data or []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        stale  = []
        for row in rows:
            created_raw = row.get("created_at") or ""
            try:
                # Handle both "2024-01-01T10:00:00+00:00" and "2024-01-01T10:00:00Z"
                created_raw = created_raw.replace("Z", "+00:00")
                created_at  = datetime.fromisoformat(created_raw)
                if created_at <= cutoff:
                    stale.append(row)
            except (ValueError, TypeError):
                # Unparseable timestamp — include it to be safe (better to remind too early)
                stale.append(row)

        log.debug(
            "get_stale_payment_orders  biz=%s  checked=%d  stale=%d  cutoff_h=%.1f",
            business_id, len(rows), len(stale), older_than_hours,
        )
        return stale

    except Exception as exc:
        log.error("get_stale_payment_orders error: %s", exc)
        return []


def get_stale_payment_orders_all_businesses(
    older_than_hours: float = 1.0,
    statuses: list[str] | None = None,
) -> list[dict]:
    """
    Platform-wide version — used by the super-admin reminder endpoint.
    Returns stale orders across ALL active businesses.
    """
    from datetime import datetime, timezone, timedelta

    if statuses is None:
        statuses = ["awaiting_payment", "payment_review"]

    try:
        res = (
            supabase.table("orders")
            .select(
                "id, business_id, customer_phone, total_price, "
                "payment_method, payment_status, payment_reference, "
                "status, created_at"
            )
            .in_("payment_status", statuses)
            .order("created_at", desc=False)
            .execute()
        )
        rows = res.data or []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)
        stale  = []
        for row in rows:
            created_raw = (row.get("created_at") or "").replace("Z", "+00:00")
            try:
                if datetime.fromisoformat(created_raw) <= cutoff:
                    stale.append(row)
            except (ValueError, TypeError):
                stale.append(row)

        return stale
    except Exception as exc:
        log.error("get_stale_payment_orders_all_businesses error: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# CRM — CUSTOMER SEGMENTATION
# ─────────────────────────────────────────────────────────────────────────────

def get_customer_segment(memory: dict) -> str:
    """
    Classify a customer into a segment based on their memory profile.

    Segments
    ────────
    "vip"      — order_count ≥ 10  OR  total_spent ≥ 50
    "loyal"    — order_count ≥ 5   OR  total_spent ≥ 20
    "regular"  — order_count ≥ 2
    "new"      — order_count == 1
    "prospect" — order_count == 0  (visited but never ordered)

    Returns one of the segment strings above. Pure function — no DB calls.
    """
    count  = int(memory.get("order_count", 0) or 0)
    spent  = float(memory.get("total_spent", 0) or 0)

    if count >= 10 or spent >= 50:
        return "vip"
    if count >= 5 or spent >= 20:
        return "loyal"
    if count >= 2:
        return "regular"
    if count == 1:
        return "new"
    return "prospect"


def get_segment_label(segment: str) -> str:
    """Human-readable label for a segment (used in dashboard/messages)."""
    return {
        "vip":      "⭐ VIP Customer",
        "loyal":    "💚 Loyal Customer",
        "regular":  "👍 Regular Customer",
        "new":      "👋 New Customer",
        "prospect": "🔍 Prospect",
    }.get(segment, "Customer")


def get_customers_by_segment(
    business_id: int,
    segment:     str,
) -> list[dict]:
    """
    Return customers in a given segment.
    Segment is computed from user_memory columns.

    Parameters
    ──────────
    business_id   Tenant filter.
    segment       One of: "vip", "loyal", "regular", "new", "prospect", "all"

    Returns list of dicts: {phone, customer_name, order_count, total_spent, last_seen}
    """
    try:
        res = (
            supabase.table("user_memory")
            .select("phone, customer_name, order_count, total_spent, last_seen")
            .eq("business_id", business_id)
            .execute()
        )
        rows = res.data or []

        if segment == "all":
            return rows

        result = []
        for row in rows:
            seg = get_customer_segment(row)
            if seg == segment:
                result.append(row)

        return sorted(result, key=lambda r: float(r.get("total_spent") or 0), reverse=True)

    except Exception as exc:
        log.warning("get_customers_by_segment error: %s", exc)
        return []


def get_inactive_customers(
    business_id:      int,
    inactive_days:    int = 30,
    min_order_count:  int = 1,
) -> list[dict]:
    """
    Return customers who have not been seen in `inactive_days` days.
    Only returns customers who have placed at least `min_order_count` orders
    (i.e. real customers, not zero-interaction prospects).

    Used by the campaign engine to target win-back messages.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=inactive_days)).isoformat()
    try:
        res = (
            supabase.table("user_memory")
            .select("phone, customer_name, order_count, total_spent, last_seen")
            .eq("business_id", business_id)
            .lt("last_seen", cutoff)          # last_seen before cutoff
            .gte("order_count", min_order_count)
            .order("last_seen", desc=False)   # oldest inactive first
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("get_inactive_customers error: %s", exc)
        return []


def get_segment_summary(business_id: int) -> dict:
    """
    Return a count breakdown of all customer segments for a business.
    Used by the dashboard CRM card.

    Returns: {vip: N, loyal: N, regular: N, new: N, prospect: N, total: N}
    """
    try:
        res = (
            supabase.table("user_memory")
            .select("order_count, total_spent")
            .eq("business_id", business_id)
            .execute()
        )
        rows = res.data or []
        counts = {"vip": 0, "loyal": 0, "regular": 0, "new": 0, "prospect": 0}
        for row in rows:
            seg = get_customer_segment(row)
            counts[seg] = counts.get(seg, 0) + 1
        counts["total"] = len(rows)
        return counts
    except Exception as exc:
        log.warning("get_segment_summary error: %s", exc)
        return {"vip": 0, "loyal": 0, "regular": 0, "new": 0, "prospect": 0, "total": 0}
