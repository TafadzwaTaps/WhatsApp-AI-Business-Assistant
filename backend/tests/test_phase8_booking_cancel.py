"""Phase 8 (F-3): "cancel my booking" on a service business reaches the booking handler instead of being
swallowed by the global order cancel ("Nothing to cancel right now"). Driven through generate_reply."""
import pytest

import services.ai as ai
import services.booking_service as bs
from test_stage3_cart_lifecycle import world, PHONE, TEA2, state   # noqa: F401  (world = fixture)


@pytest.fixture
def booking(monkeypatch):
    b = {"bookings": [{"id": 5, "status": "confirmed", "booking_date": "2026-12-01", "start_time": "10:00"}],
         "cancelled": []}
    monkeypatch.setattr(bs, "get_bookings_for_customer", lambda biz, phone: list(b["bookings"]))
    monkeypatch.setattr(bs, "cancel_booking", lambda bid, biz: b["cancelled"].append(bid) or True)
    return b


def say(world, text, service=True):
    return ai.generate_reply(message=text, phone=PHONE, business_id=1, business_name="Test Shop",
                             products=world.db.tables["products"],
                             business_config={"id": 1, "is_service_business": service})


@pytest.mark.parametrize("text", ["cancel my booking", "cancel booking", "Cancel my appointment", "cancel the booking please"])
def test_cancel_booking_cancels_the_booking(world, booking, text):
    out = say(world, text)
    assert "Booking Cancelled" in out and booking["cancelled"] == [5]


def test_cancel_booking_with_no_active_booking_says_so(world, booking):
    booking["bookings"] = []
    out = say(world, "cancel my booking")
    assert "no active bookings" in out.lower() and "Nothing to cancel" not in out and booking["cancelled"] == []


@pytest.mark.parametrize("text", ["how do I cancel my booking?", "can I cancel my booking", "don't cancel my booking",
                                  "do not cancel my appointment"])
def test_questions_and_negations_never_cancel_a_booking(world, booking, text):
    say(world, text)
    assert booking["cancelled"] == []


def test_a_business_without_bookings_is_unchanged(world, booking):
    out = say(world, "cancel my booking", service=False)
    assert "Nothing to cancel" in out and booking["cancelled"] == []


def test_plain_cancel_is_still_the_global_cancel(world, booking):
    out = say(world, "cancel")
    assert "Nothing to cancel" in out and booking["cancelled"] == []


@pytest.mark.parametrize("st", ["checkout", "awaiting_payment", "awaiting_proof"])
def test_an_order_in_flight_keeps_cancel_meaning_the_order(world, booking, st):
    world.put_cart(TEA2, state=st, session={"cart_snapshot": TEA2})
    out = say(world, "cancel booking")
    assert booking["cancelled"] == [] and "ancelled" in out and state(world) != st


def test_a_question_that_starts_with_cancel_is_not_answered_as_nothing_to_cancel(world, booking):
    out = say(world, "cancel my booking?")
    assert booking["cancelled"] == [] and "Nothing to cancel" not in out
