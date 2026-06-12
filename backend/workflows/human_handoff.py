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
import time
from typing import Optional

log = logging.getLogger(__name__)

# ── Timeout configuration ─────────────────────────────────────────────────────
# After this many seconds with no agent activity, the AI auto-resumes.
# The timer resets whenever the agent sends a message from the dashboard.
# Set to 0 to disable auto-resume entirely.
HANDOFF_TIMEOUT_SECONDS = int(60 * 45)   # 45 minutes by default

# Words that should always escape human_handoff and return to AI,
# even if no agent has explicitly released the session.
AUTO_RESUME_INTENTS = {
    "hi", "hello", "hey", "hie", "yo", "start",
    "menu", "order", "cart", "checkout",
    "help", "back", "nevermind", "never mind",
    "resume", "ai", "bot", "restart", "reset",
}


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


def should_auto_resume(text: str, sd: dict) -> tuple[bool, str]:
    """
    Determine whether the AI should automatically resume from human_handoff.

    Returns (should_resume: bool, reason: str).

    Auto-resume triggers:
    1. Customer types a restart intent ("menu", "hi", "cart" etc.)
       — SKIPPED if an agent manually initiated this handoff
         (session.agent_initiated == True). In that case only the
         "▶ Resume AI" button or the timeout below can end it —
         a customer typing "menu" while waiting for the agent should
         not silently kick the agent out of the conversation.
    2. HANDOFF_TIMEOUT_SECONDS has elapsed since handoff was set
       and no agent has replied recently (last_agent_reply_at is old/absent)
    """
    session = sd.get("session") or {}
    agent_initiated = bool(session.get("agent_initiated"))

    # Trigger 1: restart intent keyword (skipped for agent-initiated handoffs)
    if not agent_initiated:
        t_lower = text.lower().strip()
        if t_lower in AUTO_RESUME_INTENTS:
            log.info("auto_resume: restart intent detected  word=%r", t_lower)
            return True, f"customer restart intent: {t_lower!r}"
    else:
        log.debug("auto_resume: skipping restart-intent check — agent_initiated handoff")

    # Trigger 2: timeout elapsed
    if HANDOFF_TIMEOUT_SECONDS > 0:
        handoff_at = sd.get("handoff_started_at", 0)
        if handoff_at:
            elapsed = time.time() - float(handoff_at)
            if elapsed > HANDOFF_TIMEOUT_SECONDS:
                log.info(
                    "auto_resume: timeout elapsed  elapsed=%.0fs  limit=%ds",
                    elapsed, HANDOFF_TIMEOUT_SECONDS,
                )
                return True, f"timeout after {elapsed:.0f}s"

    return False, ""


def handoff_customer_message(phone: str, business_id: int, text: str = "") -> str:
    """
    Called when a customer sends a message while in human_handoff mode.

    Logic:
    1. Check auto-resume conditions (restart intent OR timeout elapsed).
       If triggered → reset state to browsing and return "" so generate_reply
       continues processing the message normally.
    2. First message after handoff: acknowledge once.
    3. Subsequent messages: stay silent so the human agent's conversation
       is not interrupted by bot noise.

    Tracks:
      state_data.handoff_msg_count    — number of customer messages since handoff
      state_data.handoff_started_at   — Unix timestamp when handoff was activated
      state_data.last_agent_reply_at  — Unix timestamp of last agent reply
    """
    try:
        from core.db import supabase
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

        # ── Auto-resume check ─────────────────────────────────────────────────
        resume, reason = should_auto_resume(text, sd)
        if resume:
            log.info(
                "handoff auto-resume  phone=%s  biz=%s  reason=%s",
                phone, business_id, reason,
            )
            # Reset to browsing — let generate_reply handle this message normally
            sd["state"]              = "browsing"
            sd["handoff_msg_count"]  = 0
            sd.pop("handoff_started_at",  None)
            sd.pop("last_agent_reply_at", None)
            # Clear handoff session flags (agent_initiated, reason, priority)
            # so they don't linger into the resumed "browsing" state.
            if isinstance(sd.get("session"), dict):
                sd["session"].pop("agent_initiated",  None)
                sd["session"].pop("agent_name",       None)
                sd["session"].pop("handoff_reason",   None)
                sd["session"].pop("handoff_priority", None)
            supabase.table("carts").upsert(
                {
                    "phone":       phone,
                    "business_id": business_id,
                    "state_data":  sd,
                    "updated_at":  datetime.now(timezone.utc).isoformat(),
                },
                on_conflict="phone,business_id",
            ).execute()
            # Return special sentinel so ai.py knows to re-run generate_reply
            return "__AUTO_RESUMED__"

        # ── Normal handoff handling ───────────────────────────────────────────
        count = sd.get("handoff_msg_count", 0)
        sd["handoff_msg_count"] = count + 1

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
            # First message after handoff — acknowledge once
            log.info(
                "handoff first ack  phone=%s  biz=%s",
                phone, business_id,
            )
            return (
                "⏳ *Your message has been received.*\n\n"
                "Our team member will reply shortly. 🙏\n\n"
                "_To return to the AI assistant, type *menu* or *hi* at any time._"
            )

        # Subsequent messages — stay silent, agent handles it
        log.debug(
            "handoff silent  phone=%s  biz=%s  msg_count=%d",
            phone, business_id, count,
        )
        return ""

    except Exception as exc:
        log.warning("handoff_customer_message error: %s  — returning safe fallback", exc)
        # On any DB error, acknowledge rather than silently dropping
        return "⏳ Message received. A team member will respond shortly."


def record_agent_reply(phone: str, business_id: int) -> None:
    """
    Record that a human agent has replied to this customer.
    Called from /chat/send endpoint so the timeout knows activity is ongoing.
    Stores a Unix timestamp in state_data.last_agent_reply_at.
    """
    try:
        from core.db import supabase
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
        sd["last_agent_reply_at"] = time.time()
        supabase.table("carts").upsert(
            {
                "phone": phone, "business_id": business_id,
                "state_data": sd,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="phone,business_id",
        ).execute()
        log.debug("record_agent_reply  phone=%s  biz=%s", phone, business_id)
    except Exception as exc:
        log.warning("record_agent_reply error: %s", exc)


# ── Crud helpers (imported lazily to avoid circular imports) ─────────────────

def notify_dashboard(phone: str, business_id: int, business_name: str) -> None:
    """
    Flag this customer as needing human attention.
    Stores a marker in user_memory AND records handoff_started_at timestamp
    in state_data so the auto-resume timeout has a reference point.
    Safe to call even if crud is unavailable.
    """
    # Store handoff start timestamp in state_data for timeout tracking
    try:
        from core.db import supabase
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
        sd["handoff_started_at"] = time.time()   # Unix timestamp for timeout calc
        sd["handoff_msg_count"]  = 0              # Reset ack counter
        supabase.table("carts").upsert(
            {
                "phone": phone, "business_id": business_id,
                "state_data": sd,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="phone,business_id",
        ).execute()
        log.info(
            "handoff: started_at stamped  phone=%s  biz=%s  timeout=%ds",
            phone, business_id, HANDOFF_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        log.warning("handoff: timestamp stamp failed: %s", exc)

    # Store needs_human flag in user_memory for dashboard surfacing
    try:
        import crud
        mem = crud.get_user_memory(phone, business_id) or {}
        mem["needs_human"] = True
        mem["handoff_business"] = business_name
        crud.save_user_memory(phone, business_id, mem)
        log.info("handoff: flagged in user_memory  phone=%s  biz=%s", phone, business_id)
    except Exception as exc:
        log.error("handoff: notify_dashboard failed: %s", exc)

    # Increment unread badge on customer record so inbox shows the notification
    try:
        from core.db import supabase as _sb
        from datetime import datetime, timezone
        # Find customer record and bump unread_count
        res = (
            _sb.table("customers")
            .select("id, unread_count")
            .eq("phone", phone)
            .eq("business_id", business_id)
            .limit(1)
            .execute()
        )
        if res.data:
            cust = res.data[0]
            new_count = int(cust.get("unread_count") or 0) + 1
            _sb.table("customers").update({
                "unread_count": new_count,
                "last_seen":    datetime.now(timezone.utc).isoformat(),
            }).eq("id", cust["id"]).execute()
            log.debug("handoff: unread incremented  customer=%s  count=%d", cust["id"], new_count)
    except Exception as exc:
        log.debug("handoff: unread increment failed (non-fatal): %s", exc)


def clear_handoff_flag(phone: str, business_id: int) -> None:
    """Clear the human-needed flag and reset message counter when AI is resumed."""
    # Reset handoff_msg_count in state_data so next handoff acks correctly
    try:
        from core.db import supabase
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

    Includes customer_id (looked up from the customers table) so the
    frontend can match against currentCustomerId and conversation lists.
    """
    try:
        from core.db import supabase
        # Fetch all cart rows for this business that have state_data
        res = (
            supabase.table("carts")
            .select("phone, business_id, state_data, updated_at")
            .eq("business_id", business_id)
            .execute()
        )
        handoff_rows = []
        for row in (res.data or []):
            sd = row.get("state_data") or {}
            if sd.get("state") == "human_handoff":
                handoff_rows.append(row)

        if not handoff_rows:
            return []

        # Look up customer_id for each phone in one query
        phones = [r["phone"] for r in handoff_rows]
        cust_res = (
            supabase.table("customers")
            .select("id, phone, customer_name, unread_count")
            .eq("business_id", business_id)
            .in_("phone", phones)
            .execute()
        )
        cust_by_phone = {c["phone"]: c for c in (cust_res.data or [])}

        pending = []
        for row in handoff_rows:
            phone = row.get("phone")
            sd    = row.get("state_data") or {}
            cust  = cust_by_phone.get(phone, {})
            handoff_started_at = sd.get("handoff_started_at")
            wait_seconds = None
            if handoff_started_at:
                try:
                    wait_seconds = max(0, int(time.time() - float(handoff_started_at)))
                except Exception:
                    wait_seconds = None
            pending.append({
                "customer_id":  cust.get("id"),
                "id":           cust.get("id"),  # alias for frontend compatibility
                "phone":        phone,
                "business_id":  row.get("business_id"),
                "customer_name": cust.get("customer_name") or "",
                "unread_count": cust.get("unread_count") or 0,
                "handoff_reason": sd.get("handoff_reason", ""),
                "wait_seconds": wait_seconds,
                "state":        "human_handoff",
            })
        return pending
    except Exception as exc:
        log.error("get_pending_handoffs error: %s", exc)
        return []
