"""
conversation_states.py — Centralized conversation state machine for WaziBot.

STATES
──────
  browsing          → Customer is browsing the menu / adding items
  confirm_order     → Cart shown, awaiting yes/no confirmation
  checkout          → Payment method selection screen shown
  awaiting_payment  → Order placed, waiting for customer to say "paid"
  awaiting_proof    → "paid" received, waiting for txn ID or screenshot
  manual_review     → Proof submitted, human agent reviewing
  human_handoff     → Customer requested human support; AI paused
  survey            → Post-completion satisfaction survey
  completed         → Order fully completed
  cancelled         → Order cancelled

TRANSITIONS (valid moves between states)
──────────────────────────────────────────
  browsing          → confirm_order, checkout, human_handoff
  confirm_order     → checkout, browsing, cancelled
  checkout          → awaiting_payment, browsing, cancelled
  awaiting_payment  → awaiting_proof, completed, cancelled, manual_review
  awaiting_proof    → manual_review, cancelled
  manual_review     → completed, cancelled, browsing
  human_handoff     → browsing  (only when agent hands back)
  survey            → browsing
  completed         → browsing
  cancelled         → browsing

Usage
─────
  from conversation_states import STATE, can_transition, label_for

  if can_transition(current_state, STATE.CHECKOUT):
      ...
"""

from typing import Optional


class STATE:
    """String constants for all conversation states. Use these instead of
    raw string literals throughout the codebase to prevent typos."""
    BROWSING          = "browsing"
    CONFIRM_ORDER     = "confirm_order"
    CHECKOUT          = "checkout"
    AWAITING_PAYMENT  = "awaiting_payment"
    AWAITING_PROOF    = "awaiting_proof"
    MANUAL_REVIEW     = "manual_review"
    HUMAN_HANDOFF     = "human_handoff"
    SURVEY            = "survey"
    COMPLETED         = "completed"
    CANCELLED         = "cancelled"

    ALL = {
        BROWSING, CONFIRM_ORDER, CHECKOUT,
        AWAITING_PAYMENT, AWAITING_PROOF,
        MANUAL_REVIEW, HUMAN_HANDOFF,
        SURVEY, COMPLETED, CANCELLED,
    }


# Valid state transitions. Key = current state, value = set of allowed next states.
_TRANSITIONS: dict[str, set[str]] = {
    STATE.BROWSING:         {STATE.CONFIRM_ORDER, STATE.CHECKOUT, STATE.HUMAN_HANDOFF},
    STATE.CONFIRM_ORDER:    {STATE.CHECKOUT, STATE.BROWSING, STATE.CANCELLED},
    STATE.CHECKOUT:         {STATE.AWAITING_PAYMENT, STATE.BROWSING, STATE.CANCELLED},
    STATE.AWAITING_PAYMENT: {STATE.AWAITING_PROOF, STATE.COMPLETED, STATE.CANCELLED,
                              STATE.MANUAL_REVIEW},
    STATE.AWAITING_PROOF:   {STATE.MANUAL_REVIEW, STATE.CANCELLED},
    STATE.MANUAL_REVIEW:    {STATE.COMPLETED, STATE.CANCELLED, STATE.BROWSING},
    STATE.HUMAN_HANDOFF:    {STATE.BROWSING},
    STATE.SURVEY:           {STATE.BROWSING},
    STATE.COMPLETED:        {STATE.BROWSING},
    STATE.CANCELLED:        {STATE.BROWSING},
}

# Any state can always transition to browsing (hard reset / cancel)
# and to human_handoff (emergency escalation)
_ALWAYS_ALLOWED = {STATE.BROWSING, STATE.HUMAN_HANDOFF}

# Human-readable labels for logging and status messages
_LABELS: dict[str, str] = {
    STATE.BROWSING:         "Browsing",
    STATE.CONFIRM_ORDER:    "Confirming order",
    STATE.CHECKOUT:         "Selecting payment",
    STATE.AWAITING_PAYMENT: "Awaiting payment",
    STATE.AWAITING_PROOF:   "Awaiting payment proof",
    STATE.MANUAL_REVIEW:    "Under manual review",
    STATE.HUMAN_HANDOFF:    "Human support mode",
    STATE.SURVEY:           "Satisfaction survey",
    STATE.COMPLETED:        "Completed",
    STATE.CANCELLED:        "Cancelled",
}


def can_transition(current: str, next_state: str) -> bool:
    """
    Returns True if moving from `current` to `next_state` is a valid transition.

    Always allows moving to BROWSING (reset) or HUMAN_HANDOFF (escalation).
    Treats unknown states as BROWSING to fail-safe.

    Example:
        can_transition(STATE.CHECKOUT, STATE.AWAITING_PAYMENT)  → True
        can_transition(STATE.COMPLETED, STATE.CHECKOUT)         → False
    """
    if next_state in _ALWAYS_ALLOWED:
        return True
    allowed = _TRANSITIONS.get(current, _TRANSITIONS[STATE.BROWSING])
    return next_state in allowed


def label_for(state: str) -> str:
    """Return a human-readable label for a state constant."""
    return _LABELS.get(state, state.replace("_", " ").title())


def normalize_state(raw: Optional[str]) -> str:
    """
    Coerce a raw state string from the DB into a valid STATE constant.
    Falls back to STATE.BROWSING for any unknown/None value.
    """
    if raw and raw in STATE.ALL:
        return raw
    return STATE.BROWSING


def is_active_order_state(state: str) -> bool:
    """
    Returns True when the customer has an active order in progress
    and should not be pushed to browse/add items.
    """
    return state in {
        STATE.CONFIRM_ORDER,
        STATE.CHECKOUT,
        STATE.AWAITING_PAYMENT,
        STATE.AWAITING_PROOF,
        STATE.MANUAL_REVIEW,
    }


def is_ai_paused(state: str) -> bool:
    """
    Returns True when the AI should not auto-reply.
    Currently only HUMAN_HANDOFF pauses the AI.
    """
    return state == STATE.HUMAN_HANDOFF
