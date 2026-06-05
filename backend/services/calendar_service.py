"""
services/calendar_service.py — Calendar Abstraction Layer (Phase 4)

PURPOSE
───────
Provides a unified interface over booking records, with optional
Google Calendar synchronization. The system works fully without
Google credentials — gcal sync is a progressive enhancement.

DESIGN
──────
• create_event() / update_event() / delete_event() always write to
  the internal bookings table first.
• If GOOGLE_CALENDAR_ID and google-auth credentials are configured,
  changes are mirrored to Google Calendar.
• All Google calls are wrapped in try/except — a Google failure never
  blocks the local booking.
• Stubs are safe to call even without google-auth installed.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# ── Google Calendar optional import ──────────────────────────────────────────

_GCAL_ENABLED = False
_gcal_service = None

def _try_init_gcal():
    """Attempt to initialise Google Calendar client. Silent on failure."""
    global _GCAL_ENABLED, _gcal_service
    try:
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
        if not creds_json or not calendar_id:
            return

        import json
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds = service_account.Credentials.from_service_account_info(
            json.loads(creds_json),
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        _gcal_service = build("googleapiclient", "v3", credentials=creds)
        _GCAL_ENABLED = True
        log.info("calendar_service: Google Calendar connected ✓")
    except Exception as exc:
        log.debug("calendar_service: Google Calendar not configured (%s)", exc)


_try_init_gcal()


# ── Internal calendar helpers ─────────────────────────────────────────────────

def _booking_to_gcal_event(booking: dict, business_name: str = "") -> dict:
    """Convert a booking row to a Google Calendar event dict."""
    d     = booking.get("booking_date", "")
    t_s   = booking.get("start_time",   "09:00")
    t_e   = booking.get("end_time",     "10:00")
    phone = booking.get("customer_phone", "")
    svc   = booking.get("service_name",   "Appointment")

    return {
        "summary":     f"{svc} — {phone}" if phone else svc,
        "description": f"Booking #{booking.get('id','')} at {business_name}",
        "start": {"dateTime": f"{d}T{t_s}:00", "timeZone": "Africa/Harare"},
        "end":   {"dateTime": f"{d}T{t_e}:00", "timeZone": "Africa/Harare"},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup",  "minutes": 60},
                {"method": "email",  "minutes": 1440},
            ],
        },
    }


# ── Public API ────────────────────────────────────────────────────────────────

def create_event(booking: dict, business_name: str = "") -> Optional[str]:
    """
    Mirror a booking to Google Calendar.
    Returns the Google event ID (str) on success, None otherwise.
    Safe to call even if Google Calendar is not configured.
    """
    if not _GCAL_ENABLED or not _gcal_service:
        log.debug("create_event: Google Calendar not configured — skipped")
        return None

    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    try:
        event = _booking_to_gcal_event(booking, business_name)
        result = (
            _gcal_service.events()
            .insert(calendarId=calendar_id, body=event)
            .execute()
        )
        gcal_id = result.get("id")
        log.info("create_event: gcal event created  id=%s  booking=%s",
                 gcal_id, booking.get("id"))
        # Persist gcal_event_id to DB if column exists
        _update_gcal_id(booking.get("id"), gcal_id)
        return gcal_id
    except Exception as exc:
        log.warning("create_event: Google Calendar error: %s", exc)
        return None


def update_event(gcal_event_id: str, booking: dict, business_name: str = "") -> bool:
    """Update an existing Google Calendar event. Returns True on success."""
    if not _GCAL_ENABLED or not _gcal_service or not gcal_event_id:
        return False

    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    try:
        event = _booking_to_gcal_event(booking, business_name)
        _gcal_service.events().update(
            calendarId=calendar_id, eventId=gcal_event_id, body=event
        ).execute()
        log.info("update_event: gcal event updated  id=%s", gcal_event_id)
        return True
    except Exception as exc:
        log.warning("update_event: Google Calendar error: %s", exc)
        return False


def delete_event(gcal_event_id: str) -> bool:
    """Delete a Google Calendar event. Returns True on success."""
    if not _GCAL_ENABLED or not _gcal_service or not gcal_event_id:
        return False

    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    try:
        _gcal_service.events().delete(
            calendarId=calendar_id, eventId=gcal_event_id
        ).execute()
        log.info("delete_event: gcal event deleted  id=%s", gcal_event_id)
        return True
    except Exception as exc:
        log.warning("delete_event: Google Calendar error: %s", exc)
        return False


def sync_booking(booking: dict, business_name: str = "") -> None:
    """
    Full sync: create or update Google Calendar event for a booking.
    No-op if Google Calendar not configured.
    """
    if not _GCAL_ENABLED:
        return
    gcal_id = booking.get("gcal_event_id")
    if gcal_id:
        update_event(gcal_id, booking, business_name)
    else:
        create_event(booking, business_name)


def gcal_status() -> dict:
    """Return integration status for the /debug/security equivalent endpoint."""
    return {
        "google_calendar_enabled": _GCAL_ENABLED,
        "calendar_id":             os.getenv("GOOGLE_CALENDAR_ID", "") or "not configured",
        "credentials_set":         bool(os.getenv("GOOGLE_CREDENTIALS_JSON", "")),
        "note": (
            "Set GOOGLE_CREDENTIALS_JSON and GOOGLE_CALENDAR_ID env vars to enable sync."
            if not _GCAL_ENABLED else "Google Calendar sync active."
        ),
    }


# ── Internal helper ───────────────────────────────────────────────────────────

def _update_gcal_id(booking_id, gcal_id: str) -> None:
    """Save Google Calendar event ID back to the bookings table if column exists."""
    if not booking_id or not gcal_id:
        return
    try:
        from core.db import supabase
        supabase.table("bookings").update(
            {"gcal_event_id": gcal_id}
        ).eq("id", booking_id).execute()
    except Exception:
        pass  # Column may not exist yet — non-fatal
