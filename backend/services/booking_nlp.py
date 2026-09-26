"""
services/booking_nlp.py — Phase 7 (Natural Date/Time + Bookings) helpers.

services/booking_service.py already parses a lot of natural language
("tomorrow", "Friday", "14/09", "10am", "2:30pm"...) but has one real gap
the Phase 7 spec calls out directly: a handful of phrasings aren't
recognised at all ("around 3", "half past four", "after work", "after
lunch"), and — more importantly — a VAGUE time-of-day word ("morning",
"afternoon", "evening"...) is silently turned into a single guessed exact
clock time (booking_service._TIME_OF_DAY_WORDS, e.g. "afternoon" →
"14:00") at high confidence, with nothing ever checking whether that
guessed time is actually available. That is exactly the behavior the
spec forbids: "The AI MUST NOT invent available times."

This module adds the missing phrasings and — the important part — lets a
caller tell an EXACT, customer-stated time apart from a VAGUE one, so
services/ai.py's booking flow can look up REAL availability
(booking_service.get_available_slots) for a vague request instead of
presenting a guessed time as if it were confirmed or available.

DESIGN PRINCIPLES
─────────────────
• Zero imports from services/ai.py (no circular imports).
• Never raises — every function returns a safe default on any failure.
• Does NOT modify services/booking_service.py's _parse_time(),
  _TIME_OF_DAY_WORDS, or any existing regex — those keep working exactly
  as before for every existing caller. This module only ADDS new
  recognition and a separate classification on top, imported the other
  direction (this module imports booking_service, never the reverse).
• Only ever returns real slot times it received from
  booking_service.get_available_slots() — this module never invents a
  time itself.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from services import booking_service as _bs

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Extra EXACT time phrasings not recognised by booking_service.py
# ("around 3", "half past four", "quarter past four", "quarter to five")
# ─────────────────────────────────────────────────────────────────────────────

_HOUR_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_HOUR_TOKEN = r"(?:\d{1,2}|" + "|".join(_HOUR_WORDS.keys()) + r")"


def _hour_value(token: str) -> Optional[int]:
    token = token.lower()
    if token.isdigit():
        v = int(token)
        return v if 1 <= v <= 12 else None
    return _HOUR_WORDS.get(token)


def _assume_period(h: int) -> int:
    """Same "no am/pm given" heuristic booking_service._parse_time() already
    uses for a bare hour: 1-8 -> pm, 9-12 -> am."""
    if 1 <= h <= 8:
        return h + 12
    return h % 24


_TIME_APPROX_RE = re.compile(
    r"\b(?:around|about|approx(?:imately)?)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)
_TIME_HALF_PAST_RE = re.compile(
    r"\bhalf\s+past\s+(" + _HOUR_TOKEN + r")\b", re.IGNORECASE,
)
_TIME_QUARTER_PAST_RE = re.compile(
    r"\bquarter\s+past\s+(" + _HOUR_TOKEN + r")\b", re.IGNORECASE,
)
_TIME_QUARTER_TO_RE = re.compile(
    r"\bquarter\s+to\s+(" + _HOUR_TOKEN + r")\b", re.IGNORECASE,
)


def _parse_exact_extra(text: str) -> Optional[str]:
    """The new exact-time phrasings this module adds. Returns HH:MM or None.
    Only consulted by classify_time_phrase() after booking_service's own
    12h/24h/o'clock regexes have already had first chance — those are the
    most explicit and take priority."""
    m = _TIME_APPROX_RE.search(text)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2) or 0)
        period = (m.group(3) or "").lower()
        if period == "pm" and h != 12:
            h += 12
        elif period == "am" and h == 12:
            h = 0
        elif not period:
            h = _assume_period(h)
        return f"{h % 24:02d}:{mi:02d}"

    m = _TIME_HALF_PAST_RE.search(text)
    if m:
        h = _hour_value(m.group(1))
        if h is not None:
            return f"{_assume_period(h) % 24:02d}:30"

    m = _TIME_QUARTER_PAST_RE.search(text)
    if m:
        h = _hour_value(m.group(1))
        if h is not None:
            return f"{_assume_period(h) % 24:02d}:15"

    m = _TIME_QUARTER_TO_RE.search(text)
    if m:
        h = _hour_value(m.group(1))
        if h is not None:
            h2 = (_assume_period(h) - 1) % 24
            return f"{h2:02d}:45"

    return None


# ─────────────────────────────────────────────────────────────────────────────
# VAGUE time-of-day -> a real search RANGE (never a single guessed clock time)
# ─────────────────────────────────────────────────────────────────────────────

_VAGUE_TIME_RANGES = {
    "early morning": ("06:00", "08:00"),
    "morning":       ("08:00", "12:00"),
    "midday":        ("12:00", "13:00"),
    "noon":          ("12:00", "13:00"),
    "lunchtime":     ("12:00", "13:00"),
    "after lunch":   ("13:00", "17:00"),
    "afternoon":     ("12:00", "17:00"),
    "mid-afternoon": ("14:00", "16:00"),
    "after work":    ("17:00", "20:00"),
    "evening":       ("17:00", "20:00"),
    "tonight":       ("18:00", "21:00"),
    "late":          ("19:00", "21:00"),
}
# Longest phrase first so "after lunch" / "after work" match before any
# future shorter, overlapping key could.
_VAGUE_TIME_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(_VAGUE_TIME_RANGES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def classify_time_phrase(text: str) -> Optional[dict]:
    """
    Classify the time expressed in `text`, distinguishing an exact,
    customer-stated time from a vague time-of-day word.

    Returns one of:
      {"time": "15:00", "vague": False}
          — an exact time the customer actually said (digits, "3pm",
            "half past four", "around 3"...). Safe to use directly.
      {"time": None, "vague": True, "range": {"start": "12:00", "end": "17:00"}}
          — only a vague time-of-day word was found ("afternoon",
            "after work"...). The caller MUST check real availability
            (booking_service.get_available_slots) within this range
            rather than presenting any single guessed time as if it
            were confirmed or available.
      None
          — no time expression found at all.

    Never raises. Does not call booking_service._parse_time() and does
    not change its behavior — this is an independent classification of
    the same text, checked in the same precedence order.
    """
    if not text:
        return None
    try:
        m = _bs._TIME_12H.search(text)
        if m:
            h, mi = int(m.group(1)), int(m.group(2) or 0)
            period = m.group(3).lower()
            if period == "pm" and h != 12:
                h += 12
            if period == "am" and h == 12:
                h = 0
            return {"time": f"{h:02d}:{mi:02d}", "vague": False}

        m = _bs._TIME_24H.search(text)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                return {"time": f"{h:02d}:{mi:02d}", "vague": False}

        m = _bs._TIME_OCLOCK.search(text)
        if m:
            h = int(m.group(1))
            if 1 <= h <= 8:
                h += 12
            return {"time": f"{h:02d}:00", "vague": False}

        exact = _parse_exact_extra(text)
        if exact:
            return {"time": exact, "vague": False}

        m = _VAGUE_TIME_PATTERN.search(text)
        if m:
            start, end = _VAGUE_TIME_RANGES[m.group(1).lower()]
            return {"time": None, "vague": True, "range": {"start": start, "end": end}}

    except Exception as exc:
        log.debug("classify_time_phrase failed (ignored): %s", exc)

    return None


def filter_slots_in_range(slots: list, time_range: dict, limit: int = 3) -> list:
    """Return up to `limit` real slots (HH:MM strings, exactly as returned
    by booking_service.get_available_slots) that fall within time_range's
    start/end window. Pure list filtering — no DB access, never invents a
    slot: only ever returns values that were already in `slots`."""
    if not slots or not time_range:
        return []
    start = time_range.get("start", "00:00")
    end = time_range.get("end", "23:59")
    return [s for s in slots if start <= s < end][:limit]


def format_slot_options(slots: list, date_label: str = "") -> str:
    """Format a short list of REAL available times for a WhatsApp reply,
    e.g. "I have 2:00 PM, 3:30 PM, and 4:30 PM available tomorrow. Which
    works best?" `slots` must already be real, checked times (HH:MM) —
    this function only formats, it never generates or guesses times."""
    if not slots:
        return ""
    formatted = [_bs._format_time(s) for s in slots]
    when = f" {date_label}" if date_label else ""
    if len(formatted) == 1:
        listing = formatted[0]
    elif len(formatted) == 2:
        listing = f"{formatted[0]} and {formatted[1]}"
    else:
        listing = ", ".join(formatted[:-1]) + f", and {formatted[-1]}"
    return f"I have {listing} available{when}. Which works best? 😊"


def resolve_time_for_booking(
    business_id: int,
    date_str: str,
    text: str,
    duration_hrs: float = 1.0,
    max_options: int = 3,
) -> dict:
    """
    Given a customer's message and an already-known booking date, decide
    what to do about the TIME, honoring the "never invent an available
    time" rule. Returns one of:

      {"outcome": "no_time"}
          No time expression found in `text` at all.
      {"outcome": "exact", "time": "15:00"}
          An exact, customer-stated time — safe to use directly (still
          subject to the caller's own check_availability() call before
          actually confirming/creating the booking).
      {"outcome": "options", "slots": ["14:00", "15:30", "16:30"]}
          A vague time-of-day word was given AND real matching slots
          exist for that day within that window — these are genuine
          slots straight from booking_service.get_available_slots().
      {"outcome": "ask"}
          A vague time-of-day word was given but no real slots exist in
          that window (or availability couldn't be verified) — the
          caller should ask an open question rather than guess, per the
          spec's own example: "Sure 😊 What time would you prefer?"

    Never raises.
    """
    try:
        classified = classify_time_phrase(text)
        if not classified:
            return {"outcome": "no_time"}
        if not classified.get("vague"):
            return {"outcome": "exact", "time": classified["time"]}

        result = _bs.get_available_slots(business_id, date_str, duration_hrs=duration_hrs)
        matches = filter_slots_in_range(result.get("slots") or [], classified.get("range") or {}, limit=max_options)
        if matches:
            return {"outcome": "options", "slots": matches}
        return {"outcome": "ask"}
    except Exception as exc:
        log.debug("resolve_time_for_booking failed (ignored): %s", exc)
        return {"outcome": "no_time"}
