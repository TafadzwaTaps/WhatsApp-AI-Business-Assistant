"""
tenant_router.py — Shared WhatsApp Number Multi-Tenant Router for WaziBot.

ARCHITECTURE
────────────
One WhatsApp number serves all businesses.
Customer messages arrive with a fixed phone_number_id (the platform number).
This module resolves WHICH business the customer is talking to.

ROUTING LOGIC
─────────────
1. Incoming message: phone_number_id == SHARED_PHONE_NUMBER_ID
2. Look up customer session → selected_business_id
3. If no business selected → enter "selecting_business" state
4. Show picker: list of active businesses
5. Customer selects → session updated → all future messages route to that business
6. Customer can say "switch" at any time to change business

FALLBACK
────────
If phone_number_id is NOT the shared number, existing per-business routing
works exactly as before (backward compatible).

ENVIRONMENT VARIABLES
─────────────────────
  SHARED_PHONE_NUMBER_ID   The Meta phone_number_id for the platform's shared number
  SHARED_WA_TOKEN          The permanent access token for the shared number
  SHARED_WA_PHONE          Human-readable number (e.g. +447774128484) — display only

STATE STORAGE
─────────────
Stored in carts.state_data JSONB (same as all other state):
  state_data.selected_business_id   int   | None
  state_data.selected_business_name str   | None

This means business selection persists across sessions without a new table.
"""

import os
import logging
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

def get_shared_phone_number_id() -> str:
    return os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()


def get_shared_wa_token() -> str:
    return os.getenv("SHARED_WA_TOKEN", "").strip()


def get_shared_wa_phone() -> str:
    return os.getenv("SHARED_WA_PHONE", "WaziBot").strip()


def is_shared_number(phone_number_id: str) -> bool:
    """Returns True if this phone_number_id is the platform's shared number."""
    shared = get_shared_phone_number_id()
    return bool(shared and phone_number_id == shared)


# ─────────────────────────────────────────────────────────────────────────────
# SESSION HELPERS — read/write selected business in carts.state_data
# ─────────────────────────────────────────────────────────────────────────────

# We store business selection in state_data UNDER a fixed "platform" business_id
# so the carts table row can be found. We use business_id=0 as the platform slot.
_PLATFORM_BIZ_ID = 0


def _read_platform_state(phone: str) -> dict:
    """Read platform-level state_data for this phone (business_id=0 row)."""
    try:
        from core.db import supabase
        res = (
            supabase.table("carts")
            .select("state_data")
            .eq("phone", phone)
            .eq("business_id", _PLATFORM_BIZ_ID)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0].get("state_data") or {}
        return {}
    except Exception as exc:
        log.error("_read_platform_state error: %s", exc)
        return {}


def _write_platform_state(phone: str, patch: dict) -> None:
    """Write platform-level state_data for this phone."""
    try:
        from core.db import supabase
        from datetime import datetime, timezone
        existing = _read_platform_state(phone)
        existing.update(patch)
        supabase.table("carts").upsert(
            {
                "phone":       phone,
                "business_id": _PLATFORM_BIZ_ID,
                "items":       [],           # no cart at platform level
                "state_data":  existing,
                "updated_at":  datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="phone,business_id",
        ).execute()
    except Exception as exc:
        log.error("_write_platform_state error: %s", exc)


def get_selected_business_id(phone: str) -> Optional[int]:
    """Return the business_id the customer has selected, or None."""
    sd = _read_platform_state(phone)
    val = sd.get("selected_business_id")
    return int(val) if val is not None else None


def get_selected_business_name(phone: str) -> str:
    sd = _read_platform_state(phone)
    return sd.get("selected_business_name", "")


def set_selected_business(phone: str, business_id: int, business_name: str) -> None:
    """Persist the customer's business selection."""
    _write_platform_state(phone, {
        "selected_business_id":   business_id,
        "selected_business_name": business_name,
        "state":                  "browsing",    # reset to browsing for the new business
    })
    log.info("business selected  phone=%s  biz_id=%s  name=%r", phone, business_id, business_name)


def clear_selected_business(phone: str) -> None:
    """Clear the business selection — customer must pick again."""
    _write_platform_state(phone, {
        "selected_business_id":   None,
        "selected_business_name": None,
        "state":                  "selecting_business",
    })
    log.info("business selection cleared  phone=%s", phone)


def is_in_selection_state(phone: str) -> bool:
    """Returns True if the customer needs to pick a business."""
    bid = get_selected_business_id(phone)
    return bid is None


# ─────────────────────────────────────────────────────────────────────────────
# SWITCH DETECTION — customer wants to change business
# ─────────────────────────────────────────────────────────────────────────────

_SWITCH_TRIGGERS = {
    "switch", "switch business", "change business", "change store",
    "other business", "other store", "go back", "main menu",
    "businesses", "list businesses", "show businesses",
    "different store", "different business",
}


def is_switch_request(text: str) -> bool:
    t = text.lower().strip()
    return t in _SWITCH_TRIGGERS or t.startswith("switch")


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS PICKER — generate and parse the selection menu
# ─────────────────────────────────────────────────────────────────────────────

def build_business_picker(businesses: list[dict], platform_name: str = "WaziBot") -> str:
    """
    Build the WhatsApp business selection menu.
    businesses: list of {id, name, category (optional), is_active}
    """
    if not businesses:
        return (
            f"👋 Welcome to *{platform_name}*!\n\n"
            "No businesses are currently available.\n"
            "Please check back soon. 🙏"
        )

    lines = []
    for i, biz in enumerate(businesses, 1):
        category = biz.get("category", "").strip()
        cat_tag  = f" _{category}_" if category else ""
        lines.append(f"  {i}️⃣  *{biz['name']}*{cat_tag}")

    return (
        f"👋 *Welcome to {platform_name}!*\n\n"
        f"Choose a business to order from:\n\n"
        + "\n".join(lines) +
        "\n\n_Reply with the *number* or *business name* to get started._"
    )


def parse_business_selection(text: str, businesses: list[dict]) -> Optional[dict]:
    """
    Parse the customer's reply to the business picker.
    Returns the matched business dict or None.

    Accepts:
      - Digit: "1", "2", "3"
      - Business name (exact or fuzzy): "Flavoury Foods", "flavoury"
    """
    t = text.strip()

    # Numeric selection
    if t.isdigit():
        idx = int(t) - 1
        if 0 <= idx < len(businesses):
            return businesses[idx]
        return None

    # Name match — try exact then fuzzy
    t_lower = t.lower()
    for biz in businesses:
        if biz["name"].lower() == t_lower:
            return biz

    # Partial / starts-with match
    for biz in businesses:
        if biz["name"].lower().startswith(t_lower) or t_lower in biz["name"].lower():
            return biz

    # Fuzzy match via rapidfuzz if available
    try:
        from rapidfuzz import process as rf
        names   = [b["name"] for b in businesses]
        result  = rf.extractOne(t, names, score_cutoff=55)
        if result:
            matched_name = result[0]
            for biz in businesses:
                if biz["name"] == matched_name:
                    return biz
    except ImportError:
        pass

    return None


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ROUTING FUNCTION — called from webhook
# ─────────────────────────────────────────────────────────────────────────────

def resolve_business_for_shared_number(
    phone: str,
    text: str,
    active_businesses: list[dict],
) -> tuple[Optional[dict], str]:
    """
    Resolve which business to use for an incoming message on the shared number.

    Returns:
        (business_dict | None, reply_or_empty_string)

    If (business, "") → business found, proceed normally with generate_reply()
    If (None, reply)  → send reply directly, do not call generate_reply()
    If (None, "")     → no business found, no reply (should not happen)
    """
    platform_name = get_shared_wa_phone() or "WaziBot"

    # ── Explicit switch request ───────────────────────────────────────────────
    if is_switch_request(text):
        clear_selected_business(phone)
        picker = build_business_picker(active_businesses, platform_name)
        return None, picker

    # ── Already has a selected business ──────────────────────────────────────
    selected_id = get_selected_business_id(phone)
    if selected_id is not None:
        for biz in active_businesses:
            if biz["id"] == selected_id:
                return biz, ""
        # Selected business is no longer active — clear and re-show picker
        log.warning("selected business %s no longer active  phone=%s", selected_id, phone)
        clear_selected_business(phone)
        picker = build_business_picker(active_businesses, platform_name)
        return None, (
            "⚠️ The business you were chatting with is currently unavailable.\n\n"
            + picker
        )

    # ── Customer needs to select a business ──────────────────────────────────
    selected = parse_business_selection(text, active_businesses)
    if selected:
        set_selected_business(phone, selected["id"], selected["name"])
        # Return business but with an empty reply —
        # generate_reply() will handle the greeting since state is now browsing
        return selected, ""

    # Not a valid selection — show picker again
    picker = build_business_picker(active_businesses, platform_name)
    return None, picker
