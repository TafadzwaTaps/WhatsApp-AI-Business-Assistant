"""
services/model_routing.py — Phase 12: AI Cost Optimization / model routing.

WHAT THIS IS
────────────
A pure, stateless classifier that decides — for a given customer message —
whether any LLM call is worth its cost at all, and if so, how much model
("cheap" vs "full") the situation actually needs. This is the "hybrid
architecture" the project's standing cost rule calls for: most WhatsApp
traffic never touches an LLM (it's handled by the deterministic engine
that already existed in Phases 0-9), and this module is the single place
that decision is made explicitly, so it can be reasoned about, tested, and
tuned without touching the 2500+ line priority chain in services/ai.py.

Three tiers, matching the spec's own worked routing table:

    "none"  — no LLM. Simple greeting, simple product lookup, cart
              command, known order status. The deterministic engine's
              own reply is used as-is.
    "cheap" — a small/cheap model. Ambiguous questions the deterministic
              engine can't confidently answer, but that don't need deep
              reasoning.
    "full"  — a fuller model. Complex recommendations, complex
              multilingual conversation, customer complaints — cases
              where getting the tone/nuance right has real value.

HOW IT'S USED TODAY
───────────────────
Phase 10 wired exactly one LLM call site into services/ai.py (the P7
single-item-add confirmation rephrase) — that site's nature is already
known and fixed (a short, low-stakes rephrase of an already-fully-decided
deterministic message), so it doesn't need a per-message classification;
it's hardcoded to the "cheap" tier at its call site. classify_route() is
general-purpose, reusable infrastructure for that site and for any future
call site that needs a per-message routing decision (e.g. a future
complaint-handling or recommendation call site).

NEVER a database/business decision
───────────────────────────────────
This module never touches price, stock, order status, or any other
business fact — it only decides how a customer's already-received TEXT
should be handled, using the same pure-text detectors already used
elsewhere in the codebase (services/_ai_intent.py, services/
handoff_triggers.py, services/sales_conversation.py). Reusing those
detectors, rather than inventing new NLP, keeps this consistent with how
every other phase in this project classifies customer intent.
"""

from __future__ import annotations

import os
import re
from typing import Optional

ROUTE_NONE = "none"
ROUTE_CHEAP = "cheap"
ROUTE_FULL = "full"

_VALID_TIERS = (ROUTE_NONE, ROUTE_CHEAP, ROUTE_FULL)

# Intents from services._ai_intent._intent() that are always fully
# deterministic — a plain menu/cart/checkout/remove command never needs an
# LLM opinion; the reply is a direct, verified readout of business data.
_DETERMINISTIC_INTENTS = {"help", "browse", "cart", "checkout", "remove"}

_QUESTION_STARTS = (
    "what", "how", "why", "when", "where", "which", "who",
    "can you", "do you", "does it", "is there", "are there", "could you",
    "would you", "will it", "can i", "may i",
)

# A short, mostly-numeric line ("2 burgers", "1x fries") is a plain order,
# never a question, even though it falls into _ai_intent's catch-all
# "order" bucket alongside genuinely ambiguous chat.
_PLAIN_ITEM_ORDER_RE = re.compile(r"^\s*\d+\s*x?\s*[a-z]", re.IGNORECASE)


def _looks_like_question(low: str) -> bool:
    if not low:
        return False
    if low.rstrip().endswith("?"):
        return True
    padded = f" {low} "
    return any(low.startswith(w) for w in _QUESTION_STARTS) or any(
        f" {w} " in padded for w in _QUESTION_STARTS
    )


def _looks_like_plain_item_order(low: str) -> bool:
    return bool(_PLAIN_ITEM_ORDER_RE.match(low)) and "?" not in low


def classify_route(
    text: str,
    intent: str,
    customer_language: str = "English",
) -> str:
    """
    Returns one of "none" / "cheap" / "full" for the given customer
    message. Never raises — any error in an optional sub-detector is
    treated as "that signal didn't fire", falling through to the next
    check, with a safe, cost-minimizing default of "none".
    """
    t = (text or "").strip()
    low = t.lower()

    # Simple greeting / simple product lookup / cart command / known
    # order status — deterministic, no LLM.
    if intent in _DETERMINISTIC_INTENTS:
        return ROUTE_NONE

    try:
        from services._ai_intent import _is_status_query
        if _is_status_query(t):
            return ROUTE_NONE
    except Exception:
        pass

    # Customer complaint -> full LLM.
    try:
        from services.handoff_triggers import (
            is_serious_complaint, is_sensitive_issue, is_booking_conflict_complaint,
        )
        if is_serious_complaint(t) or is_sensitive_issue(t) or is_booking_conflict_complaint(t):
            return ROUTE_FULL
    except Exception:
        pass

    # Complex recommendation (price objection / complement / gift-occasion
    # sales conversation) -> full LLM.
    try:
        from services.sales_conversation import (
            is_price_objection, is_complement_query, is_gift_occasion_statement,
        )
        if is_price_objection(t) or is_complement_query(t) or is_gift_occasion_statement(t):
            return ROUTE_FULL
    except Exception:
        pass

    # Complex multilingual conversation -> full LLM. A short non-English
    # message ("hola", "merci") still doesn't need one; a longer free-text
    # message in another language is exactly the "get the nuance right"
    # case the spec means.
    if customer_language and customer_language.strip().lower() != "english" and len(t.split()) > 6:
        return ROUTE_FULL

    # Ambiguous question the deterministic engine only catch-all-bucketed
    # as "order" -> cheap LLM.
    if intent == "order" and _looks_like_question(low) and not _looks_like_plain_item_order(low):
        return ROUTE_CHEAP

    return ROUTE_NONE


def model_for_tier(tier: str, provider: str) -> str:
    """
    Per-tier model override, honoring the project's standing "provider and
    model must be environment-variable configurable" rule. Defaults to the
    exact same model Phase 10 already uses for its one call site when no
    override is set, so simply having this module present changes NOTHING
    about cost or behavior until a business owner explicitly configures a
    different model for the "cheap" or "full" tier.
    """
    if tier not in _VALID_TIERS or tier == ROUTE_NONE:
        return ""

    env_name = "AI_ROUTING_MODEL_CHEAP" if tier == ROUTE_CHEAP else "AI_ROUTING_MODEL_FULL"
    override = os.getenv(env_name, "").strip()
    if override:
        return override

    try:
        from services.llm_response import _DEFAULT_MODELS
        return _DEFAULT_MODELS.get(provider, "")
    except Exception:
        return ""
