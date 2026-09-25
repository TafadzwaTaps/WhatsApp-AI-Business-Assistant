"""
tests/test_conversation_context.py — Phase 3 regression tests.

Covers services/conversation_context.py: the short-term context module
that resolves elliptical replies ("Two." after "which one?" / "Chicken.")
against a customer's own last few messages, without any new storage
(reads the existing `messages` table via crud.get_recent_messages) and
without touching the deterministic state machine.
"""

import crud
from services.conversation_context import (
    classify_with_context,
    get_recent_customer_texts,
    resolve_with_context,
)
from services.intent_engine import classify_intent


PRODUCTS = [
    {"id": 1, "name": "Chicken Burger", "price": 8},
    {"id": 2, "name": "Coke", "price": 2},
    {"id": 3, "name": "Fries", "price": 3},
]


# ── The spec's own worked example ───────────────────────────────────────────

def test_spec_example_burger_chicken_two_resolves_to_two_chicken_burgers():
    """
    Customer: I want a burger.
    Bot:      Which one?
    Customer: Chicken.
    Bot:      How many?
    Customer: Two.
    -> must resolve to add_to_cart, product=Chicken Burger, quantity=2,
    without asking again.
    """
    base = classify_intent("Two.", products=PRODUCTS)
    assert base.tier == "low"  # confirms this genuinely needed context to resolve

    resolved = resolve_with_context(
        base, ["I want a burger.", "Chicken."], products=PRODUCTS
    )
    assert resolved.intent == "add_to_cart"
    assert resolved.entities["product"] == "Chicken Burger"
    assert resolved.entities["quantity"] == 2
    assert resolved.tier == "high"
    assert resolved.requires_clarification is False


def test_bare_number_replies_resolve_quantity():
    for reply in ("two", "Two.", "2"):
        base = classify_intent(reply, products=PRODUCTS)
        resolved = resolve_with_context(base, ["I'll have the fries"], products=PRODUCTS)
        assert resolved.entities.get("quantity") == 2, f"{reply!r} should resolve to quantity 2"
        assert resolved.entities.get("product") == "Fries"


# ── Must never invent context that isn't there ──────────────────────────────

def test_no_context_returns_base_result_unchanged():
    base = classify_intent("Two.", products=PRODUCTS)
    resolved = resolve_with_context(base, [], products=PRODUCTS)
    assert resolved is base


def test_unrelated_prior_messages_do_not_get_grafted_onto_unrelated_text():
    """A genuinely unrelated/gibberish message must stay unknown — context
    resolution only fills a gap, it never forces an intent onto text that
    gives it no signal at all."""
    base = classify_intent("asdkjfh qqq zzz", products=PRODUCTS)
    resolved = resolve_with_context(base, ["I want a burger.", "Chicken."], products=PRODUCTS)
    assert resolved.intent == "unknown"


def test_already_complete_message_is_not_altered_by_context():
    """"give me two cokes" already has both entities on its own — a
    different product mentioned earlier must not override it."""
    base = classify_intent("give me two cokes", products=PRODUCTS)
    assert base.entities.get("product") == "Coke"
    assert base.entities.get("quantity") == 2

    resolved = resolve_with_context(base, ["chicken burger please"], products=PRODUCTS)
    assert resolved.entities["product"] == "Coke"
    assert resolved.entities["quantity"] == 2


def test_context_only_applies_to_eligible_intents():
    """A booking or payment-question message shouldn't have a random
    product entity grafted on just because an earlier message mentioned
    one — context resolution is scoped to cart-related intents."""
    base = classify_intent("can I pay by card?", products=PRODUCTS)
    resolved = resolve_with_context(base, ["chicken burger please"], products=PRODUCTS)
    assert resolved is base
    assert resolved.intent == "payment_question"


# ── get_recent_customer_texts: reuses existing messages, no new storage ────

def test_get_recent_customer_texts_filters_to_incoming_only(monkeypatch):
    fake_rows = [
        {"text": "hi", "direction": "incoming", "created_at": "2026-01-01T00:00:00Z"},
        {"text": "hello! how can I help?", "direction": "outgoing", "created_at": "2026-01-01T00:00:01Z"},
        {"text": "chicken burger", "direction": "incoming", "created_at": "2026-01-01T00:00:02Z"},
    ]
    monkeypatch.setattr(crud, "get_recent_messages", lambda customer_id, limit=8: fake_rows)
    texts = get_recent_customer_texts(customer_id=1)
    assert texts == ["hi", "chicken burger"]


def test_get_recent_customer_texts_fails_safe_on_error(monkeypatch):
    def _boom(customer_id, limit=8):
        raise RuntimeError("db down")
    monkeypatch.setattr(crud, "get_recent_messages", _boom)
    assert get_recent_customer_texts(customer_id=1) == []


# ── classify_with_context: the end-to-end entry point used by the webhook ──

def test_classify_with_context_without_customer_id_behaves_like_plain_classify():
    result = classify_with_context("hello", customer_id=None, products=PRODUCTS)
    assert result.intent == "greeting"


def test_classify_with_context_pulls_recent_messages_and_resolves(monkeypatch):
    fake_rows = [
        {"text": "I want a burger.", "direction": "incoming", "created_at": "t1"},
        {"text": "Which one?", "direction": "outgoing", "created_at": "t2"},
        {"text": "Chicken.", "direction": "incoming", "created_at": "t3"},
        {"text": "How many?", "direction": "outgoing", "created_at": "t4"},
    ]
    monkeypatch.setattr(crud, "get_recent_messages", lambda customer_id, limit=8: fake_rows)
    result = classify_with_context("Two.", customer_id=42, products=PRODUCTS)
    assert result.intent == "add_to_cart"
    assert result.entities["product"] == "Chicken Burger"
    assert result.entities["quantity"] == 2


def test_classify_with_context_never_raises_when_crud_is_broken(monkeypatch):
    def _boom(customer_id, limit=8):
        raise RuntimeError("boom")
    monkeypatch.setattr(crud, "get_recent_messages", _boom)
    result = classify_with_context("Two.", customer_id=42, products=PRODUCTS)
    assert result.intent == "unknown"  # degrades to the plain base classification
