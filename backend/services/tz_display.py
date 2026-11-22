"""
services/tz_display.py: show stored UTC timestamps in the BUSINESS's own time zone (display only).

Timestamps stay UTC in the database. The business zone is `businesses.features_json["timezone"]` (an IANA name such as
"Europe/Warsaw"). Conversion uses zoneinfo, so daylight-saving changes are handled by the tz database, never by fixed
offsets. An unset, invalid or unavailable zone falls back to UTC; nothing here raises.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

log = logging.getLogger("wazibot")

try:                                   # pragma: no cover - import guard for systems without tz data
    from zoneinfo import ZoneInfo
except Exception:                      # pragma: no cover
    ZoneInfo = None                    # type: ignore


def business_tz(business: dict | None):
    """tzinfo for the business; UTC when unset, invalid or tz data is missing."""
    try:
        fj = (business or {}).get("features_json")
        name = str(fj.get("timezone") or "").strip() if isinstance(fj, dict) else ""
        if name and ZoneInfo is not None:
            return ZoneInfo(name)
    except Exception as exc:                       # invalid name or no tz data: show UTC rather than fail
        log.debug("business timezone unavailable (%s): %s", type(exc).__name__, exc)
    return timezone.utc


def parse_utc(value) -> datetime | None:
    """An aware UTC datetime from an ISO string ('Z' or offset; naive text is taken as UTC) or a datetime."""
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00").replace(" ", "T", 1))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def to_business_time(value, business: dict | None = None) -> datetime | None:
    dt = parse_utc(value)
    if dt is None:
        return None
    try:
        return dt.astimezone(business_tz(business))
    except Exception:
        return dt.astimezone(timezone.utc)


def format_order_datetime(value, business: dict | None = None, with_tz: bool = False) -> str:
    """'2026-11-22 17:58' in the business zone. Unparseable text is returned as the old '[:16]' form, never an error."""
    dt = to_business_time(value, business)
    if dt is None:
        return (str(value or "")[:16]).replace("T", " ")
    out = dt.strftime("%Y-%m-%d %H:%M")
    if with_tz:
        out += " " + (dt.tzname() or "")
    return out
