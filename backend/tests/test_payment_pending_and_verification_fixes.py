"""
tests/test_payment_pending_and_verification_fixes.py

Regression tests for the "Request Payment / Mark Paid / Needs Attention
don't agree with each other" bug report and its root causes:

  1. crud/analytics.py::get_stale_payment_orders() (and its
     _all_businesses sibling) defaulted to statuses=["awaiting_payment",
     "payment_review"], silently excluding "pending_cash" — so a cash
     order never appeared in GET /payments/reminders/pending (the
     Reminders / "pending payments" dashboard page) no matter how long it
     sat unconfirmed, even though the Needs Attention panel flagged it
     immediately. Fixed by adding "pending_cash" to the default.

  2. crud/businesses.py::get_business_payment_settings() omitted
     bank_transfer_details/blik_number entirely, so even a business that
     had already configured Bank Transfer or BLIK in Settings would never
     have those methods actually offered to customers in the WhatsApp
     checkout menu (services/payment_service.py::available_methods()
     never saw the values). Fixed by including both fields.

Not covered here (pure frontend logic, no backend to unit-test):
  - static/dashboard.js's loadNeedsAttention() now reads payment_status
    instead of status.
  - static/inbox.js's qaRequestPayment/qaMarkPaid/qaGenerateInvoice now
    read the full order list (GET /orders) instead of the stale-only
    reminders endpoint, and filter on payment_status.
  - static/inbox.js's new payment-verification banner (checkPaymentVerification).
  - static/inbox.html's quick-action buttons now disabled until handoff
    is on (data-requires-handoff).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ── 1. get_stale_payment_orders now includes pending_cash by default ───────

def test_stale_payment_orders_default_includes_pending_cash(fake_supabase):
    import crud

    fake_supabase.results["orders"] = [
        {"id": 1, "business_id": 7, "customer_phone": "+1", "total_price": 10,
         "payment_method": "ecocash", "payment_status": "awaiting_payment",
         "payment_reference": "", "status": "pending", "created_at": _iso(2), "items": []},
        {"id": 2, "business_id": 7, "customer_phone": "+2", "total_price": 20,
         "payment_method": "cash", "payment_status": "pending_cash",
         "payment_reference": "", "status": "pending_cash", "created_at": _iso(2), "items": []},
    ]

    # Called exactly as every real call site calls it — no explicit
    # statuses, relying on the default.
    stale = crud.get_stale_payment_orders(7, older_than_hours=1.0)
    ids = {o["id"] for o in stale}

    # FakeQueryBuilder.in_() ignores its filter args and returns every row
    # for the table, so this exercises the *default statuses list* and the
    # age cutoff, not the DB-side filter — which is exactly what the bug
    # was (the Python-level default, not the query itself).
    assert 1 in ids, "awaiting_payment order should still be included"
    assert 2 in ids, "pending_cash order must now be included (previously silently excluded)"


def test_stale_payment_orders_all_businesses_default_includes_pending_cash(fake_supabase):
    import crud

    fake_supabase.results["orders"] = [
        {"id": 3, "business_id": 9, "customer_phone": "+3", "total_price": 30,
         "payment_method": "cash", "payment_status": "pending_cash",
         "payment_reference": "", "status": "pending_cash", "created_at": _iso(5)},
    ]
    stale = crud.get_stale_payment_orders_all_businesses(older_than_hours=1.0)
    assert any(o["id"] == 3 for o in stale)


def test_stale_payment_orders_respects_explicit_statuses_override(fake_supabase):
    """An explicit statuses= argument must still be honored unchanged —
    this fix only changes the *default*, never overrides a caller's own
    explicit list."""
    import crud

    fake_supabase.results["orders"] = [
        {"id": 4, "business_id": 7, "customer_phone": "+4", "total_price": 10,
         "payment_method": "ecocash", "payment_status": "awaiting_payment",
         "payment_reference": "", "status": "pending", "created_at": _iso(2), "items": []},
    ]
    stale = crud.get_stale_payment_orders(7, older_than_hours=1.0, statuses=["awaiting_payment"])
    assert len(stale) == 1
    assert stale[0]["id"] == 4


# ── 2. get_business_payment_settings now returns bank_transfer_details/blik ─

def test_business_payment_settings_includes_banktransfer_and_blik(fake_supabase):
    import crud

    fake_supabase.results["businesses"] = [{
        "id": 42, "ecocash_number": "", "ecocash_name": "",
        "paypal_email": "shop@example.com",
        "bank_transfer_details": "IBAN: PL00 0000 0000 0000 0000",
        "blik_number": "+48500000000",
    }]

    settings = crud.get_business_payment_settings(42)
    assert settings["bank_transfer_details"] == "IBAN: PL00 0000 0000 0000 0000"
    assert settings["blik_number"] == "+48500000000"
    assert settings["paypal_email"] == "shop@example.com"


def test_business_payment_settings_defaults_empty_when_unset(fake_supabase):
    import crud

    fake_supabase.results["businesses"] = [{
        "id": 43, "ecocash_number": "+263771234567", "ecocash_name": "Shop",
        "paypal_email": "", "bank_transfer_details": None, "blik_number": None,
    }]

    settings = crud.get_business_payment_settings(43)
    assert settings["bank_transfer_details"] == ""
    assert settings["blik_number"] == ""
    assert settings["ecocash_number"] == "+263771234567"


def test_business_payment_settings_unknown_business_still_has_new_keys(fake_supabase):
    import crud

    fake_supabase.results["businesses"] = []
    settings = crud.get_business_payment_settings(999)
    assert settings["bank_transfer_details"] == ""
    assert settings["blik_number"] == ""


# ── 3. available_methods() actually offers Bank Transfer / BLIK once the
#        settings dict carries them (the end-to-end point of fix #2) ───────

def test_available_methods_offers_banktransfer_and_blik_for_poland_style_business():
    from services.payment_service import available_methods

    order = {
        "ecocash_number": "",                      # not configured — Zimbabwe-specific, opted out
        "bank_transfer_details": "IBAN: PL00 ...",  # configured
        "blik_number": "+48500000000",              # configured
        "paypal_email": "shop@example.com",
    }
    methods = available_methods(order)
    assert "ecocash" not in methods, "should not offer EcoCash when not configured"
    assert "banktransfer" in methods
    assert "blik" in methods
    assert "paypal" in methods
    assert methods[-1] == "cash", "cash is always included, always last"


def test_available_methods_offers_ecocash_for_zimbabwe_style_business():
    from services.payment_service import available_methods

    order = {
        "ecocash_number": "+263771234567",
        "ecocash_name": "Shop",
        "bank_transfer_details": "",
        "blik_number": "",
        "paypal_email": "",
    }
    methods = available_methods(order)
    assert "ecocash" in methods
    assert "banktransfer" not in methods
    assert "blik" not in methods
    assert methods[-1] == "cash"
