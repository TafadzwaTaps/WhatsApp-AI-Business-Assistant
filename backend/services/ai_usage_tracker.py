"""
services/ai_usage_tracker.py — Phase 12: AI Cost Optimization tracking + safeguards.

WHAT THIS IS
────────────
Two independent responsibilities, both required by the Phase 12 spec:

1. USAGE TRACKING — record_llm_usage() writes one row per LLM call
   actually made (business_id, conversation_id, intent, tier, provider,
   model, tokens, estimated cost, latency, status) via crud/ai_usage.py.
   Best-effort: a tracking failure never affects the customer-facing
   reply (see that module's own docstring).

2. PER-BUSINESS / PER-CUSTOMER SAFEGUARDS — should_call_llm() is checked
   BEFORE any LLM call is attempted, so a business or a single abusive
   customer can be stopped from generating unlimited AI cost:
     - Per-customer: a fast, DB-free sliding-window cap on (phone,
       business_id), stored in the same carts.state_data["session"] JSONB
       blob every other phase already uses for session state (Phase 8's
       "awaiting_gift_budget", Phase 9's "abuse_warnings", etc.) — no new
       table, no extra DB round-trip, and it fails the same way session
       state always has: if the read/write fails, this degrades to
       "no history", never to "blocked forever".
     - Per-business: a daily request cap read from core.plan_guard's
       existing plan-tier system (check_ai_usage_limit), fails OPEN (never
       blocks a customer over a tracking-table outage or plan-lookup
       error) — the identical fail-open philosophy already used by
       plan_guard.check_product_limit()/_check_rate_limit() elsewhere in
       this codebase.

NO API KEYS EXPOSED
────────────────────
This module never reads, logs, or forwards OPENAI_API_KEY / ANTHROPIC_API_KEY
— it only ever sees token counts, model names, and cost estimates it
computes itself from a fixed local pricing table below.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# ── Per-customer sliding-window cap ─────────────────────────────────────────
# Stored as session["ai_llm_calls"] = [unix_timestamp, ...], trimmed to the
# window on every read so the list never grows unbounded.
_CUSTOMER_LLM_WINDOW_SECONDS = 3600     # 1 hour
_CUSTOMER_LLM_MAX_PER_WINDOW = 8        # max LLM calls per customer per hour


def customer_llm_quota_ok(session: dict) -> bool:
    """True if this customer (identified by the caller's own session dict)
    may still trigger an LLM call within the current rolling window."""
    if not isinstance(session, dict):
        return True
    now = time.time()
    calls = [t for t in (session.get("ai_llm_calls") or []) if now - t < _CUSTOMER_LLM_WINDOW_SECONDS]
    return len(calls) < _CUSTOMER_LLM_MAX_PER_WINDOW


def record_customer_llm_call(session: dict) -> dict:
    """Appends "now" to the customer's call history (trimmed to the
    window and capped at the limit), returning the same session dict for
    the caller to persist via _write_state_data(). Pure — does not touch
    the database itself."""
    if not isinstance(session, dict):
        session = {}
    now = time.time()
    calls = [t for t in (session.get("ai_llm_calls") or []) if now - t < _CUSTOMER_LLM_WINDOW_SECONDS]
    calls.append(now)
    session["ai_llm_calls"] = calls[-_CUSTOMER_LLM_MAX_PER_WINDOW:]
    return session


# ── Per-business daily cap ──────────────────────────────────────────────────

def business_llm_quota_ok(business_id: int) -> bool:
    """True if this business is still under its plan's daily AI-request
    cap. Fails OPEN (returns True) on any error — a broken usage-tracking
    table or plan lookup must never itself become a customer-facing
    outage."""
    try:
        from core.plan_guard import check_ai_usage_limit
        return check_ai_usage_limit(business_id) is None
    except Exception as exc:
        log.warning("business_llm_quota_ok: check failed for biz %s (%s) — allowing", business_id, exc)
        return True


def should_call_llm(business_id: int, session: dict) -> tuple:
    """
    The single gate every LLM call site should check immediately before
    attempting a call. Returns (allowed: bool, reason: str). Order
    matters only for the reason string returned — both checks are cheap
    and independent.
    """
    if not customer_llm_quota_ok(session):
        return False, "customer_rate_limited"
    if not business_llm_quota_ok(business_id):
        return False, "business_quota_reached"
    return True, "ok"


# ── Cost estimation ──────────────────────────────────────────────────────────
# $ per 1,000 tokens as (input_price, output_price). An unrecognised model
# never gets an invented cost figure — same "never invent a number the
# backend hasn't verified" principle applied to cost accounting.
_PRICING_PER_1K_TOKENS = {
    "gpt-4o-mini":                 (0.00015, 0.0006),
    "gpt-4o":                      (0.0025,  0.01),
    "claude-3-5-haiku-20241022":   (0.0008,  0.004),
    "claude-3-5-sonnet-20241022":  (0.003,   0.015),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    prices = _PRICING_PER_1K_TOKENS.get((model or "").strip())
    if not prices:
        return 0.0
    in_price, out_price = prices
    try:
        return round((int(prompt_tokens) / 1000) * in_price + (int(completion_tokens) / 1000) * out_price, 6)
    except (TypeError, ValueError):
        return 0.0


# ── Recording ────────────────────────────────────────────────────────────────

def record_llm_usage(
    *,
    business_id: int,
    phone: str,
    conversation_id: str,
    intent: str,
    tier: str,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
    status: str = "ok",
) -> None:
    """Best-effort usage record. Never raises — see crud/ai_usage.py."""
    try:
        from crud.ai_usage import insert_ai_usage_log
        cost = estimate_cost(model, prompt_tokens, completion_tokens)
        insert_ai_usage_log({
            "business_id":       business_id,
            "phone":             phone,
            "conversation_id":   conversation_id,
            "intent":            intent,
            "tier":              tier,
            "provider":          provider,
            "model":             model,
            "prompt_tokens":     int(prompt_tokens or 0),
            "completion_tokens": int(completion_tokens or 0),
            "total_tokens":      int(prompt_tokens or 0) + int(completion_tokens or 0),
            "estimated_cost":    cost,
            "latency_ms":        int(latency_ms or 0),
            "status":            status,
        })
    except Exception as exc:
        log.debug("record_llm_usage skipped (%s)", exc)


def make_conversation_id(business_id: int, phone: str) -> str:
    """WaziBot's schema has no standalone conversation-id concept (every
    table keys by (business_id, phone) directly) — this synthesizes a
    stable, human-readable id for tracking/analytics purposes only. It is
    never used for authorization or lookups."""
    return f"{business_id}:{phone}"
