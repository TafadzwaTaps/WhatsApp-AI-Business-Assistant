"""
services/nl_commerce.py — Phase 4: Natural Language Commerce helpers.

WHAT THIS IS
────────────
Small, pure, additive helper functions used by services/ai.py's existing
state machine to understand a few natural-language ordering patterns that
the deterministic parser (utils/fuzzy_matcher.py + services/_ai_products.py)
doesn't already cover:

  - Quantity correction on an existing cart item:
        "make it 3" / "make that three" / "change it to 5"
  - Product substitution within the cart:
        "change the chicken to beef" / "swap fries for onion rings"
  - Recommendation queries with an optional price ceiling:
        "what do you recommend?" / "something cheap" / "anything under $10?"
  - Comparative references to products the bot itself most recently showed:
        "I'll take the cheaper one" / "the first one"

WHAT THIS IS NOT
────────────────
This module does NOT talk to the database and does NOT mutate any cart or
session state itself — every function here is a pure text/data
transformation (parse text -> structured value, or filter/sort a products
list already fetched by the caller). services/ai.py is the only place that
reads carts.state_data, calls crud, and decides what to actually do; that
keeps this module trivially testable and keeps the "backend is the source
of truth" rule intact — nothing here invents a product, a price, or stock
availability. Every filter function only narrows a `products` list the
caller already loaded from crud.get_products(); nothing is fetched here.

Already-covered patterns (confirmed via audit of the existing production
pipeline — NOT reimplemented here, see services/ai.py's own P7a/P7b/P4.5):
  - "give me two burgers and a coke"  -> order_parser.py / _parse_multi_items
  - "same as last time" / "reorder"   -> _is_reorder_request() + P4.5
  - "actually remove the fries"       -> P6 (substring match already
                                          tolerates leading filler words)
"""

from __future__ import annotations

import re
from typing import Optional


# ═════════════════════════════════════════════════════════════════════════
# Quantity correction — "make it 3" / "make that three" / "change it to 5"
# ═════════════════════════════════════════════════════════════════════════

_NUM_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Deliberately requires an explicit correction prefix ("make it", "make
# that", "change it to", "update ... to") rather than matching a bare
# number — a bare "3" on its own is ambiguous (is it a quantity correction,
# a menu option reply, or something else?) and the existing pipeline has no
# "awaiting quantity" clarifying state to make that reply unambiguous, so
# guessing would risk silently changing the wrong thing. This only fires on
# the explicit-correction phrasing the Phase 4 spec itself gives as the
# example ("make that three").
_QTY_CORRECTION_RE = re.compile(
    r"^(?:make\s+(?:it|that)|change\s+(?:it|that)\s+to|"
    r"update\s+(?:it|the)?\s*(?:quantity\s*)?to)\s+"
    r"(?P<num>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\.?$",
    re.IGNORECASE,
)


def detect_quantity_correction(text: str) -> Optional[int]:
    """
    Return the corrected quantity if `text` is an explicit quantity-
    correction phrase ("make it 3", "make that three", "change it to 5"),
    else None. Never returns 0 or a negative number.
    """
    m = _QTY_CORRECTION_RE.match((text or "").strip())
    if not m:
        return None
    raw = m.group("num").lower()
    n = int(raw) if raw.isdigit() else _NUM_WORDS.get(raw)
    return n if n and n > 0 else None


# ═════════════════════════════════════════════════════════════════════════
# Product substitution — "change the chicken to beef" / "swap X for Y"
# ═════════════════════════════════════════════════════════════════════════

_SUBSTITUTION_RE = re.compile(
    r"^(?:change|swap|switch|replace)\s+(?:the\s+|my\s+)?(?P<from>.+?)\s+"
    r"(?:to|with|for)\s+(?:the\s+|a\s+|an\s+)?(?P<to>.+?)\.?$",
    re.IGNORECASE,
)


def detect_substitution_phrase(text: str) -> Optional[tuple[str, str]]:
    """
    Return (from_text, to_text) if `text` reads as a substitution request
    ("change the chicken to beef" -> ("chicken", "beef")), else None. This
    is a text-level parse only — the caller is responsible for actually
    matching from_text against a real cart line and to_text against the
    real product catalogue, and for doing nothing if either match fails
    (never guess which item was meant).
    """
    m = _SUBSTITUTION_RE.match((text or "").strip())
    if not m:
        return None
    from_text = m.group("from").strip()
    to_text = m.group("to").strip()
    if not from_text or not to_text:
        return None
    return from_text, to_text


def match_cart_item_by_text(cart: list, ref_text: str) -> Optional[dict]:
    """
    Find the cart line item `ref_text` most plausibly refers to, using the
    same lenient substring-containment strategy P6 (remove item) already
    uses in services/ai.py — kept here as a shared helper so both features
    resolve "which cart item does this text mean" the same way. Returns
    None (never guesses) if nothing matches.
    """
    ref = (ref_text or "").lower().strip()
    if not ref or not cart:
        return None
    for item in cart:
        name = (item.get("name") or "").lower()
        if name and (name in ref or ref in name):
            return item
    return None


# ═════════════════════════════════════════════════════════════════════════
# Recommendation queries — "what do you recommend?" / "anything under $10?"
# ═════════════════════════════════════════════════════════════════════════

_RECOMMEND_TRIGGER_PHRASES = (
    "what do you recommend", "any recommendations", "any recommendation",
    "any suggestions", "any suggestion", "recommend something",
    "recommend me something", "what should i get", "what should i order",
    "what's good", "whats good", "what's popular", "whats popular",
    "best seller", "bestseller", "what do you suggest",
    "something for dinner", "something for lunch", "something for breakfast",
    "something for a gift", "something for a party",
    "something cheap", "something affordable",
    "anything cheap", "anything affordable", "surprise me",
)

_PRICE_CEILING_RE = re.compile(
    r"(?:under|below|less\s+than|cheaper\s+than)\s*\$?\s*(?P<amt>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
# "anything/something ... under $10" — requires one of these nearby so a
# stray "under $10" in an unrelated sentence (e.g. a delivery-fee question)
# doesn't get misread as a recommendation request.
_PRICE_QUERY_CONTEXT_WORDS = ("anything", "something", "products", "items", "you have", "got")


def is_recommendation_query(text: str) -> bool:
    """True if `text` reads as an open-ended recommendation request."""
    t = (text or "").lower().strip()
    if any(p in t for p in _RECOMMEND_TRIGGER_PHRASES):
        return True
    if _PRICE_CEILING_RE.search(t) and any(w in t for w in _PRICE_QUERY_CONTEXT_WORDS):
        return True
    return False


def extract_price_ceiling(text: str) -> Optional[float]:
    """Return the customer-stated max price ("anything under $10" -> 10.0), or None."""
    m = _PRICE_CEILING_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group("amt"))
    except (TypeError, ValueError):
        return None


def filter_recommended_products(
    products: list,
    max_price: Optional[float] = None,
    is_service_business: bool = False,
    limit: int = 3,
) -> list:
    """
    Filter+sort an ALREADY-FETCHED real `products` list (from
    crud.get_products()) down to at most `limit` in-stock, affordable
    picks, cheapest first. Never invents a product — every item returned
    is one already present in `products`. A service business has no stock
    tracking (mirrors services/ai.py's own _resolve_stock() convention).
    """
    candidates = []
    for p in products:
        stock = None if is_service_business else p.get("stock")
        if stock is not None and stock <= 0:
            continue
        if max_price is not None:
            try:
                if float(p.get("price", 0) or 0) > max_price:
                    continue
            except (TypeError, ValueError):
                continue
        candidates.append(p)
    candidates.sort(key=lambda p: float(p.get("price", 0) or 0))
    return candidates[:limit]


# ═════════════════════════════════════════════════════════════════════════
# Comparative reference — "I'll take the cheaper one" / "the first one"
# ═════════════════════════════════════════════════════════════════════════

_CHEAPER_PHRASES = (
    "cheaper one", "the cheap one", "cheaper option", "less expensive one",
    "the cheapest", "cheapest one", "cheapest option",
)
_PRICIER_PHRASES = (
    "more expensive one", "pricier one", "expensive one", "premium one",
    "the most expensive", "priciest one",
)
_FIRST_PHRASES = ("the first one", "first option", "the first option")
_SECOND_PHRASES = ("the second one", "second option", "the second option")
_THAT_ONE_PHRASES = ("that one", "i'll take that", "i'll have that", "ill take that")


def resolve_comparative_reference(text: str, last_shown_products: list) -> Optional[dict]:
    """
    Resolve a comparative reference ("the cheaper one") against a small
    list of products the bot itself most recently displayed (each dict
    needs at least "name" and "price"). Returns one of the SAME dicts from
    `last_shown_products` (never invents a new one), or None if nothing in
    the text refers to them or nothing was recently shown.
    """
    if not last_shown_products:
        return None
    t = (text or "").lower().strip()

    if any(p in t for p in _CHEAPER_PHRASES):
        return min(last_shown_products, key=lambda p: float(p.get("price", 0) or 0))
    if any(p in t for p in _PRICIER_PHRASES):
        return max(last_shown_products, key=lambda p: float(p.get("price", 0) or 0))
    if any(p in t for p in _FIRST_PHRASES):
        return last_shown_products[0]
    if any(p in t for p in _SECOND_PHRASES) and len(last_shown_products) >= 2:
        return last_shown_products[1]
    if len(last_shown_products) == 1 and any(p in t for p in _THAT_ONE_PHRASES):
        return last_shown_products[0]
    return None
