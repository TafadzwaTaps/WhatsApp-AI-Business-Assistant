"""
services/_ai_state.py — Conversation state management for the AI engine.

All state is stored in carts.state_data (JSONB) — not in user_memory.
This means state survives separate save_user_memory() calls without being wiped.

Imported by ai.py. Do not import ai.py from here (circular import).
"""

import logging
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


# ── Timestamp helper ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Raw state_data read/write ─────────────────────────────────────────────────

def _read_state_data(phone: str, business_id: int) -> dict:
    """Read state_data from carts table. Never raises."""
    try:
        from core.db import supabase
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
        from core.db import supabase
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


# ── State getters ─────────────────────────────────────────────────────────────

def _get_state(phone: str, business_id: int) -> str:
    from services._ai_lazy import _states
    raw = _read_state_data(phone, business_id).get("state", "browsing")
    return _states().normalize_state(raw)


def _get_session(phone: str, business_id: int) -> dict:
    return _read_state_data(phone, business_id).get("session") or {}


def _get_pending_payment(phone: str, business_id: int) -> dict | None:
    return _read_state_data(phone, business_id).get("pending_payment") or None


def _get_pending_proof(phone: str, business_id: int) -> dict | None:
    return _read_state_data(phone, business_id).get("pending_proof") or None


# ── State setters ─────────────────────────────────────────────────────────────

def _set_state(phone: str, business_id: int, state: str, **extra) -> None:
    patch = {"state": state}
    patch.update(extra)
    _write_state_data(phone, business_id, patch)


def _set_checkout_state(phone: str, business_id: int, cart_snapshot: list) -> None:
    from services.conversation_service import can_transition, STATE
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
    from services.conversation_service import can_transition, STATE
    current = _get_state(phone, business_id)
    if not can_transition(current, STATE.AWAITING_PAYMENT):
        log.warning("_set_awaiting_payment: invalid transition %s→awaiting_payment  phone=%s",
                    current, phone)
    _set_state(phone, business_id, "awaiting_payment",
               session={},
               pending_payment={"order_id": order_id, "method": method, "reference": reference},
               pending_proof=None)


def _set_awaiting_proof(phone: str, business_id: int,
                         order_id, method: str, reference: str) -> None:
    """Enter awaiting_proof state — customer must provide txn ID or image."""
    from services.conversation_service import can_transition, STATE
    current = _get_state(phone, business_id)
    if not can_transition(current, STATE.AWAITING_PROOF):
        log.warning("_set_awaiting_proof: invalid transition %s→awaiting_proof  phone=%s",
                    current, phone)
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


def _set_order_preview_state(phone: str, business_id: int, cart_lines: list) -> None:
    """
    Enter order_preview state: customer has been shown a parsed order
    and must reply 'yes' to add it to their cart.
    cart_lines: the ParsedOrder.cart_lines() result.
    """
    _set_state(phone, business_id, "order_preview",
               session={"preview_cart": cart_lines})


def _set_awaiting_fulfillment(phone: str, business_id: int,
                               order_id, reference: str) -> None:
    """Enter awaiting_fulfillment state — asking delivery vs pickup."""
    _set_state(phone, business_id, "awaiting_fulfillment",
               session={"order_id": order_id, "reference": reference},
               pending_payment=None, pending_proof=None)


def _set_awaiting_address(phone: str, business_id: int,
                           order_id, reference: str) -> None:
    """Enter awaiting_address state — customer needs to provide delivery address."""
    _set_state(phone, business_id, "awaiting_address",
               session={"order_id": order_id, "reference": reference})


def _set_human_handoff(phone: str, business_id: int) -> None:
    """Pause AI and flag this customer for human agent attention."""
    _set_state(phone, business_id, "human_handoff",
               session={}, pending_payment=None, pending_proof=None)


# ── Convenience checkers ──────────────────────────────────────────────────────

def _in_survey_state(phone: str, business_id: int) -> bool:
    return _get_state(phone, business_id) == "survey"


# ── Checkout rate limiting ────────────────────────────────────────────────────

_CHECKOUT_RATE_WINDOW = 600   # 10 minutes
_CHECKOUT_RATE_LIMIT  = 5     # max checkouts per window


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


# ── Active order lookup ───────────────────────────────────────────────────────

def _get_active_order(phone: str, business_id: int) -> dict | None:
    """
    Find the most recent active (non-completed, non-cancelled) order for
    this customer. Used for contextual intent detection.
    Returns the order dict or None.
    """
    try:
        from core.db import supabase as _sb
        res = (
            _sb.table("orders")
            .select("*")
            .eq("customer_phone", phone)
            .eq("business_id", business_id)
            .not_.in_("status", ["completed", "cancelled", "refunded", "delivered"])
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        log.warning("_get_active_order error: %s", exc)
        return None
