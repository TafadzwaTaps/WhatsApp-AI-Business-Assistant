"""
services/business_knowledge.py — Phase 5: Business Knowledge Layer.

WHAT THIS IS
────────────
A small, additive Q&A layer that lets the AI answer factual questions
about the business itself — hours, location, delivery fee, payment
methods, contact info, social links, etc. — using ONLY real data already
stored on that business's own `businesses` row. Nothing here invents a
policy, a fee, or a set of hours: every answer function reads one or more
real columns and returns None (never a guess) when the business hasn't
filled that field in. services/ai.py is responsible for turning a None
into the spec's own honest fallback line ("I'm not sure about that yet.
Let me connect you with the team.") — this module never emits that text
itself, so there is exactly one place that owns the fallback wording.

DATA MODEL — confirmed via a full-codebase audit before writing this
(services/nl_commerce.py-style "extend, don't invent a new field" rule
applies here too):

  Category              | Real column(s) on `businesses`         | Exists?
  -----------------------------------------------------------------------
  name                  | name                                   | yes
  description / "about" | description                            | yes
  opening hours         | business_hours (free text)             | yes*
  location              | address, city                          | yes
  delivery fee          | delivery_fee (flat, single fee)        | yes
  delivery areas        | — none —                                | no
  payment methods       | cash_enabled, ecocash_number,          | yes
                         | paypal_email, bank_transfer_details,   |
                         | blik_number (assembled into a list)    |
  return policy         | — none —                                | no
  cancellation policy   | — none —                                | no
  booking rules         | — none —                                | no
  promotions            | — none —                                | no
  FAQs                  | — none —                                | no
  contact info          | contact_phone, support_email            | yes
  social links          | instagram, facebook (only two)         | yes*

  (*) partial — see the docstrings on answer_hours()/answer_social()
      below for the exact limitation.

For the "no" rows above, there is genuinely nothing to retrieve — no
migration has added those columns yet — so the caller always gets the
honest fallback for those categories. This is a known, documented gap
(see the Phase 5 completion report), not a bug: the spec explicitly
prefers "I'm not sure about that yet" over inventing a policy.

WHAT THIS IS NOT
────────────────
This module does not touch the database itself — services/ai.py fetches
the business record (the same `crud.get_business_by_id(business_id)` call
already used elsewhere in ai.py, e.g. for the pickup-location line) and
passes the resulting dict in. Keeping DB access out of this module keeps
every function here a pure, trivially-testable transform.
"""

from __future__ import annotations

from typing import Optional


# ═════════════════════════════════════════════════════════════════════════
# Category detection
# ═════════════════════════════════════════════════════════════════════════

_HOURS_PHRASES = (
    "opening hours", "business hours", "your hours", "what hours",
    "are you open", "when do you open", "when are you open",
    "what time do you open", "what time are you open",
    "when do you close", "what time do you close", "closing time",
    "still open", "open today", "open tomorrow", "open on",
    "open sunday", "open saturday", "open monday",
)
# Checked before _DELIVERY_FEE_PHRASES — both share the word "deliver",
# but "deliver to <place>" asks about coverage, not price.
_DELIVERY_AREA_PHRASES = (
    "deliver to", "delivery area", "delivery areas", "delivery zone",
    "areas you deliver", "where do you deliver", "do you deliver to",
)
_DELIVERY_FEE_PHRASES = (
    "delivery fee", "delivery cost", "delivery charge", "how much is delivery",
    "how much for delivery", "shipping fee", "shipping cost", "cost of delivery",
)
_LOCATION_PHRASES = (
    "where are you", "your location", "where is your", "your address",
    "what's your address", "whats your address", "where can i find you",
    "where are you located", "where is the shop", "where is your shop",
    "where is your store",
)
_PAYMENT_METHODS_PHRASES = (
    "what payment methods", "payment methods", "how can i pay", "how do i pay",
    "payment options", "ways to pay", "how do you accept payment",
    "what payments do you accept",
)
_RETURN_POLICY_PHRASES = (
    "return policy", "can i return", "how do returns work",
    "your return policy", "what's your return policy",
)
_CANCELLATION_POLICY_PHRASES = (
    "cancellation policy", "cancel policy", "how do i cancel my order",
    "can i cancel my order", "what's your cancellation policy",
)
_BOOKING_RULES_PHRASES = (
    "booking rules", "booking policy", "how far in advance can i book",
    "how do bookings work", "how does booking work",
)
_PROMOTIONS_PHRASES = (
    "any promotions", "any discounts", "any deals", "any specials",
    "current promotions", "do you have any offers", "any offers",
    "running any promotions",
)
_FAQ_PHRASES = ("faq", "frequently asked", "common questions")

# NOTE: deliberately does NOT include "phone number" or "contact us" — the
# existing (pre-Phase 5) human-handoff detector already treats those exact
# phrases as an explicit request for a human agent (workflows/human_
# handoff.py's _HANDOFF_CONTAINS), and that check runs much earlier in
# services/ai.py's priority chain (P-2.5) than this Q&A layer (P11.5).
# Listing them here would be dead weight — they'd never actually reach
# this module — and reasonable behavior anyway (asking for "a phone
# number" often does mean "let me talk to a person"). Smarter handoff
# triggering is explicitly Phase 9's job, not this phase's, so that
# existing behavior is left untouched here.
_CONTACT_PHRASES = (
    "contact number", "how can i contact you",
    "how do i contact you", "your email", "contact email",
    "customer support", "support email", "how can i reach you",
)
_SOCIAL_PHRASES = (
    "instagram", "facebook", "social media", "your ig", "your fb",
    "find you on",
)
# Deliberately narrow — "what do you sell" / "show me" / "menu" etc. are
# already answered by the existing browse (P9) handler earlier in ai.py's
# priority chain, so this never needs to (and must not) duplicate those.
_ABOUT_PHRASES = (
    "tell me about your business", "tell me about you",
    "what is this business", "what kind of business are you",
    "who are you", "about your business",
)

# Order matters: more specific categories are checked before ones whose
# phrases could otherwise shadow them (delivery area before delivery fee).
_CATEGORY_TABLE = (
    ("hours", _HOURS_PHRASES),
    ("delivery_area", _DELIVERY_AREA_PHRASES),
    ("delivery_fee", _DELIVERY_FEE_PHRASES),
    ("location", _LOCATION_PHRASES),
    ("payment_methods", _PAYMENT_METHODS_PHRASES),
    ("return_policy", _RETURN_POLICY_PHRASES),
    ("cancellation_policy", _CANCELLATION_POLICY_PHRASES),
    ("booking_rules", _BOOKING_RULES_PHRASES),
    ("promotions", _PROMOTIONS_PHRASES),
    ("faq", _FAQ_PHRASES),
    ("contact", _CONTACT_PHRASES),
    ("social", _SOCIAL_PHRASES),
    ("about", _ABOUT_PHRASES),
)


def detect_business_info_category(text: str) -> Optional[str]:
    """Return the business-info category `text` asks about, or None."""
    t = (text or "").lower().strip()
    if not t:
        return None
    for category, phrases in _CATEGORY_TABLE:
        if any(p in t for p in phrases):
            return category
    return None


# ═════════════════════════════════════════════════════════════════════════
# Per-category answer builders — each returns None (never a guess) when
# the business hasn't configured that field.
# ═════════════════════════════════════════════════════════════════════════

def answer_hours(business: dict) -> Optional[str]:
    """
    Returns the business's own free-text hours verbatim. Deliberately does
    NOT attempt to parse "Mon-Fri 9-5" style text to answer a specific
    day ("are you open Sunday?") with a yes/no — that would require
    guessing at a format the business typed freely, and a parsing mistake
    would be exactly the kind of invented answer the spec rules out.
    Echoing the real text is honest even when it doesn't pinpoint the one
    day asked about; the "Remaining risks" section of the Phase 5 report
    calls this out explicitly.
    """
    hours = (business.get("business_hours") or "").strip()
    if not hours:
        return None
    return f"🕒 Our hours: {hours}"


def answer_location(business: dict) -> Optional[str]:
    address = (business.get("address") or "").strip()
    city = (business.get("city") or "").strip()
    parts = [p for p in (address, city) if p]
    if not parts:
        return None
    return f"📍 We're located at {', '.join(parts)}."


def answer_delivery_fee(business: dict, currency_sym: str) -> Optional[str]:
    fee = business.get("delivery_fee")
    if fee is None:
        return None
    try:
        fee_f = float(fee)
    except (TypeError, ValueError):
        return None
    if fee_f <= 0:
        return "🚚 We offer free delivery! 😊"
    return f"🚚 Delivery costs {currency_sym}{fee_f:.2f}."


def answer_payment_methods(business: dict) -> Optional[str]:
    methods = []
    if business.get("cash_enabled"):
        methods.append("Cash (on delivery/pickup)")
    if business.get("ecocash_number"):
        methods.append("EcoCash")
    if business.get("paypal_email"):
        methods.append("PayPal")
    if business.get("bank_transfer_details"):
        methods.append("Bank transfer")
    if business.get("blik_number"):
        methods.append("BLIK")
    if not methods:
        return None
    return "💳 We accept: " + ", ".join(methods) + "."


def answer_contact(business: dict) -> Optional[str]:
    phone = (business.get("contact_phone") or "").strip()
    email = (business.get("support_email") or business.get("owner_email") or "").strip()
    lines = []
    if phone:
        lines.append(f"📞 {phone}")
    if email:
        lines.append(f"✉️ {email}")
    if not lines:
        return None
    return "You can reach us at:\n" + "\n".join(lines)


def answer_social(business: dict) -> Optional[str]:
    """Only Instagram and Facebook have real columns today — no Twitter/X,
    TikTok, LinkedIn, or website-URL field exists yet."""
    lines = []
    ig = (business.get("instagram") or "").strip()
    fb = (business.get("facebook") or "").strip()
    if ig:
        lines.append(f"📸 Instagram: {ig}")
    if fb:
        lines.append(f"👍 Facebook: {fb}")
    if not lines:
        return None
    return "Find us here:\n" + "\n".join(lines)


def answer_about(business: dict, business_name: str) -> Optional[str]:
    desc = (business.get("description") or "").strip()
    if not desc:
        return None
    return f"*{business_name}* — {desc}"


# Categories with no backing data model at all today — always fall back
# honestly rather than guess. Listed explicitly (not just "whatever isn't
# in the dispatch dict below") so it's obvious at a glance which
# categories are a real, known gap vs. a bug.
_NO_DATA_CATEGORIES = (
    "delivery_area", "return_policy", "cancellation_policy",
    "booking_rules", "promotions", "faq",
)


def build_business_info_answer(
    category: str,
    business: dict,
    business_name: str,
    currency_sym: str = "$",
) -> Optional[str]:
    """
    Single dispatch point services/ai.py calls: given a detected category
    and the business's real DB record, return the answer string, or None
    if that category has no real data to answer from (the category was
    never configured, or has no column at all yet). The caller turns None
    into the spec's fallback line — this function never does.
    """
    if category in _NO_DATA_CATEGORIES:
        return None
    if category == "hours":
        return answer_hours(business)
    if category == "location":
        return answer_location(business)
    if category == "delivery_fee":
        return answer_delivery_fee(business, currency_sym)
    if category == "payment_methods":
        return answer_payment_methods(business)
    if category == "contact":
        return answer_contact(business)
    if category == "social":
        return answer_social(business)
    if category == "about":
        return answer_about(business, business_name)
    return None
