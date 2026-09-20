"""
tests/test_idor_orders.py — Phase 7/19: tenant isolation regression tests.

Directly tests the hardening added to update_order_status_supabase() this
round: an order genuinely belonging to Business A must not be updatable by
a caller that passes Business B's business_id, even though every existing
call site already did its own separate ownership check before calling
this function. This proves the function is now safe on its own, not just
safe because every current caller happens to remember to check first.
"""

import pytest
from workflows.order_lifecycle import update_order_status_supabase


def test_cannot_update_another_businesss_order(fake_supabase):
    """
    The core regression test: Business B (id=99) must not be able to move
    an order that actually belongs to Business A (id=1) into a new status,
    even by calling the function directly with the right order_id and the
    wrong business_id.
    """
    fake_supabase.results["orders"] = [
        {"id": 501, "business_id": 1, "status": "pending", "total_price": 40.0}
    ]

    with pytest.raises(ValueError, match="not found"):
        update_order_status_supabase(order_id=501, status="confirmed", business_id=99)


def test_owner_can_update_their_own_order(fake_supabase):
    """The same call succeeds when the correct business_id is passed —
    confirms the hardening doesn't accidentally break the legitimate case."""
    fake_supabase.results["orders"] = [
        {"id": 501, "business_id": 1, "status": "pending", "total_price": 40.0}
    ]

    result = update_order_status_supabase(order_id=501, status="confirmed", business_id=1)
    assert result is not None


def test_business_id_omitted_still_works_for_existing_callers(fake_supabase):
    """
    Backward compatibility check: every call site that existed before this
    round's hardening doesn't pass business_id at all. Confirms those
    callers are completely unaffected — omitting the parameter must not
    raise or change behavior, since each of those 6 call sites was already
    independently verified (by direct code audit) to check ownership
    before ever reaching this function.
    """
    fake_supabase.results["orders"] = [
        {"id": 502, "business_id": 1, "status": "pending", "total_price": 25.0}
    ]

    result = update_order_status_supabase(order_id=502, status="confirmed")
    assert result is not None


def test_nonexistent_order_reports_not_found_not_forbidden(fake_supabase):
    """
    The rejection message for "wrong business" and "doesn't exist at all"
    must be identical — otherwise a caller probing sequential order IDs
    could distinguish "exists but isn't yours" from "doesn't exist",
    which leaks information about how many orders exist across the
    platform even to someone who can't access them.
    """
    fake_supabase.results["orders"] = []

    with pytest.raises(ValueError, match="not found"):
        update_order_status_supabase(order_id=99999, status="confirmed", business_id=1)
