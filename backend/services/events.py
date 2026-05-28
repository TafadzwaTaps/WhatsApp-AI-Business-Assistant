"""
services/events.py — Lightweight Internal Event System (Phase 7)

PURPOSE
───────
A pure-Python, zero-dependency event bus for internal triggering.
No Redis, no Celery, no external queues — just a dict of listeners
called synchronously in a try/except so they can never crash the caller.

EVENTS
──────
  order_created         — new order placed
  payment_received      — payment confirmed (paid/proof verified)
  payment_failed        — payment proof rejected
  low_stock_detected    — product stock at or below threshold
  customer_repeat       — customer placed their Nth order
  handoff_requested     — human handoff triggered
  handoff_released      — AI resumed
  broadcast_sent        — campaign/broadcast dispatched

USAGE
─────
Subscribe (at startup or in service init):

    from services.events import Events

    @Events.on("order_created")
    def handle_new_order(payload):
        log.info("New order: %s", payload)

Emit (anywhere in the codebase):

    from services.events import Events

    Events.emit("order_created", {
        "order_id":    21,
        "business_id": 3,
        "phone":       "263771234567",
        "total":       3.25,
    })

RULES
─────
• Listeners MUST NOT raise — wrap any risky code in try/except.
• Listeners are called synchronously — keep them fast (< 50ms).
• emit() itself never raises — all listener exceptions are caught and logged.
• This is for internal analytics / AI suggestions only.
  Do NOT use for critical business logic (orders, payments).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# EVENT BUS
# ─────────────────────────────────────────────────────────────────────────────

class _EventBus:
    """
    Simple synchronous event bus. Thread-safe for reads (listener dispatch).
    Adding listeners at runtime while emitting is NOT safe — register all
    listeners at startup.
    """

    def __init__(self):
        self._listeners: dict[str, list[Callable]] = defaultdict(list)

    def on(self, event: str):
        """Decorator to register a listener for an event."""
        def decorator(fn: Callable) -> Callable:
            self._listeners[event].append(fn)
            log.debug("events: registered listener  event=%s  fn=%s", event, fn.__name__)
            return fn
        return decorator

    def subscribe(self, event: str, fn: Callable) -> None:
        """Register a listener programmatically (alternative to @Events.on)."""
        self._listeners[event].append(fn)
        log.debug("events: subscribed  event=%s  fn=%s", event, fn.__name__)

    def emit(self, event: str, payload: dict | None = None) -> int:
        """
        Emit an event to all registered listeners.

        Returns the number of listeners successfully called.
        Never raises — all exceptions are caught and logged.
        """
        if payload is None:
            payload = {}

        listeners = self._listeners.get(event, [])
        if not listeners:
            return 0

        log.debug("events: emit  event=%s  listeners=%d  payload=%s",
                  event, len(listeners), {k: v for k, v in payload.items() if k != "token"})

        called = 0
        for fn in listeners:
            try:
                fn(payload)
                called += 1
            except Exception as exc:
                log.warning(
                    "events: listener error  event=%s  fn=%s  exc=%s",
                    event, getattr(fn, "__name__", str(fn)), exc,
                )
        return called

    def listeners(self, event: str) -> list[str]:
        """Return listener names for an event (useful for debugging)."""
        return [getattr(fn, "__name__", str(fn)) for fn in self._listeners.get(event, [])]

    def all_events(self) -> list[str]:
        """Return list of all events that have at least one listener."""
        return [e for e, ls in self._listeners.items() if ls]


# Singleton instance — import this everywhere
Events = _EventBus()


# ─────────────────────────────────────────────────────────────────────────────
# BUILT-IN LISTENERS
# These run automatically on relevant events for analytics / AI hints.
# All are non-critical — failures are logged but never affect main flow.
# ─────────────────────────────────────────────────────────────────────────────

@Events.on("order_created")
def _on_order_created(payload: dict) -> None:
    """Log new orders to analytics."""
    log.info(
        "EVENT order_created  order=%s  biz=%s  phone=%s  total=%s",
        payload.get("order_id"), payload.get("business_id"),
        payload.get("phone"), payload.get("total"),
    )


@Events.on("payment_received")
def _on_payment_received(payload: dict) -> None:
    """Log payment confirmations."""
    log.info(
        "EVENT payment_received  order=%s  biz=%s  method=%s  amount=%s",
        payload.get("order_id"), payload.get("business_id"),
        payload.get("payment_method"), payload.get("amount"),
    )


@Events.on("low_stock_detected")
def _on_low_stock(payload: dict) -> None:
    """Log low stock events — could trigger owner notification in future."""
    log.warning(
        "EVENT low_stock_detected  product=%s  stock=%s  biz=%s",
        payload.get("product_name"), payload.get("stock"),
        payload.get("business_id"),
    )


@Events.on("customer_repeat")
def _on_customer_repeat(payload: dict) -> None:
    """Log repeat customer milestone."""
    log.info(
        "EVENT customer_repeat  phone=%s  biz=%s  order_count=%s  segment=%s",
        payload.get("phone"), payload.get("business_id"),
        payload.get("order_count"), payload.get("segment"),
    )


@Events.on("broadcast_sent")
def _on_broadcast_sent(payload: dict) -> None:
    """Log broadcast/campaign dispatch."""
    log.info(
        "EVENT broadcast_sent  biz=%s  audience=%s  sent=%s  failed=%s",
        payload.get("business_id"), payload.get("audience"),
        payload.get("sent"), payload.get("failed"),
    )
