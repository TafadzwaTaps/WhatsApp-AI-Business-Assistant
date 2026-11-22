"""Phase 8 (F-4): in survey state a real request (cancel order, refund, order status, cancel booking) is served
instead of being stored as survey "feedback". Ratings and ordinary chat behave as before. Driven through generate_reply."""
import pytest

import services.booking_service as bs
from test_stage3_cart_lifecycle import world, PHONE, TEA2, state   # noqa: F401  (world = fixture)


def survey(world):
    world.put_cart([], state="survey")


def memory_suggestion(world):
    return [r for r in world.db.tables.get("user_memory", []) if r.get("last_suggestion")]


def test_cancel_my_order_in_survey_asks_to_confirm_the_cancel(world):
    oid = world.place_order()
    survey(world)
    out = world.say("cancel my order")
    assert f"Cancel ORDER-{oid}?" in out and state(world) == "awaiting_cancel_confirm"
    assert "suggestion" not in out.lower()


def test_where_is_my_order_in_survey_is_answered(world):
    oid = world.place_order(status="preparing", fulfillment_method="delivery", delivery_address="12 High St")
    survey(world)
    out = world.say("Where is my order?")
    assert f"ORDER-{oid}" in out and "suggestion" not in out.lower()
    assert state(world) != "survey"


def test_refund_request_in_survey_is_not_swallowed(world):
    world.place_order()
    survey(world)
    out = world.say("I want a refund for my order")
    assert "suggestion" not in out.lower() and "Thanks again" not in out and state(world) != "survey"


def test_cancel_booking_in_survey_reaches_the_booking_handler(world, monkeypatch):
    cancelled = []
    monkeypatch.setattr(bs, "get_bookings_for_customer",
                        lambda biz, phone: [{"id": 5, "status": "confirmed", "booking_date": "2026-12-01", "start_time": "10:00"}])
    monkeypatch.setattr(bs, "cancel_booking", lambda bid, biz: cancelled.append(bid) or True)
    survey(world)
    import services.ai as ai
    out = ai.generate_reply(message="cancel my booking", phone=PHONE, business_id=1, business_name="Test Shop",
                            products=world.db.tables["products"],
                            business_config={"id": 1, "is_service_business": True})
    assert cancelled == [5] and "Booking Cancelled" in out


@pytest.mark.parametrize("text,expect", [("excellent", "Rating: *Excellent*"), ("4", "Rating: *Poor*")])
def test_a_rating_is_still_recorded(world, text, expect):
    survey(world)
    assert expect in world.say(text) and state(world) != "survey"


def test_ordinary_feedback_is_still_thanked_as_a_suggestion(world):
    survey(world)
    out = world.say("the staff were really friendly today")
    assert "Thank you for your suggestion" in out and state(world) != "survey"


def test_a_short_no_still_ends_the_survey_politely(world):
    survey(world)
    assert "Thanks again" in world.say("no")


def test_a_bare_cancel_in_survey_is_unchanged(world):
    survey(world)
    assert "Thanks again" in world.say("cancel")
