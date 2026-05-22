"""
human_handoff.py — Human agent handoff system for WaziBot.

When activated, the AI pauses and all customer messages are held for a human
agent to respond to manually via the dashboard inbox.

FLOW
────
  Customer: "talk to human" / "support" / "agent"
    → state set to human_handoff
    → AI sends handoff acknowledgement to customer
    → Dashboard shows customer in "needs human" mode (inbox highlight)
    → Business owner replies manually via dashboard chat

  Business owner: clicks "Return to AI" button in dashboard
    → POST /chat/handoff/{customer_id}/release
    → State reset to browsing
    → AI resumes normal operation

TRIGGERS (customer-facing)
──────────────────────────
  "human", "talk to human", "real person", "agent", "support",
  "help me", "speak to someone", "contact us", "phone number",
  "complaint", "I want to speak to a manager"

ADMIN ENDPOINTS (added to main.py)
───────────────────────────────────
  POST /chat/handoff/{customer_id}/request  — mark as needing human
  POST /chat/handoff/{customer_id}/release  — return customer to AI mode
  GET  /chat/handoff/pending                — list all customers in handoff mode
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)


# ── Trigger detection ─────────────────────────────────────────────────────────

_HANDOFF_EXACT = {
    "human", "agent", "support", "help",
    "talk to human", "real person", "speak to someone",
    "talk to someone", "contact you", "contact us",
    "phone number", "call me", "complaint",
    "speak to manager", "manager", "supervisor",
    "i want to complain", "this is wrong", "wrong order",
    "i need help", "not happy",
}

_HANDOFF_CONTAINS = [
    # Human / person requests
    "talk to human", "talk to a human", "talk to real",
    "real person", "speak to a", "speak to someone",
    "human agent", "support agent", "contact agent",
    "connect me to", "transfer me to",
    # Agent / support
    "want to speak", "want to talk", "would like to speak",
    "would like to talk", "would like to talk to",
    "talk to someone", "speak to manager",
    # Help / complaints
    "phone number", "call me", "need help",
    "not happy", "very unhappy", "this is terrible",
    "i want to complain", "your service",
    "can i talk", "can i speak", "let me speak",
]


def is_handoff_request(text: str) -> bool:
    """
    Returns True if the customer wants to speak to a human agent.
    Checks both exact matches and substring patterns.
    """
    t = text.lower().strip()
    if t in _HANDOFF_EXACT:
        return True
    return any(phrase in t for phrase in _HANDOFF_CONTAINS)


# ── Response messages ─────────────────────────────────────────────────────────

def handoff_acknowledgement(business_name: str) -> str:
    """Message sent to the customer when they are handed off to a human agent."""
    return (
        f"🙋 *Connecting you to our support team...*\n\n"
        f"A member of the *{business_name}* team will be with you shortly.\n\n"
        f"⏱ Typical response time: *5–15 minutes* during business hours.\n\n"
        f"_Your conversation is now with a human agent. "
        f"The AI assistant has been paused._\n\n"
        f"_If this is urgent, please call us directly._"
    )


def ai_resumed_message(business_name: str) -> str:
    """Message sent to the customer when the AI is resumed."""
    return (
        f"🤖 *AI assistant resumed.*\n\n"
        f"You're now chatting with the *{business_name}* ordering assistant again.\n\n"
        f"Type *menu* to browse or *cart* to see your current order. 😊"
    )


def handoff_paused_reply() -> str:
    """
    Short reply when customer sends a message while in human_handoff mode.
    Lets them know a human is on it without the AI hijacking the conversation.
    """
    return (
        "⏳ *Your message has been received.*\n\n"
        "A team member will respond shortly. Please wait. 🙏"
    )


def handoff_customer_message(phone: str, business_id: int) -> str:
    """
    Called when a customer sends a message while in human_handoff mode.

    Smart ack logic:
    - First message after handoff: acknowledge once
    - Subsequent messages: stay silent (return empty string)
      so the human agent conversation is not interrupted by the bot.

    Tracks message count in state_data.handoff_msg_count.
    """
    try:
        from db import supabase
        from datetime import datetime, timezone
        res = (
            supabase.table("carts")
            .select("state_data")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        sd = {}
        if res.data:
            sd = res.data[0].get("state_data") or {}

        count = sd.get("handoff_msg_count", 0)
        sd["handoff_msg_count"] = count + 1

        # Persist updated count
        supabase.table("carts").upsert(
            {
                "phone":       phone,
                "business_id": business_id,
                "state_data":  sd,
                "updated_at":  datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="phone,business_id",
        ).execute()

        if count == 0:
            # First message — send one acknowledgement
            return (
                "⏳ *Your message has been received.*\n\n"
                "Our team member will reply shortly. 🙏"
            )
        # Subsequent messages — silent. Human agent handles it.
        return ""

    except Exception as exc:
        log.warning("handoff_customer_message error: %s", exc)
        # On error, acknowledge once to be safe
        return "⏳ Message received. A team member will respond shortly."


# ── Crud helpers (imported lazily to avoid circular imports) ─────────────────

def notify_dashboard(phone: str, business_id: int, business_name: str) -> None:
    """
    Flag this customer as needing human attention in the database.
    Stores a marker in user_memory so the dashboard can surface it.
    Safe to call even if crud is unavailable.
    """
    try:
        import crud
        mem = crud.get_user_memory(phone, business_id) or {}
        mem["needs_human"] = True
        mem["handoff_business"] = business_name
        crud.save_user_memory(phone, business_id, mem)
        log.info("handoff: flagged in user_memory  phone=%s  biz=%s", phone, business_id)
    except Exception as exc:
        log.error("handoff: notify_dashboard failed: %s", exc)


def clear_handoff_flag(phone: str, business_id: int) -> None:
    """Clear the human-needed flag and reset message counter when AI is resumed."""
    # Reset handoff_msg_count in state_data so next handoff acks correctly
    try:
        from db import supabase
        from datetime import datetime, timezone
        res = (
            supabase.table("carts")
            .select("state_data")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        sd = {}
        if res.data:
            sd = res.data[0].get("state_data") or {}
        sd.pop("handoff_msg_count", None)
        supabase.table("carts").upsert(
            {
                "phone": phone, "business_id": business_id,
                "state_data": sd,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="phone,business_id",
        ).execute()
    except Exception as exc:
        log.warning("clear_handoff_flag: state_data reset failed: %s", exc)

    try:
        import crud
        mem = crud.get_user_memory(phone, business_id) or {}
        mem["needs_human"] = False
        crud.save_user_memory(phone, business_id, mem)
        log.info("handoff: cleared  phone=%s  biz=%s", phone, business_id)
    except Exception as exc:
        log.error("handoff: clear_handoff_flag failed: %s", exc)


def get_pending_handoffs(business_id: int) -> list[dict]:
    """
    Return list of customers currently in human_handoff mode.
    Reads from carts.state_data where state == "human_handoff".
    This is the authoritative source — state is stored per-phone per-business.
    """
    try:
        from db import supabase
        # Fetch all cart rows for this business that have state_data
        res = (
            supabase.table("carts")
            .select("phone, business_id, state_data")
            .eq("business_id", business_id)
            .execute()
        )
        pending = []
        for row in (res.data or []):
            sd = row.get("state_data") or {}
            if sd.get("state") == "human_handoff":
                pending.append({
                    "phone":       row.get("phone"),
                    "business_id": row.get("business_id"),
                    "state":       "human_handoff",
                })
        return pending
    except Exception as exc:
        log.error("get_pending_handoffs error: %s", exc)
        return []
