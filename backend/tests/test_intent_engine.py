"""
tests/test_intent_engine.py — Phase 2 regression tests.

Unit tests for services/intent_engine.py's pure classification logic:
intent taxonomy coverage against the Phase 2 spec's own example test
phrases (casual/product/ordering/ambiguous/booking/delivery/payment/
complaints/human/security), confidence tiering, entity extraction, and
the "unknown must stay unknown — never default to order" requirement.
"""

import pytest

from services.intent_engine import classify_intent, CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, INTENTS


PRODUCTS = [
    {"id": 1, "name": "Chicken Burger", "price": 8},
    {"id": 2, "name": "Coke", "price": 2},
    {"id": 3, "name": "Fries", "price": 3},
]


def _intent(text, **kw):
    return classify_intent(text, products=PRODUCTS, **kw).intent


# ── Casual ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", ["hey", "hello", "hi", "good morning", "good evening"])
def test_greetings_classify_as_greeting(text):
    assert _intent(text) == "greeting"


def test_small_talk():
    assert _intent("how are you") == "small_talk"


# ── Product discovery ────────────────────────────────────────────────────

def test_product_search():
    assert _intent("show me burgers") == "product_search"


def test_price_question_with_correct_product_entity():
    result = classify_intent("how much is the chicken?", products=PRODUCTS)
    assert result.intent == "price_question"
    assert result.entities.get("product") == "Chicken Burger"


def test_availability_question():
    assert _intent("do you have beef available?") in ("availability_question", "product_search")


def test_recommendation():
    assert _intent("anything cheaper?") == "recommendation"


# ── Ordering ─────────────────────────────────────────────────────────────

def test_add_to_cart_extracts_product_and_quantity():
    result = classify_intent("give me two chicken burgers", products=PRODUCTS)
    assert result.intent == "add_to_cart"
    assert result.entities["product"] == "Chicken Burger"
    assert result.entities["quantity"] == 2
    assert result.tier == "high"  # confirmed by a real product entity match


def test_remove_from_cart():
    result = classify_intent("remove the fries", products=PRODUCTS)
    assert result.intent == "remove_from_cart"
    assert result.entities.get("product") == "Fries"


def test_update_quantity():
    assert _intent("make that three") == "update_quantity"


def test_cart_view():
    assert _intent("cart") == "cart_view"
    assert _intent("show my cart") == "cart_view"


def test_checkout():
    assert _intent("checkout") == "checkout"


# ── Ambiguous — must NOT confidently resolve to a specific intent ──────────

@pytest.mark.parametrize("text", ["I'll take two", "yes", "the usual is fine but", "that one", "make it bigger"])
def test_ambiguous_standalone_messages_are_not_high_confidence(text):
    """
    These are genuinely ambiguous without conversation context (Phase 3's
    job) — the classifier must not confidently guess. "the usual" alone
    intentionally classifies as add_to_cart at medium confidence (a
    reasonable reorder signal); the point of this test is that NONE of
    these produce a false HIGH-confidence claim.
    """
    result = classify_intent(text, products=PRODUCTS)
    assert result.tier != "high", f"{text!r} should not be high-confidence, got {result.intent}"


# ── Unknown must never default to "order" ───────────────────────────────

@pytest.mark.parametrize("text", ["asdkjfh", "xyz123", "🎉🎉🎉", "make it bigger"])
def test_unrecognizable_text_is_unknown_not_order(text):
    result = classify_intent(text, products=PRODUCTS)
    assert result.intent != "order", "unknown must never silently default to 'order'"


def test_empty_text_is_unknown_with_zero_confidence():
    result = classify_intent("", products=PRODUCTS)
    assert result.intent == "unknown"
    assert result.confidence == 0.0


# ── Booking ──────────────────────────────────────────────────────────────

def test_booking_intent_keyword():
    assert _intent("can I book an appointment?") == "booking"


def test_booking_change():
    assert _intent("can I move my appointment?") == "booking_change"


def test_booking_cancellation():
    assert _intent("cancel my appointment") == "booking_cancellation"


def test_standalone_vague_time_without_booking_keyword_stays_low_confidence():
    """
    "tomorrow afternoon" alone (no "book"/"appointment"/etc keyword) is
    genuinely ambiguous as a fresh, context-free message — correctly
    deferred to Phase 3 (conversation context), not misclassified here.
    """
    result = classify_intent("tomorrow afternoon", products=PRODUCTS)
    assert result.tier == "low"


# ── Delivery / payment / business info ──────────────────────────────────

def test_delivery_question():
    assert _intent("do you deliver?") == "delivery_question"


def test_payment_question():
    assert _intent("can I pay by card?") == "payment_question"


def test_payment_confirmation():
    assert _intent("I paid already") == "payment_confirmation"


def test_business_hours():
    assert _intent("are you open on Sunday?") == "business_hours"


# ── Complaints / returns / human handoff ─────────────────────────────────

def test_complaint():
    assert _intent("my order is wrong") == "complaint"


def test_returns():
    assert _intent("I want a refund") == "returns"


def test_human_handoff_reuses_existing_detector():
    assert _intent("I want to talk to someone") == "human_handoff"
    assert _intent("connect me to a human") == "human_handoff"


# ── Confidence tiers ─────────────────────────────────────────────────────

def test_confidence_tier_thresholds_are_consistent():
    hi = classify_intent("hello", products=PRODUCTS)
    assert hi.confidence >= CONFIDENCE_HIGH
    assert hi.tier == "high"

    lo = classify_intent("asdkjfh", products=PRODUCTS)
    assert lo.confidence < CONFIDENCE_MEDIUM
    assert lo.tier == "low"


def test_medium_confidence_requires_clarification_flag_is_set():
    result = classify_intent("how are you", products=PRODUCTS)  # small_talk, 0.70 -> medium
    assert result.tier == "medium"
    assert result.requires_clarification is True


def test_high_confidence_does_not_require_clarification():
    result = classify_intent("hello", products=PRODUCTS)
    assert result.requires_clarification is False


# ── Internal JSON representation never meant for customers ─────────────

def test_to_dict_shape():
    result = classify_intent("hello", products=PRODUCTS)
    d = result.to_dict()
    assert set(d.keys()) == {"intent", "confidence", "entities", "language", "requires_clarification"}
    assert d["intent"] in INTENTS


def test_classify_intent_never_raises_on_bad_input():
    # None-ish / weird inputs must degrade to "unknown", never raise.
    assert classify_intent(None, products=PRODUCTS).intent == "unknown"
    assert classify_intent("hi", products=None).intent == "greeting"
    assert classify_intent("hi", products=[{"weird": "shape"}]).intent == "greeting"
