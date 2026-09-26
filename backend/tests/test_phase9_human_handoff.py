"""
tests/test_phase9_human_handoff.py — Phase 9 (Smart Human Handoff) tests.

Two layers, same pattern as Phases 4-8's tests:
  1. Pure unit tests for services/handoff_triggers.py's detectors and its
     build_handoff_summary() formatter — no DB.
  2. End-to-end tests through services.ai.generate_reply() covering every
     spec trigger: explicit request (regression), business-specific rule,
     sensitive issue, booking conflict complaint, serious complaint,
     payment/refund dispute, repeated abusive language, complex request,
     and repeated misunderstanding — plus a guardrail confirming the
     internal AI summary never leaks into the customer-facing reply.
"""

import pytest

import services.ai as ai
import services.handoff_triggers as ht


# ═════════════════════════════════════════════════════════════════════════
# Pure detectors
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text", [
    "This is absolutely unacceptable",
    "worst service I've ever had",
    "I'm furious about this",
    "how dare you treat me like this",
    "still hasn't been resolved and it's been weeks",
])
def test_is_serious_complaint_true_cases(text):
    assert ht.is_serious_complaint(text) is True


@pytest.mark.parametrize("text", ["hi", "how much is this", "thanks!", "I'll take two"])
def test_is_serious_complaint_false_cases(text):
    assert ht.is_serious_complaint(text) is False


@pytest.mark.parametrize("text", [
    "I got food poisoning from your food",
    "someone got hurt eating this",
    "I'm going to sue you",
    "this is discrimination",
    "I need to speak to my lawyer about this",
])
def test_is_sensitive_issue_true_cases(text):
    assert ht.is_sensitive_issue(text) is True


@pytest.mark.parametrize("text", ["hi", "what's on the menu", "I want a refund"])
def test_is_sensitive_issue_false_cases(text):
    assert ht.is_sensitive_issue(text) is False


@pytest.mark.parametrize("text", [
    "you double booked me",
    "someone else was in my slot when I arrived",
    "I showed up and nobody was there",
    "you booked me at the wrong time",
])
def test_is_booking_conflict_complaint_true_cases(text):
    assert ht.is_booking_conflict_complaint(text) is True


@pytest.mark.parametrize("text", ["can I book for tomorrow", "what times are free"])
def test_is_booking_conflict_complaint_false_cases(text):
    assert ht.is_booking_conflict_complaint(text) is False


def test_is_complex_request_true_for_multiple_questions():
    assert ht.is_complex_request("Do you deliver to Avondale? And do you have vegetarian options?") is True


def test_is_complex_request_true_for_long_message_with_connector():
    text = (
        "I wanted to ask about the large pizza and also need to know if you "
        "deliver on weekends because I'm hosting a party this Saturday"
    )
    assert ht.is_complex_request(text) is True


def test_is_complex_request_false_for_ordinary_short_message():
    assert ht.is_complex_request("2 burgers and a coke please") is False


def test_is_complex_request_false_for_empty_text():
    assert ht.is_complex_request("") is False


def test_matches_business_escalation_rule_string_config():
    cfg = {"escalation_keywords": "warranty claim, franchise dispute"}
    assert ht.matches_business_escalation_rule("I have a warranty claim", cfg) == "warranty claim"


def test_matches_business_escalation_rule_list_config():
    cfg = {"escalation_keywords": ["lawsuit", "recall"]}
    assert ht.matches_business_escalation_rule("this product needs a recall", cfg) == "recall"


def test_matches_business_escalation_rule_no_config_returns_none():
    assert ht.matches_business_escalation_rule("hello", {}) is None
    assert ht.matches_business_escalation_rule("hello", None) is None


def test_matches_business_escalation_rule_no_match_returns_none():
    cfg = {"escalation_keywords": "warranty claim"}
    assert ht.matches_business_escalation_rule("hi there", cfg) is None


def test_build_handoff_summary_matches_spec_shape():
    summary = ht.build_handoff_summary(
        customer_name="John",
        issue="Customer wants to exchange a damaged product.",
        order_ref="ORDER-182",
        purchase_lines=["2 × Blue Shirt"],
        purchase_total=40.0,
        currency_sym="$",
        request_text="Exchange for Medium.",
        ai_summary="Customer wants a size exchange.\nNo refund requested.",
    )
    assert "CUSTOMER" in summary and "John" in summary
    assert "ISSUE" in summary and "damaged product" in summary
    assert "ORDER" in summary and "ORDER-182" in summary
    assert "PURCHASE" in summary and "2 × Blue Shirt" in summary and "$40.00" in summary
    assert "REQUEST" in summary and "Exchange for Medium." in summary
    assert "AI SUMMARY" in summary and "size exchange" in summary


def test_build_handoff_summary_never_invents_missing_data():
    summary = ht.build_handoff_summary(customer_name="", issue="")
    assert "Unknown" in summary
    assert "Not specified" in summary
    assert "None on file" in summary
    assert "Not available" in summary


# ═════════════════════════════════════════════════════════════════════════
# End-to-end through services.ai.generate_reply()
# ═════════════════════════════════════════════════════════════════════════

RETAIL_BIZ = {"id": 1, "is_service_business": False}
PRODUCTS = [{"id": 1, "name": "Burger", "price": 5.0, "stock": 10}]


@pytest.fixture
def ai_state(monkeypatch):
    store = {"cart": [], "state_data": {"state": "browsing", "session": {}}}

    def _get_state(phone, biz):
        return store["state_data"].get("state", "browsing")

    def _get_session(phone, biz):
        return store["state_data"].get("session") or {}

    def _read_state_data(phone, biz):
        return store["state_data"]

    def _write_state_data(phone, biz, patch):
        store["state_data"].update(patch)

    def _reset_state(phone, biz):
        store["state_data"] = {"state": "browsing", "session": {}}

    def _set_human_handoff(phone, biz):
        store["state_data"]["state"] = "human_handoff"

    monkeypatch.setattr(ai, "_get_state", _get_state)
    monkeypatch.setattr(ai, "_get_session", _get_session)
    monkeypatch.setattr(ai, "_read_state_data", _read_state_data)
    monkeypatch.setattr(ai, "_write_state_data", _write_state_data)
    monkeypatch.setattr(ai, "_reset_state", _reset_state)
    monkeypatch.setattr(ai, "_set_human_handoff", _set_human_handoff)
    monkeypatch.setattr(ai, "_load_cart", lambda phone, biz: store["cart"])
    monkeypatch.setattr(ai, "_save_cart", lambda phone, biz, cart: store.update(cart=cart))

    import crud
    monkeypatch.setattr(crud, "get_business_by_id", lambda biz_id: RETAIL_BIZ)
    monkeypatch.setattr(crud, "get_user_memory", lambda phone, biz: None)
    monkeypatch.setattr(crud, "save_user_memory", lambda *a, **k: None)
    monkeypatch.setattr(crud, "get_product_by_name",
                         lambda biz, name: next((p for p in PRODUCTS if p["name"] == name), None))
    monkeypatch.setattr(crud, "get_or_create_customer", lambda phone, biz: {"id": 7})
    monkeypatch.setattr(crud, "get_order_by_id", lambda oid, biz: None)

    from core import plan_guard
    monkeypatch.setattr(plan_guard, "feature_access",
                         lambda feature_key, business_id: {"allowed": True})

    return store


def _reply(text, ai_state):
    return ai.generate_reply(
        message=text, phone="+1555", business_id=1, business_name="Test Shop",
        products=PRODUCTS, business_config=RETAIL_BIZ,
    )


def test_explicit_human_request_still_hands_off(ai_state):
    reply = _reply("I want to talk to a human", ai_state)
    assert "human agent" in reply.lower() or "support team" in reply.lower()
    assert ai_state["state_data"]["state"] == "human_handoff"
    assert ai_state["state_data"]["session"].get("handoff_summary")


def test_business_escalation_rule_triggers_handoff(ai_state):
    biz_with_rule = {**RETAIL_BIZ, "escalation_keywords": "warranty claim"}
    reply = ai.generate_reply(
        message="I have a warranty claim on this", phone="+1555", business_id=1,
        business_name="Test Shop", products=PRODUCTS, business_config=biz_with_rule,
    )
    assert ai_state["state_data"]["state"] == "human_handoff"
    assert "warranty claim" in ai_state["state_data"]["session"]["handoff_summary"]


def test_sensitive_issue_triggers_immediate_handoff(ai_state):
    reply = _reply("I got food poisoning from your restaurant", ai_state)
    assert ai_state["state_data"]["state"] == "human_handoff"
    assert "sensitive" in ai_state["state_data"]["session"]["handoff_reason"].lower()


def test_booking_conflict_complaint_triggers_handoff(ai_state):
    reply = _reply("you double booked me and I showed up for nothing", ai_state)
    assert ai_state["state_data"]["state"] == "human_handoff"
    assert "booking" in ai_state["state_data"]["session"]["handoff_reason"].lower()


def test_serious_complaint_triggers_handoff(ai_state):
    reply = _reply("This is absolutely unacceptable service", ai_state)
    assert ai_state["state_data"]["state"] == "human_handoff"


def test_refund_dispute_now_triggers_real_handoff(ai_state):
    reply = _reply("I want a refund, this is a dispute", ai_state)
    assert ai_state["state_data"]["state"] == "human_handoff"
    assert "refund" in ai_state["state_data"]["session"]["handoff_summary"].lower()


def test_repeated_abuse_still_escalates_with_summary(ai_state):
    _reply("you are useless", ai_state)          # 1st offense — warning
    ai_state["state_data"]["state"] = "browsing"  # abuse block doesn't change state itself
    _reply("this is garbage service", ai_state)   # 2nd offense — stronger warning
    ai_state["state_data"]["state"] = "browsing"
    reply3 = _reply("you are an idiot", ai_state)  # 3rd offense — escalate
    assert "final notice" in reply3.lower() or "flagged" in reply3.lower()
    assert ai_state["state_data"]["state"] == "human_handoff"
    assert ai_state["state_data"]["session"].get("handoff_summary")


def test_complex_request_escalates_on_first_unmatched_occurrence(ai_state):
    text = (
        "Do you deliver to Borrowdale? And also can you tell me if you have "
        "gluten free options, and what's your return policy for damaged items?"
    )
    reply = _reply(text, ai_state)
    assert ai_state["state_data"]["state"] == "human_handoff"
    assert ai_state["state_data"]["session"]["handoff_reason"] == "Complex request"


def test_repeated_misunderstanding_escalates_after_threshold(ai_state):
    r1 = _reply("asdkjfh qwoeiru", ai_state)
    assert ai_state["state_data"]["state"] != "human_handoff"
    r2 = _reply("zxcvxcv blah blah", ai_state)
    assert ai_state["state_data"]["state"] != "human_handoff"
    r3 = _reply("qqqqq wwwww", ai_state)
    assert ai_state["state_data"]["state"] == "human_handoff"
    assert ai_state["state_data"]["session"]["handoff_reason"] == "Repeated misunderstanding"


def test_misunderstanding_counter_resets_after_understood_message(ai_state):
    _reply("asdkjfh qwoeiru", ai_state)
    assert ai_state["state_data"]["session"].get("misunderstood_count", 0) == 1
    _reply("menu", ai_state)  # understood — resets the streak
    assert ai_state["state_data"]["session"].get("misunderstood_count", 0) == 0


def test_internal_summary_never_appears_in_customer_facing_reply(ai_state):
    reply = _reply("I got food poisoning from your restaurant", ai_state)
    summary = ai_state["state_data"]["session"]["handoff_summary"]
    assert summary not in reply
    assert "AI SUMMARY" not in reply
