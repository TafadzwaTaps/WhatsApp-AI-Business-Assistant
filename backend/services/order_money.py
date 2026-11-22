"""
services/order_money.py: format an order's amount for a customer/owner message in the order's own currency.

Thin wrapper over services.money: it looks up the business once (for its currency and country) and never raises.
Display only: amounts are never changed here.
"""
from __future__ import annotations

import logging

from services.money import order_money
from services.tz_display import format_order_datetime

log = logging.getLogger("wazibot")


def order_amount_text(order: dict | None, amount=None, business: dict | None = None) -> str:
    """'7,53 zł' / '$7.53' for `amount` (default: the order's total_price). `business` may be passed to avoid a lookup."""
    order = order or {}
    if business is None and order.get("business_id"):
        try:
            import crud
            business = crud.get_business_by_id(order["business_id"])
        except Exception as exc:                    # a failed lookup only means the order's own currency is used
            log.debug("order_amount_text: business lookup skipped: %s", type(exc).__name__)
    return order_money(order, business, amount=amount)


def order_date_text(order: dict | None, business: dict | None = None) -> str:
    """The order's date/time in the BUSINESS's time zone ('2026-11-22 17:58'). Uses created_at (never the server clock);
    falls back to 'now' only when the order has no timestamp."""
    order = order or {}
    if business is None and order.get("business_id"):
        try:
            import crud
            business = crud.get_business_by_id(order["business_id"])
        except Exception as exc:
            log.debug("order_date_text: business lookup skipped: %s", type(exc).__name__)
    from datetime import datetime, timezone
    return format_order_datetime(order.get("created_at") or datetime.now(timezone.utc), business)
