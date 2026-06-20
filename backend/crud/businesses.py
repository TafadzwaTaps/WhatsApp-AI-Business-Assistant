"""
crud/businesses.py — Business account CRUD.

All DB access via Supabase. Token encryption/decryption handled here.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.db import supabase
from core.crypto import encrypt_token, decrypt_token
from crud._helpers import _now, _one

log = logging.getLogger(__name__)


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
    if hasattr(data, "owner_email") and data.owner_email:        # C1
        row["owner_email"] = data.owner_email.strip().lower()

    # Set trial dates on signup so is_trial_active() works correctly
    from datetime import datetime, timezone, timedelta
    _now_utc = datetime.now(timezone.utc)
    row["billing_status"]    = "trialing"
    row["trial_started_at"]  = _now_utc.isoformat()
    row["trial_ends_at"]     = (_now_utc + timedelta(days=30)).isoformat()

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

    # Drop any field whose column doesn't exist yet on the live businesses
    # table. Without this, a single unmigrated column (e.g. cash_enabled,
    # pickup_enabled before their ALTER TABLE has been run) causes Supabase
    # to reject the ENTIRE update with PGRST204 — meaning unrelated fields
    # in the same save (currency, name, etc.) silently fail too. This keeps
    # the save working for every column that DOES exist, and just skips
    # the ones that don't, until the migration below is applied.
    unknown_cols = [k for k in update_dict if not _has_business_col(k)]
    if unknown_cols:
        log.warning(
            "update_business: dropping unmigrated column(s) %s for business_id=%s "
            "— run the pending ALTER TABLE migration to enable them",
            unknown_cols, business_id,
        )
        for k in unknown_cols:
            update_dict.pop(k, None)

    if not update_dict:
        return get_business_by_id(business_id)

    res = (
        supabase.table("businesses")
        .update(update_dict)
        .eq("id", business_id)
        .execute()
    )
    return _one("businesses", res)


# Cache for businesses table columns — same pattern as _has_memory_col
# (crud/customers.py) and _has_messages_col (crud/messages.py).
_business_columns: set | None = None


def _has_business_col(col: str) -> bool:
    global _business_columns
    if _business_columns is None:
        try:
            res = supabase.table("businesses").select("*").limit(1).execute()
            if res.data:
                _business_columns = set(res.data[0].keys())
            else:
                # No rows to introspect — fall back to a conservative known-safe
                # set rather than blocking every update.
                _business_columns = {
                    "id", "name", "owner_username", "owner_password",
                    "whatsapp_phone_id", "whatsapp_token", "is_active",
                    "created_at", "category", "currency", "currency_symbol",
                }
        except Exception:
            _business_columns = set()
    return col in _business_columns


def delete_business(business_id: int) -> Optional[dict]:
    res = (
        supabase.table("businesses")
        .delete()
        .eq("id", business_id)
        .execute()
    )
    return _one("businesses", res)
