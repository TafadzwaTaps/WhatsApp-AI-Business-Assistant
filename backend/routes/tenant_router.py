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
    # Direct switch commands
    "switch", "switch business", "switch shop", "switch store",
    "change business", "change shop", "change store",
    "choose business", "choose shop", "choose store",
    "other business", "other store", "other shop",
    "different store", "different business", "different shop",
    # Recovery phrases
    "wrong business", "wrong shop", "wrong store",
    "restart", "start over", "begin again", "reset",
    # Navigation
    "go back", "main menu", "back to menu", "marketplace",
    "browse businesses", "browse shops",
}


def is_switch_request(text: str) -> bool:
    """
    Returns True if the customer wants to switch/change to a different business.
    Works from ANY conversation state — cart, checkout, booking, handoff.
    """
    t = text.lower().strip()
    if t in _SWITCH_TRIGGERS:
        return True
    if t.startswith("switch") or t.startswith("change business") or t.startswith("choose business"):
        return True
    if t.startswith("browse business") or t.startswith("wrong business"):
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# BUSINESS PICKER — generate and parse the selection menu
# ─────────────────────────────────────────────────────────────────────────────

# Number emojis for picker (handles 1-20)
_NUM_EMOJI = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟",
               "1️⃣1️⃣","1️⃣2️⃣","1️⃣3️⃣","1️⃣4️⃣","1️⃣5️⃣","1️⃣6️⃣","1️⃣7️⃣","1️⃣8️⃣","1️⃣9️⃣","2️⃣0️⃣"]


def build_business_picker(
    businesses: list[dict],
    platform_name: str = "WaziBot",
    current_name: str = "",
    title: str = "",
) -> str:
    """
    Build the WhatsApp business selection menu — marketplace format.
    Each business gets its own paragraph with number, name, and category icon.

    businesses: list of {id, name, category (optional), is_active}
    current_name: if set, shows the currently selected business at the top
    title: optional custom header (e.g. for directory command)
    """
    if not businesses:
        return (
            f"👋 Welcome to *{platform_name}*!\n\n"
            "No businesses are currently available.\n"
            "Please check back soon. 🙏"
        )

    entries = []
    for i, biz in enumerate(businesses, 1):
        category = biz.get("category", "").strip()
        icon     = _category_icon(category)
        num      = _NUM_EMOJI[i - 1] if i <= len(_NUM_EMOJI) else f"{i}."
        cat_line = f"\n{icon} _{category}_" if category else f"\n{icon}"
        entries.append(f"{num} *{biz['name']}*{cat_line}")

    if title:
        header = title
    elif current_name:
        header = f"🔄 *Switch Business*\n\n🏪 Currently chatting with: *{current_name}*"
    else:
        header = f"👋 *Welcome to {platform_name}!*"

    return (
        f"{header}\n\n"
        f"Please choose a business:\n\n"
        + "\n\n".join(entries) +
        "\n\n"
        "_Reply with the *number* or *business name*._\n"
        "_Examples: *2* or *Flavoury Foods*_\n\n"
        "_Type *switch business* anytime to change._"
    )


def parse_business_selection(text: str, businesses: list[dict]) -> Optional[dict]:
    """
    Parse the customer's reply to the business picker.
    Returns the matched business dict or None.

    Accepts:
      - Single digit or short number: "1", "2", " 3 ", "  2  "
      - Business name (exact, case-insensitive): "Flavoury Foods"
      - Partial name: "firelily", "uptown"
      - Fuzzy name: "firelilyfarrismum" (rapidfuzz WRatio ≥ 55)
    """
    import re

    t       = text.strip()
    t_lower = t.lower()

    # ── Numeric selection — accept "1" through len(businesses) ────────────────
    # Also handle "reply 2", "option 2" etc. by extracting trailing digit
    if t.isdigit():
        idx = int(t) - 1
        if 0 <= idx < len(businesses):
            return businesses[idx]
        return None

    # Single digit embedded in short phrase: "number 2", "option 3", "no 2"
    m = re.search(r"\b([1-9]\d?)\b", t)
    if m and len(t) <= 15:   # only for short inputs — don't match "order 2 sadza"
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(businesses):
            return businesses[idx]

    # ── Exact name match ──────────────────────────────────────────────────────
    for biz in businesses:
        if biz["name"].lower() == t_lower:
            return biz

    # ── Partial / starts-with / contains ────────────────────────────────────
    for biz in businesses:
        biz_lower = biz["name"].lower()
        if biz_lower.startswith(t_lower) or t_lower in biz_lower:
            return biz

    # ── Word-level match: all words in query appear in business name ─────────
    words = [w for w in re.findall(r"\w+", t_lower) if len(w) >= 3]
    if words:
        for biz in businesses:
            biz_lower = biz["name"].lower()
            if all(w in biz_lower for w in words):
                return biz

    # ── Fuzzy match via rapidfuzz ─────────────────────────────────────────────
    try:
        from rapidfuzz import process as rf, fuzz
        names  = [b["name"] for b in businesses]
        result = rf.extractOne(t, names, scorer=fuzz.WRatio, score_cutoff=55)
        if result:
            for biz in businesses:
                if biz["name"] == result[0]:
                    return biz
    except ImportError:
        pass

    return None



# ── Additional helper: category icon mapping ────────────────────────────────

def _category_icon(category: str) -> str:
    """Return an emoji icon for a business category."""
    cat = (category or "").lower()
    if any(w in cat for w in ["food", "restaurant", "fast food", "bakery", "cafe",
                                "coffee", "butchery", "pie", "flavour"]):
        return "🍽️"
    # Check beauty/salon BEFORE health so "health & beauty" → 💅 not 💊
    if any(w in cat for w in ["salon", "barber", "beauty", "spa", "hair", "nail",
                                "cosmetic", "makeup"]):
        return "💅"
    if any(w in cat for w in ["pharmacy", "clinic", "hospital", "doctor", "dentist",
                                "health", "medical"]):
        return "💊"
    if any(w in cat for w in ["fashion", "clothing", "boutique", "shoe", "jewelry",
                                "apparel"]):
        return "👗"
    if any(w in cat for w in ["electronics", "computer", "tech", "phone"]):
        return "📱"
    if any(w in cat for w in ["gym", "fitness", "sport"]):
        return "💪"
    if any(w in cat for w in ["grocery", "supermarket", "wholesale", "retail"]):
        return "🛒"
    if any(w in cat for w in ["agriculture", "farm"]):
        return "🌾"
    if any(w in cat for w in ["hardware", "construction"]):
        return "🔧"
    return "🏪"


def is_businesses_help_request(text: str) -> bool:
    """
    Returns True if the customer wants to see the business directory
    WITHOUT switching — they're just browsing what's available.
    Separate from is_switch_request which clears the current selection.
    """
    t = text.lower().strip()
    return t in {
        # Directory commands (Phase 7)
        "directory", "business directory", "show businesses", "list businesses",
        "businesses", "shops", "stores", "available businesses",
        # Info queries
        "what businesses", "which businesses", "all businesses", "all shops",
        "what shops", "which shops", "other businesses", "other shops",
        # Help phrases
        "help businesses", "show directory",
    }


# ─────────────────────────────────────────────────────────────────────────────
# CURRENT BUSINESS QUERY — "what shop is this", "who am I talking to"
# ─────────────────────────────────────────────────────────────────────────────

_CURRENT_BIZ_TRIGGERS = {
    "current business", "which business", "what business",
    "what shop", "which shop", "who am i talking to",
    "what store", "which store", "current shop", "current store",
    "what business am i in", "who is this", "what shop is this",
    "am i talking to", "this shop", "current",
}

def is_current_business_query(text: str) -> bool:
    """Returns True if the customer is asking which business they are in."""
    t = text.lower().strip()
    if t in _CURRENT_BIZ_TRIGGERS:
        return True
    if any(phrase in t for phrase in [
        "which business", "what business", "who am i", "what shop",
        "current business", "which shop", "who is this",
    ]):
        return True
    return False


def build_current_business_response(
    business_name: str,
    category: str = "",
    platform_name: str = "WaziBot",
) -> str:
    """Build the 'you are currently chatting with X' response."""
    icon = _category_icon(category)
    cat_line = f"\n_{category}_" if category else ""
    return (
        f"🏪 *You are currently chatting with:*\n\n"
        f"{icon} *{business_name}*{cat_line}\n\n"
        f"Type:\n"
        f"  📋 *menu* — to see products\n"
        f"  🛒 *cart* — to view your cart\n"
        f"  ✅ *checkout* — to place an order\n\n"
        f"_Type *switch business* to browse other businesses._\n"
        f"_Type *businesses* to see the full directory._"
    )



# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3: Safe switch — check active cart before clearing selection
# ─────────────────────────────────────────────────────────────────────────────

def _has_active_cart(phone: str, business_id: int) -> bool:
    """Returns True if the customer has items in their cart with this business."""
    if not business_id:
        return False
    try:
        from core.db import supabase
        res = (
            supabase.table("carts")
            .select("items")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        if res.data:
            items = res.data[0].get("items") or []
            return len(items) > 0
    except Exception:
        pass
    return False


_SWITCH_CONFIRM_TRIGGERS = {"1", "yes", "yeah", "yep", "ok", "confirm", "continue", "proceed"}
_SWITCH_CANCEL_TRIGGERS   = {"2", "no", "cancel", "back", "stay", "keep"}

def is_switch_confirm(text: str) -> bool:
    return text.lower().strip() in _SWITCH_CONFIRM_TRIGGERS

def is_switch_cancel(text: str) -> bool:
    return text.lower().strip() in _SWITCH_CANCEL_TRIGGERS


def build_switch_warning(current_biz_name: str) -> str:
    """Warning shown when switching while a cart is active."""
    return (
        f"⚠️ You have items in your cart with *{current_biz_name}*.\n\n"
        f"Switching businesses will *keep* your cart — "
        f"you can return to it anytime by saying *switch business* again.\n\n"
        f"Continue switching?\n\n"
        f"1️⃣ Yes, show me other businesses\n"
        f"2️⃣ No, stay with {current_biz_name}"
    )

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
        (business, "")           → existing selection, proceed to generate_reply()
        (business, confirmation) → just selected, send confirmation + proceed to generate_reply()
        (None, reply)            → send reply directly, do NOT call generate_reply()

    Priority order (Phase 6):
      1. Current-business query  → answer in place, stay in current business
      2. Switch request          → Phase 3 cart-warning or immediate clear + picker
      3. Pending switch confirm  → confirm or cancel the pending switch
      4. Existing selection      → route to current business
      5. New selection attempt   → parse + confirm
      6. Unknown                 → show picker
    """
    import re as _re
    platform_name = get_shared_wa_phone() or "WaziBot"
    selected_id   = get_selected_business_id(phone)
    selected_name = get_selected_business_name(phone)

    # ── P0: Current-business query (Phase 1) ──────────────────────────────────
    if selected_id is not None and is_current_business_query(text):
        biz = next((b for b in active_businesses if b["id"] == selected_id), None)
        if biz:
            return biz, build_current_business_response(
                biz["name"], biz.get("category", ""), platform_name)
        # Fall through to re-show picker

    # ── P1: Pending switch confirmation (Phase 3) ─────────────────────────────
    sd = _read_platform_state(phone)
    if sd.get("pending_switch"):
        if is_switch_confirm(text):
            # Confirmed — clear current business and show picker
            _write_platform_state(phone, {"pending_switch": False})
            clear_selected_business(phone)
            current_name = sd.get("selected_business_name", "")
            picker = build_business_picker(active_businesses, platform_name,
                                           current_name=current_name)
            return None, picker
        elif is_switch_cancel(text):
            # Cancelled — stay with current business
            _write_platform_state(phone, {"pending_switch": False})
            biz = next((b for b in active_businesses if b["id"] == selected_id), None)
            if biz:
                return biz, f"👍 Staying with *{selected_name}*. Type *menu* to continue. 😊"
            # Fall through

    # ── P2: Switch request (Phase 2 + 3) ─────────────────────────────────────
    if is_switch_request(text):
        if selected_id and _has_active_cart(phone, selected_id):
            # Phase 3: warn before switching — don't clear cart
            _write_platform_state(phone, {"pending_switch": True})
            return None, build_switch_warning(selected_name or "current business")
        # No cart — immediate switch
        clear_selected_business(phone)
        current_name = selected_name
        picker = build_business_picker(active_businesses, platform_name,
                                       current_name=current_name)
        return None, picker

    # ── P3: Already has a selected business ──────────────────────────────────
    if selected_id is not None:
        biz = next((b for b in active_businesses if b["id"] == selected_id), None)
        if biz:
            return biz, ""
        # Business no longer active — clear and re-show picker
        log.warning("selected business %s no longer active  phone=%s", selected_id, phone)
        clear_selected_business(phone)
        return None, (
            "⚠️ The business you were chatting with is currently unavailable.\n\n"
            + build_business_picker(active_businesses, platform_name)
        )

    # ── P4: Parse new business selection (Phase 5+6) ─────────────────────────
    selected = parse_business_selection(text, active_businesses)
    if selected:
        set_selected_business(phone, selected["id"], selected["name"])
        cat  = selected.get("category", "").strip()
        icon = _category_icon(cat)
        # Phase 5: enhanced confirmation
        confirmation = (
            f"✅ *Business Selected!*\n\n"
            f"{icon} *{selected['name']}*"
            + (f"\n_{cat}_" if cat else "") +
            f"\n\n"
            f"You can now:\n"
            f"  📋 Type *menu* — see products\n"
            f"  🛒 Type *cart* — view your cart\n"
            f"  ✅ Type *checkout* — place an order\n\n"
            f"_Type *switch business* anytime to switch._\n"
            f"_Type *businesses* to see the directory._"
        )
        log.info("business selected  phone=%s  biz=%s  name=%r",
                 phone, selected["id"], selected["name"])
        return selected, confirmation

    # ── P5: No valid selection — show picker (Phase 4) ───────────────────────
    return None, build_business_picker(active_businesses, platform_name)
