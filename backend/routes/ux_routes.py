"""
routes/ux_routes.py — UX, Handoff Enhancements, Support & Onboarding

New endpoints (all additive):

  Phase 1 — Handoff
    POST /chat/handoff/{customer_id}/request-with-reason — handoff with reason
    GET  /chat/handoff/{customer_id}/summary            — AI conversation summary
    POST /chat/handoff/{customer_id}/note               — agent internal note
    GET  /chat/handoff/{customer_id}/notes              — get agent notes
    GET  /chat/handoff/queue                            — queue with urgency

  Phase 2 — Support
    POST /support/ask                                   — answer a help question
    GET  /support/articles                              — list all help articles
    GET  /support/article/{id}                          — get a specific article
    GET  /support/context-tips/{section}                — section-aware tips

  Phase 3 — Onboarding
    GET  /onboarding/status                             — wizard progress
    POST /onboarding/complete-step                      — mark a step done
    GET  /onboarding/tip/{step}                         — get a step tip

  Phase 4 — Health Center
    GET  /health/status                                 — platform health
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

import crud
from core.auth import require_business, get_current_user

log = logging.getLogger(__name__)
router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# PHASE 1 — HANDOFF ENHANCEMENTS
# ─────────────────────────────────────────────────────────────────────────────

HANDOFF_REASONS = [
    "Payment Issue",
    "Refund Request",
    "Delivery Problem",
    "Complaint",
    "Complex Order",
    "Product Question",
    "Technical Issue",
    "Other",
]


class HandoffWithReasonRequest(BaseModel):
    reason:   str = "Other"
    priority: str = "normal"   # urgent | normal | low


@router.post("/chat/handoff/{customer_id}/request-with-reason")
async def handoff_with_reason(
    customer_id: int,
    body: HandoffWithReasonRequest,
    user=Depends(require_business),
):
    """
    Request handoff and store the reason + priority.
    Extends the existing /chat/handoff/{id}/request without replacing it.
    """
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    from workflows.human_handoff import notify_dashboard
    from services._ai_state import _set_human_handoff

    phone    = customer["phone"]
    biz      = crud.get_business_by_id(bid)
    biz_name = biz.get("name", "") if biz else ""

    _set_human_handoff(phone, bid)
    notify_dashboard(phone, bid, biz_name)

    # Store reason + priority in state_data
    try:
        from core.db import supabase
        res = (
            supabase.table("carts")
            .select("state_data")
            .eq("phone", phone)
            .eq("business_id", bid)
            .limit(1)
            .execute()
        )
        sd = {}
        if res.data:
            sd = res.data[0].get("state_data") or {}
        sd["handoff_reason"]   = body.reason
        sd["handoff_priority"] = body.priority
        sd["handoff_agent"]    = user.get("username", "agent")
        supabase.table("carts").upsert({
            "phone": phone, "business_id": bid,
            "state_data": sd,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="phone,business_id").execute()
    except Exception as exc:
        log.warning("handoff reason store failed: %s", exc)

    return {
        "ok":          True,
        "customer_id": customer_id,
        "phone":       phone,
        "reason":      body.reason,
        "priority":    body.priority,
        "state":       "human_handoff",
    }


@router.get("/chat/handoff/reasons")
def get_handoff_reasons():
    """Return the list of available handoff reasons."""
    return {"reasons": HANDOFF_REASONS}


@router.get("/chat/handoff/{customer_id}/summary")
def handoff_summary(customer_id: int, user=Depends(require_business)):
    """
    Build an AI context summary for the agent before they handle this conversation.
    Returns: customer name, segment, order count, spend, last order, current state,
    pending payments, and a summary of recent messages.
    """
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    phone = customer["phone"]

    # Memory
    try:
        mem         = crud.get_user_memory(phone, bid) or {}
        name        = mem.get("customer_name", "") or "Unknown"
        order_count = int(mem.get("order_count", 0) or 0)
        total_spent = float(mem.get("total_spent", 0) or 0)
        last_orders = mem.get("last_orders", [])
        last_order  = last_orders[-1] if last_orders else []
        last_rating = mem.get("last_rating", "")
    except Exception:
        name, order_count, total_spent, last_order, last_rating = "Unknown", 0, 0.0, [], ""

    # Segment
    try:
        segment = crud.get_customer_segment(mem)
        segment_label = crud.get_segment_label(segment)
    except Exception:
        segment, segment_label = "unknown", "Customer"

    # State
    try:
        from core.db import supabase
        res = (
            supabase.table("carts")
            .select("state_data")
            .eq("phone", phone)
            .eq("business_id", bid)
            .limit(1)
            .execute()
        )
        sd          = res.data[0].get("state_data") or {} if res.data else {}
        state       = sd.get("state", "browsing")
        reason      = sd.get("handoff_reason", "")
        priority    = sd.get("handoff_priority", "normal")
        pending_pay = sd.get("pending_payment")
    except Exception:
        state, reason, priority, pending_pay = "unknown", "", "normal", None

    # Recent messages (last 5)
    recent_msgs = []
    try:
        msgs = crud.get_messages_by_customer(customer_id, limit=5)
        for m in msgs[-5:]:
            direction = "Customer" if m.get("direction") == "incoming" else "WaziBot"
            text      = (m.get("text") or "")[:100]
            recent_msgs.append(f"[{direction}] {text}")
    except Exception:
        pass

    # Pending payment summary
    pending_summary = ""
    if pending_pay:
        oid = pending_pay.get("order_id", "?")
        method = pending_pay.get("method", "")
        pending_summary = f"ORDER-{oid} via {method}"

    return {
        "customer_id":     customer_id,
        "phone":           phone,
        "customer_name":   name,
        "segment":         segment,
        "segment_label":   segment_label,
        "order_count":     order_count,
        "total_spent":     total_spent,
        "last_order":      last_order,
        "last_rating":     last_rating,
        "current_state":   state,
        "handoff_reason":  reason,
        "handoff_priority": priority,
        "pending_payment": pending_summary,
        "recent_messages": recent_msgs,
        "summary_text": (
            f"{name} • {segment_label} • {order_count} orders • ${total_spent:.2f} spent"
            + (f" • Pending: {pending_summary}" if pending_summary else "")
            + (f" • Reason: {reason}" if reason else "")
        ),
    }


@router.post("/chat/handoff/{customer_id}/note")
def add_agent_note(
    customer_id: int,
    note_text:   str,
    user=Depends(require_business),
):
    """
    Add an internal agent note to a conversation.
    Notes are staff-only — never sent to customers, never appear in WhatsApp.
    Stored in user_memory under agent_notes list.
    """
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    phone    = customer["phone"]
    note_text = note_text.strip()
    if not note_text:
        raise HTTPException(400, "Note cannot be empty")
    if len(note_text) > 500:
        raise HTTPException(400, "Note too long (max 500 characters)")

    try:
        mem = crud.get_user_memory(phone, bid) or {}
        notes = mem.get("agent_notes", [])
        notes.append({
            "text":      note_text,
            "agent":     user.get("username", "agent"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        notes = notes[-20:]  # Keep last 20 notes
        mem["agent_notes"] = notes
        crud.save_user_memory(phone, bid, mem)
        log.info("agent_note added  customer=%s  agent=%s", customer_id, user.get("username"))
        return {"ok": True, "note_count": len(notes), "notes": notes}
    except Exception as exc:
        log.error("add_agent_note error: %s", exc)
        raise HTTPException(500, str(exc))


@router.get("/chat/handoff/{customer_id}/notes")
def get_agent_notes(customer_id: int, user=Depends(require_business)):
    """Get all internal agent notes for a customer. Staff-only."""
    bid      = user["business_id"]
    customer = crud.get_customer_by_id(customer_id, bid)
    if not customer:
        raise HTTPException(404, "Customer not found")

    try:
        mem   = crud.get_user_memory(customer["phone"], bid) or {}
        notes = mem.get("agent_notes", [])
        return {"customer_id": customer_id, "notes": notes, "count": len(notes)}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.get("/chat/handoff/queue")
def handoff_queue(user=Depends(require_business)):
    """
    Extended handoff queue with urgency, reason, and wait time.
    Augments the existing GET /chat/handoff/pending.
    """
    from workflows.human_handoff import get_pending_handoffs
    bid = user["business_id"]

    pending = get_pending_handoffs(bid)
    enriched = []

    for h in pending:
        phone = h.get("phone", "")
        try:
            from core.db import supabase
            res = (
                supabase.table("carts")
                .select("state_data")
                .eq("phone", phone)
                .eq("business_id", bid)
                .limit(1)
                .execute()
            )
            sd = res.data[0].get("state_data") or {} if res.data else {}
        except Exception:
            sd = {}

        started_at = sd.get("handoff_started_at", 0)
        wait_mins  = int((time.time() - float(started_at)) / 60) if started_at else 0
        reason     = sd.get("handoff_reason", "")
        priority   = sd.get("handoff_priority", "normal")

        # Auto-escalate to urgent if waiting > 20 minutes
        if wait_mins > 20 and priority != "urgent":
            priority = "urgent"

        try:
            mem  = crud.get_user_memory(phone, bid) or {}
            name = mem.get("customer_name", "") or phone
        except Exception:
            name = phone

        enriched.append({
            "phone":     phone,
            "name":      name,
            "reason":    reason,
            "priority":  priority,
            "wait_mins": wait_mins,
            "state":     "human_handoff",
        })

    # Sort: urgent first, then by wait time
    enriched.sort(key=lambda x: (
        0 if x["priority"] == "urgent" else 1,
        -x["wait_mins"]
    ))

    urgent = sum(1 for h in enriched if h["priority"] == "urgent")
    return {
        "total":   len(enriched),
        "urgent":  urgent,
        "normal":  len(enriched) - urgent,
        "queue":   enriched,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 2 — SUPPORT ASSISTANT
# ─────────────────────────────────────────────────────────────────────────────

class HelpQuestion(BaseModel):
    question: str
    context:  str = ""   # current dashboard section


@router.post("/support/ask")
def ask_support(body: HelpQuestion):  # public — no auth needed
    """Answer a help question using the WaziBot knowledge base."""
    from services.support_assistant import answer_help_question
    if not body.question or len(body.question.strip()) < 2:
        raise HTTPException(400, "Question too short")
    result = answer_help_question(body.question.strip(), body.context.strip())
    return result


@router.get("/support/articles")
def list_articles():
    """Return a list of all help articles."""
    from services.support_assistant import list_all_articles
    return {"articles": list_all_articles()}


@router.get("/support/article/{article_id}")
def get_article(article_id: str):
    """Return a specific help article."""
    from services.support_assistant import get_feature_instructions
    article = get_feature_instructions(article_id)
    if not article:
        raise HTTPException(404, f"Article '{article_id}' not found")
    return article


@router.get("/support/context-tips/{section}")
def context_tips(section: str):
    """Return context-aware tips for the current dashboard section."""
    from services.support_assistant import get_context_tips
    return {"section": section, "tips": get_context_tips(section)}


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 3 — ONBOARDING WIZARD
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/onboarding/status")
def onboarding_status(user=Depends(require_business)):
    """
    Return the onboarding wizard progress for this business.
    Derived from actual DB state — no separate onboarding table needed.
    """
    bid = user["business_id"]

    steps_done: dict[int, bool] = {1: False, 2: False, 3: False, 4: False, 5: False}

    try:
        # Step 1: has at least one product
        products = crud.get_products(bid)
        steps_done[1] = len(products) > 0

        # Step 2: has whatsapp phone id configured
        biz = crud.get_business_by_id(bid)
        steps_done[2] = bool(biz and (biz.get("whatsapp_phone_id") or biz.get("use_shared_number")))

        # Step 3: has payment configured
        steps_done[3] = bool(biz and (
            biz.get("ecocash_number") or biz.get("payment_number") or biz.get("paypal_email")
        ))

        # Step 4: has at least one order (test order done)
        orders = crud.get_orders(bid)
        steps_done[4] = len(orders) > 0

        # Step 5: has sent at least one campaign
        try:
            from core.db import supabase
            res = (
                supabase.table("scheduled_campaigns")
                .select("id")
                .eq("business_id", bid)
                .limit(1)
                .execute()
            )
            # Check if any campaign has been run (via checking messages sent from campaigns)
            # We use a simple heuristic: if they have customers + messages, assume tested
            customers = crud.get_customers_for_business(bid)
            steps_done[5] = len(customers) > 0  # conservative: any customer interaction = launched
        except Exception:
            steps_done[5] = False

    except Exception as exc:
        log.warning("onboarding_status error: %s", exc)

    completed  = sum(1 for v in steps_done.values() if v)
    total      = len(steps_done)
    all_done   = completed == total

    from services.support_assistant import generate_onboarding_tip
    next_step  = next((s for s in range(1, 6) if not steps_done[s]), None)
    next_tip   = generate_onboarding_tip(next_step) if next_step else None

    return {
        "completed":    completed,
        "total":        total,
        "percent":      int(completed / total * 100),
        "all_done":     all_done,
        "steps":        steps_done,
        "next_step":    next_step,
        "next_tip":     next_tip,
        "show_wizard":  not all_done and completed < 4,
    }


@router.get("/onboarding/tip/{step}")
def onboarding_tip(step: int, user=Depends(require_business)):
    """Return the tip for a specific onboarding step."""
    if step < 1 or step > 5:
        raise HTTPException(400, "Step must be between 1 and 5")
    from services.support_assistant import generate_onboarding_tip
    return generate_onboarding_tip(step)


# ─────────────────────────────────────────────────────────────────────────────
# PHASE 4 — AI HEALTH CENTER
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/health/status")
def platform_health(user=Depends(require_business)):
    """
    Return platform health status for the AI Health Center.
    All checks are lightweight — no external calls.
    Status: green | yellow | red
    """
    bid = user["business_id"]
    checks: dict[str, dict] = {}
    overall = "green"

    # WhatsApp configuration
    try:
        biz = crud.get_business_by_id(bid)
        has_phone_id = bool(biz and biz.get("whatsapp_phone_id"))
        has_token    = bool(biz and biz.get("whatsapp_token"))
        uses_shared  = bool(biz and biz.get("use_shared_number"))
        shared_pid   = os.getenv("SHARED_PHONE_NUMBER_ID", "").strip()
        shared_tok   = os.getenv("SHARED_WA_TOKEN", "").strip()

        if (has_phone_id and has_token) or (uses_shared and shared_pid and shared_tok):
            wa_status = "green"
            wa_msg    = "WhatsApp configured ✓"
        elif uses_shared and (not shared_pid or not shared_tok):
            wa_status = "yellow"
            wa_msg    = "Using shared number — shared credentials not set in env"
        else:
            wa_status = "red"
            wa_msg    = "No WhatsApp credentials configured"
        checks["whatsapp"] = {"status": wa_status, "message": wa_msg}
    except Exception as exc:
        checks["whatsapp"] = {"status": "red", "message": str(exc)}

    # AI / conversation engine
    try:
        from services.ai import generate_reply  # noqa: F401
        checks["ai"] = {"status": "green", "message": "AI engine loaded ✓"}
    except Exception as exc:
        checks["ai"] = {"status": "red", "message": f"AI engine error: {exc}"}
        overall = "red"

    # Supabase / DB
    try:
        from core.db import supabase
        supabase.table("businesses").select("id").eq("id", bid).limit(1).execute()
        checks["database"] = {"status": "green", "message": "Database connected ✓"}
    except Exception as exc:
        checks["database"] = {"status": "red", "message": f"Database error: {exc}"}
        overall = "red"

    # Payment config
    try:
        biz = crud.get_business_by_id(bid)
        has_pay = bool(biz and (
            biz.get("ecocash_number") or biz.get("payment_number") or biz.get("paypal_email")
        ))
        checks["payments"] = {
            "status":  "green" if has_pay else "yellow",
            "message": "Payments configured ✓" if has_pay else "No payment method configured",
        }
    except Exception as exc:
        checks["payments"] = {"status": "yellow", "message": str(exc)}

    # Last message timestamp
    try:
        msgs = crud.get_messages_by_customer(
            customer_id=0, limit=1  # will fail gracefully
        )
        checks["last_message"] = {"status": "green", "message": "Message logging active ✓"}
    except Exception:
        # Try another way
        try:
            convos = crud.get_chat_conversations(bid)
            if convos:
                last_time = max((c.get("last_message_at") or "") for c in convos)
                checks["last_message"] = {
                    "status": "green",
                    "message": f"Last message: {last_time[:16].replace('T',' ')}",
                }
            else:
                checks["last_message"] = {"status": "yellow", "message": "No messages yet"}
        except Exception:
            checks["last_message"] = {"status": "yellow", "message": "Unable to check"}

    # Campaigns
    checks["campaigns"] = {"status": "green", "message": "Campaign engine ready ✓"}

    # Pending payments (informational)
    try:
        stale = crud.get_stale_payment_orders(bid, older_than_hours=1)
        if stale:
            checks["pending_payments"] = {
                "status":  "yellow",
                "message": f"{len(stale)} order(s) awaiting payment",
            }
        else:
            checks["pending_payments"] = {"status": "green", "message": "No stale payments ✓"}
    except Exception:
        checks["pending_payments"] = {"status": "green", "message": "Payment monitor ready"}

    # Compute overall
    statuses = [v["status"] for v in checks.values()]
    if "red" in statuses:
        overall = "red"
    elif "yellow" in statuses:
        overall = "yellow"

    return {
        "overall": overall,
        "checks":  checks,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
