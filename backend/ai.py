"""
ai.py — WaziBot Ordering Engine  v7

═══════════════════════════════════════════════════════════════════════════════
BUGS FIXED IN THIS VERSION
═══════════════════════════════════════════════════════════════════════════════

BUG 1 — "paid" not recognised after selecting payment method
  Root cause: crud.clear_cart() was deleting the entire carts row, which also
  wiped state_data (containing pending_payment). Fixed: clear_cart now sets
  items=[] via UPSERT instead of DELETE, preserving state_data intact.

BUG 2 — NameError: business_id not in scope in _build_payment_menu()
  Root cause: _build_payment_menu(cart) referenced business_id but it was only
  a parameter of generate_reply(). Fixed: business_id passed explicitly.

BUG 3 — No cancel/order-lookup in awaiting_payment state
  "Order-9" typed after checkout fell to fallback. Fixed: awaiting_payment
  state handles cancel, order reference lookup (ORDER-X), and re-shows
  payment instructions if user asks.

BUG 4 — No proof of payment / confirmation security
  Fixed: After "paid", bot enters awaiting_proof state. Customer must send
  a screenshot/image OR a transaction ID. Plain "paid" alone is no longer
  accepted without follow-up proof.

═══════════════════════════════════════════════════════════════════════════════
NEW FEATURES
═══════════════════════════════════════════════════════════════════════════════

✅ CANCEL ANYWHERE — "cancel" works from any state:
   - In checkout   → returns to cart
   - In payment    → cancels order, restores cart
   - In browsing   → acknowledges nothing to cancel

✅ ORDER DOUBLE-CONFIRMATION — before placing order, bot shows cart + asks
   "Confirm?" to avoid accidental orders. User must reply "yes" / "confirm".

✅ PROOF OF PAYMENT — after "paid", bot asks for:
   - Transaction ID/screenshot description (text)
   - Or image (WhatsApp image messages)
   Bot records proof and then confirms the order.

✅ SPAM PREVENTION — rate limiting: if the same customer sends checkout
   more than 3 times without completing, they're rate-limited for 10 minutes.

✅ ORDER REFERENCE LOOKUP — typing "ORDER-9" or "order 9" shows order status.

✅ AWAITING_PROOF STATE — new state between paid and confirmed.

═══════════════════════════════════════════════════════════════════════════════
CONVERSATION STATE MACHINE
═══════════════════════════════════════════════════════════════════════════════
  browsing        → normal shopping
  confirm_order   → double-confirmation before order is placed
  checkout        → waiting for payment method selection
  awaiting_payment→ order placed, waiting for "paid" reply
  awaiting_proof  → "paid" received, waiting for proof (txn ID / image)

All state is stored in carts.state_data (JSONB) — NOT in user_memory, so it
survives across separate save_user_memory() calls without being wiped.

═══════════════════════════════════════════════════════════════════════════════
INTENT PRIORITY (do not reorder)
═══════════════════════════════════════════════════════════════════════════════
  P0  Global cancel         "cancel" / "stop" — works in every state
  P1  State: awaiting_proof → handle proof submission
  P2  State: awaiting_payment → handle "paid", order lookup, re-send instr.
  P3  State: confirm_order → handle "yes"/"no"
  P4  State: checkout → handle payment method selection
  P5  Checkout trigger       "checkout"
  P6  Remove item            "remove X"
  P7  Add to cart            NLP product match
  P8  Cart view              "cart"
  P9  Browse menu            "menu"
  P10 Order reference lookup "order-X" typed in browsing state
  P11 Help / greeting        "hi"
  P12 Fallback               last resort only
"""

import re
import logging
import time
from difflib import get_close_matches
from datetime import datetime, timezone
import crud


# ── Lazy module accessors — avoids circular imports at module level ───────────

def _states():
    import conversation_states
    return conversation_states


def _fuzzy():
    import fuzzy_matcher
    return fuzzy_matcher


def _handoff_mod():
    import human_handoff
    return human_handoff

log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# STATE MANAGEMENT  (carts.state_data JSONB — persists across saves)
# ═════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_state_data(phone: str, business_id: int) -> dict:
    """Read state_data from carts table. Never raises."""
    try:
        from db import supabase
        res = (
            supabase.table("carts")
            .select("state_data")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        if res.data:
            return res.data[0].get("state_data") or {}
        return {}
    except Exception as exc:
        log.error("_read_state_data error: %s", exc)
        return {}


def _write_state_data(phone: str, business_id: int, patch: dict) -> None:
    """
    Merge patch into existing state_data and save.
    Only touches state_data — never changes items column.
    Never raises.
    """
    try:
        from db import supabase
        existing = _read_state_data(phone, business_id)
        existing.update(patch)
        supabase.table("carts").upsert(
            {
                "phone":       phone,
                "business_id": business_id,
                "state_data":  existing,
                "updated_at":  _now_iso(),
            },
            on_conflict="phone,business_id",
        ).execute()
        log.debug("_write_state_data  phone=%s  keys=%s", phone, list(patch.keys()))
    except Exception as exc:
        log.error("_write_state_data error: %s", exc)


def _get_state(phone: str, business_id: int) -> str:
    raw = _read_state_data(phone, business_id).get("state", "browsing")
    return _states().normalize_state(raw)


def _get_session(phone: str, business_id: int) -> dict:
    return _read_state_data(phone, business_id).get("session") or {}


def _get_pending_payment(phone: str, business_id: int) -> dict | None:
    return _read_state_data(phone, business_id).get("pending_payment") or None


def _get_pending_proof(phone: str, business_id: int) -> dict | None:
    return _read_state_data(phone, business_id).get("pending_proof") or None


def _set_state(phone: str, business_id: int, state: str, **extra) -> None:
    patch = {"state": state}
    patch.update(extra)
    _write_state_data(phone, business_id, patch)


def _set_checkout_state(phone: str, business_id: int, cart_snapshot: list) -> None:
    from conversation_states import can_transition, STATE
    current = _get_state(phone, business_id)
    if not can_transition(current, STATE.CHECKOUT):
        log.warning("_set_checkout_state: invalid transition %s→checkout  phone=%s", current, phone)
    _set_state(phone, business_id, "checkout",
               session={"cart_snapshot": cart_snapshot},
               pending_payment=None)


def _set_confirm_state(phone: str, business_id: int, cart_snapshot: list) -> None:
    """Enter double-confirmation state before placing the order."""
    _set_state(phone, business_id, "confirm_order",
               session={"cart_snapshot": cart_snapshot},
               pending_payment=None)


def _set_awaiting_payment(phone: str, business_id: int,
                          order_id, method: str, reference: str) -> None:
    from conversation_states import can_transition, STATE
    current = _get_state(phone, business_id)
    if not can_transition(current, STATE.AWAITING_PAYMENT):
        log.warning("_set_awaiting_payment: invalid transition %s→awaiting_payment  phone=%s", current, phone)
    _set_state(phone, business_id, "awaiting_payment",
               session={},
               pending_payment={"order_id": order_id, "method": method, "reference": reference},
               pending_proof=None)


def _set_awaiting_proof(phone: str, business_id: int,
                        order_id, method: str, reference: str) -> None:
    """Enter awaiting_proof state — customer must provide txn ID or image."""
    from conversation_states import can_transition, STATE
    current = _get_state(phone, business_id)
    if not can_transition(current, STATE.AWAITING_PROOF):
        log.warning("_set_awaiting_proof: invalid transition %s→awaiting_proof  phone=%s", current, phone)
    _set_state(phone, business_id, "awaiting_proof",
               session={},
               pending_payment=None,
               pending_proof={"order_id": order_id, "method": method, "reference": reference})


def _reset_state(phone: str, business_id: int) -> None:
    _set_state(phone, business_id, "browsing",
               session={}, pending_payment=None, pending_proof=None)


def _set_survey_state(phone: str, business_id: int) -> None:
    """Enter survey state — awaiting satisfaction rating."""
    _set_state(phone, business_id, "survey", session={})


def _set_human_handoff(phone: str, business_id: int) -> None:
    """Pause AI and flag this customer for human agent attention."""
    _set_state(phone, business_id, "human_handoff",
               session={}, pending_payment=None, pending_proof=None)


def _in_survey_state(phone: str, business_id: int) -> bool:
    return _get_state(phone, business_id) == "survey"


# ═════════════════════════════════════════════════════════════════════════════
# SPAM / RATE LIMITING
# ═════════════════════════════════════════════════════════════════════════════

_CHECKOUT_RATE_WINDOW  = 600   # 10 minutes
_CHECKOUT_RATE_LIMIT   = 5     # max checkouts per window


def _check_rate_limit(phone: str, business_id: int) -> bool:
    """
    Returns True if customer is within rate limit (allowed to checkout).
    Returns False if they're spamming checkouts.
    Records the current checkout attempt.
    """
    try:
        sd  = _read_state_data(phone, business_id)
        now = time.time()

        attempts = sd.get("checkout_attempts") or []
        # Keep only attempts within the window
        attempts = [t for t in attempts if now - t < _CHECKOUT_RATE_WINDOW]

        if len(attempts) >= _CHECKOUT_RATE_LIMIT:
            return False

        attempts.append(now)
        _write_state_data(phone, business_id, {"checkout_attempts": attempts})
        return True
    except Exception:
        return True   # fail open — don't block on error


def _rate_limit_message() -> str:
    mins = _CHECKOUT_RATE_WINDOW // 60
    return (
        f"⚠️ *Too many checkout attempts.*\n\n"
        f"Please wait {mins} minutes before trying again.\n"
        f"If you need help, type *help*."
    )


# ═════════════════════════════════════════════════════════════════════════════
# MEMORY (order history + recommendations)
# ═════════════════════════════════════════════════════════════════════════════

def _get_memory(phone: str, business_id: int) -> dict:
    try:
        return crud.get_user_memory(phone, business_id) or {
            "frequent_items": {}, "last_orders": [],
        }
    except Exception as exc:
        log.warning("_get_memory failed: %s", exc)
        return {"frequent_items": {}, "last_orders": []}


def _update_order_history(phone: str, business_id: int, cart: list) -> None:
    try:
        mem = _get_memory(phone, business_id)
        for item in cart:
            name = item["name"]
            mem["frequent_items"][name] = mem["frequent_items"].get(name, 0) + item["qty"]
        mem["last_orders"].append([i["name"] for i in cart])
        mem["last_orders"] = mem["last_orders"][-10:]
        crud.save_user_memory(phone, business_id, mem)
    except Exception as exc:
        log.warning("_update_order_history failed: %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
# CART HELPERS
# ═════════════════════════════════════════════════════════════════════════════

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


# ═════════════════════════════════════════════════════════════════════════════
# INTENT DETECTION
# ═════════════════════════════════════════════════════════════════════════════

# ── Global cancel ─────────────────────────────────────────────────────────

_CANCEL_EXACT = {
    "cancel", "back", "stop", "quit", "nevermind",
    "never mind", "go back", "no thanks", "nope",
    "cancel order", "cancel my order", "i changed my mind",
    "changed my mind", "don't want", "dont want",
}


def _is_cancel(text: str) -> bool:
    t = text.lower().strip()
    return t in _CANCEL_EXACT or t.startswith("cancel")


# ── Refund / dispute intent ───────────────────────────────────────────────────

_REFUND_WORDS = {
    "refund", "refund please", "i want a refund", "give me a refund",
    "money back", "get my money back", "want my money back",
    "dispute", "chargeback", "wrong order", "not received",
    "didn't receive", "didnt receive", "never got", "where is my order",
    "where is my food", "where is my delivery",
}


def _is_refund_request(text: str) -> bool:
    t = text.lower().strip()
    return t in _REFUND_WORDS or any(w in t for w in [
        "refund", "money back", "dispute", "chargeback",
        "not received", "never got", "where is my order",
    ])


# ── Completion / farewell detection ──────────────────────────────────────────

_DONE_EXACT = {
    "thank you", "thanks", "ty", "thx", "thank u",
    "that's all", "thats all", "nothing else", "i'm done", "im done",
    "no thanks", "no thank you", "nah thanks", "all good",
    "okay thanks", "ok thanks", "okay thank you", "ok thank you",
    "thanks bye", "thank you bye", "bye", "goodbye", "good bye",
    "cheers", "cool thanks", "perfect thanks", "great thanks",
    "awesome thanks", "sorted thanks", "sorted",
    "we're done", "we are done", "that will be all",
}

def _is_conversation_done(text: str) -> bool:
    """Detect farewell / completion phrases so we can close gracefully."""
    t = text.lower().strip()
    if t in _DONE_EXACT:
        return True
    # Short gratitude that starts with thanks/thank
    if t.startswith("thank") and len(t) < 30:
        return True
    return False


# ── Survey state helpers ──────────────────────────────────────────────────────

_SURVEY_OPTIONS = {"1": "excellent", "2": "good", "3": "average", "4": "poor",
                   "excellent": "excellent", "good": "good",
                   "average": "average", "poor": "poor",
                   "👍": "excellent", "😊": "good", "😐": "average", "😞": "poor"}

def _is_survey_response(text: str) -> bool:
    return text.lower().strip() in _SURVEY_OPTIONS

def _parse_survey_rating(text: str) -> str:
    return _SURVEY_OPTIONS.get(text.lower().strip(), "")


# ── Urgency / delivery follow-up detection ───────────────────────────────────

_URGENCY_PHRASES = [
    "hurry", "urgent", "asap", "quickly", "fast", "how long", "when will",
    "where is", "still waiting", "taking long", "taking too long",
    "late", "delayed", "not arrived", "hasn't arrived", "not here yet",
    "cold", "hungry", "starving",
]

def _is_urgency_message(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _URGENCY_PHRASES)


# ── Business-agent message detection ─────────────────────────────────────────
# Detects when a business owner sends a status update directly into the chat
# (e.g. "Your payment has been verified") so we don't reply with a fallback.

_AGENT_PHRASES = [
    "your payment has been verified", "payment verified", "payment confirmed",
    "order is being prepared", "order is ready", "ready for pickup",
    "rider has been assigned", "out for delivery", "on the way",
    "delivered", "order complete", "thank you for your order",
    "your order is ready", "being prepared", "preparation",
]

def _is_agent_message(text: str) -> bool:
    """
    Returns True if the message looks like it came from a business agent
    (not the customer). These are status updates sent into the chat by staff.
    We should not reply with a generic fallback to these.
    """
    t = text.lower()
    return any(p in t for p in _AGENT_PHRASES)


# ── Payment confirmation ──────────────────────────────────────────────────

_PAID_EXACT = {
    "paid", "sent", "done", "transferred",
    "i paid", "ive paid", "i've paid",
    "i sent", "money sent", "payment sent",
    "already paid", "i have paid", "i transferred",
}


def _is_payment_confirmation(text: str) -> bool:
    t = text.lower().strip()
    return t in _PAID_EXACT or "sent money" in t or "i already paid" in t


# ── Order reference ───────────────────────────────────────────────────────

_ORDER_REF_RE = re.compile(r"\border[-\s#]*(\d+)\b", re.IGNORECASE)


def _extract_order_id(text: str) -> int | None:
    """Extract order number from 'ORDER-9', 'order 9', '#9', 'order #9' etc."""
    m = _ORDER_REF_RE.search(text)
    if m:
        return int(m.group(1))
    # Also match bare number if text starts with # or is just digits (in order context)
    m2 = re.match(r"^#(\d+)$", text.strip())
    if m2:
        return int(m2.group(1))
    return None


# ── Yes / No (confirmation flow) ─────────────────────────────────────────

_YES_WORDS = {"yes", "y", "yep", "yeah", "yup", "confirm", "ok", "okay", "sure", "go ahead", "proceed"}
_NO_WORDS  = {"no", "n", "nope", "nah", "not yet", "wait", "hold on"}


def _is_yes(text: str) -> bool:
    return text.lower().strip() in _YES_WORDS


def _is_no(text: str) -> bool:
    return text.lower().strip() in _NO_WORDS


# ── Payment method ────────────────────────────────────────────────────────

_ECOCASH_TRIGGERS = {
    "1", "1️⃣", "ecocash", "eco cash", "eco-cash",
    "cash transfer", "mobile money", "econet",
}
_PAYPAL_TRIGGERS = {
    "2", "2️⃣", "paypal", "pay pal", "payp", "pp",
    "pay with paypal",
}
_CASH_TRIGGERS = {
    "3", "3️⃣", "cash", "cod", "cash on delivery",
    "on delivery", "pickup", "pick up", "collect",
    "pay on delivery", "deliver",
}


def _detect_payment_method(text: str) -> str | None:
    """
    Detect payment method from customer text.
    Returns: 'ecocash' | 'paypal' | 'cash' | 'cancel' | None

    Primary: uses fuzzy_matcher.normalize_payment_choice() for broad coverage.
    Fallback: original set/substring matching.
    """
    t = text.lower().strip()

    # Cancel check always first
    if t in _CANCEL_EXACT or t.startswith("cancel"):
        return "cancel"

    # Primary: fuzzy_matcher covers all variations
    try:
        result = _fuzzy().normalize_payment_choice(text)
        if result:
            return result
    except Exception as exc:
        log.warning("_detect_payment_method: fuzzy_matcher failed (%s) — using fallback", exc)

    # Fallback: original matching
    if t in _ECOCASH_TRIGGERS:
        return "ecocash"
    if t in _PAYPAL_TRIGGERS:
        return "paypal"
    if t in _CASH_TRIGGERS:
        return "cash"
    if "ecocash" in t or "eco cash" in t or "cash transfer" in t:
        return "ecocash"
    if "paypal" in t or "pay pal" in t:
        return "paypal"
    if any(w in t for w in ["on delivery", "pickup", "pick up", "collect", "cash on"]):
        return "cash"
    if t == "cash":
        return "cash"
    return None


# ── General intent ────────────────────────────────────────────────────────

def _intent(text: str) -> str:
    t = text.lower().strip()

    if any(w in t for w in [
        "checkout", "confirm order", "place order", "complete order",
        "order now", "submit order", "i'm done", "im done", "finish order",
    ]) or t in ("pay", "checkout", "place my order", "submit"):
        return "checkout"

    if t.startswith("remove ") or t.startswith("delete ") or "remove " in t:
        return "remove"

    if any(w in t for w in [
        "my cart", "view cart", "show cart", "whats in my cart",
        "what's in my cart", "whats in cart", "my order so far",
        "what i have", "show my order",
    ]) or t in ("cart", "my cart", "basket"):
        return "cart"

    if any(w in t for w in [
        "menu", "list", "browse", "show me", "catalog",
        "what do you have", "what do you sell", "products",
        "whats available", "what's available", "show products",
        "what can i order", "what's on the menu",
    ]) or t in ("menu", "list", "catalog"):
        return "browse"

    if (any(w in t for w in ["help", "hi ", "hello", "hey ", "hie", "howzit"])
            or t in ("hi", "hello", "hey", "hie", "yo", "sup", "howzit", "start", "help")):
        return "help"

    return "order"


# ═════════════════════════════════════════════════════════════════════════════
# PROOF OF PAYMENT — detect if message looks like a proof submission
# ═════════════════════════════════════════════════════════════════════════════

# Words that look like txn IDs by length/charset but are NOT proof.
# Extended to prevent common words and intent-words from being misclassified.
_PROOF_SKIP_WORDS = {
    # Payment methods / system words
    "ORDER", "PAYPAL", "ECOCASH", "BITCOIN", "CRYPTO", "PROOF", "IMAGE",
    # Common English words users type (6+ uppercase chars)
    "REFUND", "CANCEL", "THANKS", "SORTED", "CHEERS", "DONEIT",
    "PLEASE", "CHANGE", "RETURN", "UNABLE", "FAILED", "IGNORE",
    "HELPME", "SOMETH", "NEWONE", "REUNDO", "REVERT",
    # Single-word intent expressions (all-caps version)
    "CANCEL", "REFUND", "PAID", "DONE", "SENT",
}

# Real txn IDs almost always:
#   - Are ≥ 8 characters (EcoCash: 10-15, PayPal: 17+)
#   - Contain at least one digit
#   - Are mixed alphanumeric (not purely alphabetic)
_TXN_PATTERN = re.compile(r"\b([A-Z0-9]{8,30})\b")


def _looks_like_txn_id(token: str) -> bool:
    """
    Returns True if token looks like a real transaction ID:
      - 8–30 chars of A-Z and 0-9
      - Contains at least one digit (pure words are not txn IDs)
      - Not in the skip list of common words
    """
    t = token.upper().strip()
    if t in _PROOF_SKIP_WORDS:
        return False
    if not re.fullmatch(r"[A-Z0-9]{8,30}", t):
        return False
    # Must contain at least one digit — real IDs are never purely alphabetic
    if not any(c.isdigit() for c in t):
        return False
    return True


def _is_proof_submission(text: str, message_has_image: bool = False) -> tuple[bool, str]:
    """
    Detect if the customer is submitting payment proof.
    Returns (is_proof: bool, proof_text: str).

    Accepts:
      - WhatsApp image attachments (message_has_image=True)
      - Transaction IDs: ≥ 8 chars, alphanumeric, contains a digit
      - Descriptive proof phrases ("here is my receipt", "transaction 1A2B3C")
    Rejects:
      - Pure English words (REFUND, CANCEL, THANKS, SORTED, etc.)
      - Words shorter than 8 chars passed alone
    """
    if message_has_image:
        return True, "image_attached"

    t = text.strip()
    t_lower = t.lower()

    # ── Check for explicit proof phrases ─────────────────────────────────────
    proof_phrases = [
        "transaction", "reference", "txn", "receipt", "confirmation",
        "screenshot", "transfer id", "payment id", "proof",
        "here is", "here's", "attached",
    ]
    has_proof_phrase = any(p in t_lower for p in proof_phrases)

    # ── Look for transaction ID tokens ────────────────────────────────────────
    found_txn = None
    for match in _TXN_PATTERN.finditer(t.upper()):
        candidate = match.group(1)
        if _looks_like_txn_id(candidate):
            found_txn = candidate
            break

    if found_txn:
        return True, found_txn

    if has_proof_phrase and len(t) > 8:
        return True, t[:120]

    return False, ""


# ═════════════════════════════════════════════════════════════════════════════
# PRODUCT MATCHING
# ═════════════════════════════════════════════════════════════════════════════

def _find_product(text: str, products: list) -> dict | None:
    """
    Multi-strategy product matcher.

    Primary: delegates to fuzzy_matcher.find_product() which uses rapidfuzz
    (when installed) or difflib. Handles spelling mistakes, pluralization,
    case differences, and intent prefixes automatically.

    Fallback (if fuzzy_matcher unavailable): original difflib-based logic.
    """
    if not products:
        return None

    # ── Primary: fuzzy_matcher ────────────────────────────────────────────────
    try:
        result = _fuzzy().find_product(text, products)
        if result:
            return result
    except Exception as exc:
        log.warning("_find_product: fuzzy_matcher failed (%s) — using difflib", exc)

    # ── Fallback: original difflib matching ───────────────────────────────────
    t        = text.lower().strip()
    name_map = {p["name"].lower(): p for p in products}
    names    = list(name_map.keys())

    if t in name_map:
        return name_map[t]

    stripped = re.sub(
        r"^(i want|i'?d like|give me|add|order|get me|can i (?:have|get)|please)\s+",
        "", t, flags=re.IGNORECASE
    ).strip()
    stripped = re.sub(r"^(?:x\s*)?\d+\s+", "", stripped).strip()
    if stripped and stripped != t and stripped in name_map:
        return name_map[stripped]

    for candidate in dict.fromkeys([t, stripped]):
        if not candidate:
            continue
        m = get_close_matches(candidate, names, n=1, cutoff=0.55)
        if m:
            return name_map[m[0]]

    for word in t.split():
        if len(word) < 3:
            continue
        m = get_close_matches(word, names, n=1, cutoff=0.60)
        if m:
            return name_map[m[0]]

    for name, product in name_map.items():
        if name in t:
            return product

    for name, product in name_map.items():
        for part in name.split():
            if len(part) >= 3 and part in t:
                return product

    return None


# ═════════════════════════════════════════════════════════════════════════════
# QUANTITY PARSER
# ═════════════════════════════════════════════════════════════════════════════

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3,
    "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "couple": 2, "few": 3,
}


def _qty(text: str) -> int:
    t = text.lower()
    m = re.search(r"x\s*(\d+)", t)
    if m:
        return max(1, int(m.group(1)))
    m = re.search(r"\b(\d+)\b", t)
    if m:
        return max(1, int(m.group(1)))
    for w in t.split():
        if w in _NUMBER_WORDS:
            return _NUMBER_WORDS[w]
    return 1


# ═════════════════════════════════════════════════════════════════════════════
# RECOMMENDATIONS
# ═════════════════════════════════════════════════════════════════════════════

def _recommend(phone: str, business_id: int, products: list, exclude: str = "") -> list:
    try:
        mem  = _get_memory(phone, business_id)
        freq = mem.get("frequent_items", {})
        recs = [p for p in products if p["name"].lower() != exclude.lower()]
        if freq:
            recs.sort(key=lambda p: freq.get(p["name"], 0), reverse=True)
        return recs[:2]
    except Exception:
        return []


# ═════════════════════════════════════════════════════════════════════════════
# FORMATTERS
# ═════════════════════════════════════════════════════════════════════════════

def _format_cart(cart: list) -> str:
    if not cart:
        return "🛒 Your cart is empty. Type *menu* to see what we have!"
    total = 0.0
    lines = []
    for i in cart:
        sub = i["qty"] * float(i["price"])
        total += sub
        lines.append(f"  • {i['name']} ×{i['qty']}  —  ${sub:.2f}")
    return "🛒 *Your Cart:*\n" + "\n".join(lines) + f"\n\n💰 *Total: ${total:.2f}*"


def _build_payment_menu(cart: list, business_id: int) -> str:
    """Payment method selection message. business_id required for per-business settings."""
    from payments import available_methods
    try:
        pay_settings = crud.get_business_payment_settings(business_id)
    except Exception:
        pay_settings = {}

    cart_summary = _format_cart(cart)
    methods      = available_methods({**pay_settings, "business_id": business_id})

    options: list[str] = []
    num = 1
    for m in methods:
        if m == "ecocash":
            options.append(f"{num}️⃣  *EcoCash* — Dial *151# (Zimbabwe)")
        elif m == "paypal":
            options.append(f"{num}️⃣  *PayPal* — Email or secure link")
        elif m == "cash":
            options.append(f"{num}️⃣  *Cash* — Pay on delivery or pickup")
        num += 1

    return (
        f"{cart_summary}\n\n"
        f"You're almost there! 😊\n\n"
        f"How would you like to pay?\n\n"
        + "\n".join(options) +
        "\n\n_Reply with the number or name — e.g. *1*, *ecocash*, *paypal*, *cash*_\n"
        "_Type *cancel* to go back._"
    )


def _build_confirm_prompt(cart: list) -> str:
    """Double-confirmation message shown before placing the order."""
    cart_summary = _format_cart(cart)
    return (
        f"📋 *Please confirm your order:*\n\n"
        f"{cart_summary}\n\n"
        f"Is this correct? Reply *yes* to continue or *no* to edit your cart.\n"
        f"_Type *cancel* to cancel entirely._"
    )


def _build_payment_instructions(pending: dict, business_id: int, business_name: str) -> str:
    """Re-generate payment instructions from stored pending_payment session."""
    from payments import (
        generate_ecocash_instructions,
        paypal_payment,
        generate_cash_instructions,
    )
    method    = pending.get("method", "cash")
    reference = pending.get("reference", "")
    order_id  = pending.get("order_id")

    # Build a minimal order dict for the gateway
    try:
        pay_settings = crud.get_business_payment_settings(business_id)
    except Exception:
        pay_settings = {}

    # Look up the actual total from DB
    total = 0.0
    try:
        from order_lifecycle import get_order
        ord_row = get_order(order_id)
        if ord_row:
            total = float(ord_row.get("total_price") or 0)
    except Exception:
        pass

    order = {
        "id":            order_id,
        "total_price":   total,
        "business_name": business_name,
        **pay_settings,
    }

    try:
        if method == "ecocash":
            pay = generate_ecocash_instructions(order)
        elif method == "paypal":
            pay = paypal_payment(order)
        else:
            pay = generate_cash_instructions(order)
        return pay.get("message", f"Please complete payment for *{reference}*.")
    except Exception as exc:
        log.error("_build_payment_instructions error: %s", exc)
        return (
            f"💳 Please complete payment for *{reference}*.\n"
            "Contact us if you need the payment details again."
        )


# ═════════════════════════════════════════════════════════════════════════════
# ORDER STATUS LOOKUP
# ═════════════════════════════════════════════════════════════════════════════

# Full order lifecycle with emoji and human-readable labels
_LIFECYCLE_ICONS = {
    "pending":           ("🕐", "Order received — awaiting payment"),
    "awaiting_payment":  ("⏳", "Awaiting payment"),
    "payment_pending":   ("⏳", "Payment pending"),
    "awaiting_confirmation": ("🔍", "Payment under review by our team"),
    "confirmed":         ("✅", "Payment confirmed"),
    "paid":              ("✅", "Payment confirmed"),
    "preparing":         ("👨‍🍳", "Your order is being prepared"),
    "ready":             ("🎉", "Ready for pickup!"),
    "out_for_delivery":  ("🛵", "Out for delivery — on the way!"),
    "delivered":         ("📦", "Delivered — enjoy your meal!"),
    "completed":         ("🎉", "Order completed"),
    "cancelled":         ("❌", "Order cancelled"),
}


def _order_status_message(order_id: int, phone: str, business_id: int) -> str:
    """Look up an order and return a rich formatted status message."""
    try:
        from order_lifecycle import get_order
        order = get_order(order_id)
        if not order:
            return (
                f"❓ I couldn't find *ORDER-{order_id}*.\n\n"
                "Please check the order number and try again, "
                "or type *help* for assistance."
            )

        # Verify this order belongs to this customer or business
        if str(order.get("customer_phone", "")).replace("+", "") != str(phone).replace("+", ""):
            if order.get("business_id") != business_id:
                return f"❓ I couldn't find *ORDER-{order_id}* for your account."

        status         = order.get("status", "pending")
        payment_status = order.get("payment_status", "pending")
        total          = float(order.get("total_price") or 0)
        created        = (order.get("created_at") or "")[:16].replace("T", " ")

        # Determine effective display status
        # payment_status can be more informative than order status
        effective_key = payment_status if payment_status in _LIFECYCLE_ICONS else status
        icon, label   = _LIFECYCLE_ICONS.get(effective_key,
                         _LIFECYCLE_ICONS.get(status, ("📋", status.upper())))

        pay_icon = "✅" if payment_status in ("paid", "confirmed") else "⏳"

        # Build a lifecycle progress bar
        stages  = ["received", "payment", "preparing", "ready", "delivered"]
        s_lower = status.lower()
        p_lower = payment_status.lower()

        if s_lower in ("cancelled",):
            progress = "❌ Cancelled"
        elif s_lower == "delivered" or s_lower == "completed":
            progress = "✅ ✅ ✅ ✅ ✅  Complete!"
        elif s_lower in ("preparing",):
            progress = "✅ ✅ ✅ ⬜ ⬜  Preparing"
        elif s_lower in ("paid", "confirmed") or p_lower in ("paid",):
            progress = "✅ ✅ ⬜ ⬜ ⬜  Preparing soon"
        elif p_lower in ("awaiting_confirmation", "awaiting_payment"):
            progress = "✅ ⏳ ⬜ ⬜ ⬜  Verifying payment"
        else:
            progress = "✅ ⬜ ⬜ ⬜ ⬜  Order received"

        # Human-agent note for payment verification
        agent_note = ""
        if p_lower == "awaiting_confirmation":
            agent_note = "\n🔍 _A team member is reviewing your payment proof._"
        elif p_lower in ("awaiting_payment", "pending") and s_lower == "pending":
            agent_note = "\n⏳ _Waiting for your payment._"

        return (
            f"📋 *Order Status*\n"
            f"{'─' * 26}\n"
            f"  Order   : *ORDER-{order_id}*\n"
            f"  Date    : {created}\n"
            f"  Total   : *${total:.2f}*\n"
            f"{'─' * 26}\n"
            f"{icon} {label}\n"
            f"{pay_icon} Payment : *{payment_status.replace('_', ' ').upper()}*\n"
            f"{'─' * 26}\n"
            f"📊 {progress}"
            f"{agent_note}\n"
            f"{'─' * 26}\n"
            f"_Type *menu* to place a new order._"
        )
    except Exception as exc:
        log.error("_order_status_message error: %s", exc)
        return f"❓ Could not load order *ORDER-{order_id}* right now. Please try again."


# ═════════════════════════════════════════════════════════════════════════════
# CHECKOUT PIPELINE — create order + dispatch payment
# ═════════════════════════════════════════════════════════════════════════════

def _parse_multi_items(text: str, products: list) -> list[tuple]:
    """
    Parse a message that may contain multiple products.
    Returns a list of (product_dict, qty) tuples.

    Handles:
      "Pizza and ice cream"
      "2 beef and a sadza"
      "pizza, ice cream and 2 sadza"
      "pizza + ice cream"
    """
    if not products:
        return []

    # Normalise separators
    t = text.lower().strip()
    # Replace connectors with a pipe for splitting
    for sep in [" and ", ", and ", " & ", " + ", ", "]:
        t = t.replace(sep, "|")

    parts = [p.strip() for p in t.split("|") if p.strip()]
    if len(parts) <= 1:
        # Single item — let normal path handle it
        return []

    found = []
    for part in parts:
        # Use fuzzy matcher for each part — handles spelling/case in multi-items
        product, qty = _fuzzy().extract_product_and_quantity(part, products)
        if product is None:
            product = _find_product(part, products)
            qty = _qty(part) if product else 1
        if product:
            found.append((product, qty))

    # Only return if we found ≥ 2 items (otherwise let normal path handle)
    return found if len(found) >= 2 else []


def _handle_paypal_paid_message(
    phone: str,
    business_id: int,
    business_name: str,
    order_id,
    reference: str,
) -> str:
    """
    Called when a user says "paid" while awaiting a PayPal payment.

    Logic:
      1. Read paypal_order_id from state_data
      2. Call PayPal API to check if payment is COMPLETED
      3a. If paid → mark order, reset state, confirm
      3b. If pending → tell user we're verifying (webhook will fire soon)
      3c. No paypal_order_id → fall back to manual proof flow
    """
    from payments import get_paypal_order_details

    # Get the PayPal order ID we stored when creating the checkout
    state_data     = _read_state_data(phone, business_id)
    paypal_order_id = state_data.get("paypal_order_id", "")

    if not paypal_order_id:
        log.warning("_handle_paypal_paid_message: no paypal_order_id in state  phone=%s", phone)
        # No API order ID — this is manual PayPal email mode, require proof
        _set_awaiting_proof(phone, business_id,
                            order_id=order_id,
                            method="paypal",
                            reference=reference)
        return (
            f"✅ *Got it! Thank you for paying.*\n\n"
            f"To confirm your PayPal payment, please send your *transaction ID* "
            f"or a *screenshot* of the payment.\n\n"
            f"📦 Order: *{reference}*\n\n"
            f"_This helps us verify and process your order. 🙏_"
        )

    # Poll PayPal API for the current status
    try:
        details = get_paypal_order_details(paypal_order_id)
    except Exception as exc:
        log.error("PayPal status check failed: %s", exc)
        details = {"paid": False, "error": str(exc)}

    if details.get("paid"):
        # Payment confirmed — mark order and reset state
        try:
            if order_id:
                crud.update_order_payment(order_id, business_id, {
                    "payment_status":    "paid",
                    "payment_reference": reference,
                })
        except Exception as exc:
            log.warning("PayPal payment status update failed: %s", exc)

        _reset_state(phone, business_id)
        amount = details.get("amount", 0)

        return (
            f"✅ *PayPal Payment Confirmed!*\n\n"
            f"Thank you! Your payment of *${amount:.2f} USD* has been verified.\n\n"
            f"📦 Order : *{reference}*\n"
            f"📍 Status: *CONFIRMED*\n\n"
            f"We're now preparing your order. You'll hear from us shortly! 🙌\n\n"
            f"_Thank you for choosing *{business_name}*!_"
        )

    # Payment not yet completed — webhook will fire when it does
    return (
        f"⏳ *We're verifying your PayPal payment.*\n\n"
        f"📦 Order: *{reference}*\n\n"
        f"This usually only takes a few seconds. You'll receive an automatic "
        f"confirmation message as soon as your payment clears.\n\n"
        f"_No action needed — just wait for our message! 😊_\n"
        f"_Type *cancel* if you want to cancel this order._"
    )


def _process_payment(
    method: str,
    cart: list,
    phone: str,
    business_id: int,
    business_name: str,
) -> str:
    from order_lifecycle import create_order_supabase
    from payments import (
        generate_ecocash_instructions,
        paypal_payment,
        generate_cash_instructions,
    )

    # 1. Create order
    try:
        log.info("checkout  method=%s  phone=%s  items=%d", method, phone, len(cart))
        order = create_order_supabase(
            business_id=business_id,
            customer_phone=phone,
            cart=cart,
            payment_method=method,
        )
        order["business_name"] = business_name
        try:
            pay_settings = crud.get_business_payment_settings(business_id)
            order.update(pay_settings)
        except Exception as exc:
            log.warning("payment settings injection failed: %s", exc)
        log.info("order created  id=%s  method=%s", order.get("id", "?"), method)
    except ValueError as exc:
        log.warning("order blocked: %s", exc)
        return (
            f"⚠️ Couldn't place your order:\n_{exc}_\n\n"
            "Please adjust your cart and try *checkout* again."
        )
    except Exception as exc:
        log.exception("order creation error: %s", exc)
        return (
            "❌ Something went wrong saving your order.\n\n"
            "Your cart is still saved — please try *checkout* again in a moment."
        )

    # 2. Call payment gateway
    try:
        if method == "ecocash":
            pay = generate_ecocash_instructions(order)
        elif method == "paypal":
            pay = paypal_payment(order)
        else:
            pay = generate_cash_instructions(order)
    except Exception as exc:
        log.exception("payment gateway error  method=%s: %s", method, exc)
        pay = {
            "message":   (
                f"⚠️ Payment details couldn't load right now.\n"
                f"Your order *ORDER-{order.get('id', '?')}* is saved.\n"
                "Please contact us to complete payment."
            ),
            "reference": f"ORDER-{order.get('id', '?')}",
            "error":     str(exc),
        }

    # 3. Persist payment fields (including paypal_order_id if available)
    try:
        oid = order.get("id")
        if oid:
            update = {
                "payment_method":    method,
                "payment_status":    "awaiting_payment" if not pay.get("error") else "payment_error",
                "payment_reference": pay.get("reference", f"ORDER-{oid}"),
            }
            if pay.get("url"):
                update["payment_url"] = pay["url"]
            # Store PayPal's order ID so webhook can find this order later
            if pay.get("paypal_order_id"):
                update["paypal_order_id"] = pay["paypal_order_id"]
            crud.update_order_payment(oid, business_id, update)
    except Exception as exc:
        log.warning("update payment details failed: %s", exc)

    # 4. Set state — depends on payment method and whether it's auto-verified
    auto_verified = pay.get("auto_verified", False)

    if method == "cash" or auto_verified:
        # Cash = instant, PayPal API = webhook will confirm
        # Both go to awaiting_payment (not awaiting_proof)
        if method == "cash":
            _reset_state(phone, business_id)
        else:
            # PayPal API: store paypal_order_id in session for status polling
            _set_awaiting_payment(
                phone, business_id,
                order_id=order.get("id"),
                method=method,
                reference=pay.get("reference", f"ORDER-{order.get('id', '?')}"),
            )
            # Also stash paypal_order_id in state for quick lookup
            _write_state_data(phone, business_id, {
                "paypal_order_id": pay.get("paypal_order_id", "")
            })
    else:
        # Manual methods (EcoCash, PayPal email) — stay in awaiting_payment
        # until customer says "paid", then move to awaiting_proof
        _set_awaiting_payment(
            phone, business_id,
            order_id=order.get("id"),
            method=method,
            reference=pay.get("reference", f"ORDER-{order.get('id', '?')}"),
        )

    # 5. Clear cart items (preserves state_data via UPSERT)
    _update_order_history(phone, business_id, cart)
    crud.clear_cart(phone, business_id)

    # 6. PDF invoice (non-blocking)
    _send_pdf_invoice(order, phone, business_id)

    return pay.get("message", "Order placed! We'll be in touch. 🙏")


# ═════════════════════════════════════════════════════════════════════════════
# PDF INVOICE
# ═════════════════════════════════════════════════════════════════════════════

def _send_pdf_invoice(order: dict, phone: str, business_id: int) -> None:
    try:
        from pdf_invoice import generate_pdf_invoice
        pdf_path = generate_pdf_invoice(order)
    except Exception as exc:
        log.error("PDF generation failed: %s", exc)
        return
    try:
        biz      = crud.get_business_by_id(business_id)
        token    = crud.get_decrypted_token(biz) if biz else ""
        phone_id = biz.get("whatsapp_phone_id", "") if biz else ""
        if not token or not phone_id:
            return
        from whatsapp import send_whatsapp_document
        result = send_whatsapp_document(
            phone=phone, file_path=pdf_path,
            access_token=token, phone_number_id=phone_id,
            caption=f"📄 Invoice for ORDER-{order.get('id', '?')}",
        )
        if "error" not in result:
            log.info("PDF invoice sent  order=%s", order.get("id"))
    except Exception as exc:
        log.exception("_send_pdf_invoice error: %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def generate_reply(
    message: str,
    phone: str,
    business_id: int,
    business_name: str,
    products: list,
    message_has_image: bool = False,   # True when WhatsApp message contains an image
) -> str:
    """
    Single entry point called by the webhook for every incoming message.
    message_has_image=True signals that the customer sent a photo (payment proof).
    Returns a WhatsApp-formatted reply string.
    """
    text = message.strip()

    log.info("▶ msg  phone=%s  biz=%s  img=%s  text=%r",
             phone, business_id, message_has_image, text[:80])

    current_state = _get_state(phone, business_id)
    cart          = _load_cart(phone, business_id)

    log.info("state=%s  cart=%d", current_state, len(cart))

    # ══════════════════════════════════════════════════════════════════════════
    # P-3 — HUMAN HANDOFF MODE (AI paused — only ack message, no AI responses)
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "human_handoff":
        from conversation_states import is_ai_paused
        # Safety: always re-check state with the canonical function
        if is_ai_paused(current_state):
            log.info("human_handoff: AI paused  phone=%s", phone)
            return _handoff_mod().handoff_paused_reply()

    # ══════════════════════════════════════════════════════════════════════════
    # P-2.5 — HUMAN HANDOFF REQUEST DETECTION
    # Customer asks for a human agent — pause AI immediately
    # ══════════════════════════════════════════════════════════════════════════
    if _handoff_mod().is_handoff_request(text):
        _set_human_handoff(phone, business_id)
        _handoff_mod().notify_dashboard(phone, business_id, business_name)
        log.info("human_handoff: triggered  phone=%s  biz=%s", phone, business_id)
        return _handoff_mod().handoff_acknowledgement(business_name)

    # ══════════════════════════════════════════════════════════════════════════
    # P-2 — AGENT MESSAGE DETECTION (silence bot when staff posts status)
    # If the incoming text looks like a business-owner/agent status update,
    # do not reply with a fallback — the agent is talking TO the customer.
    # ══════════════════════════════════════════════════════════════════════════
    if _is_agent_message(text):
        log.info("agent message detected — suppressing reply  phone=%s", phone)
        # Return empty string — webhook will not send anything
        return ""

    # ══════════════════════════════════════════════════════════════════════════
    # P-1 — SURVEY STATE (post-conversation satisfaction rating)
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "survey":
        if _is_survey_response(text):
            rating = _parse_survey_rating(text)
            _reset_state(phone, business_id)

            # Log the rating (stored as a note in user_memory for lightweight persistence)
            try:
                mem = _get_memory(phone, business_id)
                mem["last_rating"] = rating
                crud.save_user_memory(phone, business_id, mem)
            except Exception:
                pass

            follow_up = (
                "We're sorry to hear that. We'll work on improving! 🙏"
                if rating in ("poor", "average")
                else "That's wonderful to hear! 😊"
            )
            return (
                f"🙏 *Thank you for your feedback!*\n\n"
                f"Rating: *{rating.title()}*\n\n"
                f"{follow_up}\n\n"
                f"_We look forward to serving you again at *{business_name}*!_"
            )

        # Optional suggestion
        t_lower = text.lower().strip()
        if len(t_lower) > 8 and not _is_conversation_done(text):
            # Treat longer text as a suggestion
            try:
                mem = _get_memory(phone, business_id)
                mem["last_suggestion"] = text[:200]
                crud.save_user_memory(phone, business_id, mem)
            except Exception:
                pass
            _reset_state(phone, business_id)
            return (
                f"📝 *Thank you for your suggestion!*\n\n"
                f"We really appreciate the feedback and will pass it on to our team.\n\n"
                f"_See you next time at *{business_name}*! 🙏_"
            )

        # They said something unrelated — let them exit gracefully
        _reset_state(phone, business_id)
        return (
            f"Thanks again! Have a great day. 😊\n\n"
            f"_Type *menu* anytime to start a new order._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0 — GLOBAL CANCEL  (works in every state)
    # ══════════════════════════════════════════════════════════════════════════
    if _is_cancel(text):
        if current_state == "browsing":
            # Check if the customer means they want to cancel a recent order.
            # "Cancel order" typed after checkout completion is a common pattern.
            t_lower = text.lower()
            order_ref_id = _extract_order_id(text)
            if order_ref_id or any(w in t_lower for w in ["cancel order", "cancel my order"]):
                # Try to find a recent pending order for this customer
                try:
                    from db import supabase as _sb
                    res = (
                        _sb.table("orders")
                        .select("id,status,payment_status,total_price")
                        .eq("customer_phone", phone)
                        .eq("business_id", business_id)
                        .in_("status", ["pending", "confirmed"])
                        .order("id", desc=True)
                        .limit(1)
                        .execute()
                    )
                    recent = res.data[0] if res.data else None
                except Exception:
                    recent = None

                if recent:
                    ref = order_ref_id or recent["id"]
                    return (
                        f"🚫 *Cancel ORDER-{recent['id']}?*\n\n"
                        f"💰 Amount: ${float(recent['total_price']):.2f}\n"
                        f"📍 Status: {recent['status'].upper()}\n\n"
                        f"Reply *yes, cancel* to confirm cancellation, "
                        f"or type anything else to keep your order.\n\n"
                        f"_If you've already paid, reply *refund* and we'll arrange a refund._"
                    )

            return (
                "ℹ️ Nothing to cancel right now.\n\n"
                "Type *menu* to browse, or *cart* to see what's in your cart. 😊"
            )

        if current_state in ("checkout", "confirm_order"):
            _reset_state(phone, business_id)
            return (
                "🚫 *Checkout cancelled.*\n\n"
                f"{_format_cart(cart)}\n\n"
                "Your cart is saved. Type *checkout* whenever you're ready."
            )

        if current_state == "awaiting_payment":
            pending = _get_pending_payment(phone, business_id)
            if pending:
                order_id  = pending.get("order_id")
                reference = pending.get("reference", f"ORDER-{order_id}")
                # Mark order as cancelled
                try:
                    if order_id:
                        crud.update_order_payment(order_id, business_id, {
                            "payment_status": "cancelled",
                        })
                        from order_lifecycle import update_order_status_supabase
                        try:
                            update_order_status_supabase(order_id, "pending")
                        except Exception:
                            pass
                except Exception as exc:
                    log.warning("order cancel update failed: %s", exc)

            _reset_state(phone, business_id)
            return (
                "🚫 *Order cancelled.*\n\n"
                "If you've already sent payment, please contact us immediately "
                "and we'll sort it out.\n\n"
                "Type *menu* to start a new order. 😊"
            )

        if current_state == "awaiting_proof":
            # They want to cancel during proof submission — unusual but handle it
            _reset_state(phone, business_id)
            return (
                "🚫 *Cancelled.*\n\n"
                "If you've already made a payment, please contact us directly "
                "so we can verify and refund if needed.\n\n"
                "Type *menu* to browse. 😊"
            )

        _reset_state(phone, business_id)
        return "🚫 Cancelled. Type *menu* to start fresh. 😊"

    # ══════════════════════════════════════════════════════════════════════════
    # P0.5 — REFUND / DISPUTE REQUEST (works in any state)
    # ══════════════════════════════════════════════════════════════════════════
    if _is_refund_request(text):
        # Look up the customer's most recent order
        recent_order = None
        try:
            from db import supabase as _sb
            res = (
                _sb.table("orders")
                .select("id,status,payment_status,total_price,created_at")
                .eq("customer_phone", phone)
                .eq("business_id", business_id)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            recent_order = res.data[0] if res.data else None
        except Exception as exc:
            log.warning("refund handler: order lookup failed: %s", exc)

        if recent_order:
            ref = f"ORDER-{recent_order['id']}"
            pay_status = recent_order.get("payment_status", "pending")
            total = float(recent_order.get("total_price") or 0)
            return (
                f"💳 *Refund / Dispute Request*\n\n"
                f"We've noted your request regarding *{ref}*.\n\n"
                f"  💰 Amount : ${total:.2f}\n"
                f"  📍 Payment: {pay_status.upper()}\n\n"
                f"Our team will review your request and get back to you shortly.\n\n"
                f"_For urgent issues, please contact us directly. "
                f"Refunds are processed within 24–48 hours once verified._\n\n"
                f"_Thank you for your patience. 🙏_"
            )
        return (
            f"💳 *Refund / Dispute Request*\n\n"
            f"We've noted your request and our team will be in touch shortly.\n\n"
            f"_Please include your order reference (e.g. *ORDER-13*) "
            f"to help us find your payment. Thank you! 🙏_"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0.7 — CONVERSATION COMPLETION DETECTION
    # Detect farewell phrases and trigger optional survey
    # Only trigger when NOT in the middle of an active order flow
    # ══════════════════════════════════════════════════════════════════════════
    if _is_conversation_done(text) and current_state == "browsing":
        # Check if they have a recent completed order — personalise the goodbye
        recent_ref = ""
        try:
            from db import supabase as _sb
            res = (
                _sb.table("orders")
                .select("id,status")
                .eq("customer_phone", phone)
                .eq("business_id", business_id)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            if res.data:
                o = res.data[0]
                if o.get("status") in ("paid", "confirmed", "delivered"):
                    recent_ref = f"ORDER-{o['id']}"
        except Exception:
            pass

        order_line = f"\n📦 Order *{recent_ref}* is being taken care of.\n" if recent_ref else "\n"

        _set_survey_state(phone, business_id)
        return (
            f"😊 *You're welcome! We hope to see you again soon.*\n"
            f"{order_line}\n"
            f"Before you go — how was your experience today?\n\n"
            f"  1️⃣ *Excellent*\n"
            f"  2️⃣ *Good*\n"
            f"  3️⃣ *Average*\n"
            f"  4️⃣ *Poor*\n\n"
            f"_Reply with a number or word — this is optional and helps us improve! 🙏_"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P0.8 — URGENCY / DELIVERY FOLLOW-UP
    # Customer is anxious about their order ("hurry", "how long", "cold food")
    # ══════════════════════════════════════════════════════════════════════════
    if _is_urgency_message(text) and current_state == "browsing":
        # Check if they have a recent active order
        active_order = None
        try:
            from db import supabase as _sb
            res = (
                _sb.table("orders")
                .select("id,status,payment_status,total_price")
                .eq("customer_phone", phone)
                .eq("business_id", business_id)
                .in_("status", ["pending", "confirmed", "paid"])
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            active_order = res.data[0] if res.data else None
        except Exception:
            pass

        if active_order:
            ref    = f"ORDER-{active_order['id']}"
            status = active_order.get("status", "pending").upper()
            return (
                f"⏳ *We hear you! Checking on your order...*\n\n"
                f"📦 Order : *{ref}*\n"
                f"📍 Status: *{status}*\n\n"
                f"Our team has been notified of your message and will update you shortly.\n"
                f"We apologise for any delay! 🙏\n\n"
                f"_Type *{ref.lower()}* to see full order details._"
            )

        return (
            f"⏳ We're sorry you're waiting!\n\n"
            f"Please share your *order reference* (e.g. *ORDER-12*) "
            f"and we'll check the status for you right away. 🙏"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P1 — AWAITING PROOF STATE
    # Customer has said "paid" — now waiting for transaction ID / screenshot
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "awaiting_proof":
        pending_proof = _get_pending_proof(phone, business_id)

        if not pending_proof:
            # Session data lost — gracefully reset
            _reset_state(phone, business_id)
            return (
                "⚠️ I lost track of your payment session.\n\n"
                "Please type *checkout* to start again, or contact us directly."
            )

        order_id  = pending_proof.get("order_id")
        method    = pending_proof.get("method", "unknown")
        reference = pending_proof.get("reference", f"ORDER-{order_id}")

        # Check if message is valid proof
        is_proof, proof_value = _is_proof_submission(text, message_has_image)

        if is_proof:
            # Record the proof
            try:
                proof_note = (
                    f"[IMAGE ATTACHED]" if proof_value == "image_attached"
                    else f"Txn/Proof: {proof_value}"
                )
                if order_id:
                    crud.update_order_payment(order_id, business_id, {
                        "payment_status": "awaiting_confirmation",
                        "payment_reference": f"{reference} | {proof_note}",
                    })
            except Exception as exc:
                log.warning("proof recording failed: %s", exc)

            _reset_state(phone, business_id)

            method_label = {"ecocash": "EcoCash", "paypal": "PayPal", "cash": "Cash"}.get(
                method, method.title()
            )
            proof_display = (
                "📸 *Image received.*"
                if proof_value == "image_attached"
                else f"📋 *Reference noted:* `{proof_value}`"
            )

            return (
                f"✅ *Payment proof received. Thank you!*\n\n"
                f"{proof_display}\n\n"
                f"📦 Order   : *{reference}*\n"
                f"💳 Method  : *{method_label}*\n\n"
                f"🔍 *A human agent is now reviewing your payment proof.*\n"
                f"You'll receive a confirmation message shortly.\n\n"
                f"Typical verification time: *5–15 minutes* ⏱\n\n"
                f"_Thank you for choosing *{business_name}*! We'll be in touch. 🙏_"
            )

        # Message doesn't look like proof — ask again
        method_label = {"ecocash": "EcoCash", "paypal": "PayPal"}.get(method, "payment")
        return (
            f"📋 *We need proof of your {method_label} payment to proceed.*\n\n"
            f"Please send:\n"
            f"  • Your *transaction ID* or *reference number*, OR\n"
            f"  • A *screenshot* of your payment confirmation\n\n"
            f"Order: *{reference}*\n\n"
            f"_Type *cancel* if you haven't paid yet._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P2 — AWAITING PAYMENT STATE
    # Order is placed, waiting for customer to say "paid"
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "awaiting_payment":
        pending = _get_pending_payment(phone, business_id)

        if not pending:
            _reset_state(phone, business_id)
            return (
                "⚠️ I lost your payment session. Please type *checkout* to start again."
            )

        order_id  = pending.get("order_id")
        method    = pending.get("method", "unknown")
        reference = pending.get("reference", f"ORDER-{order_id}")

        # Handle "paid" — behaviour differs by method
        if _is_payment_confirmation(text):
            if method == "paypal":
                # PayPal: check if webhook already confirmed, or poll the API
                return _handle_paypal_paid_message(
                    phone=phone,
                    business_id=business_id,
                    business_name=business_name,
                    order_id=order_id,
                    reference=reference,
                )
            else:
                # EcoCash / manual PayPal email: require proof
                _set_awaiting_proof(phone, business_id,
                                    order_id=order_id,
                                    method=method,
                                    reference=reference)
                method_label = {"ecocash": "EcoCash", "paypal": "PayPal (email)"}.get(method, "payment")
                return (
                    f"✅ *Got it! Thank you for paying.*\n\n"
                    f"To complete your order, please send your *{method_label} "
                    f"transaction ID* or a *screenshot* of your payment.\n\n"
                    f"📦 Order: *{reference}*\n\n"
                    f"_This helps us verify your payment quickly and process your order. 🙏_"
                )

        # Handle image directly in awaiting_payment state
        if message_has_image:
            _set_awaiting_proof(phone, business_id,
                                order_id=order_id,
                                method=method,
                                reference=reference)
            # Re-run as awaiting_proof with image flag
            return generate_reply(
                message="image",
                phone=phone,
                business_id=business_id,
                business_name=business_name,
                products=products,
                message_has_image=True,
            )

        # Handle order reference lookup (e.g. "ORDER-9")
        ref_id = _extract_order_id(text)
        if ref_id:
            return _order_status_message(ref_id, phone, business_id)

        # Re-show payment instructions if user seems confused
        confused_words = {
            "how", "what", "where", "instructions", "again", "resend",
            "send again", "help me", "show me", "details",
        }
        if any(w in text.lower() for w in confused_words):
            instructions = _build_payment_instructions(pending, business_id, business_name)
            return (
                f"{instructions}\n\n"
                f"{'─' * 28}\n"
                f"Once paid, reply *paid* to confirm.\n"
                f"_Type *cancel* to cancel this order._"
            )

        # Anything else — remind them what to do
        return (
            f"⏳ *Waiting for your payment.*\n\n"
            f"📦 Order  : *{reference}*\n\n"
            f"Once you've paid, reply *paid* and then send your "
            f"transaction ID or screenshot.\n\n"
            f"_Need the payment details again? Type *help*._\n"
            f"_To cancel, type *cancel*._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P3 — CONFIRM ORDER STATE (double-confirmation)
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "confirm_order":
        session  = _get_session(phone, business_id)
        snapshot = session.get("cart_snapshot") or cart

        if _is_yes(text):
            # Proceed to payment method selection
            _set_checkout_state(phone, business_id, snapshot)
            return _build_payment_menu(snapshot, business_id)

        if _is_no(text):
            _reset_state(phone, business_id)
            return (
                f"👌 No problem! Take your time.\n\n"
                f"{_format_cart(cart)}\n\n"
                "Type *checkout* when you're ready, or *remove [item]* to edit."
            )

        # Neither yes nor no — re-show confirmation
        return (
            "Please reply *yes* to confirm your order or *no* to go back.\n\n"
            + _format_cart(snapshot)
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P4 — CHECKOUT STATE (payment method selection)
    # ══════════════════════════════════════════════════════════════════════════
    if current_state == "checkout":
        method = _detect_payment_method(text)

        if method in ("ecocash", "paypal", "cash"):
            session     = _get_session(phone, business_id)
            cart_to_use = session.get("cart_snapshot") or cart
            return _process_payment(
                method=method,
                cart=cart_to_use,
                phone=phone,
                business_id=business_id,
                business_name=business_name,
            )

        # Not a valid method
        from payments import available_methods
        try:
            pay_settings = crud.get_business_payment_settings(business_id)
        except Exception:
            pay_settings = {}
        methods = available_methods({**pay_settings, "business_id": business_id})

        opts, num = [], 1
        for m in methods:
            label = {"ecocash": "EcoCash", "paypal": "PayPal", "cash": "Cash on delivery"}.get(m, m)
            opts.append(f"  {num}️⃣  *{label}*")
            num += 1

        return (
            "I didn't catch that — please choose how you'd like to pay:\n\n"
            + "\n".join(opts) +
            "\n\n_Reply with the number or name (e.g. *1*, *ecocash*, *cash*)_\n"
            "_Type *cancel* to go back._"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # General intent detection (browsing state)
    # ══════════════════════════════════════════════════════════════════════════
    intent = _intent(text)
    log.info("intent=%s", intent)

    # ══════════════════════════════════════════════════════════════════════════
    # P5 — CHECKOUT TRIGGER
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "checkout":
        if not cart:
            return (
                "🛒 Your cart is empty!\n\n"
                "Type *menu* to browse, then add something — "
                "e.g. _\"Sadza\"_ or _\"2 Beef\"_"
            )

        # Spam / rate limit check
        if not _check_rate_limit(phone, business_id):
            return _rate_limit_message()

        # Double-confirmation before proceeding
        _set_confirm_state(phone, business_id, cart)
        return _build_confirm_prompt(cart)

    # ══════════════════════════════════════════════════════════════════════════
    # P6 — REMOVE ITEM
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "remove":
        t_lower = text.lower()
        for item in list(cart):
            if item["name"].lower() in t_lower:
                cart.remove(item)
                _save_cart(phone, business_id, cart)
                return f"🗑️ Removed *{item['name']}* from your cart.\n\n{_format_cart(cart)}"
        return f"⚠️ I couldn't find that item in your cart.\n\n{_format_cart(cart)}"

    # ══════════════════════════════════════════════════════════════════════════
    # P7 — ADD TO CART (NLP product match + multi-item parsing)
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "order":

        # ── Multi-item check first ("Pizza and ice cream") ────────────────────
        multi = _parse_multi_items(text, products)
        if multi:
            added_names = []
            blocked     = []
            for product, qty in multi:
                try:
                    fresh = crud.get_product_by_name(business_id, product["name"])
                    if fresh:
                        product = fresh
                except Exception:
                    pass

                product_name = product["name"]
                available    = product.get("stock")
                in_cart      = next((i["qty"] for i in cart if i["name"] == product_name), 0)

                if available is not None and in_cart + qty > available:
                    if available == 0:
                        blocked.append(f"*{product_name}* (out of stock)")
                    else:
                        blocked.append(f"*{product_name}* (only {available} left)")
                    continue

                found = False
                for item in cart:
                    if item["name"] == product_name:
                        item["qty"] += qty
                        found = True
                        break
                if not found:
                    cart.append({"name": product_name, "qty": qty, "price": float(product["price"])})
                added_names.append(f"*{product_name}*" + (f" ×{qty}" if qty > 1 else ""))

            if added_names:
                _save_cart(phone, business_id, cart)
                log.info("multi-add  items=%s  phone=%s", added_names, phone)
                blocked_note = ""
                if blocked:
                    blocked_note = f"\n\n⚠️ Could not add: {', '.join(blocked)}"
                return (
                    f"👍 Added {', '.join(added_names)} to your cart.\n\n"
                    f"{_format_cart(cart)}"
                    f"{blocked_note}"
                    f"\n\n_Type *checkout* when you're ready to order._"
                )
            # If none could be added (all blocked), fall through to single match

        # ── Single item — use extract_product_and_quantity for best accuracy ──
        product, qty = _fuzzy().extract_product_and_quantity(text, products)

        if product is None:
            # Fallback to legacy matcher if fuzzy returns nothing
            product = _find_product(text, products)
            if product:
                qty = _qty(text)

        if product:
            try:
                fresh = crud.get_product_by_name(business_id, product["name"])
                if fresh:
                    product = fresh
            except Exception as exc:
                log.warning("stock refresh failed: %s", exc)

            product_name = product["name"]

            available = product.get("stock")
            if available is not None:
                in_cart = next((i["qty"] for i in cart if i["name"] == product_name), 0)
                if in_cart + qty > available:
                    if available == 0:
                        return (
                            f"😔 *{product_name}* is currently out of stock.\n\n"
                            "Type *menu* to see what's available."
                        )
                    return (
                        f"⚠️ Only *{available}* unit(s) of *{product_name}* available "
                        f"(you already have {in_cart} in your cart)."
                    )

            found = False
            for item in cart:
                if item["name"] == product_name:
                    item["qty"] += qty
                    found = True
                    break
            if not found:
                cart.append({"name": product_name, "qty": qty, "price": float(product["price"])})

            _save_cart(phone, business_id, cart)
            log.info("added  %s ×%d  phone=%s", product_name, qty, phone)

            recs      = _recommend(phone, business_id, products, exclude=product_name)
            qty_label = f" ×{qty}" if qty > 1 else ""
            msg       = (
                f"👍 Nice choice! Added *{product_name}*{qty_label} to your cart.\n\n"
                f"{_format_cart(cart)}"
            )
            if recs:
                msg += "\n\n💡 You might also like " + " or ".join(f"*{r['name']}*" for r in recs) + "."
            msg += "\n\n_Type *checkout* when you're ready to order._"
            return msg

    # ══════════════════════════════════════════════════════════════════════════
    # P8 — CART VIEW
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "cart":
        reply = _format_cart(cart)
        if cart:
            reply += "\n\n_Ready? Type *checkout* to place your order._"
        return reply

    # ══════════════════════════════════════════════════════════════════════════
    # P9 — BROWSE MENU
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "browse":
        if not products:
            return f"📋 *{business_name}*\n\nNo items available yet. Check back soon! 🙏"

        lines = []
        for i, p in enumerate(products):
            note = ""
            s = p.get("stock")
            if s is not None and s <= 5:
                note = f"  ⚠️ _only {s} left_"
            lines.append(f"  {i+1}. *{p['name']}* — ${float(p['price']):.2f}{note}")

        recs     = _recommend(phone, business_id, products)
        rec_text = ""
        if recs:
            rec_text = "\n\n⭐ *You usually order:*\n" + "\n".join(f"  • {r['name']}" for r in recs)

        return (
            f"📋 *{business_name} Menu*\n\n"
            + "\n".join(lines)
            + rec_text
            + "\n\n_Just type an item name to add it — e.g. \"Sadza\" or \"2 Beef\"_"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P10 — ORDER REFERENCE LOOKUP ("ORDER-9", "order 9")
    # ══════════════════════════════════════════════════════════════════════════
    ref_id = _extract_order_id(text)
    if ref_id:
        return _order_status_message(ref_id, phone, business_id)

    # ══════════════════════════════════════════════════════════════════════════
    # P11 — HELP / GREETING
    # ══════════════════════════════════════════════════════════════════════════
    if intent == "help":
        hint = f"*{products[0]['name']}*" if products else "an item"
        return (
            f"👋 Hey! Welcome to *{business_name}*!\n\n"
            f"Here's how to order:\n"
            f"  📋 *menu* — see everything we offer\n"
            f"  🛍️ Type a name — e.g. _{hint}_\n"
            f"  🛒 *cart* — review what you've added\n"
            f"  ✅ *checkout* — place your order\n"
            f"  ❌ *remove [item]* — remove from cart\n"
            f"  🔍 *ORDER-9* — check an order status\n"
            f"  🚫 *cancel* — cancel checkout at any time\n\n"
            f"What can I get you today? 😊"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # P12 — FALLBACK (last resort — one more product match attempt)
    # ══════════════════════════════════════════════════════════════════════════
    product = _find_product(text, products)
    if product:
        return generate_reply(
            message=product["name"],
            phone=phone,
            business_id=business_id,
            business_name=business_name,
            products=products,
        )

    hint = f"e.g. _{products[0]['name']}_" if products else "e.g. _Burger_"
    return (
        f"🤖 I didn't quite get that.\n\n"
        f"Try:\n"
        f"  📋 *menu* — browse products\n"
        f"  🛍️ Type a product name — {hint}\n"
        f"  🛒 *cart* — view your cart\n"
        f"  ✅ *checkout* — place your order\n"
        f"  🔍 *ORDER-9* — check order status\n"
    )
