"""
services/handoff_triggers.py — Phase 9: Smart Human Handoff detectors.

WHAT THIS IS
────────────
Small, pure, additive detector functions plus one formatter, used by
services/ai.py to recognise moments a conversation should be handed to a
human agent that the existing handoff system (workflows/human_handoff.py)
doesn't already catch. That existing system already handles:
  - an explicit request ("talk to a human", "agent", etc.)
  - repeated abusive/offensive language (3rd offense)
This module adds detectors for the rest of the spec's trigger list:
  - a serious complaint that isn't abusive/profane ("this is unacceptable")
  - a sensitive issue (safety, health, legal/discrimination)
  - a booking-conflict complaint ("you double-booked me")
  - a structurally complex message (several distinct asks in one message)
  - a business's own configured escalation keywords

WHAT THIS IS NOT
────────────────
Like nl_commerce.py, booking_nlp.py, and sales_conversation.py before it:
no DB access, no side effects, no LLM. Every function is text-in/bool-out
(or, for build_handoff_summary, text-in/text-out formatting of values the
caller already looked up). services/ai.py decides what to do with a True
result — set state, generate a ticket, notify the dashboard — using the
SAME existing plumbing (workflows/human_handoff.py's notify_dashboard(),
handoff_acknowledgement()) already used for explicit requests.

INTERNAL SUMMARY
────────────────
build_handoff_summary() produces the "CUSTOMER / ISSUE / ORDER / PURCHASE
/ REQUEST / AI SUMMARY" block the spec's own worked example shows — for
the business/agent only, never sent to the customer. Every field is either
a value the caller looked up from the real database (order id, purchase
lines, customer name) or the customer's own words (REQUEST) or a short
deterministic template sentence describing which trigger fired (AI
SUMMARY) — nothing here is invented by a model, since Phase 9 runs before
Phase 10 introduces one.
"""

from __future__ import annotations

import re
from typing import Optional


# ═════════════════════════════════════════════════════════════════════════
# Serious complaint (not necessarily profane/abusive — that's
# workflows/human_handoff.py's/_ai_intent.py's _is_abusive_message's job)
# ═════════════════════════════════════════════════════════════════════════

_SERIOUS_COMPLAINT_PHRASES = (
    "unacceptable", "this is ridiculous", "absolutely ridiculous",
    "worst service", "worst experience", "terrible experience",
    "extremely disappointed", "very disappointed", "so disappointed",
    "never again", "never ordering again", "never using this again",
    "i'm furious", "im furious", "i am furious", "i'm outraged", "im outraged",
    "disgusted", "appalling", "this is a disgrace", "totally unacceptable",
    "completely unacceptable", "i've had enough", "ive had enough",
    "i'm sick of this", "im sick of this", "fed up", "sick and tired",
    "how dare you", "this is a joke", "what a joke", "you people",
    "third time this has happened", "keeps happening", "still not resolved",
    "still hasn't been resolved", "nobody is helping me", "no one is helping me",
)


def is_serious_complaint(text: str) -> bool:
    """True for a strongly negative complaint even without profanity —
    the "angry/serious complaint" handoff trigger. Deliberately separate
    from the existing profanity/threat-based abuse detector."""
    t = (text or "").lower().strip()
    return any(p in t for p in _SERIOUS_COMPLAINT_PHRASES)


# ═════════════════════════════════════════════════════════════════════════
# Sensitive issue — safety, health, legal/discrimination
# ═════════════════════════════════════════════════════════════════════════

_SENSITIVE_ISSUE_PHRASES = (
    "food poisoning", "got sick", "made me sick", "allergic reaction",
    "got hurt", "someone got hurt", "i got injured", "got injured",
    "injury", "injured", "safety concern", "unsafe", "not safe",
    "lawyer", "legal action", "going to sue", "i will sue", "sue you",
    "discriminat", "harassment", "harassed", "assaulted",
    "health inspector", "hospital", "ambulance",
)


def is_sensitive_issue(text: str) -> bool:
    """True for a message touching safety, health, or legal/discrimination
    concerns — always a human-agent matter, never something the AI should
    try to resolve or reassure its way through."""
    t = (text or "").lower().strip()
    return any(p in t for p in _SENSITIVE_ISSUE_PHRASES)


# ═════════════════════════════════════════════════════════════════════════
# Booking conflict complaint (already happened — not a live slot check,
# which booking_service.py's create_booking()/get_available_slots() already
# handle on their own by offering a different time)
# ═════════════════════════════════════════════════════════════════════════

_BOOKING_CONFLICT_PHRASES = (
    "double booked", "double-booked", "double book",
    "someone else was in my slot", "someone else had my slot",
    "my slot was given away", "my appointment was given away",
    "showed up and no one", "showed up and nobody", "nobody was there",
    "no one was there when i", "wrong time booked", "booked me at the wrong time",
    "booked at the same time as", "overlapping booking", "overlapping appointment",
    "two people booked", "double appointment",
)


def is_booking_conflict_complaint(text: str) -> bool:
    """True when the customer is reporting a scheduling conflict that
    already happened (double-booked, no one there, etc.) — distinct from
    the routine "that slot's taken, here are other times" flow, which
    resolves itself without a human."""
    t = (text or "").lower().strip()
    return any(p in t for p in _BOOKING_CONFLICT_PHRASES)


# ═════════════════════════════════════════════════════════════════════════
# Structurally complex request
# ═════════════════════════════════════════════════════════════════════════
# A heuristic on the message's SHAPE only (length + multiple distinct asks)
# — never a claim of true language understanding. Used only as a last
# resort, after the deterministic engine has already failed to match the
# message to anything (see services/ai.py's P12 fallback), so an ordinary
# multi-item order that the parser DOES understand is never affected.

_COMPLEX_CONNECTOR_PHRASES = (
    "also need", "also want", "also wondering", "one more thing",
    "another thing", "as well as", "additionally", "and also",
    "on top of that", "at the same time", "while i'm at it", "while im at it",
)


def is_complex_request(text: str) -> bool:
    """True when a message LOOKS structurally complex — long, and/or
    stacking multiple distinct asks or questions — the "complex request"
    handoff trigger. Only meaningful once the deterministic engine has
    already failed to understand the message on its own terms."""
    t = (text or "").strip()
    if not t:
        return False
    word_count = len(t.split())
    question_marks = t.count("?")
    connector_hits = sum(1 for p in _COMPLEX_CONNECTOR_PHRASES if p in t.lower())
    if question_marks >= 2:
        return True
    if connector_hits >= 1 and word_count >= 15:
        return True
    if word_count >= 40:
        return True
    return False


# ═════════════════════════════════════════════════════════════════════════
# Business-specific escalation rule
# ═════════════════════════════════════════════════════════════════════════

def matches_business_escalation_rule(text: str, business_config: Optional[dict]) -> Optional[str]:
    """
    Checks the business's own configured escalation keywords/phrases —
    read from business_config.get("escalation_keywords"), an optional
    comma-separated string or list a business can set for terms specific
    to them (a warranty program, a franchise policy, a compliance term)
    that should always reach a human. Returns the matched keyword, or
    None if the business has no rule configured or nothing matched.

    This reads an OPTIONAL config key that may not exist for every
    business yet (no dashboard UI writes it as of Phase 9) — absent or
    malformed config is treated as "no rule configured", never an error.
    """
    if not business_config:
        return None
    raw = business_config.get("escalation_keywords")
    if not raw:
        return None
    if isinstance(raw, str):
        keywords = [k.strip() for k in raw.split(",") if k.strip()]
    elif isinstance(raw, (list, tuple)):
        keywords = [str(k).strip() for k in raw if str(k).strip()]
    else:
        return None

    t = (text or "").lower().strip()
    for kw in keywords:
        if kw.lower() in t:
            return kw
    return None


# ═════════════════════════════════════════════════════════════════════════
# Internal handoff summary — for the business/agent only, never the customer
# ═════════════════════════════════════════════════════════════════════════

def build_handoff_summary(
    customer_name:  str,
    issue:          str,
    order_ref:      str = "",
    purchase_lines: Optional[list] = None,
    purchase_total: Optional[float] = None,
    currency_sym:   str = "$",
    request_text:   str = "",
    ai_summary:     str = "",
) -> str:
    """
    Formats the internal handoff summary block, matching the spec's own
    worked example shape:

        CUSTOMER
        John

        ISSUE
        Customer wants to exchange a damaged product.

        ORDER
        ORDER-182

        PURCHASE
        2 × Blue Shirt
        $40

        REQUEST
        Exchange for Medium.

        AI SUMMARY
        Customer wants a size exchange.
        No refund requested.

    Every field the caller doesn't have real data for is shown as a plain
    "Not available" line — never guessed or invented. This text is meant
    for the business dashboard only; nothing here is sent to the customer.
    """
    lines = ["CUSTOMER", customer_name or "Unknown", "", "ISSUE", issue or "Not specified", ""]

    lines += ["ORDER", order_ref or "None on file", ""]

    lines.append("PURCHASE")
    if purchase_lines:
        lines.extend(purchase_lines)
        if purchase_total is not None:
            lines.append(f"{currency_sym}{purchase_total:.2f}")
    else:
        lines.append("Not available")
    lines.append("")

    lines += ["REQUEST", (request_text or "").strip() or "Not specified", ""]
    lines += ["AI SUMMARY", ai_summary or "No additional summary available."]

    return "\n".join(lines)
