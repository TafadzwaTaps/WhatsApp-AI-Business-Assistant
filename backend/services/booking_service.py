"""
services/booking_service.py — Appointment Booking Engine (Phases 2-5)

PURPOSE
───────
Adds appointment booking to WaziBot WITHOUT touching the existing
order/product/payment flows.

Retail businesses: unaffected. Booking is gated on is_service_business=True
in the business record. The AI only routes to booking logic when this flag
is set.

DESIGN PRINCIPLES
─────────────────
• Zero imports from services/ai.py  (no circular imports)
• Never raises — all parse/check functions return safe defaults
• Coexists with order_parser_service — doesn't replace or conflict
• Booking states are separate from order states (different prefix)
• Supabase table: bookings (see MIGRATION at bottom of this file)

BOOKING FLOW (WhatsApp conversation)
─────────────────────────────────────
  customer: "Book me tomorrow at 2pm"
  AI: "Confirming your booking:
       📅 Date: Thursday 5 June 2026
       🕐 Time: 2:00 PM
       Is this correct? Reply yes to confirm or no to change."
  customer: "yes"
  AI: "✅ Booking confirmed! You'll receive a reminder 24h before."

BOOKING STATES (added to STATE.ALL — don't conflict with order states)
───────────────────────────────────────────────────────────────────────
  awaiting_booking_date   — collecting the date
  awaiting_booking_time   — date captured, collecting time
  booking_confirm         — full details shown, waiting for yes/no
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta, date, time as dt_time
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedBooking:
    """Result of parsing a booking request from customer text."""
    has_booking_intent: bool = False
    date_str:     Optional[str] = None   # ISO format: YYYY-MM-DD
    time_str:     Optional[str] = None   # HH:MM (24h)
    end_date_str: Optional[str] = None   # For range bookings (e.g. hotel)
    duration_hrs: Optional[float] = None
    service_name: Optional[str] = None   # Extracted service type if mentioned
    raw_text:     str = ""
    confidence:   float = 0.0
    parse_notes:  str = ""


@dataclass
class BookingSlot:
    """A proposed or confirmed appointment slot."""
    booking_date: str   # YYYY-MM-DD
    start_time:   str   # HH:MM
    end_time:     str   # HH:MM (start + duration)
    duration_hrs: float = 1.0
    service_name: str   = ""
    notes:        str   = ""


# ─────────────────────────────────────────────────────────────────────────────
# DATE / TIME PARSING
# ─────────────────────────────────────────────────────────────────────────────

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3,   "apr": 4, "april": 4,
    "may": 5,               "jun": 6, "june": 6,
    "jul": 7, "july": 7,    "aug": 8, "august": 8,
    "sep": 9, "september": 9, "sept": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_BOOKING_INTENT_PATTERNS = re.compile(
    r"\b(book|appointment|appoint|schedule|reserve|slot|session|visit|"
    r"come in|come over|see you|meeting|consultation)\b",
    re.IGNORECASE,
)

# Date patterns
_DATE_SLASH  = re.compile(r"\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b")
_DATE_WORDS  = re.compile(
    r"\b(\d{1,2})\s+(?:of\s+)?(" + "|".join(_MONTHS.keys()) + r")"
    r"(?:\s+(\d{4}))?\b", re.IGNORECASE
)
_DATE_ISO    = re.compile(r"\b(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\b")
_DATE_RANGE  = re.compile(
    r"\bfrom\s+(\d{1,2}[/\-\.]\d{1,2}(?:[/\-\.]\d{2,4})?)"
    r"\s+(?:to|until|till)\s+(\d{1,2}[/\-\.]\d{1,2}(?:[/\-\.]\d{2,4})?)\b",
    re.IGNORECASE,
)

# Relative dates
_REL_TODAY     = re.compile(r"\btoday\b",                   re.IGNORECASE)
_REL_TOMORROW  = re.compile(r"\btomorrow\b",                re.IGNORECASE)
_REL_NEXT_WEEK = re.compile(r"\bnext\s+week\b",             re.IGNORECASE)
_REL_THIS_WEEK = re.compile(r"\bthis\s+(\w+day)\b",         re.IGNORECASE)
_REL_NEXT_DAY  = re.compile(r"\bnext\s+(\w+day)\b",         re.IGNORECASE)
_REL_IN_DAYS   = re.compile(r"\bin\s+(\d+)\s+days?\b",      re.IGNORECASE)

# Time patterns
_TIME_12H = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.IGNORECASE
)
_TIME_24H = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_TIME_OCLOCK = re.compile(r"\b(\d{1,2})\s*o'?\s*clock\b", re.IGNORECASE)

# Duration
_DUR_PATTERN = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(?:hour|hr|h)\s*(?:(\d+)\s*(?:min|minutes?))?\b",
    re.IGNORECASE,
)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date_str(raw: str) -> Optional[date]:
    """Try to extract a date from a short string like '14/09' or '5 june 2026'."""
    raw = raw.strip()
    today = _today()

    m = _DATE_ISO.match(raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    m = _DATE_SLASH.match(raw)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        yr = int(m.group(3)) if m.group(3) else today.year
        if yr < 100: yr += 2000
        try:
            dt = date(yr, mo, d)
            if dt < today: dt = date(yr + 1, mo, d)
            return dt
        except ValueError:
            try:
                return date(yr, d, mo)  # try swapped
            except ValueError:
                pass

    m = _DATE_WORDS.match(raw)
    if m:
        d  = int(m.group(1))
        mo = _MONTHS.get(m.group(2).lower(), 0)
        yr = int(m.group(3)) if m.group(3) else today.year
        if mo:
            try:
                dt = date(yr, mo, d)
                if dt < today: dt = date(yr + 1, mo, d)
                return dt
            except ValueError:
                pass
    return None


def _resolve_relative_date(text: str) -> Optional[date]:
    today = _today()

    if _REL_TODAY.search(text):
        return today
    if _REL_TOMORROW.search(text):
        return today + timedelta(days=1)
    if _REL_NEXT_WEEK.search(text):
        return today + timedelta(weeks=1)

    m = _REL_IN_DAYS.search(text)
    if m:
        return today + timedelta(days=int(m.group(1)))

    for pattern in (_REL_NEXT_DAY, _REL_THIS_WEEK):
        m = pattern.search(text)
        if m:
            day_word = m.group(1).lower()
            target_wd = _WEEKDAYS.get(day_word)
            if target_wd is not None:
                delta = (target_wd - today.weekday() + 7) % 7
                if delta == 0 and pattern == _REL_NEXT_DAY:
                    delta = 7
                if delta == 0:
                    delta = 7
                return today + timedelta(days=delta)

    return None


def _parse_time(text: str) -> Optional[str]:
    """Return HH:MM (24h) or None."""
    m = _TIME_12H.search(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        period = m.group(3).lower()
        if period == "pm" and h != 12: h += 12
        if period == "am" and h == 12: h = 0
        return f"{h:02d}:{mi:02d}"

    m = _TIME_24H.search(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"

    m = _TIME_OCLOCK.search(text)
    if m:
        h = int(m.group(1))
        # If no am/pm, assume business hours: 1-8 → pm, 9-12 → am
        if 1 <= h <= 8: h += 12
        return f"{h:02d}:00"

    return None


def _parse_duration(text: str) -> Optional[float]:
    m = _DUR_PATTERN.search(text)
    if m:
        hrs  = float(m.group(1))
        mins = int(m.group(2) or 0)
        return round(hrs + mins / 60, 2)
    return None


def _add_time(time_str: str, hours: float) -> str:
    """Add hours to HH:MM string, return HH:MM."""
    h, mi = map(int, time_str.split(":"))
    total_mins = h * 60 + mi + int(hours * 60)
    return f"{(total_mins // 60) % 24:02d}:{total_mins % 60:02d}"


def _format_date(d: date) -> str:
    return d.strftime("%A, %-d %B %Y")


def _format_time(t: str) -> str:
    h, mi = map(int, t.split(":"))
    period = "AM" if h < 12 else "PM"
    h12    = h % 12 or 12
    return f"{h12}:{mi:02d} {period}"


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC PARSE FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def parse_booking_request(text: str) -> ParsedBooking:
    """
    Parse a customer message into a ParsedBooking.
    Never raises — returns has_booking_intent=False on any failure.

    Examples:
      "Book me for tomorrow at 2pm"              → date=tomorrow, time=14:00
      "I'd like an appointment on 14/09 at 10am" → date=14 Sep, time=10:00
      "Book from 21/05 to 24/05"                 → date=21 May, end_date=24 May
      "Schedule me for next Friday at 9am"       → date=next Friday, time=09:00
    """
    result = ParsedBooking(raw_text=text)

    t = text.lower().strip()

    # ── Booking intent detection ──────────────────────────────────────────────
    if not _BOOKING_INTENT_PATTERNS.search(t):
        return result
    result.has_booking_intent = True

    # ── Date range (e.g. hotel stays, multi-day) ──────────────────────────────
    m = _DATE_RANGE.search(text)
    if m:
        d1 = _parse_date_str(m.group(1))
        d2 = _parse_date_str(m.group(2))
        if d1 and d2:
            result.date_str     = d1.isoformat()
            result.end_date_str = d2.isoformat()
            result.confidence   = 0.9
            result.parse_notes  = "date_range"

    # ── Relative date ─────────────────────────────────────────────────────────
    if not result.date_str:
        rel = _resolve_relative_date(t)
        if rel:
            result.date_str   = rel.isoformat()
            result.parse_notes = "relative_date"

    # ── Explicit date ─────────────────────────────────────────────────────────
    if not result.date_str:
        # Try ISO first
        m = _DATE_ISO.search(text)
        if m:
            try:
                d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                result.date_str = d.isoformat()
                result.parse_notes = "iso_date"
            except ValueError:
                pass

    if not result.date_str:
        # Try word month format: "14 june", "5th of july"
        m = _DATE_WORDS.search(text)
        if m:
            d = _parse_date_str(m.group(0))
            if d:
                result.date_str   = d.isoformat()
                result.parse_notes = "word_date"

    if not result.date_str:
        m = _DATE_SLASH.search(text)
        if m:
            d = _parse_date_str(m.group(0))
            if d:
                result.date_str   = d.isoformat()
                result.parse_notes = "numeric_date"

    # ── Time ─────────────────────────────────────────────────────────────────
    result.time_str = _parse_time(text)

    # ── Duration ─────────────────────────────────────────────────────────────
    result.duration_hrs = _parse_duration(text)

    # ── Confidence ───────────────────────────────────────────────────────────
    if result.date_str and result.time_str:
        result.confidence = 0.95
    elif result.date_str:
        result.confidence = 0.70
    elif result.time_str:
        result.confidence = 0.50
    elif result.has_booking_intent:
        result.confidence = 0.30

    return result


def format_booking_preview(parsed: ParsedBooking, business_name: str = "") -> str:
    """
    Build the confirmation message shown to the customer before they confirm.
    """
    lines = ["📅 *Booking Details*\n"]

    if parsed.date_str:
        try:
            d = date.fromisoformat(parsed.date_str)
            lines.append(f"  📆 Date  : *{_format_date(d)}*")
        except Exception:
            lines.append(f"  📆 Date  : *{parsed.date_str}*")

    if parsed.end_date_str:
        try:
            d2 = date.fromisoformat(parsed.end_date_str)
            lines.append(f"  📆 Until : *{_format_date(d2)}*")
        except Exception:
            lines.append(f"  📆 Until : *{parsed.end_date_str}*")

    if parsed.time_str:
        lines.append(f"  🕐 Time  : *{_format_time(parsed.time_str)}*")

    if parsed.duration_hrs:
        hrs  = int(parsed.duration_hrs)
        mins = int((parsed.duration_hrs - hrs) * 60)
        dur  = f"{hrs}h" + (f" {mins}min" if mins else "")
        lines.append(f"  ⏱ Duration: *{dur}*")

    if parsed.service_name:
        lines.append(f"  💼 Service: *{parsed.service_name}*")

    biz_line = f" at *{business_name}*" if business_name else ""
    lines.append(f"\nIs this correct{biz_line}?\n\nReply *yes* to confirm or *no* to change.")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# DATABASE OPERATIONS (Supabase)
# ─────────────────────────────────────────────────────────────────────────────

def check_availability(
    business_id: int,
    booking_date: str,
    start_time:   str,
    duration_hrs: float = 1.0,
) -> dict:
    """
    Check if a slot is available.
    Returns {"available": bool, "reason": str, "conflicts": list}
    Never raises.
    """
    try:
        from core.db import supabase
        end_time = _add_time(start_time, duration_hrs)

        res = (
            supabase.table("bookings")
            .select("id, start_time, end_time, customer_phone, status")
            .eq("business_id", business_id)
            .eq("booking_date", booking_date)
            .in_("status", ["pending", "confirmed"])
            .execute()
        )
        existing = res.data or []

        conflicts = []
        for b in existing:
            # Simple overlap: if time ranges intersect
            b_start = b.get("start_time", "00:00")
            b_end   = b.get("end_time",   "23:59")
            if not (end_time <= b_start or start_time >= b_end):
                conflicts.append(b)

        return {
            "available": len(conflicts) == 0,
            "reason":    "Slot taken" if conflicts else "Available",
            "conflicts": conflicts,
        }
    except Exception as exc:
        log.warning("check_availability error: %s", exc)
        # Fail open — don't block booking on DB error
        return {"available": True, "reason": "Could not verify", "conflicts": []}


def create_booking(
    business_id:   int,
    customer_phone: str,
    booking_date:  str,
    start_time:    str,
    duration_hrs:  float = 1.0,
    service_name:  str   = "",
    notes:         str   = "",
) -> Optional[dict]:
    """
    Create a booking record. Returns the created row or None on error.
    Does NOT send WhatsApp messages — caller handles that.
    """
    from datetime import datetime, timezone
    from core.db import supabase

    end_time = _add_time(start_time, duration_hrs)
    now      = datetime.now(timezone.utc).isoformat()

    try:
        res = supabase.table("bookings").insert({
            "business_id":    business_id,
            "customer_phone": customer_phone,
            "booking_date":   booking_date,
            "start_time":     start_time,
            "end_time":       end_time,
            "duration_hrs":   duration_hrs,
            "service_name":   service_name or "",
            "notes":          notes or "",
            "status":         "confirmed",
            "created_at":     now,
        }).execute()

        booking = res.data[0] if res.data else None
        if booking:
            log.info("booking created  id=%s  biz=%s  date=%s  time=%s",
                     booking.get("id"), business_id, booking_date, start_time)
        return booking
    except Exception as exc:
        log.error("create_booking error: %s", exc)
        return None


def get_bookings(business_id: int, upcoming_only: bool = True) -> list[dict]:
    """Return bookings for a business, ordered by date/time."""
    try:
        from core.db import supabase
        today = _today().isoformat()
        q = (
            supabase.table("bookings")
            .select("*")
            .eq("business_id", business_id)
            .order("booking_date", desc=False)
            .order("start_time",   desc=False)
        )
        if upcoming_only:
            q = q.gte("booking_date", today)
        return q.execute().data or []
    except Exception as exc:
        log.warning("get_bookings error: %s", exc)
        return []


def get_bookings_for_customer(business_id: int, customer_phone: str) -> list[dict]:
    """Return all bookings for a specific customer."""
    try:
        from core.db import supabase
        res = (
            supabase.table("bookings")
            .select("*")
            .eq("business_id", business_id)
            .eq("customer_phone", customer_phone)
            .order("booking_date", desc=True)
            .limit(10)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        log.warning("get_bookings_for_customer error: %s", exc)
        return []


def cancel_booking(booking_id: int, business_id: int) -> Optional[dict]:
    """Cancel a booking. Returns updated row or None."""
    try:
        from core.db import supabase
        res = (
            supabase.table("bookings")
            .update({"status": "cancelled"})
            .eq("id", booking_id)
            .eq("business_id", business_id)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        log.error("cancel_booking error: %s", exc)
        return None


def reschedule_booking(
    booking_id:   int,
    business_id:  int,
    new_date:     str,
    new_time:     str,
    duration_hrs: float = 1.0,
) -> Optional[dict]:
    """Update date/time of a booking, set status to rescheduled."""
    try:
        from core.db import supabase
        end_time = _add_time(new_time, duration_hrs)
        res = (
            supabase.table("bookings")
            .update({
                "booking_date": new_date,
                "start_time":   new_time,
                "end_time":     end_time,
                "status":       "rescheduled",
            })
            .eq("id", booking_id)
            .eq("business_id", business_id)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        log.error("reschedule_booking error: %s", exc)
        return None


def get_upcoming_reminders(
    business_id:  int,
    window_hours: float = 24.5,
) -> list[dict]:
    """
    Return bookings whose start falls within the next `window_hours` hours.
    Used by the reminder cron job.
    """
    try:
        from core.db import supabase
        now    = datetime.now(timezone.utc)
        future = now + timedelta(hours=window_hours)
        today  = _today().isoformat()

        res = (
            supabase.table("bookings")
            .select("*")
            .eq("business_id", business_id)
            .in_("status", ["confirmed", "rescheduled"])
            .gte("booking_date", today)
            .execute()
        )
        rows = res.data or []

        due = []
        for b in rows:
            try:
                bdt = datetime.fromisoformat(f"{b['booking_date']}T{b['start_time']}:00+00:00")
                if now <= bdt <= future:
                    due.append(b)
            except Exception:
                pass
        return due
    except Exception as exc:
        log.warning("get_upcoming_reminders error: %s", exc)
        return []


def format_booking_confirmation(booking: dict, business_name: str = "") -> str:
    """Build a WhatsApp confirmation message for a created booking."""
    bid  = booking.get("id", "?")
    d    = booking.get("booking_date", "")
    t    = booking.get("start_time", "")
    t2   = booking.get("end_time", "")
    svc  = booking.get("service_name", "")

    try:
        d_fmt = _format_date(date.fromisoformat(d))
    except Exception:
        d_fmt = d

    t_fmt  = _format_time(t)  if t  else ""
    t2_fmt = _format_time(t2) if t2 else ""

    svc_line = f"\n  💼 Service : *{svc}*" if svc else ""
    end_line = f" – {t2_fmt}" if t2_fmt else ""
    biz_line = f" with *{business_name}*" if business_name else ""

    return (
        f"✅ *Booking Confirmed!*\n\n"
        f"  📅 Date  : *{d_fmt}*\n"
        f"  🕐 Time  : *{t_fmt}{end_line}*"
        f"{svc_line}\n\n"
        f"📌 *Booking #{bid}*{biz_line}\n\n"
        f"You'll receive a reminder 24h before your appointment.\n\n"
        f"_To cancel or reschedule, reply *cancel booking* or *reschedule booking*._"
    )


def format_reminder_message(booking: dict, business_name: str = "") -> str:
    """WhatsApp reminder message sent before the appointment."""
    d   = booking.get("booking_date", "")
    t   = booking.get("start_time", "")
    svc = booking.get("service_name", "")

    try:
        d_fmt = _format_date(date.fromisoformat(d))
    except Exception:
        d_fmt = d

    t_fmt    = _format_time(t) if t else ""
    svc_line = f" for *{svc}*" if svc else ""
    biz_line = f" at *{business_name}*" if business_name else ""

    return (
        f"⏰ *Appointment Reminder*\n\n"
        f"Your appointment{svc_line} is coming up{biz_line}!\n\n"
        f"  📅 *{d_fmt}*\n"
        f"  🕐 *{t_fmt}*\n\n"
        f"We look forward to seeing you! 😊\n\n"
        f"_Need to cancel or reschedule? Reply *cancel booking* or *reschedule booking*._"
    )


# ─────────────────────────────────────────────────────────────────────────────
# MIGRATION SQL (run once in Supabase SQL Editor)
# ─────────────────────────────────────────────────────────────────────────────
"""
CREATE TABLE IF NOT EXISTS bookings (
  id              BIGSERIAL PRIMARY KEY,
  business_id     INTEGER NOT NULL,
  customer_phone  VARCHAR(30) NOT NULL,
  booking_date    DATE NOT NULL,
  start_time      TIME NOT NULL,
  end_time        TIME,
  duration_hrs    NUMERIC(4,2) DEFAULT 1.0,
  service_name    TEXT DEFAULT '',
  notes           TEXT DEFAULT '',
  status          VARCHAR(20) DEFAULT 'confirmed',
  reminder_24h_sent BOOLEAN DEFAULT FALSE,
  reminder_2h_sent  BOOLEAN DEFAULT FALSE,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bookings_business_date
  ON bookings (business_id, booking_date);
CREATE INDEX IF NOT EXISTS idx_bookings_customer
  ON bookings (business_id, customer_phone);

-- Add service mode flag to businesses table
ALTER TABLE businesses
  ADD COLUMN IF NOT EXISTS is_service_business BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS default_slot_mins   INTEGER DEFAULT 60,
  ADD COLUMN IF NOT EXISTS booking_lead_hrs    INTEGER DEFAULT 1,
  ADD COLUMN IF NOT EXISTS working_hours_start TIME    DEFAULT '08:00',
  ADD COLUMN IF NOT EXISTS working_hours_end   TIME    DEFAULT '17:00';
"""
