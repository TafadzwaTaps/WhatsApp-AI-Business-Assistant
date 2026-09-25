"""
tests/test_phase5_business_knowledge.py — Phase 5 (Business Knowledge Layer) tests.

Two layers, same pattern as Phase 4's tests:
  1. Pure unit tests for services/business_knowledge.py — category
     detection and per-category answer builders, no DB.
  2. End-to-end tests through services.ai.generate_reply() for the new
     P11.5 branch, with crud.get_business_by_id monkeypatched to a fake
     business record so these run without a real Supabase connection.
"""

import pytest

import crud
import services.ai as ai
from services.business_knowledge import (
    detect_business_info_category,
    build_business_info_answer,
    answer_hours,
    answer_location,
    answer_delivery_fee,
    answer_payment_methods,
    answer_contact,
    answer_social,
    answer_about,
)


# ═════════════════════════════════════════════════════════════════════════
# Category detection
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("Are you open Sunday?", "hours"),
    ("what time do you open?", "hours"),
    ("do you deliver to Avondale?", "delivery_area"),
    ("what's your delivery fee?", "delivery_fee"),
    ("how much is delivery", "delivery_fee"),
    ("where are you located?", "location"),
    ("what's your address", "location"),
    ("what payment methods do you accept", "payment_methods"),
    ("how can i pay", "payment_methods"),
    ("what's your return policy", "return_policy"),
    ("can i cancel my order", "cancellation_policy"),
    ("how do bookings work", "booking_rules"),
    ("any promotions right now?", "promotions"),
    ("do you have an faq", "faq"),
    ("contact number please", "contact"),
    ("are you on instagram", "social"),
    ("tell me about your business", "about"),
])
def test_detect_business_info_category(text, expected):
    assert detect_business_info_category(text) == expected


@pytest.mark.parametrize("text", [
    "2 chicken burgers", "hello", "checkout", "what do you sell",
    "show me the menu", "",
])
def test_detect_business_info_category_none_for_unrelated_or_handled_elsewhere(text):
    # "what do you sell" / "show me the menu" are already answered by the
    # existing browse (P9) handler earlier in ai.py's priority chain — this
    # module must never claim them, or it would shadow working behavior.
    assert detect_business_info_category(text) is None


def test_phone_number_phrase_deliberately_not_claimed_here():
    """"phone number" is intentionally excluded from _CONTACT_PHRASES —
    the pre-existing human-handoff detector (workflows/human_handoff.py)
    already treats it as an explicit request for a human agent, and that
    check runs earlier in services/ai.py's priority chain than this Q&A
    layer. See the comment above _CONTACT_PHRASES."""
    assert detect_business_info_category("what's your phone number?") is None


def test_delivery_area_checked_before_delivery_fee_despite_shared_word():
    assert detect_business_info_category("do you deliver to Chitungwiza?") == "delivery_area"
    assert detect_business_info_category("how much is delivery?") == "delivery_fee"


# ═════════════════════════════════════════════════════════════════════════
# Per-category answer builders — real data present
# ═════════════════════════════════════════════════════════════════════════

def test_answer_hours_echoes_real_text_verbatim():
    biz = {"business_hours": "Mon-Fri 9am-5pm, Sat 10am-2pm"}
    assert "Mon-Fri 9am-5pm" in answer_hours(biz)


def test_answer_location_combines_address_and_city():
    biz = {"address": "12 Main St", "city": "Harare"}
    out = answer_location(biz)
    assert "12 Main St" in out and "Harare" in out


def test_answer_delivery_fee_formats_currency():
    assert "5.00" in answer_delivery_fee({"delivery_fee": 5}, "$")


def test_answer_delivery_fee_zero_means_free():
    assert "free" in answer_delivery_fee({"delivery_fee": 0}, "$").lower()


def test_answer_payment_methods_lists_all_configured():
    biz = {"cash_enabled": True, "ecocash_number": "0771234567", "paypal_email": "x@x.com"}
    out = answer_payment_methods(biz)
    assert "Cash" in out and "EcoCash" in out and "PayPal" in out
    assert "Bank transfer" not in out  # not configured for this business


def test_answer_contact_prefers_support_email_over_owner_email():
    biz = {"contact_phone": "+263771234567", "support_email": "help@biz.com", "owner_email": "owner@biz.com"}
    out = answer_contact(biz)
    assert "help@biz.com" in out
    assert "owner@biz.com" not in out


def test_answer_contact_falls_back_to_owner_email():
    biz = {"owner_email": "owner@biz.com"}
    assert "owner@biz.com" in answer_contact(biz)


def test_answer_social_lists_configured_platforms_only():
    out = answer_social({"instagram": "@mybiz"})
    assert "@mybiz" in out
    assert "Facebook" not in out


def test_answer_about_uses_real_description():
    out = answer_about({"description": "We sell fresh burgers daily."}, "Test Biz")
    assert "Test Biz" in out and "fresh burgers" in out


# ═════════════════════════════════════════════════════════════════════════
# Per-category answer builders — never invent when data is missing
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("fn,biz,args", [
    (answer_hours, {}, ()),
    (answer_hours, {"business_hours": ""}, ()),
    (answer_location, {}, ()),
    (answer_delivery_fee, {}, ("$",)),
    (answer_delivery_fee, {"delivery_fee": None}, ("$",)),
    (answer_payment_methods, {}, ()),
    (answer_contact, {}, ()),
    (answer_social, {}, ()),
    (answer_about, {}, ("Test Biz",)),
])
def test_answer_builders_return_none_when_no_real_data(fn, biz, args):
    assert fn(biz, *args) is None


@pytest.mark.parametrize("category", [
    "delivery_area", "return_policy", "cancellation_policy",
    "booking_rules", "promotions", "faq",
])
def test_no_data_model_categories_always_return_none(category):
    """These categories have no backing column at all — confirmed by
    audit — so they must never produce an answer, however complete the
    business record otherwise is."""
    fully_filled_biz = {
        "business_hours": "9-5", "address": "1 St", "city": "Harare",
        "delivery_fee": 3, "cash_enabled": True, "contact_phone": "+123",
        "support_email": "a@b.com", "instagram": "@x", "description": "desc",
    }
    assert build_business_info_answer(category, fully_filled_biz, "Test Biz", "$") is None


def test_build_business_info_answer_dispatches_correctly():
    biz = {"business_hours": "9am-5pm daily"}
    assert "9am-5pm" in build_business_info_answer("hours", biz, "Test Biz", "$")


def test_build_business_info_answer_unknown_category_returns_none():
    assert build_business_info_answer("not_a_real_category", {"business_hours": "9-5"}, "Test Biz", "$") is None


# ═════════════════════════════════════════════════════════════════════════
# End-to-end tests through services.ai.generate_reply()
# ═════════════════════════════════════════════════════════════════════════

PRODUCTS = [{"id": 1, "name": "Chicken Burger", "price": 8.0, "stock": 5}]


@pytest.fixture
def ai_state(monkeypatch):
    store = {"cart": [], "session": {}, "state": "browsing"}
    monkeypatch.setattr(ai, "_get_state", lambda phone, biz: store["state"])
    monkeypatch.setattr(ai, "_load_cart", lambda phone, biz: store["cart"])
    monkeypatch.setattr(ai, "_save_cart", lambda phone, biz, cart: store.update(cart=cart))
    monkeypatch.setattr(ai, "_get_session", lambda phone, biz: store["session"])
    monkeypatch.setattr(ai, "_write_state_data", lambda phone, biz, patch: None)
    return store


def _reply(text, ai_state, business_record):
    import crud as _crud
    import services.ai as _ai
    _ai_module = __import__("services.ai", fromlist=["crud"])
    orig = crud.get_business_by_id
    crud.get_business_by_id = lambda biz_id: business_record
    try:
        return ai.generate_reply(
            message=text, phone="+1555", business_id=1, business_name="Test Biz",
            products=PRODUCTS,
        )
    finally:
        crud.get_business_by_id = orig


def test_hours_question_answers_from_real_data(ai_state):
    reply = _reply("are you open sunday?", ai_state, {"business_hours": "Mon-Sun 9am-9pm"})
    assert "Mon-Sun 9am-9pm" in reply


def test_hours_question_falls_back_honestly_when_unset(ai_state):
    reply = _reply("what time do you open?", ai_state, {})
    assert "not sure about that yet" in reply.lower()
    assert "connect you with the team" in reply.lower()


def test_delivery_fee_question_answers_from_real_data(ai_state):
    reply = _reply("what's your delivery fee?", ai_state, {"delivery_fee": 4.5})
    assert "4.50" in reply


def test_return_policy_question_always_honest_fallback_no_data_model(ai_state):
    # Even a fully-configured business has no return_policy column at all.
    reply = _reply("what's your return policy?", ai_state, {
        "business_hours": "9-5", "address": "1 St", "delivery_fee": 3,
    })
    assert "not sure about that yet" in reply.lower()


def test_payment_methods_question_lists_real_configured_methods(ai_state):
    reply = _reply("how can i pay?", ai_state, {"cash_enabled": True, "ecocash_number": "0771234567"})
    assert "cash" in reply.lower() and "ecocash" in reply.lower()


def test_business_info_qna_never_fires_for_ordinary_ordering_message(ai_state):
    reply = _reply("2 chicken burgers", ai_state, {"business_hours": "9-5"})
    assert "chicken burger" in reply.lower()
    assert "not sure about that yet" not in reply.lower()
