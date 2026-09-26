"""
services/sales_conversation.py — Phase 8: Sales AI conversation helpers.

WHAT THIS IS
────────────
Small, pure, additive helper functions used by services/ai.py's existing
state machine to recognise a few natural sales moments the deterministic
pipeline doesn't already handle:

  - Price objections:       "that's too expensive" / "too pricey"
  - Complementary questions: "what goes well with this?" / "what pairs with it?"
  - Gift/occasion framing:   "I'm buying a birthday gift" -> ask a useful
                             clarifying question (budget) before recommending

WHAT THIS IS NOT
────────────────
Exactly like services/nl_commerce.py and services/business_knowledge.py
before it: this module does NOT talk to the database, does NOT mutate cart
or session state, and does NOT invent products, prices, discounts, or
scarcity. Every function here is a pure text/data transformation — parse
text -> structured value, or filter/sort a `products` list the caller
already loaded from crud.get_products(). services/ai.py decides what to
actually do with the result, using the REAL sales-recommendation engine
that already exists (services/sales_ai_service.py — its get_suggestions()
already does category/history-based cross-sell scoring; this module is
about recognising WHEN to call into it and WHICH product to call it with,
not about re-implementing product scoring).

Phase 8 spec constraints this module is built to respect:
  - Do not invent discounts.
  - Do not pressure customers.
  - Do not falsely claim scarcity.
  - Do not manipulate customers.
No copy anywhere in this module (or in services/ai.py's Phase 8 block)
uses urgency language ("hurry", "only X left", "limited time") or invents
a percentage-off — any stock-based statement only ever reflects a number
already returned by the backend.
"""

from __future__ import annotations

import re
from typing import Optional


# ═════════════════════════════════════════════════════════════════════════
# Price objection — "that's too expensive"
# ═════════════════════════════════════════════════════════════════════════

_PRICE_OBJECTION_PHRASES = (
    "too expensive", "too pricey", "too pricy", "too much money", "too costly",
    "that's expensive", "thats expensive", "that's too much", "thats too much",
    "a bit pricey", "a bit expensive", "kind of expensive", "kinda expensive",
    "out of my budget", "outside my budget", "can't afford", "cant afford",
    "not in my budget", "over my budget", "too dear",
)


def is_price_objection(text: str) -> bool:
    """True if `text` reads as a customer objecting to price (not asking a
    price question, not a plain "cheaper" comparative reference — see
    nl_commerce.py's _CHEAPER_PHRASES for that, which resolves against
    products already shown rather than reacting to a fresh complaint)."""
    t = (text or "").lower().strip()
    return any(p in t for p in _PRICE_OBJECTION_PHRASES)


def pick_cheaper_alternatives(
    products: list,
    anchor_price: Optional[float] = None,
    is_service_business: bool = False,
    limit: int = 3,
) -> list:
    """
    Filter+sort an ALREADY-FETCHED real `products` list down to at most
    `limit` in-stock picks, cheapest first — for use after a price
    objection. When `anchor_price` is known (the price of whatever the
    customer was just reacting to), only genuinely cheaper items are
    returned, so the AI never re-offers the same item, or something priced
    the same or higher, as a "more affordable option". When no anchor is
    known, simply returns the cheapest real in-stock items overall.

    Never invents a product, price, or discount — every item returned is
    one already present in `products`, at its real stored price.
    """
    candidates = []
    for p in products:
        stock = None if is_service_business else p.get("stock")
        if stock is not None and stock <= 0:
            continue
        try:
            price = float(p.get("price", 0) or 0)
        except (TypeError, ValueError):
            continue
        if anchor_price is not None and price >= anchor_price:
            continue
        candidates.append(p)
    candidates.sort(key=lambda p: float(p.get("price", 0) or 0))
    return candidates[:limit]


# ═════════════════════════════════════════════════════════════════════════
# Complementary-product question — "what goes well with this?"
# ═════════════════════════════════════════════════════════════════════════

_COMPLEMENT_PHRASES = (
    "goes well with", "goes with this", "goes with that", "goes with it",
    "pairs well with", "pairs with this", "pairs with that", "pairs with it",
    "what pairs with", "what goes with", "what would go with",
    "anything that goes with", "anything to go with", "what compliments this",
    "what complements this", "what should i add to this", "what else do i need",
)


def is_complement_query(text: str) -> bool:
    """True if the customer is asking what pairs/goes with something they
    already have in mind (usually the item just added to their cart)."""
    t = (text or "").lower().strip()
    return any(p in t for p in _COMPLEMENT_PHRASES)


# ═════════════════════════════════════════════════════════════════════════
# Gift / occasion framing — "I'm buying a birthday gift"
# ═════════════════════════════════════════════════════════════════════════
# Deliberately narrower than nl_commerce.py's existing "something for a
# gift" / "something for a party" trigger phrases (those already return an
# immediate top-3 recommendation via P6.8 — a reasonable behavior for that
# exact phrasing). These phrases instead describe BUYING FOR an occasion
# without saying "something for a ___", which is the spec's own worked
# example ("I'm buying a birthday gift") and calls for a clarifying
# question (budget) before recommending, per Phase 8.

_GIFT_OCCASION_PHRASES = (
    "buying a gift", "buying a present", "buying for someone",
    "it's a gift", "its a gift", "it's for a gift", "as a gift",
    "birthday gift", "birthday present", "anniversary gift", "anniversary present",
    "wedding gift", "wedding present", "graduation gift", "christmas gift",
    "buying this for my", "getting a gift for",
)


def is_gift_occasion_statement(text: str) -> bool:
    """True if the customer is framing their shopping around a gift/occasion
    without already stating what they want — the moment to ask a useful
    clarifying question (budget) rather than guess."""
    t = (text or "").lower().strip()
    return any(p in t for p in _GIFT_OCCASION_PHRASES)


_BUDGET_AMOUNT_RE = re.compile(
    r"\$?\s*(?P<amt>\d+(?:\.\d+)?)\s*(?:dollars?|usd|bucks)?",
    re.IGNORECASE,
)


def extract_budget_amount(text: str) -> Optional[float]:
    """
    Parse a customer's answer to "What's your budget?" — "$20", "20",
    "around $15", "about 30 dollars", "R150" all yield a plain number.
    Only meant to be called on a reply to that specific question (the
    caller gates this on conversation state), since a bare number
    elsewhere in a conversation isn't safe to assume is a budget.
    Returns None if no number is found.
    """
    m = _BUDGET_AMOUNT_RE.search(text or "")
    if not m:
        return None
    try:
        amt = float(m.group("amt"))
    except (TypeError, ValueError):
        return None
    return amt if amt > 0 else None
