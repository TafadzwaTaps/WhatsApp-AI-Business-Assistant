"""Customer-chat fixes: #14 natural-language order tracking, #10 handoff messages, #8 product search / sold out.
WhatsApp is mocked; nothing is sent."""
import asyncio
import types

import pytest

import crud
import routes.chat_routes as cr
import workflows.human_handoff as hh
from services._ai_intent import _is_order_status_inquiry as inquiry
from test_stage3_cart_lifecycle import world, PHONE, OTHER, TEA2, state   # noqa: F401  (world = fixture)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═════════ #14 tracking phrases ═════════
@pytest.mark.parametrize("text", [
    "Where is my order?", "Track my order", "Is my order ready?", "Has my order shipped?",
    "When can I collect my order?", "when can i pick up my order", "can i collect my order",
    "how long until my order is ready", "is my order ready for collection", "did my order ship", "track order",
])
def test_tracking_phrases_are_recognised(text):
    assert inquiry(text) is True


@pytest.mark.parametrize("text", [
    "is it ready to checkout", "I want a refund, where is my order", "cancel my order", "where is the menu",
    "hello", "my order never arrived", "I want my money back",
])
def test_non_tracking_and_refund_wording_is_not_tracking(text):
    assert inquiry(text) is False


def test_tracking_answers_from_the_customers_own_order(world):
    world.place_order(status="ready", fulfillment_method="pickup")
    out = world.say("When can I collect my order?")
    assert "ORDER-" in out and "Ready for pickup" in out


def test_tracking_works_mid_checkout(world):
    world.place_order(status="preparing", fulfillment_method="pickup")
    world.put_cart(TEA2, state="checkout", session={"cart_snapshot": TEA2})
    out = world.say("where is my order?")
    assert "ORDER-" in out and "Order Status" in out and state(world) == "checkout"


def test_several_open_orders_ask_which_one(world):
    a = world.place_order()
    b = world.place_order(status="confirmed")
    out = world.say("track my order")
    assert "more than one order" in out and f"ORDER-{a}" in out and f"ORDER-{b}" in out


def test_another_customers_order_is_never_shown(world):
    world.place_order(phone=OTHER, status="ready")
    out = world.say("where is my order?")
    assert "ORDER-" not in out or "couldn't find" in out.lower()
    assert "Order Status" not in out


def test_tracking_is_not_routed_into_the_refund_flow(world):
    world.place_order(status="ready", fulfillment_method="pickup")
    out = world.say("where is my order")
    assert "refund" not in out.lower() and "dispute" not in out.lower()
    assert world.handoffs == []


def test_refund_wording_still_goes_to_the_refund_flow(world):
    world.place_order(status="ready", fulfillment_method="pickup")
    world.say("I want a refund for my order")
    assert world.handoffs, "an explicit refund request must still be handled by the refund flow"


def test_help_text_no_longer_tells_customers_to_memorise_an_order_id(world):
    out = world.say("hi")
    assert "where is my order" in out and "ORDER-9" not in out


# ═════════ #10 handoff wording ═════════
def test_owner_initiated_handoff_does_not_claim_the_customer_asked():
    msg = hh.handoff_acknowledgement("Flavoury Foods", ticket="SUP-1", reason="Payment Issue", agent_initiated=True)
    assert "Flavoury Foods" in msg and "joining this conversation" in msg
    assert "5–15" not in msg and "Reason" not in msg and "Ticket" not in msg and "Payment Issue" not in msg
    assert "Connecting you to a human" not in msg


def test_customer_requested_handoff_keeps_the_ticket_but_no_invented_estimate():
    msg = hh.handoff_acknowledgement("Shop", ticket="SUP-9", reason="Payment Issue")
    assert "SUP-9" in msg and "Payment Issue" in msg and "5–15" not in msg and "Estimated Response" not in msg
    assert "5–15" not in hh.handoff_acknowledgement("Shop")


def test_an_estimate_is_shown_only_when_supplied():
    assert "20 minutes" in hh.handoff_acknowledgement("Shop", ticket="T", eta="20 minutes")
    assert "20 minutes" in hh.handoff_acknowledgement("Shop", agent_initiated=True, eta="20 minutes")
    assert "business name" not in hh.handoff_acknowledgement("Test Biz")
    assert "Test Biz" in hh.handoff_acknowledgement("Test Biz")


@pytest.fixture
def ho(monkeypatch):
    st = types.SimpleNamespace(sent=[], paused=False, notice_recent=False, resets=0)
    biz = {"id": 1, "name": "Flavoury Foods", "whatsapp_phone_id": "PID"}
    monkeypatch.setattr(cr, "_handoff_get_customer", lambda cid, bid, user, action: {"id": cid, "phone": "2637"})
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: biz)
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOK")
    monkeypatch.setattr(cr, "send_whatsapp", lambda pid, tok, phone, text: st.sent.append(text) or {})
    monkeypatch.setattr(cr, "manager", None, raising=False)
    import services._ai_state as ai_state
    monkeypatch.setattr(ai_state, "is_in_human_handoff", lambda p, b: st.paused)

    def set_paused(p, b):
        st.paused = True
    monkeypatch.setattr(ai_state, "_set_human_handoff", set_paused)
    monkeypatch.setattr(ai_state, "_write_state_data", lambda *a, **k: True)

    def reset(p, b):
        st.paused = False
        st.resets += 1
    monkeypatch.setattr(ai_state, "_reset_state", reset)
    monkeypatch.setattr(hh, "notify_dashboard", lambda *a, **k: None)
    monkeypatch.setattr(hh, "clear_handoff_flag", lambda *a, **k: None)
    monkeypatch.setattr(hh, "handoff_notice_seen_recently", lambda p, b, within_seconds=10: st.notice_recent)
    return st


USER = {"business_id": 1, "username": "owner", "role": "business"}


def _pause(reason="Payment Issue"):
    return _run(cr.handoff_request_with_reason(1, cr.HandoffReasonRequest(reason=reason, priority="normal"), user=USER))


def test_pausing_the_ai_tells_the_customer_a_team_member_joined(ho):
    out = _pause()
    assert out["ok"] is True and len(ho.sent) == 1
    assert "joining this conversation" in ho.sent[0] and "5–15" not in ho.sent[0] and "Reason" not in ho.sent[0]


def test_pausing_twice_sends_only_one_message(ho):
    _pause()
    _pause()
    assert len(ho.sent) == 1


def test_a_quick_undo_and_pause_does_not_message_again(ho):
    ho.notice_recent = True
    _pause()
    assert ho.sent == []


def test_plain_request_route_uses_the_same_wording(ho):
    _run(cr.handoff_request(1, user=USER))
    assert len(ho.sent) == 1 and "joining this conversation" in ho.sent[0] and "5–15" not in ho.sent[0]
    _run(cr.handoff_request(1, user=USER))
    assert len(ho.sent) == 1


def test_resume_is_announced_only_when_the_ai_was_really_paused(ho):
    _pause()
    ho.sent.clear()
    _run(cr.handoff_release(1, user=USER))
    assert len(ho.sent) == 1 and "resumed" in ho.sent[0]
    ho.sent.clear()
    _run(cr.handoff_release(1, user=USER))                     # not paused any more: nothing to announce
    assert ho.sent == []


def test_pause_and_resume_still_work(ho):
    _pause()
    assert ho.paused is True
    _run(cr.handoff_release(1, user=USER))
    assert ho.paused is False and ho.resets >= 1


# ═════════ #8 search and sold out ═════════
def test_search_finds_an_item_and_shows_price_and_availability(world):
    out = world.say("search cup")
    assert "Cup" in out and "Available" in out and "3.00" in out


def test_search_says_plainly_when_an_item_is_sold_out(world):
    world.db.tables["products"][0]["stock"] = 0
    out = world.say("find tea")
    assert "Sold out" in out and "Tea" in out


def test_search_warns_about_low_stock(world):
    assert "Only 5 left" in world.say("looking for tea")


def test_search_reports_when_the_item_is_already_in_the_cart(world):
    world.put_cart(TEA2, state="browsing")
    assert "already have 2 in your cart" in world.say("search tea")


def test_search_for_something_we_do_not_sell_says_so(world):
    out = world.say("search unicorn saddle")
    assert "couldn't find" in out and "menu" in out


def test_search_never_changes_the_cart(world):
    world.put_cart(TEA2, state="browsing")
    world.say("search cup")
    assert [i["name"] for i in world.db.cart_row()["items"]] == ["Tea"]
