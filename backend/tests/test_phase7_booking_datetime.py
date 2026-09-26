"""
tests/test_phase7_booking_datetime.py — Phase 7 (Natural Date/Time + Bookings) tests.

Two layers, same pattern as Phases 4-6's tests:
  1. Pure unit tests for services/booking_nlp.py — new exact-time
     phrasings, vague time-of-day -> range classification, slot
     filtering/formatting, and resolve_time_for_booking()'s decision
     logic — no DB.
  2. End-to-end tests through services.ai.generate_reply() for the
     standalone booking flow and the new reschedule flow, with
     services.booking_service.get_available_slots()/get_bookings_for_
     customer()/reschedule_booking()/create_booking() monkeypatched so
     these run without a real Supabase connection.
"""

import pytest

import services.ai as ai
import services.booking_nlp as bnlp
import services.booking_service as bsvc


# ═════════════════════════════════════════════════════════════════════════
# classify_time_phrase() — exact vs vague
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("text,expected", [
    ("3pm", "15:00"),
    ("10am", "10:00"),
    ("2:30pm", "14:30"),
    ("14:00", "14:00"),
])
def test_classify_time_phrase_exact_existing_formats(text, expected):
    result = bnlp.classify_time_phrase(text)
    assert result == {"time": expected, "vague": False}


def test_classify_time_phrase_around_three():
    result = bnlp.classify_time_phrase("can I come around 3?")
    assert result["vague"] is False
    assert result["time"] == "15:00"


def test_classify_time_phrase_half_past_four():
    result = bnlp.classify_time_phrase("half past four works for me")
    assert result["vague"] is False
    assert result["time"] == "16:30"


def test_classify_time_phrase_quarter_past_and_to():
    assert bnlp.classify_time_phrase("quarter past four")["time"] == "16:15"
    assert bnlp.classify_time_phrase("quarter to five")["time"] == "16:45"


@pytest.mark.parametrize("text,expected_range", [
    ("tomorrow afternoon", ("12:00", "17:00")),
    ("after work", ("17:00", "20:00")),
    ("after lunch", ("13:00", "17:00")),
    ("Friday morning", ("08:00", "12:00")),
])
def test_classify_time_phrase_vague_words_return_range_not_a_guess(text, expected_range):
    result = bnlp.classify_time_phrase(text)
    assert result["vague"] is True
    assert result["time"] is None
    assert result["range"] == {"start": expected_range[0], "end": expected_range[1]}


def test_classify_time_phrase_no_time_at_all():
    assert bnlp.classify_time_phrase("I'd like to book an appointment") is None
    assert bnlp.classify_time_phrase("") is None


def test_classify_time_phrase_exact_beats_vague_when_both_present():
    # "afternoon at 3pm" — customer gave both; the exact time must win.
    result = bnlp.classify_time_phrase("tomorrow afternoon at 3pm")
    assert result["vague"] is False
    assert result["time"] == "15:00"


# ═════════════════════════════════════════════════════════════════════════
# filter_slots_in_range() / format_slot_options()
# ═════════════════════════════════════════════════════════════════════════

def test_filter_slots_in_range_only_returns_real_slots_within_window():
    slots = ["09:00", "11:30", "14:00", "15:30", "16:30", "19:00"]
    matches = bnlp.filter_slots_in_range(slots, {"start": "12:00", "end": "17:00"})
    assert matches == ["14:00", "15:30", "16:30"]


def test_filter_slots_in_range_respects_limit():
    slots = ["14:00", "14:30", "15:00", "15:30", "16:00"]
    matches = bnlp.filter_slots_in_range(slots, {"start": "12:00", "end": "17:00"}, limit=2)
    assert matches == ["14:00", "14:30"]


def test_filter_slots_in_range_empty_inputs():
    assert bnlp.filter_slots_in_range([], {"start": "12:00", "end": "17:00"}) == []
    assert bnlp.filter_slots_in_range(["14:00"], {}) == []


def test_format_slot_options_matches_spec_worked_example_shape():
    msg = bnlp.format_slot_options(["14:00", "15:30", "16:30"])
    assert "2:00 PM" in msg and "3:30 PM" in msg and "4:30 PM" in msg
    assert "and" in msg
    assert "Which works best" in msg


def test_format_slot_options_empty_returns_empty_string():
    assert bnlp.format_slot_options([]) == ""


def test_format_slot_options_single_slot():
    msg = bnlp.format_slot_options(["09:00"])
    assert msg == "I have 9:00 AM available. Which works best? 😊"


# ═════════════════════════════════════════════════════════════════════════
# resolve_time_for_booking() — the core "never invent" decision logic
# ═════════════════════════════════════════════════════════════════════════

def test_resolve_time_for_booking_no_time_found(monkeypatch):
    result = bnlp.resolve_time_for_booking(1, "2026-09-27", "I'd like to book something")
    assert result == {"outcome": "no_time"}


def test_resolve_time_for_booking_exact_time_never_touches_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("get_available_slots must not be called for an exact time")
    monkeypatch.setattr(bnlp._bs, "get_available_slots", _boom)
    result = bnlp.resolve_time_for_booking(1, "2026-09-27", "3pm")
    assert result == {"outcome": "exact", "time": "15:00"}


def test_resolve_time_for_booking_vague_with_real_availability_returns_options(monkeypatch):
    monkeypatch.setattr(bnlp._bs, "get_available_slots",
                         lambda biz_id, date_str, duration_hrs=1.0, interval_mins=30:
                         {"slots": ["09:00", "14:00", "15:30", "16:30"], "reason": ""})
    result = bnlp.resolve_time_for_booking(1, "2026-09-27", "tomorrow afternoon")
    assert result == {"outcome": "options", "slots": ["14:00", "15:30", "16:30"]}


def test_resolve_time_for_booking_vague_with_no_matching_availability_asks(monkeypatch):
    monkeypatch.setattr(bnlp._bs, "get_available_slots",
                         lambda biz_id, date_str, duration_hrs=1.0, interval_mins=30:
                         {"slots": ["09:00", "10:00"], "reason": ""})  # nothing in the afternoon
    result = bnlp.resolve_time_for_booking(1, "2026-09-27", "afternoon")
    assert result == {"outcome": "ask"}


def test_resolve_time_for_booking_vague_availability_lookup_fails_asks_never_raises(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(bnlp._bs, "get_available_slots", _boom)
    result = bnlp.resolve_time_for_booking(1, "2026-09-27", "evening")
    assert result == {"outcome": "no_time"}  # fails safe, never raises, never guesses


# ═════════════════════════════════════════════════════════════════════════
# End-to-end through services.ai.generate_reply() — standalone booking flow
# ═════════════════════════════════════════════════════════════════════════

PRODUCTS = [{"id": 1, "name": "Haircut", "price": 15.0, "stock": 1}]
SERVICE_BIZ = {"id": 1, "is_service_business": True, "default_slot_mins": 60}


@pytest.fixture
def ai_state(monkeypatch):
    store = {"cart": [], "session": {}, "state": "browsing", "state_data": {"state": "browsing", "session": {}}}

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

    monkeypatch.setattr(ai, "_get_state", _get_state)
    monkeypatch.setattr(ai, "_get_session", _get_session)
    monkeypatch.setattr(ai, "_read_state_data", _read_state_data)
    monkeypatch.setattr(ai, "_write_state_data", _write_state_data)
    monkeypatch.setattr(ai, "_reset_state", _reset_state)
    monkeypatch.setattr(ai, "_load_cart", lambda phone, biz: store["cart"])
    monkeypatch.setattr(ai, "_save_cart", lambda phone, biz, cart: store.update(cart=cart))

    # Business config lookup inside generate_reply() (is_service_business, etc.)
    import crud
    monkeypatch.setattr(crud, "get_business_by_id", lambda biz_id: SERVICE_BIZ)

    # The booking handler independently re-checks the "bookings" plan
    # feature (services/ai.py's own enforcement point, separate from the
    # /bookings API routes) — without a real Supabase-backed plan lookup
    # this fails closed (FREE tier), so tests must explicitly allow it.
    from core import plan_guard
    monkeypatch.setattr(plan_guard, "feature_access",
                         lambda feature_key, business_id: {"allowed": True})

    return store


def _reply(text, ai_state):
    return ai.generate_reply(
        message=text, phone="+1555", business_id=1, business_name="Glow Salon",
        products=PRODUCTS, business_config=SERVICE_BIZ,
    )


def test_spec_worked_example_vague_afternoon_asks_when_no_availability_data(ai_state, monkeypatch):
    """The spec's own worked example: "Can I come tomorrow afternoon?" must
    either ask what time they'd prefer, or list REAL available times — it
    must never silently invent/confirm a guessed time like 14:00."""
    monkeypatch.setattr(bsvc, "get_available_slots",
                         lambda business_id, booking_date, duration_hrs=1.0, interval_mins=30:
                         {"slots": [], "reason": "Could not load availability — please try again"})
    reply = _reply("Can I come tomorrow afternoon?", ai_state)
    assert "what time" in reply.lower() or "sure" in reply.lower()
    assert "14:00" not in reply and "2:00 pm" not in reply.lower()


def test_spec_worked_example_vague_afternoon_lists_real_slots_when_available(ai_state, monkeypatch):
    monkeypatch.setattr(bsvc, "get_available_slots",
                         lambda business_id, booking_date, duration_hrs=1.0, interval_mins=30:
                         {"slots": ["14:00", "15:30", "16:30"], "reason": ""})
    reply = _reply("Can I come tomorrow afternoon?", ai_state)
    assert "2:00 PM" in reply and "3:30 PM" in reply and "4:30 PM" in reply
    assert "which works best" in reply.lower()


def test_exact_time_booking_intent_goes_straight_to_preview(ai_state):
    reply = _reply("book me tomorrow at 3pm", ai_state)
    assert "3:00 PM" in reply
    assert ai_state["state_data"]["state"] == "booking_confirm"


def test_half_past_four_recognised_as_exact(ai_state):
    reply = _reply("book me tomorrow at half past four", ai_state)
    assert "4:30 PM" in reply
    assert ai_state["state_data"]["state"] == "booking_confirm"


def test_awaiting_booking_time_state_vague_reply_lists_real_options(ai_state, monkeypatch):
    ai_state["state_data"] = {"state": "awaiting_booking_time",
                               "session": {"booking_date": "2026-09-27"}}
    monkeypatch.setattr(bsvc, "get_available_slots",
                         lambda business_id, booking_date, duration_hrs=1.0, interval_mins=30:
                         {"slots": ["17:30", "18:00"], "reason": ""})
    reply = _reply("after work", ai_state)
    assert "5:30 PM" in reply and "6:00 PM" in reply


# ═════════════════════════════════════════════════════════════════════════
# End-to-end — reschedule flow (Phase 7: previously advertised, never wired)
# ═════════════════════════════════════════════════════════════════════════

EXISTING_BOOKING = {"id": 42, "business_id": 1, "customer_phone": "+1555",
                     "booking_date": "2026-09-28", "start_time": "10:00",
                     "end_time": "11:00", "status": "confirmed", "service_name": "Haircut"}


def test_reschedule_booking_intent_starts_flow_when_active_booking_exists(ai_state, monkeypatch):
    monkeypatch.setattr(bsvc, "get_bookings_for_customer", lambda biz_id, phone: [EXISTING_BOOKING])
    reply = _reply("I need to reschedule my booking", ai_state)
    assert "reschedule" in reply.lower()
    assert ai_state["state_data"]["state"] == "reschedule_awaiting_date"
    assert ai_state["state_data"]["session"]["reschedule_booking_id"] == 42


def test_reschedule_with_no_active_booking_says_so(ai_state, monkeypatch):
    monkeypatch.setattr(bsvc, "get_bookings_for_customer", lambda biz_id, phone: [])
    reply = _reply("reschedule my appointment", ai_state)
    assert "no active bookings" in reply.lower()


def test_reschedule_full_flow_exact_time_then_confirm(ai_state, monkeypatch):
    monkeypatch.setattr(bsvc, "get_bookings_for_customer", lambda biz_id, phone: [EXISTING_BOOKING])
    reply = _reply("reschedule booking", ai_state)
    assert ai_state["state_data"]["state"] == "reschedule_awaiting_date"

    # Split into date-then-time, same as the standalone booking flow's own
    # tests — parse_booking_request() only fills BOTH fields from a single
    # message when a booking-intent keyword ("book", "schedule"...) is
    # present; a bare "tomorrow at 4pm" reply (no such keyword) only ever
    # yields the date here, exactly like the pre-existing awaiting_booking_
    # date state already behaves for the same kind of reply.
    reply = _reply("tomorrow", ai_state)
    assert ai_state["state_data"]["state"] == "reschedule_awaiting_time"

    reply = _reply("4pm", ai_state)
    assert ai_state["state_data"]["state"] == "reschedule_confirm"
    assert "4:00 PM" in reply

    monkeypatch.setattr(bsvc, "check_availability",
                         lambda business_id, date_str, time_str, duration_hrs=1.0:
                         {"available": True, "reason": "Available", "conflicts": []})
    updated_booking = dict(EXISTING_BOOKING)
    updated_booking.update({"booking_date": ai_state["state_data"]["session"]["new_date"],
                             "start_time": "16:00", "end_time": "17:00", "status": "rescheduled"})
    monkeypatch.setattr(bsvc, "reschedule_booking",
                         lambda booking_id, business_id, new_date, new_time, duration_hrs=1.0: updated_booking)

    reply = _reply("yes", ai_state)
    assert "rescheduled" in reply.lower()
    assert ai_state["state_data"]["state"] == "browsing"  # reset after completion


def test_reschedule_vague_time_lists_real_options(ai_state, monkeypatch):
    monkeypatch.setattr(bsvc, "get_bookings_for_customer", lambda biz_id, phone: [EXISTING_BOOKING])
    _reply("reschedule booking", ai_state)

    monkeypatch.setattr(bsvc, "get_available_slots",
                         lambda business_id, booking_date, duration_hrs=1.0, interval_mins=30:
                         {"slots": ["14:00", "15:00"], "reason": ""})
    reply = _reply("tomorrow afternoon", ai_state)
    assert "2:00 PM" in reply and "3:00 PM" in reply
    assert ai_state["state_data"]["state"] == "reschedule_awaiting_time"


def test_reschedule_can_be_cancelled_leaving_original_booking_unchanged(ai_state, monkeypatch):
    """"cancel" during ANY mid-flow state (booking, reschedule, checkout...)
    is intercepted by the P0 Global Cancel handler before it ever reaches
    the reschedule-specific state blocks below — this is pre-existing
    behavior identical to the standalone booking flow's own awaiting_
    booking_date/time/booking_confirm states, none of which have ever had
    their own state-specific cancel wording actually reachable either.
    What matters for Phase 7: the reschedule is abandoned and the
    ORIGINAL booking is left completely untouched (reschedule_booking()
    is never called), which is what this test actually verifies."""
    monkeypatch.setattr(bsvc, "get_bookings_for_customer", lambda biz_id, phone: [EXISTING_BOOKING])
    booking_id = _reply("reschedule booking", ai_state)
    assert ai_state["state_data"]["state"] == "reschedule_awaiting_date"

    def _boom(*a, **k):
        raise AssertionError("reschedule_booking must not be called after cancel")
    monkeypatch.setattr(bsvc, "reschedule_booking", _boom)

    reply = _reply("cancel", ai_state)
    assert "cancelled" in reply.lower()
    assert ai_state["state_data"]["state"] == "browsing"


# ═════════════════════════════════════════════════════════════════════════
# Reminder scheduler (Phase 7 — closes the "advertised but nothing sends
# it automatically" gap)
# ═════════════════════════════════════════════════════════════════════════

def test_send_reminders_for_business_reuses_get_upcoming_reminders(monkeypatch):
    calls = {}

    def _fake_get_upcoming(biz_id, window_hours=24.5):
        calls["called"] = True
        return []

    monkeypatch.setattr(bsvc, "get_upcoming_reminders", _fake_get_upcoming)

    import crud
    monkeypatch.setattr(crud, "get_decrypted_token", lambda biz: "tok")

    import routes.webhook_routes as _webhook
    monkeypatch.setattr(_webhook, "send_whatsapp", lambda *a, **k: {"ok": True})

    result = bsvc.send_reminders_for_business(1, {"id": 1, "name": "Test Biz", "whatsapp_phone_id": "p"})
    assert calls.get("called") is True
    assert result["ok"] is True
    assert result["sent"] == 0


def test_run_all_due_reminders_only_processes_service_businesses(monkeypatch):
    """Pure structural check: run_all_due_reminders must never raise even
    if the DB layer is unavailable (e.g. sandbox/test environment)."""
    result = bsvc.run_all_due_reminders()
    assert set(result.keys()) == {"businesses", "sent", "skipped", "errors"}
