"""
tests/test_phase4_nl_commerce.py — Phase 4 (Natural Language Commerce) tests.

Two layers:
  1. Pure unit tests for services/nl_commerce.py's parsing/filtering
     functions — no DB, no state, just text/data in -> value out.
  2. Light end-to-end tests through services.ai.generate_reply() for the
     four new state-machine branches (P6.5-P6.8), with the DB-touching
     helpers monkeypatched to simple in-memory fakes so these run without
     a real Supabase connection.
"""

import pytest

import crud
import services.ai as ai
from services.nl_commerce import (
    detect_quantity_correction,
    detect_substitution_phrase,
    match_cart_item_by_text,
    is_recommendation_query,
    extract_price_ceiling,
    filter_recommended_products,
    resolve_comparative_reference,
)


# ═════════════════════════════════════════════════════════════════════════
# Unit tests — detect_quantity_correction
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("make it 3", 3),
    ("make that three", 3),
    ("Make It 5.", 5),
    ("change it to 10", 10),
    ("update the quantity to 2", 2),
    ("update it to one", 1),
])
def test_quantity_correction_detects_explicit_phrases(text, expected):
    assert detect_quantity_correction(text) == expected


@pytest.mark.parametrize("text", [
    "3", "three", "2 beef", "hello", "chicken burger",
    "make it spicy", "change it up",
])
def test_quantity_correction_ignores_everything_else(text):
    assert detect_quantity_correction(text) is None


# ═════════════════════════════════════════════════════════════════════════
# Unit tests — detect_substitution_phrase / match_cart_item_by_text
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,frm,to", [
    ("change the chicken to beef", "chicken", "beef"),
    ("swap fries for onion rings", "fries", "onion rings"),
    ("replace the coke with sprite", "coke", "sprite"),
    ("switch my burger to a wrap", "burger", "wrap"),
])
def test_substitution_phrase_parses_from_and_to(text, frm, to):
    result = detect_substitution_phrase(text)
    assert result == (frm, to)


@pytest.mark.parametrize("text", [
    "hello", "add a coke", "change my mind", "change",
])
def test_substitution_phrase_ignores_non_matching_text(text):
    assert detect_substitution_phrase(text) is None


def test_match_cart_item_by_text_finds_containment_match():
    cart = [{"name": "Chicken Burger", "qty": 1, "price": 8}, {"name": "Coke", "qty": 2, "price": 2}]
    assert match_cart_item_by_text(cart, "chicken")["name"] == "Chicken Burger"
    assert match_cart_item_by_text(cart, "coke")["name"] == "Coke"
    assert match_cart_item_by_text(cart, "fries") is None
    assert match_cart_item_by_text([], "chicken") is None


# ═════════════════════════════════════════════════════════════════════════
# Unit tests — recommendation query detection + filtering
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text", [
    "what do you recommend?", "any suggestions?", "something for dinner",
    "something cheap", "what's good", "anything under $10?",
    "do you have anything under 5",
])
def test_is_recommendation_query_true_cases(text):
    assert is_recommendation_query(text) is True


@pytest.mark.parametrize("text", [
    "hello", "2 chicken burgers", "how much is delivery",
    "my order is under review",  # "under" present but no price/context match
])
def test_is_recommendation_query_false_cases(text):
    assert is_recommendation_query(text) is False


def test_extract_price_ceiling_variants():
    assert extract_price_ceiling("anything under $10?") == 10.0
    assert extract_price_ceiling("something below 7.5") == 7.5
    assert extract_price_ceiling("cheaper than $20") == 20.0
    assert extract_price_ceiling("what do you recommend") is None


PRODUCTS = [
    {"id": 1, "name": "Chicken Burger", "price": 8.0, "stock": 5},
    {"id": 2, "name": "Beef Burger", "price": 9.0, "stock": 0},
    {"id": 3, "name": "Coke", "price": 2.0, "stock": 10},
    {"id": 4, "name": "Fries", "price": 3.0, "stock": None},
    {"id": 5, "name": "Deluxe Platter", "price": 25.0, "stock": 3},
]


def test_filter_recommended_products_excludes_out_of_stock_and_sorts_by_price():
    picks = filter_recommended_products(PRODUCTS)
    names = [p["name"] for p in picks]
    assert "Beef Burger" not in names  # stock == 0
    assert names == sorted(names, key=lambda n: next(p["price"] for p in PRODUCTS if p["name"] == n))
    assert len(picks) <= 3


def test_filter_recommended_products_respects_price_ceiling():
    picks = filter_recommended_products(PRODUCTS, max_price=5)
    assert all(p["price"] <= 5 for p in picks)
    assert all(p["name"] != "Deluxe Platter" for p in picks)


def test_filter_recommended_products_never_invents_a_product():
    picks = filter_recommended_products(PRODUCTS, max_price=1000)
    for p in picks:
        assert p in PRODUCTS  # every result is literally one of the real rows


def test_filter_recommended_products_service_business_ignores_stock():
    picks = filter_recommended_products(PRODUCTS, is_service_business=True, limit=10)
    names = [p["name"] for p in picks]
    assert "Beef Burger" in names  # stock=0 no longer excluded for services


# ═════════════════════════════════════════════════════════════════════════
# Unit tests — resolve_comparative_reference
# ═════════════════════════════════════════════════════════════════════════

SHOWN = [{"name": "Coke", "price": 2.0}, {"name": "Fries", "price": 3.0}]


def test_comparative_reference_cheaper_and_pricier():
    assert resolve_comparative_reference("I'll take the cheaper one", SHOWN)["name"] == "Coke"
    assert resolve_comparative_reference("give me the more expensive one", SHOWN)["name"] == "Fries"


def test_comparative_reference_first_and_second():
    assert resolve_comparative_reference("the first one please", SHOWN)["name"] == "Coke"
    assert resolve_comparative_reference("I want the second one", SHOWN)["name"] == "Fries"


def test_comparative_reference_returns_none_when_nothing_was_shown():
    assert resolve_comparative_reference("the cheaper one", []) is None


def test_comparative_reference_returns_none_for_unrelated_text():
    assert resolve_comparative_reference("hello there", SHOWN) is None


# ═════════════════════════════════════════════════════════════════════════
# End-to-end tests through services.ai.generate_reply()
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def ai_state(monkeypatch):
    """
    Minimal in-memory fakes for the DB-touching helpers generate_reply()
    calls, so the new P6.5-P6.8 branches can be exercised without a real
    Supabase connection. Returns a dict the test can inspect/mutate:
      {"cart": [...], "session": {...}, "state": "browsing"}
    """
    store = {"cart": [], "session": {}, "state": "browsing"}

    monkeypatch.setattr(ai, "_get_state", lambda phone, biz: store["state"])
    monkeypatch.setattr(ai, "_load_cart", lambda phone, biz: store["cart"])
    monkeypatch.setattr(ai, "_save_cart", lambda phone, biz, cart: store.update(cart=cart))
    monkeypatch.setattr(ai, "_get_session", lambda phone, biz: store["session"])

    def _write_state_data(phone, biz, patch):
        if "session" in patch:
            store["session"] = patch["session"]
        if "state" in patch:
            store["state"] = patch["state"]

    monkeypatch.setattr(ai, "_write_state_data", _write_state_data)
    monkeypatch.setattr(crud, "get_product_by_name", lambda biz, name: next(
        (p for p in PRODUCTS if p["name"] == name), None
    ))
    return store


def _reply(text, ai_state, cart=None, session=None):
    if cart is not None:
        ai_state["cart"] = cart
    if session is not None:
        ai_state["session"] = session
    return ai.generate_reply(
        message=text, phone="+1555", business_id=1, business_name="Test Biz",
        products=PRODUCTS,
    )


def test_quantity_correction_updates_single_item_cart(ai_state):
    cart = [{"name": "Chicken Burger", "qty": 1, "price": 8.0}]
    reply = _reply("make it 3", ai_state, cart=cart)
    assert "×3" in reply or "x3" in reply.lower()
    assert ai_state["cart"][0]["qty"] == 3


def test_quantity_correction_asks_instead_of_guessing_when_cart_has_multiple_items(ai_state):
    cart = [
        {"name": "Chicken Burger", "qty": 1, "price": 8.0},
        {"name": "Coke", "qty": 1, "price": 2.0},
    ]
    reply = _reply("make it 3", ai_state, cart=cart)
    assert "which item" in reply.lower()
    # Neither item's quantity was silently changed.
    assert ai_state["cart"][0]["qty"] == 1
    assert ai_state["cart"][1]["qty"] == 1


def test_quantity_correction_respects_stock_limit(ai_state):
    cart = [{"name": "Coke", "qty": 1, "price": 2.0}]  # Coke stock=10
    reply = _reply("make it 999", ai_state, cart=cart)
    assert "only" in reply.lower()
    assert ai_state["cart"][0]["qty"] == 1  # unchanged


def test_product_substitution_swaps_cart_item(ai_state):
    cart = [{"name": "Chicken Burger", "qty": 2, "price": 8.0}]
    reply = _reply("change the chicken to beef", ai_state, cart=cart)
    # Beef Burger has stock=0 in PRODUCTS, so the swap should be blocked,
    # not silently performed with an out-of-stock item.
    assert "beef burger" in reply.lower()
    assert "available" in reply.lower()
    assert ai_state["cart"][0]["name"] == "Chicken Burger"  # unchanged, nothing invented


def test_product_substitution_succeeds_with_in_stock_target(ai_state):
    cart = [{"name": "Chicken Burger", "qty": 2, "price": 8.0}]
    reply = _reply("change the chicken to fries", ai_state, cart=cart)
    assert "swapped" in reply.lower()
    names = [i["name"] for i in ai_state["cart"]]
    assert "Fries" in names
    assert "Chicken Burger" not in names
    assert next(i for i in ai_state["cart"] if i["name"] == "Fries")["qty"] == 2


def test_product_substitution_falls_through_when_source_not_in_cart(ai_state):
    cart = [{"name": "Coke", "qty": 1, "price": 2.0}]
    # "chicken" isn't in the cart at all — must not invent a removal, and
    # must not crash; falls through to the normal add-to-cart flow instead.
    reply = _reply("change the chicken to fries", ai_state, cart=cart)
    assert ai_state["cart"][0]["name"] == "Coke"  # original item untouched


def test_recommendation_query_lists_real_in_stock_products_and_remembers_them(ai_state):
    reply = _reply("what do you recommend?", ai_state)
    assert "Coke" in reply or "Fries" in reply
    assert "Beef Burger" not in reply  # out of stock, never suggested
    assert ai_state["session"].get("last_shown_products")
    shown_names = [p["name"] for p in ai_state["session"]["last_shown_products"]]
    assert "Beef Burger" not in shown_names


def test_recommendation_query_with_price_ceiling_never_invents_when_nothing_fits(ai_state):
    reply = _reply("anything under $0.50?", ai_state)
    assert "don't have anything" in reply.lower()


def test_comparative_reference_resolves_after_recommendation(ai_state):
    session = {"last_shown_products": [
        {"name": "Coke", "price": 2.0, "id": 3},
        {"name": "Fries", "price": 3.0, "id": 4},
    ]}
    reply = _reply("I'll take the cheaper one", ai_state, cart=[], session=session)
    assert "coke" in reply.lower()
    assert any(i["name"] == "Coke" for i in ai_state["cart"])


def test_comparative_reference_does_nothing_when_nothing_was_shown(ai_state):
    reply = _reply("I'll take the cheaper one", ai_state, cart=[], session={})
    # No last_shown_products -> falls through; cart stays empty since
    # "I'll take the cheaper one" also fails to fuzzy-match any real product.
    assert ai_state["cart"] == []


def test_the_usual_triggers_existing_reorder_flow(ai_state, monkeypatch):
    monkeypatch.setattr(ai, "_get_memory", lambda phone, biz: {"last_orders": [["Coke"]]})
    reply = _reply("the usual", ai_state, cart=[])
    assert "rebuilt" in reply.lower()
    assert any(i["name"] == "Coke" for i in ai_state["cart"])
