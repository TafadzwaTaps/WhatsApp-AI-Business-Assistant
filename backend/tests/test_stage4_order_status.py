"""Stage 4: "Where is my order?" is an order-STATUS inquiry, not a refund/dispute. Refund, return, dispute and
cancellation requests keep their own flows."""
import re

import pytest

from test_stage3_cart_lifecycle import world, PHONE, OTHER, TEA2, state   # noqa: F401  (shared fake database)
from services import _ai_lifecycle as lc
from services._ai_intent import _is_order_status_inquiry as inquiry, _is_refund_request as refund


@pytest.mark.parametrize("text", [
    "Where is my order?", "where is my order", "Where's my order", "where is my food", "where is my delivery",
    "Has my order shipped?", "has my order been delivered", "When will my order arrive?",
    "what time will my order arrive", "is my order ready", "order status", "track my order",
    "status of my order", "where is the order",
])
def test_status_phrases_are_status_inquiries_and_not_refunds(text):
    assert inquiry(text) is True and refund(text) is False


@pytest.mark.parametrize("text,is_refund", [
    ("I want a refund", True), ("My order never arrived and I want my money back", True),
    ("give me my money back", True), ("this is a dispute", True), ("I never got my order", True),
    ("where is my refund", True),
])
def test_refund_and_dispute_requests_are_not_status_inquiries(text, is_refund):
    assert refund(text) is is_refund and inquiry(text) is False


@pytest.mark.parametrize("text", ["I want to cancel my order", "cancel my order", "where is my order, cancel it"])
def test_cancellation_wording_is_not_a_status_inquiry(text):
    assert inquiry(text) is False


@pytest.mark.parametrize("text", ["hello", "where is the menu", "where is my cart", "I want 2 burgers", "", "where are you located"])
def test_unrelated_messages_are_not_status_inquiries(text):
    assert inquiry(text) is False


# ── end to end ───────────────────────────────────────────────────────────────
def test_one_open_order_gets_its_status_and_delivery_info_and_no_handoff(world):
    oid = world.place_order(status="preparing", fulfillment_method="delivery", delivery_address="12 High St")
    world.put_cart([], state="browsing")
    out = world.say("Where is my order?")
    assert f"ORDER-{oid}" in out and "12 High St" in out
    assert world.handoffs == [] and state(world) == "browsing"


def test_pickup_orders_show_pickup_information(world):
    oid = world.place_order(status="ready", fulfillment_method="pickup")
    world.put_cart([], state="browsing")
    out = world.say("has my order shipped?")
    assert f"ORDER-{oid}" in out and "ready for collection" in out


def test_several_open_orders_ask_which_one(world):
    a = world.place_order()
    b = world.place_order(status="confirmed")
    world.put_cart([], state="browsing")
    out = world.say("when will my order arrive?")
    assert f"ORDER-{a}" in out and f"ORDER-{b}" in out and "Which one" in out
    assert world.handoffs == []


def test_naming_the_order_answers_just_that_order(world):
    a = world.place_order()
    b = world.place_order(status="confirmed")
    world.put_cart([], state="browsing")
    out = world.say(f"where is my order ORDER-{a}")
    assert f"ORDER-{a}" in out and f"ORDER-{b}" not in out


def test_never_discloses_another_customers_order(world):
    theirs = world.place_order(phone=OTHER, status="preparing", fulfillment_method="delivery",
                               delivery_address="SECRET ROAD 9")
    world.put_cart([], state="browsing")
    out = world.say("where is my order")
    assert "SECRET ROAD" not in out and not re.search(rf"ORDER-{theirs}\b", out) and "couldn't find any orders" in out
    out = world.say(f"where is my order ORDER-{theirs}")
    assert "SECRET ROAD" not in out and "couldn't find" in out


def test_a_delivered_order_is_reported_as_delivered(world):
    oid = world.place_order(status="delivered", fulfillment_method="delivery")
    world.put_cart([], state="browsing")
    out = world.say("where is my order")
    assert f"ORDER-{oid}" in out and "delivered" in out.lower() and world.handoffs == []


def test_a_cancelled_latest_order_is_reported_as_cancelled(world):
    oid = world.place_order(status="cancelled")
    world.put_cart([], state="browsing")
    out = world.say("where is my order")
    assert "cancelled" in out and f"ORDER-{oid}" in out and world.handoffs == []


def test_no_orders_says_so_without_a_refund_flow(world):
    world.put_cart([], state="browsing")
    out = world.say("where is my order")
    assert "couldn't find any orders" in out and world.handoffs == []


def test_status_question_while_waiting_to_pay_keeps_the_payment_step(world):
    oid = world.place_order()
    pp = {"order_id": oid, "method": "ecocash", "reference": f"ORDER-{oid}"}
    world.put_cart([], state="awaiting_payment", session={}, pending_payment=pp)
    out = world.say("where is my order")
    assert f"ORDER-{oid}" in out and state(world) == "awaiting_payment"
    assert world.db.cart_row()["state_data"]["pending_payment"] == pp


def test_status_lookup_failure_is_safe(world):
    world.place_order()
    world.put_cart([], state="browsing")
    world.db.fail_tables[("orders", "select")] = True
    out = world.say("where is my order")
    assert "couldn't look up" in out and world.handoffs == []


def test_explicit_refund_and_dispute_still_reach_the_human_handoff(world):
    world.place_order()
    for text in ("I want a refund", "My order never arrived and I want my money back"):
        world.put_cart([], state="browsing")
        assert world.say(text) == "HANDOFF"
    assert len(world.handoffs) == 2 and all("refund" in h.lower() for h in world.handoffs)


def test_cancellation_still_asks_for_confirmation(world):
    oid = world.place_order()
    world.put_cart([], state="browsing")
    out = world.say("I want to cancel my order")
    assert f"Cancel ORDER-{oid}?" in out and world.handoffs == []


def test_fulfilment_line_variants():
    assert "12 High" in lc.fulfilment_line({"fulfillment_method": "delivery", "delivery_address": "12 High"})
    assert "no address" in lc.fulfilment_line({"fulfillment_method": "delivery"})
    assert "out for delivery" in lc.fulfilment_line({"fulfillment_method": "delivery", "status": "out_for_delivery"})
    assert lc.fulfilment_line({}) == ""
