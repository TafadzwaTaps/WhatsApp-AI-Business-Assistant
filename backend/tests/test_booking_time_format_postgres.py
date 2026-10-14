"""Regression: Postgres TIME columns come back as HH:MM:SS. The strict
HH:MM unpack in _format_time used to raise, which the webhook turned into the
generic 'Thanks for contacting us' reply right after a booking was confirmed."""
from services.booking_service import _format_time, _add_time, format_booking_confirmation


def test_format_time_accepts_seconds_and_offset():
    assert _format_time("13:00") == "1:00 PM"
    assert _format_time("13:00:00") == "1:00 PM"
    assert _format_time("09:30:00+00") == "9:30 AM"


def test_add_time_accepts_seconds():
    assert _add_time("13:00:00", 1) == "14:00"


def test_confirmation_with_db_row_shape():
    msg = format_booking_confirmation(
        {"id": 3, "booking_date": "2026-10-19", "start_time": "13:00:00",
         "end_time": "14:00:00", "service_name": ""}, "Golden Clippers Barber")
    assert "Booking Confirmed" in msg and "1:00 PM" in msg and "2:00 PM" in msg
