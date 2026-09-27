"""
services/conversation_analytics.py — Phase 14: Conversation Analytics.

WHAT THIS IS
────────────
A single function, get_conversation_analytics(business_id), that computes
every metric the Phase 14 spec asks for, entirely by READING data other
phases already produce — no customer-facing behavior changes, no new LLM
calls, and (with one small exception) no new schema:

  conversation count           — distinct customers with an incoming
                                  message in the window (best available
                                  proxy: WaziBot has no separate
                                  conversation-session concept — see
                                  services/ai_usage_tracker.make_conversation_id's
                                  own docstring, unchanged since Phase 12)
  resolved by AI / handoff rate — messages.sender_type ("ai" vs "agent")
                                  already computed by crud.analytics
                                  .get_business_stats(); handoff RATE is
                                  new (see below)
  unknown intent rate,
  low-confidence rate,
  top customer questions        — computed lazily by re-running Phase 2's
                                  existing, free, deterministic
                                  services.intent_engine.classify_intent()
                                  over recent incoming messages. Nothing is
                                  stored beyond what `messages` already
                                  holds — this is intentional: it avoids a
                                  second, parallel copy of customer text
                                  ever existing at rest.
  top products requested        — the exact same deterministic
                                  services._ai_products._find_product()
                                  every typed order already uses, tallied
                                  over recent incoming messages (never an
                                  LLM guess).
  abandoned carts                — reuses growth.cart_recovery's own
                                  "browsing state + has items + idle past
                                  the threshold" definition (its constants,
                                  not a second one invented here).
  AI-generated sales              — orders from customers who never
                                  received an "agent" (human) reply.
  booking conversations           — a plain count from the existing
                                  `bookings` table.
  failed conversations            — handoff_log rows (new — see below)
                                  whose reason is one of the two the
                                  codebase already uses for "the AI
                                  genuinely couldn't understand this"
                                  (Phase 9's "Complex request" /
                                  "Repeated misunderstanding" — as opposed
                                  to a refund, business rule, sensitive
                                  issue, etc., which are correct routing,
                                  not AI failure).
  average response time           — paired incoming→outgoing message
                                  timestamps from `messages`, per customer.
  AI cost                         — crud.ai_usage.get_ai_usage_summary()
                                  (Phase 12), unchanged. Correctly mostly
                                  $0 — most traffic never touches an LLM,
                                  by design.

THE ONE NEW TABLE
──────────────────
handoff_log (crud/handoff_log.py, Phase 14) — a durable record of every
human-handoff event, written by services/ai.py's existing
_escalate_to_human() (the single function every trigger already funnels
through). Before this, only a CURRENT snapshot of who's mid-handoff
existed (carts.state_data), and /analytics/handoff-stats's "today" figure
relied on matching an emoji prefix on outgoing message text. This adds
accurate handoff-rate-over-time and lets "failed conversations" be counted
by its actual escalation reason, not guessed at.

PRIVACY
────────
"Do not expose private customer information unnecessarily" (spec's own
words) is honored structurally: every function in this module returns
aggregated counts, rates, or a small list of representative PHRASES —
never a phone number, customer id, or per-customer breakdown. The lazy
re-classification never persists a second copy of customer text anywhere.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Simple in-process TTL cache — same rationale as crud/analytics.py's own
# 60s cache: this endpoint does several table scans, and a dashboard tab
# left open would otherwise re-run all of them on every poll.
_CACHE_TTL_SECONDS = 60
_cache: dict = {}


def _cache_get(key):
    entry = _cache.get(key)
    if entry is None:
        return False, None
    ts, val = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        del _cache[key]
        return False, None
    return True, val


def _cache_set(key, value):
    _cache[key] = (time.monotonic(), value)


# Reasons that mean "the AI genuinely couldn't handle this" (Phase 9's own
# two "the deterministic engine didn't understand" triggers), as opposed
# to a handoff that's just correct routing (refund, sensitive issue,
# business rule, serious complaint, abuse, damaged/return photo, explicit
# customer request for a human).
_AI_FAILURE_REASONS = {"Complex request", "Repeated misunderstanding"}

# A message that's just too short to be a meaningful "customer question"
# once lowercased/stripped (e.g. "hi", "2", "yes") is excluded from the
# top-questions list — it would just be noise.
_MIN_QUESTION_CHARS = 6

_MAX_RESPONSE_PAIR_SECONDS = 24 * 3600  # ignore a reply more than a day later (not "response time")


def _normalize_question(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _lazy_intent_stats(business_id: int, products: list, hours: float) -> dict:
    """
    Re-runs Phase 2's free, deterministic classify_intent() over recent
    incoming messages. Nothing here is stored — it's recomputed every
    call (subject to this module's own 60s cache).
    """
    from crud.messages import get_incoming_texts_since
    from services.intent_engine import classify_intent

    texts = get_incoming_texts_since(business_id, hours=hours, limit=1000)
    if not texts:
        return {
            "unknown_intent_rate": 0.0, "low_confidence_rate": 0.0,
            "top_customer_questions": [], "classified_count": 0,
        }

    unknown = 0
    low_conf = 0
    question_counter: Counter = Counter()

    for text in texts:
        try:
            result = classify_intent(text, products=products)
        except Exception:
            continue
        if result.intent == "unknown":
            unknown += 1
        if result.tier == "low":
            low_conf += 1

        norm = _normalize_question(text)
        if len(norm) >= _MIN_QUESTION_CHARS and "?" in text:
            question_counter[norm] += 1

    total = len(texts)
    top_questions = [
        {"text": q, "count": c} for q, c in question_counter.most_common(5)
    ]

    return {
        "unknown_intent_rate":   round(unknown / total, 4) if total else 0.0,
        "low_confidence_rate":   round(low_conf / total, 4) if total else 0.0,
        "top_customer_questions": top_questions,
        "classified_count":      total,
    }


def _top_products_requested(business_id: int, products: list, hours: float) -> list:
    from crud.messages import get_incoming_texts_since
    from services._ai_products import _find_product

    texts = get_incoming_texts_since(business_id, hours=hours, limit=1000)
    if not texts or not products:
        return []

    counter: Counter = Counter()
    for text in texts:
        try:
            match = _find_product(text, products)
        except Exception:
            match = None
        if match:
            counter[match["name"]] += 1

    return [{"name": n, "count": c} for n, c in counter.most_common(5)]


def _conversation_count_and_response_time(business_id: int, hours: float) -> dict:
    from crud.messages import get_messages_since

    rows = get_messages_since(business_id, hours=hours, limit=4000)
    customers = set()
    per_customer_pending_incoming: dict = {}
    response_times = []

    for row in rows:
        cid = row.get("customer_id")
        direction = row.get("direction")
        created_at = row.get("created_at")
        if cid is None or not created_at:
            continue
        try:
            ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        if direction == "incoming":
            customers.add(cid)
            # Only remember the EARLIEST pending incoming message per
            # customer awaiting a reply, so a burst of several customer
            # messages before one reply doesn't multiply-count.
            per_customer_pending_incoming.setdefault(cid, ts)
        elif direction == "outgoing":
            pending = per_customer_pending_incoming.pop(cid, None)
            if pending is not None:
                delta = (ts - pending).total_seconds()
                if 0 <= delta <= _MAX_RESPONSE_PAIR_SECONDS:
                    response_times.append(delta)

    avg_response_seconds = round(sum(response_times) / len(response_times), 1) if response_times else None

    return {
        "conversation_count":        len(customers),
        "average_response_seconds":  avg_response_seconds,
    }


def _abandoned_cart_count(business_id: int) -> int:
    """Reuses growth.cart_recovery's own idle/skip-state definition — a
    current-moment snapshot, not windowed by `hours` (an abandoned cart is
    inherently a "right now" fact, the same way pending_handoffs is)."""
    try:
        from growth.cart_recovery import CART_IDLE_SECONDS, SKIP_STATES
        from crud.customers import get_carts_for_business

        now = datetime.now(timezone.utc)
        count = 0
        for row in get_carts_for_business(business_id):
            items = row.get("items") or []
            if not items:
                continue
            sd = row.get("state_data") or {}
            if sd.get("state") in SKIP_STATES:
                continue
            updated_raw = row.get("updated_at")
            if not updated_raw:
                continue
            try:
                updated_at = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if (now - updated_at).total_seconds() >= CART_IDLE_SECONDS:
                count += 1
        return count
    except Exception as exc:
        log.warning("_abandoned_cart_count failed for biz %s: %s", business_id, exc)
        return 0


def _booking_conversation_count(business_id: int, hours: float) -> int:
    try:
        from datetime import timedelta
        from core.db import supabase
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = (
            supabase.table("bookings")
            .select("id")
            .eq("business_id", business_id)
            .gte("created_at", cutoff)
            .execute()
        )
        return len(res.data or [])
    except Exception as exc:
        log.warning("_booking_conversation_count failed for biz %s: %s", business_id, exc)
        return 0


def _handoff_metrics(business_id: int, hours: float, conversation_count: int) -> dict:
    from crud.handoff_log import get_handoff_events

    events = get_handoff_events(business_id, hours=hours)
    total = len(events)
    failed = sum(1 for e in events if e.get("reason") in _AI_FAILURE_REASONS)

    reason_counter: Counter = Counter(e.get("reason") or "Unspecified" for e in events)

    handoff_rate = round(total / conversation_count, 4) if conversation_count else 0.0

    return {
        "human_handoff_count":     total,
        "human_handoff_rate":      handoff_rate,
        "failed_conversations":    failed,
        "handoff_reason_breakdown": dict(reason_counter),
    }


def _ai_generated_sales(business_id: int, hours: float) -> dict:
    """
    Revenue from orders whose customer never received an 'agent' (human)
    outgoing message in the window — a conservative, existing-data-only
    proxy for "the AI closed this sale on its own". Never claims more
    precision than that.
    """
    try:
        from datetime import timedelta
        from core.db import supabase

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        orders_res = (
            supabase.table("orders")
            .select("id, customer_phone, total_price, payment_status, status, created_at")
            .eq("business_id", business_id)
            .gte("created_at", cutoff)
            .execute()
        )
        orders = [
            o for o in (orders_res.data or [])
            if o.get("payment_status") == "paid" and o.get("status") != "cancelled"
        ]
        if not orders:
            return {"ai_generated_sales_count": 0, "ai_generated_sales_total": 0.0}

        agent_res = (
            supabase.table("messages")
            .select("customer_id")
            .eq("business_id", business_id)
            .eq("direction", "outgoing")
            .eq("sender_type", "agent")
            .execute()
        )
        # customer_id isn't on `orders` — match by phone via customers table
        # only for the phones actually seen in this window's orders, to
        # keep the query small.
        phones = {o.get("customer_phone") for o in orders if o.get("customer_phone")}
        agent_touched_phones = set()
        if phones:
            cust_res = (
                supabase.table("customers")
                .select("id, phone")
                .eq("business_id", business_id)
                .in_("phone", list(phones))
                .execute()
            )
            id_to_phone = {c["id"]: c["phone"] for c in (cust_res.data or [])}
            agent_customer_ids = {r["customer_id"] for r in (agent_res.data or []) if r.get("customer_id")}
            agent_touched_phones = {id_to_phone[cid] for cid in agent_customer_ids if cid in id_to_phone}

        ai_only_orders = [o for o in orders if o.get("customer_phone") not in agent_touched_phones]
        return {
            "ai_generated_sales_count": len(ai_only_orders),
            "ai_generated_sales_total": round(sum(float(o.get("total_price") or 0) for o in ai_only_orders), 2),
        }
    except Exception as exc:
        log.warning("_ai_generated_sales failed for biz %s: %s", business_id, exc)
        return {"ai_generated_sales_count": 0, "ai_generated_sales_total": 0.0}


def get_conversation_analytics(business_id: int, hours: float = 720.0) -> dict:
    """
    The one function routes/business_routes.py calls. `hours` defaults to
    30 days. Every sub-computation is independently best-effort — one
    failing (e.g. a table not migrated yet) never breaks the rest; each
    metric just falls back to a safe zero/empty value.
    """
    cache_key = ("analytics", business_id, round(hours))
    hit, cached = _cache_get(cache_key)
    if hit:
        return cached

    try:
        import crud
        products = crud.get_products(business_id) or []
    except Exception:
        products = []

    conv_and_response = _conversation_count_and_response_time(business_id, hours)
    conversation_count = conv_and_response["conversation_count"]

    intent_stats = _lazy_intent_stats(business_id, products, hours)
    top_products = _top_products_requested(business_id, products, hours)
    abandoned_carts = _abandoned_cart_count(business_id)
    booking_conversations = _booking_conversation_count(business_id, hours)
    handoff_stats = _handoff_metrics(business_id, hours, conversation_count)
    ai_sales = _ai_generated_sales(business_id, hours)

    try:
        from crud.ai_usage import get_ai_usage_summary
        ai_cost = get_ai_usage_summary(business_id, hours=hours)
    except Exception:
        ai_cost = {"requests": 0, "total_tokens": 0, "estimated_cost": 0.0}

    try:
        from crud.analytics import get_business_stats
        biz_stats = get_business_stats(business_id)
    except Exception:
        biz_stats = {"ai_handled": 0, "human_handled": 0}

    result = {
        "window_hours":            hours,
        "conversation_count":      conversation_count,
        "resolved_by_ai_messages": biz_stats.get("ai_handled", 0),
        "human_handled_messages":  biz_stats.get("human_handled", 0),
        "human_handoff_rate":      handoff_stats["human_handoff_rate"],
        "human_handoff_count":     handoff_stats["human_handoff_count"],
        "handoff_reason_breakdown": handoff_stats["handoff_reason_breakdown"],
        "unknown_intent_rate":     intent_stats["unknown_intent_rate"],
        "low_confidence_rate":     intent_stats["low_confidence_rate"],
        "top_customer_questions":  intent_stats["top_customer_questions"],
        "top_products_requested":  top_products,
        "abandoned_carts":         abandoned_carts,
        "ai_generated_sales_count": ai_sales["ai_generated_sales_count"],
        "ai_generated_sales_total": ai_sales["ai_generated_sales_total"],
        "booking_conversations":   booking_conversations,
        "failed_conversations":    handoff_stats["failed_conversations"],
        "average_response_seconds": conv_and_response["average_response_seconds"],
        "ai_cost": {
            "requests":       ai_cost.get("requests", 0),
            "total_tokens":   ai_cost.get("total_tokens", 0),
            "estimated_cost": ai_cost.get("estimated_cost", 0.0),
        },
        "insights": _build_insights(intent_stats, top_products, handoff_stats),
    }

    _cache_set(cache_key, result)
    return result


def _build_insights(intent_stats: dict, top_products: list, handoff_stats: dict) -> list:
    """
    Plain-English lines in exactly the spec's own worked-example shape —
    for the dashboard to show verbatim. Never includes a phone number,
    customer id, or anything else identifying.
    """
    lines = []

    top_q = intent_stats.get("top_customer_questions") or []
    if top_q:
        lines.append(f'Customers frequently ask:\n"{top_q[0]["text"]}"')

    if top_products:
        lines.append(f'Customers frequently request:\n"{top_products[0]["name"]}"')

    failed = handoff_stats.get("failed_conversations", 0)
    lines.append(f"AI failed to understand:\n{failed} conversation{'s' if failed != 1 else ''}")

    rate_pct = round(handoff_stats.get("human_handoff_rate", 0.0) * 100, 1)
    lines.append(f"Human handoff:\n{rate_pct}%")

    return lines
