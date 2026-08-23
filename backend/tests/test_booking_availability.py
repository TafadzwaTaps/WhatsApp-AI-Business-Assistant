"""
tests/test_booking_availability.py — Phase 6/7/40/41: atomic double-booking
protection, fail-closed availability, business hours enforcement.

Directly tests the exact bug found and fixed during this audit:
check_availability() used to fail OPEN on any database error (comment
literally said "Fail open — don't block booking on DB error"). These
tests would have caught that regression, and will catch it again if it
ever comes back.
"""

import pytest
from services.booking_service import check_availability


def test_fails_closed_on_database_error(fake_supabase, monkeypatch):
    """
    The core Phase 7 fix. If the DB call raises, the function must report
    unavailable — NOT available=True (the old, dangerous fail-open
    behaviour). A double-booking could slip through in exactly the moment
    the safety check itself is unreliable.
    """
    def _boom(*a, **kw):
        raise RuntimeError("simulated database outage")
    monkeypatch.setattr(fake_supabase, "table", _boom)

    result = check_availability(business_id=1, booking_date="2026-09-01", start_time="10:00")

    assert result["available"] is False, "a DB error must fail CLOSED, not open"
    assert "verify" in result["reason"].lower() or "try again" in result["reason"].lower()


def test_rejects_slot_outside_working_hours(fake_supabase):
    fake_supabase.results["businesses"] = [{
        "features_json": {"timezone": "UTC"},
        "working_hours_start": "09:00",
        "working_hours_end": "17:00",
        "booking_lead_hrs": 0,
    }]
    fake_supabase.results["bookings"] = []

    result = check_availability(business_id=1, booking_date="2026-09-01", start_time="20:00")

    assert result["available"] is False
    assert "working hours" in result["reason"].lower()


def test_accepts_slot_within_working_hours_with_no_conflicts(fake_supabase):
    from datetime import datetime, timedelta, timezone
    future_date = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()

    fake_supabase.results["businesses"] = [{
        "features_json": {"timezone": "UTC"},
        "working_hours_start": "09:00",
        "working_hours_end": "17:00",
        "booking_lead_hrs": 0,
    }]
    fake_supabase.results["bookings"] = []

    result = check_availability(business_id=1, booking_date=future_date, start_time="11:00")

    assert result["available"] is True


def test_rejects_conflicting_slot(fake_supabase):
    from datetime import datetime, timedelta, timezone
    future_date = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()

    fake_supabase.results["businesses"] = [{
        "features_json": {"timezone": "UTC"},
        "working_hours_start": "09:00",
        "working_hours_end": "17:00",
        "booking_lead_hrs": 0,
    }]
    # An existing 11:00-12:00 booking already occupies the slot being requested.
    fake_supabase.results["bookings"] = [
        {"id": 99, "start_time": "11:00", "end_time": "12:00", "status": "confirmed"}
    ]

    result = check_availability(business_id=1, booking_date=future_date, start_time="11:00")

    assert result["available"] is False
    assert len(result["conflicts"]) == 1


def test_rejects_slot_inside_minimum_notice_window(fake_supabase):
    """A slot that's technically within working hours but too soon (violates
    booking_lead_hrs) must be rejected — this is the gap found during the
    audit: create_booking() never enforced this at all before the fix."""
    from datetime import datetime, timedelta, timezone
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5))
    fake_supabase.results["businesses"] = [{
        "features_json": {"timezone": "UTC"},
        "working_hours_start": "00:00",
        "working_hours_end": "23:59",
        "booking_lead_hrs": 2,   # requires 2 hours notice
    }]
    fake_supabase.results["bookings"] = []

    result = check_availability(
        business_id=1,
        booking_date=soon.date().isoformat(),
        start_time=soon.strftime("%H:%M"),
    )

    assert result["available"] is False
    assert "notice" in result["reason"].lower()


def test_unknown_timezone_falls_back_to_utc_without_crashing(fake_supabase):
    """A malformed/unknown timezone string must not crash the availability
    check — it should log and fall back to UTC rather than raising."""
    from datetime import datetime, timedelta, timezone
    future_date = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()

    fake_supabase.results["businesses"] = [{
        "features_json": {"timezone": "Not/A_Real_Zone"},
        "working_hours_start": "09:00",
        "working_hours_end": "17:00",
        "booking_lead_hrs": 0,
    }]
    fake_supabase.results["bookings"] = []

    result = check_availability(business_id=1, booking_date=future_date, start_time="11:00")

    # Should not raise, and should still produce a sensible result.
    assert "available" in result


def test_get_available_slots_does_one_query_pair_not_one_per_candidate(fake_supabase, monkeypatch):
    """
    Phase 38 performance fix. Before this fix, get_available_slots() called
    check_availability() per candidate time, each doing its own two DB
    queries — an N+1 pattern. Now the business config and existing bookings
    are fetched once and passed to every candidate check. This test counts
    actual .table() calls to prove that, not just that the result is correct.
    """
    from datetime import datetime, timedelta, timezone
    from services.booking_service import get_available_slots
    future_date = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()

    fake_supabase.results["businesses"] = [{
        "features_json": {"timezone": "UTC"},
        "working_hours_start": "09:00",
        "working_hours_end": "17:00",   # 8-hour window
        "booking_lead_hrs": 0,
    }]
    fake_supabase.results["bookings"] = []

    call_count = {"n": 0}
    original_table = fake_supabase.table
    def counting_table(name, *a, **kw):
        call_count["n"] += 1
        return original_table(name, *a, **kw)
    monkeypatch.setattr(fake_supabase, "table", counting_table)

    result = get_available_slots(business_id=1, booking_date=future_date,
                                  duration_hrs=1.0, interval_mins=30)

    assert len(result["slots"]) > 5, "sanity check: an 8-hour window at 30min intervals should yield several slots"
    # Exactly 2 .table() calls total (one for businesses, one for bookings)
    # regardless of how many candidate slots were evaluated — proves the
    # fetch-once behavior, not the old N+1 pattern (which would have shown
    # 2 calls PER CANDIDATE, i.e. 20+ for an 8-hour window at 30min steps).
    assert call_count["n"] == 2, \
        f"expected exactly 2 .table() calls (fetch-once), got {call_count['n']} — N+1 regression?"
