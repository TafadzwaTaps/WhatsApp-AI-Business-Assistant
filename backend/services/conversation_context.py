"""
services/conversation_context.py — Phase 3: short-term conversation context.

WHAT THIS IS
────────────
A small, additive layer that lets services/intent_engine.py's classifier
(Phase 2 — stateless by design) resolve elliptical replies that only make
sense next to the customer's own last couple of messages:

    Customer: I want a burger.
    Bot:      Which one?
    Customer: Chicken.
    Bot:      How many?
    Customer: Two.

Classified alone, "Two." has no product entity and is low-confidence.
resolve_with_context() re-classifies the customer's last few messages
too, and — only when the current message's own classification is
missing an entity — fills the gap from the nearest prior message that
had it. "Two." + "Chicken." (from context) => add_to_cart, product =
"Chicken Burger", quantity = 2.

WHAT THIS IS NOT
────────────────
This does not add any new storage. The "context" is read on demand from
the `messages` table that routes/webhook_routes.py already writes to
for the dashboard inbox (crud.get_recent_messages(), bounded to a small
limit) — nothing is kept around longer or written anywhere new. There is
no LLM here and no summarization: with no LLM anywhere in the customer
pipeline yet (see the Phase 0 audit), there's no token cost to manage by
summarizing older turns — that becomes relevant only once Phase 10
(AI-generated responses) introduces an actual LLM call, and this module
already keeps the context window small (CONTEXT_WINDOW messages) so it
stays cheap to extend then.

Like Phase 2's classifier, this is entirely read-only with respect to
the existing state machine: it never touches carts.state_data and is
never in a position to change what generate_reply() does on its own —
routes/webhook_routes.py only ever uses its output the same way it uses
the raw Phase 2 classification (observability logging, and — only when
INTENT_ENGINE_LOW_CONFIDENCE_INTERCEPT is explicitly enabled — the same
narrow, idle-only clarification intercept described there).
"""

from __future__ import annotations

import logging
from typing import Optional

from services.intent_engine import IntentResult, classify_intent

log = logging.getLogger(__name__)

# How many of the customer's own most recent messages (not counting the
# current one) to consider as candidates for filling a missing entity.
# Small and fixed — this is "the last couple of turns", not a summary of
# the whole conversation ("do not store/replay raw history forever just
# because it exists" — Phase 3 spec).
CONTEXT_LOOKBACK = 3

# Messages fetched from the DB to search for that many customer turns —
# a bit larger than CONTEXT_LOOKBACK since outgoing (bot) messages are
# interleaved and filtered out below.
_FETCH_LIMIT = 8


def get_recent_customer_texts(customer_id: int, limit: int = CONTEXT_LOOKBACK) -> list[str]:
    """
    Return up to `limit` of the customer's own most recent prior message
    texts (oldest first among those returned), excluding bot/agent
    replies. Never raises — an empty list means "no context available",
    which callers already treat as a safe no-op.
    """
    try:
        import crud
        rows = crud.get_recent_messages(customer_id, limit=_FETCH_LIMIT)
    except Exception as exc:
        log.debug("conversation_context: fetching recent messages failed (ignored): %s", exc)
        return []

    customer_texts = [
        r.get("text", "") for r in rows
        if r.get("direction") == "incoming" and (r.get("text") or "").strip()
    ]
    return customer_texts[-limit:]


def resolve_with_context(
    current_result: IntentResult,
    recent_customer_texts: list[str],
    products: Optional[list] = None,
) -> IntentResult:
    """
    Given the current message's own (context-free) classification and a
    short list of the customer's prior message texts (oldest first, most
    recent last), try to fill in a missing `product` or `quantity` entity
    from the nearest prior message that has it.

    Only ever ADDS information the base classification was missing — never
    overrides an entity the current message already provided itself, and
    never invents an intent/entity pairing that neither the current
    message nor a recent one actually supports. Returns `current_result`
    unchanged whenever there's nothing useful to add.
    """
    if not recent_customer_texts:
        return current_result

    # Only worth attempting for the intents where a missing product/
    # quantity entity is actually the reason a message reads as
    # incomplete — a genuinely unrelated "unknown" message (spam,
    # gibberish, a completely different topic) should stay unknown, not
    # get an entity grafted on from an unrelated earlier turn.
    _CONTEXT_ELIGIBLE_INTENTS = ("add_to_cart", "update_quantity", "remove_from_cart", "unknown")
    if current_result.intent not in _CONTEXT_ELIGIBLE_INTENTS:
        return current_result

    # A completely unrelated "unknown" message (no product, no quantity —
    # e.g. gibberish, or a message about something else entirely) has no
    # gap to fill: it isn't an elliptical reply at all, just an
    # unclassifiable one. Only "unknown" messages that DID carry at least
    # a partial signal on their own (a bare quantity, or a fuzzy product
    # match) are candidates for context to complete.
    if current_result.intent == "unknown" and not current_result.entities:
        return current_result

    have_product = bool(current_result.entities.get("product"))
    have_quantity = bool(current_result.entities.get("quantity")) and current_result.entities.get("quantity") != 1
    if have_product and have_quantity:
        return current_result  # already complete, nothing to resolve

    found_product: Optional[str] = None
    found_product_id = None
    found_quantity: Optional[int] = None

    # Nearest-first: walk backwards through recent turns so the most
    # recent mention wins if more than one candidate has the same entity.
    for text in reversed(recent_customer_texts):
        candidate = classify_intent(text, products=products)
        if found_product is None and candidate.entities.get("product"):
            found_product = candidate.entities["product"]
            found_product_id = candidate.entities.get("product_id")
        if found_quantity is None and candidate.entities.get("quantity"):
            found_quantity = candidate.entities["quantity"]
        if found_product is not None and found_quantity is not None:
            break

    resolved_product = current_result.entities.get("product") or found_product
    resolved_product_id = current_result.entities.get("product_id") or found_product_id
    resolved_quantity = current_result.entities.get("quantity") or found_quantity

    # Only produce a result if this actually added something the current
    # message alone didn't have.
    added_something = (
        (resolved_product and not have_product) or
        (resolved_quantity and not have_quantity)
    )
    if not added_something or not (resolved_product and resolved_quantity):
        return current_result

    entities = dict(current_result.entities)
    entities["product"] = resolved_product
    if resolved_product_id is not None:
        entities["product_id"] = resolved_product_id
    entities["quantity"] = resolved_quantity
    entities["resolved_from_context"] = True

    return IntentResult(
        intent="add_to_cart" if current_result.intent in ("unknown", "add_to_cart") else current_result.intent,
        confidence=max(current_result.confidence, 0.8),
        entities=entities,
        language=current_result.language,
        requires_clarification=False,
        raw_text=current_result.raw_text,
    )


def classify_with_context(
    text: str,
    customer_id: Optional[int],
    products: Optional[list] = None,
    language: str = "en",
) -> IntentResult:
    """
    Convenience entry point: classify `text` the same way Phase 2's
    classify_intent() does, then attempt to fill any missing product/
    quantity entity from the customer's own recent prior messages.
    Safe to call even when customer_id is None or context lookup fails —
    degrades to a plain classify_intent() call.
    """
    base = classify_intent(text, products=products, language=language)
    if not customer_id:
        return base
    recent = get_recent_customer_texts(customer_id)
    try:
        return resolve_with_context(base, recent, products=products)
    except Exception as exc:
        log.debug("conversation_context: resolution failed (ignored): %s", exc)
        return base
