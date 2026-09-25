"""
services/intent_engine.py — Phase 2: Conversational Intent Engine.

WHAT THIS IS
────────────
A new, read-only classification layer that sits BEFORE the existing
deterministic AI (services/ai.py + services/_ai_intent.py — the
"state machine"). It produces a structured internal representation of
what a customer message probably means:

    {
      "intent": "add_to_cart",
      "confidence": 0.85,
      "entities": {"product": "Chicken Burger", "quantity": 2},
      "language": "en",
      "requires_clarification": False,
    }

This JSON is for internal use only (routing decisions, logging, future
phases) — it is never shown to the customer.

WHAT THIS IS NOT
────────────────
This does NOT replace services/ai.py's existing intent-priority dispatch
(P-3.5 .. P12, in _ai_intent.py) or the carts.state_data conversation
state machine. Those keep running exactly as before, for every message,
unchanged. This module only ever gets to (a) observe/log, and — in one
narrow, explicitly-scoped case — (b) intercept a message BEFORE it
reaches generate_reply(), and only when ALL of the following hold:

  1. The conversation is idle (state == "browsing" — no active
     order/booking/checkout flow in progress). If the customer is
     mid-flow, this module is never allowed to intercept: the existing
     state machine already knows how to interpret a bare "yes" or "2"
     in that context, and a stateless keyword classifier does not.
  2. The message is plain text (not an image/video/location/contact
     placeholder) — those already have dedicated, correct handling
     elsewhere (P8.5/P9.5 visual catalog, etc.) that this must not
     interfere with.
  3. The classifier's own confidence for that message is LOW (i.e. it
     could not confidently classify it as anything — "unknown" must
     stay unknown, never silently default to "order").

In that one case, per the Phase 2 spec, the customer is asked an open
clarification question instead of the AI risking a wrong guess. Medium-
and high-confidence classifications are observability-only in this
phase — they are logged (visible in the webhook logs / _log_event
stream) so their accuracy can be judged against real traffic, but they
do not yet change what generate_reply() does. Wiring medium-confidence
entities (e.g. "did you mean 2 Chicken Burgers?") into an actual
clarification flow needs real conversation context to be accurate
rather than generic — that's Phase 3 (conversation context) and
Phase 4 (natural-language commerce) territory, not this phase's.

COST
────
Purely deterministic (regex/keyword rules + the existing fuzzy product
matcher). No LLM call, no added API cost — consistent with the
project's cost-minimization rule ("simple messages should continue
using deterministic logic"). Model/provider-configurable LLM routing
is deferred to the phase that actually introduces LLM-generated
responses (Phase 10), where it's needed.

REUSE, NOT DUPLICATION
───────────────────────
Wherever a detector already exists and works, this module calls it
directly rather than re-implementing it with slightly different
wording (which is exactly the kind of drift Phase 1 found and fixed
with services/ai_new.py):
  - workflows.human_handoff.is_handoff_request()
  - services.booking_service._BOOKING_INTENT_PATTERNS / parse_date_time_only()
  - utils.fuzzy_matcher.find_product() / extract_quantity()
New keyword tables are only written for intents that had no existing
equivalent (business_hours, location, delivery_question, payment_*,
returns, complaint, recommendation, etc.).
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── The intent taxonomy (Phase 2 spec — do not silently extend without
#    updating the spec/docs; "unknown" must never be reassigned to "order") ──
INTENTS = (
    "greeting", "small_talk",
    "product_search", "product_question", "price_question", "availability_question",
    "recommendation",
    "add_to_cart", "remove_from_cart", "update_quantity", "cart_view", "checkout",
    "payment_question", "payment_confirmation",
    "order_status", "order_cancellation",
    "booking", "booking_change", "booking_cancellation",
    "business_hours", "location", "delivery_question",
    "returns", "complaint",
    "human_handoff", "language_change",
    "unknown",
)

CONFIDENCE_HIGH = 0.75
CONFIDENCE_MEDIUM = 0.40


@dataclass
class IntentResult:
    intent: str
    confidence: float
    entities: dict = field(default_factory=dict)
    language: str = "en"
    requires_clarification: bool = False
    raw_text: str = ""

    @property
    def tier(self) -> str:
        """'high' | 'medium' | 'low' — see module docstring for what each does."""
        if self.confidence >= CONFIDENCE_HIGH:
            return "high"
        if self.confidence >= CONFIDENCE_MEDIUM:
            return "medium"
        return "low"

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "confidence": round(self.confidence, 2),
            "entities": self.entities,
            "language": self.language,
            "requires_clarification": self.requires_clarification,
        }


# ── Rule table ───────────────────────────────────────────────────────────────
# Each rule: (intent_name, matcher(t) -> bool, confidence). Checked in order —
# first match wins. Ordered roughly safety/specificity-first: handoff and
# complaint-adjacent intents before generic commerce ones, generic greeting
# last (broadest, so it never shadows a more specific match).

def _contains_any(t: str, phrases) -> bool:
    return any(p in t for p in phrases)


def _startswith_any(t: str, phrases) -> bool:
    return any(t.startswith(p) for p in phrases)


_RETURNS_PHRASES = (
    "return this", "can i return", "want to return", "refund", "exchange it",
    "exchange for", "money back", "send it back",
)
_COMPLAINT_PHRASES = (
    "wrong order", "not what i ordered", "damaged", "broken", "poor quality",
    "bad quality", "missing item", "missing items", "order is wrong",
    "this is wrong", "not happy with", "disappointed", "unhappy",
)
_CANCEL_ORDER_PHRASES = ("cancel my order", "cancel order", "cancel the order")
_ORDER_STATUS_PHRASES = (
    "where is my order", "where's my order", "order status", "status update",
    "track my order", "track order", "eta", "when will my order",
    "is my order ready", "any update on my order",
)
_CANCEL_BOOKING_RE = re.compile(r"\bcancel\s+(my\s+)?(booking|appointment)\b", re.IGNORECASE)
_RESCHEDULE_RE = re.compile(
    r"\b(reschedule|move|change|postpone)\s+(my\s+)?(booking|appointment)\b", re.IGNORECASE
)
_UPDATE_QTY_RE = re.compile(
    r"\bmake (it|that)\s+(\d+|one|two|three|four|five|six|a couple|a few)\b", re.IGNORECASE
)
_REMOVE_PHRASES_PREFIX = ("remove ", "delete ", "cancel the ")
# "same as last time" / "the usual" are a real add_to_cart signal but name
# no literal product — resolving what they mean needs order history
# (Phase 3's conversation-context job), so they're excluded from fuzzy
# product-entity matching below (a full-sentence fuzzy match against an
# unrelated product name is worse than no entity at all).
_REORDER_PHRASES = ("same as last time", "the usual")
_ADD_TO_CART_PHRASES = (
    "give me", "i want", "i'll take", "ill take", "can i get", "can i have",
    "add ", "i'd like", "id like", "order me", "get me",
) + _REORDER_PHRASES
_CART_VIEW_PHRASES = ("my cart", "view cart", "show cart", "show my cart", "basket")
_CART_VIEW_EXACT = ("cart",)
_CHECKOUT_PHRASES = (
    "checkout", "place order", "complete order", "pay now", "i'm done",
    "im done", "finish order", "submit order",
)
_PAYMENT_CONFIRM_PHRASES = (
    "i paid", "i've paid", "ive paid", "payment done", "already paid",
    "i sent the money", "sent the payment", "just paid",
)
_PAYMENT_QUESTION_PHRASES = (
    "pay by card", "how do i pay", "where do i send payment", "payment methods",
    "do you accept", "can i pay", "how can i pay",
)
_DELIVERY_PHRASES = ("deliver", "delivery fee", "delivery cost", "do you ship")
_BUSINESS_HOURS_PHRASES = (
    "are you open", "opening hours", "what time do you open", "what time do you close",
    "business hours", "when do you open", "when are you open",
)
_LOCATION_PHRASES = (
    "where are you located", "your address", "where are you based",
    "what's your location", "whats your location", "where is your shop",
)
_AVAILABILITY_PHRASES = ("in stock", "is available", "do you have any", "still available")
_AVAILABILITY_RE = re.compile(r"\bavailable\b", re.IGNORECASE)
_PRICE_PHRASES = ("how much is", "how much does", "price of", "cost of", "what's the price")
_RECOMMEND_PHRASES = (
    "what do you recommend", "anything cheaper", "something cheap", "what goes well with",
    "suggest something", "recommend something", "what's good", "whats good",
)
_PRODUCT_QUESTION_PHRASES = ("tell me about", "what is the", "describe the", "what does the")
_PRODUCT_SEARCH_PHRASES = (
    "show me", "do you have", "what do you sell", "what do you have",
    "menu", "browse", "catalog", "products", "what's available", "whats available",
)
_LANGUAGE_CHANGE_PHRASES = (
    "switch to", "speak in", "translate to", "reply in", "talanga",  # incl. common typo/variant, harmless if unmatched
)
_SMALL_TALK_PHRASES = ("how are you", "what's up", "whats up", "how's it going", "hows it going")
_GREETING_EXACT = ("hi", "hello", "hey", "hie", "yo", "sup", "howzit", "start")
_GREETING_PREFIX = ("good morning", "good afternoon", "good evening")


def _classify_core(t: str) -> tuple[str, float]:
    """Returns (intent, confidence) for already-lowercased/stripped text `t`.
    Pure text classification — no entity extraction here."""

    # ── Safety / escalation-adjacent first ──────────────────────────────
    from workflows.human_handoff import is_handoff_request
    if is_handoff_request(t):
        return "human_handoff", 0.9

    if _contains_any(t, _RETURNS_PHRASES):
        return "returns", 0.85
    if _contains_any(t, _COMPLAINT_PHRASES):
        return "complaint", 0.8

    # ── Orders ───────────────────────────────────────────────────────────
    if _contains_any(t, _CANCEL_ORDER_PHRASES):
        return "order_cancellation", 0.9
    if _contains_any(t, _ORDER_STATUS_PHRASES):
        return "order_status", 0.85

    # ── Bookings ─────────────────────────────────────────────────────────
    if _CANCEL_BOOKING_RE.search(t):
        return "booking_cancellation", 0.9
    if _RESCHEDULE_RE.search(t):
        return "booking_change", 0.85
    from services.booking_service import _BOOKING_INTENT_PATTERNS
    if _BOOKING_INTENT_PATTERNS.search(t):
        return "booking", 0.85

    # ── Cart / checkout ──────────────────────────────────────────────────
    if _UPDATE_QTY_RE.search(t):
        return "update_quantity", 0.8
    if _startswith_any(t, _REMOVE_PHRASES_PREFIX) or "remove " in t:
        return "remove_from_cart", 0.8
    if t in _CART_VIEW_EXACT or _contains_any(t, _CART_VIEW_PHRASES):
        return "cart_view", 0.85
    if _contains_any(t, _CHECKOUT_PHRASES):
        return "checkout", 0.85
    if _contains_any(t, _ADD_TO_CART_PHRASES):
        return "add_to_cart", 0.65  # confirmed-higher once a real product entity is found

    # ── Payment ──────────────────────────────────────────────────────────
    if _contains_any(t, _PAYMENT_CONFIRM_PHRASES):
        return "payment_confirmation", 0.85
    if _contains_any(t, _PAYMENT_QUESTION_PHRASES):
        return "payment_question", 0.75

    # ── Business info ────────────────────────────────────────────────────
    if _contains_any(t, _DELIVERY_PHRASES):
        return "delivery_question", 0.8
    if _contains_any(t, _BUSINESS_HOURS_PHRASES):
        return "business_hours", 0.8
    if _contains_any(t, _LOCATION_PHRASES):
        return "location", 0.75

    # ── Product discovery ────────────────────────────────────────────────
    if _contains_any(t, _AVAILABILITY_PHRASES):
        return "availability_question", 0.7
    if _AVAILABILITY_RE.search(t):
        return "availability_question", 0.6
    if _contains_any(t, _PRICE_PHRASES):
        return "price_question", 0.75
    if _contains_any(t, _RECOMMEND_PHRASES):
        return "recommendation", 0.7
    if _contains_any(t, _PRODUCT_QUESTION_PHRASES):
        return "product_question", 0.65
    if _contains_any(t, _PRODUCT_SEARCH_PHRASES):
        return "product_search", 0.7

    # ── Misc ─────────────────────────────────────────────────────────────
    if _contains_any(t, _LANGUAGE_CHANGE_PHRASES):
        return "language_change", 0.7
    if _contains_any(t, _SMALL_TALK_PHRASES):
        return "small_talk", 0.7
    if t in _GREETING_EXACT or _startswith_any(t, _GREETING_PREFIX):
        return "greeting", 0.85

    return "unknown", 0.15


_ENTITY_INTENTS = (
    "add_to_cart", "remove_from_cart", "update_quantity",
    "price_question", "availability_question", "product_question", "recommendation",
)
_BOOKING_ENTITY_INTENTS = ("booking", "booking_change")

# A message that is JUST a bare number/quantity word ("two", "2", "Two.")
# and nothing else — the classic reply to "how many?". Deliberately
# narrow (whole-string match, not "contains") so this never fires on an
# unrelated "unknown" message that merely mentions a number in passing;
# extract_quantity() itself has no way to signal "found nothing" (it
# defaults to 1), so without this check every unclassifiable message
# would get a fake quantity=1 entity.
_BARE_QUANTITY_RE = re.compile(
    r"^(a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"a couple( of)?|a few|\d+)\.?$",
    re.IGNORECASE,
)
# utils.fuzzy_matcher.extract_quantity() only recognises a quantity when
# it's followed by a product noun ("2 beef" -> 2) — a bare "two" or "2"
# with nothing else returns 1 (its default), which is correct for THAT
# function's actual job (parsing an order line) but wrong for a standalone
# reply to "how many?". Parsed locally here instead of changing that
# shared production function's behavior for its real callers.
_BARE_QUANTITY_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "a couple": 2, "a couple of": 2, "a few": 3,
}


def _parse_bare_quantity(t: str) -> int:
    cleaned = t.strip().rstrip(".")
    if cleaned.isdigit():
        return max(1, int(cleaned))
    return _BARE_QUANTITY_WORDS.get(cleaned.lower(), 1)


# utils.fuzzy_matcher.extract_quantity() only recognises a number when it's
# immediately paired with a product noun ("2 beef" -> 2); a message with an
# explicit number but no adjacent product word ("make it 3", "I want 3",
# "2 fries please" when "fries" isn't right next to "2" for that parser)
# silently falls back to its default of 1 — a real number typed by the
# customer would otherwise be silently discarded in favor of the wrong
# default. Found via manual conversation-simulation during Phase 3 testing
# (STEP 6), not the spec's own worked example, but the same class of bug:
# never let an explicit customer-provided number be dropped in favor of a
# LLM/parser default. Used only as a fallback, and only to REPLACE the
# sentinel default of 1 — never to override a genuinely-extracted value.
_EXPLICIT_NUMBER_RE = re.compile(r"\b(\d{1,3})\b")
_NUMBER_WORD_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten)\b", re.IGNORECASE
)
_NUMBER_WORDS_MAP = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _find_explicit_quantity(t: str) -> Optional[int]:
    """Best-effort scan for *any* explicit number/number-word in short
    text, digit numbers taking priority. Returns None if none found —
    callers only use this to replace extract_quantity()'s ambiguous
    default of 1, never to invent a quantity out of nothing."""
    m = _EXPLICIT_NUMBER_RE.search(t)
    if m:
        try:
            n = int(m.group(1))
            if n > 0:
                return n
        except ValueError:
            pass
    m2 = _NUMBER_WORD_RE.search(t)
    if m2:
        return _NUMBER_WORDS_MAP[m2.group(1).lower()]
    return None

# Trigger phrases worth stripping before fuzzy product matching, so
# find_product() sees just the product reference rather than the whole
# question — e.g. "how much is the chicken?" fuzzy-matches poorly as a
# full string (find_product's own quirk, not this module's), but strips
# down to "the chicken?" -> matches correctly once the question-phrase
# noise is removed. Cart-modifying intents (add/remove/update) are NOT
# stripped here — their own phrase sets already start the sentence and
# find_product() already strips a leading intent prefix internally for
# those ("i want pizza" -> "pizza"), so this is only needed for the
# question-style intents whose trigger phrases aren't already handled by
# find_product()'s own prefix-stripping.
_STRIP_BEFORE_MATCH = {
    "price_question": _PRICE_PHRASES,
    "availability_question": _AVAILABILITY_PHRASES,
    "product_question": _PRODUCT_QUESTION_PHRASES,
    "recommendation": _RECOMMEND_PHRASES,
}


def _extract_entities(t: str, intent: str, products: Optional[list]) -> dict:
    entities: dict = {}
    try:
        if intent == "add_to_cart" and _contains_any(t, _REORDER_PHRASES):
            # No literal product name in a reorder phrase — see the
            # _REORDER_PHRASES comment above. Quantity still applies if a
            # number happens to be present ("two of the usual").
            from utils.fuzzy_matcher import extract_quantity
            entities["quantity"] = extract_quantity(t)
        elif intent in _ENTITY_INTENTS and products:
            from utils.fuzzy_matcher import find_product, extract_quantity
            match_text = t
            for phrase in _STRIP_BEFORE_MATCH.get(intent, ()):
                if phrase in match_text:
                    match_text = match_text.replace(phrase, " ", 1).strip()
                    break
            # find_product() fuzzy-matches noticeably worse with trailing
            # punctuation ("the chicken?" mismatches where "the chicken"
            # matches correctly) — harmless, local cleanup on our copy of
            # the text only; the original `t` used for intent keyword
            # matching above is untouched.
            match_text = match_text.rstrip("?!. ")
            product = find_product(match_text, products)
            if product:
                entities["product"] = product.get("name")
                entities["product_id"] = product.get("id")
            if intent in ("add_to_cart", "update_quantity"):
                qty = extract_quantity(t)
                if qty == 1:
                    explicit = _find_explicit_quantity(t)
                    if explicit and explicit != 1:
                        qty = explicit
                entities["quantity"] = qty
        elif intent == "unknown":
            # A short, keyword-free reply — the classic answer to a
            # clarifying question ("Which one?" -> "Chicken.", "How
            # many?" -> "Two.") has no add_to_cart/etc. trigger phrase at
            # all, so it always lands here. Both checks below are
            # independent (a message can carry either, neither, or both)
            # and deliberately narrow: a bare quantity word/number (see
            # _BARE_QUANTITY_RE), and — only for SHORT text, to avoid
            # fuzzy-matching random words in a long unrelated sentence —
            # a product name via find_product()'s own conservative (60%)
            # match threshold. Neither branch changes `intent` itself;
            # it stays "unknown" here. Only conversation_context.py
            # decides whether combining this with a recent turn is
            # enough to call it add_to_cart.
            if _BARE_QUANTITY_RE.match(t.strip()):
                entities["quantity"] = _parse_bare_quantity(t)
            elif len(t) <= 40:
                explicit = _find_explicit_quantity(t)
                if explicit:
                    entities["quantity"] = explicit
            if products and len(t) <= 40:
                from utils.fuzzy_matcher import find_product
                product = find_product(t.rstrip("?!. "), products)
                if product:
                    entities["product"] = product.get("name")
                    entities["product_id"] = product.get("id")
        elif intent in _BOOKING_ENTITY_INTENTS:
            from services.booking_service import parse_date_time_only
            date_str, time_str = parse_date_time_only(t)
            if date_str:
                entities["date"] = date_str
            if time_str:
                entities["time"] = time_str
    except Exception as exc:
        # Entity extraction is a bonus, never a requirement — classification
        # itself must never fail because a product list was malformed.
        log.debug("intent_engine: entity extraction failed (ignored): %s", exc)
    return entities


def classify_intent(
    text: str,
    products: Optional[list] = None,
    language: str = "en",
) -> IntentResult:
    """
    Classify a single customer message. Never raises — falls back to
    ("unknown", low confidence) on any internal error, which is always a
    safe, non-committal result for a caller to act on.
    """
    raw = text or ""
    t = raw.lower().strip()
    if not t:
        return IntentResult(intent="unknown", confidence=0.0, language=language, raw_text=raw)

    try:
        intent, confidence = _classify_core(t)
    except Exception as exc:
        log.warning("intent_engine: classification failed (ignored): %s", exc)
        intent, confidence = "unknown", 0.15

    entities = _extract_entities(t, intent, products)

    # A confirmed product entity is a strong signal the add_to_cart guess
    # was right — bump confidence rather than trusting the keyword alone.
    if intent == "add_to_cart" and entities.get("product"):
        confidence = max(confidence, 0.8)

    result = IntentResult(
        intent=intent, confidence=confidence, entities=entities,
        language=language, raw_text=raw,
    )
    result.requires_clarification = (result.tier == "medium")
    return result
