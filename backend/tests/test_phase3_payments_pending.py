"""Phase 3, second review: pending captures are not paid, a swallowed database failure is not a
successful refund, a denied event cannot overwrite a paid order, 'I paid' with no order is not
confirmed."""
import types

import pytest

import routes.chat_routes as cr
import services.payment_service as ps
import workflows.order_lifecycle as ol

from test_phase3_payments import hook, _run, _success, _cap, _ord, _event   # noqa: F401
from test_phase3_payments_extra import _preview, _paid                      # noqa: F401


# -- capture status: an order can be COMPLETED while its capture is still PENDING -------------
def _capture_via_http(monkeypatch, order_status, capture_status):
    monkeypatch.setattr(ps, "_paypal_token", lambda: "tok")
    body = {"status": order_status, "purchase_units": [{
        "reference_id": "ORDER-9", "custom_id": "9",
        "payments": {"captures": [{"status": capture_status,
                                   "amount": {"currency_code": "USD", "value": "10.00"}}]}}]}

    class R:
        def raise_for_status(self): pass
        def json(self): return body
    monkeypatch.setattr(ps.http, "post", lambda *a, **k: R())
    return ps.capture_paypal_order("PP-1")


def test_a_completed_capture_is_paid(monkeypatch):
    out = _capture_via_http(monkeypatch, "COMPLETED", "COMPLETED")
    assert out["paid"] is True and out["pending"] is False and out["capture_status"] == "COMPLETED"


@pytest.mark.parametrize("cap_status", ["PENDING", "DECLINED", ""])
def test_a_completed_order_with_a_capture_that_is_not_completed_is_not_paid(monkeypatch, cap_status):
    out = _capture_via_http(monkeypatch, "COMPLETED", cap_status)
    assert out["paid"] is False
    assert out["pending"] is (cap_status != "COMPLETED")


def test_return_page_does_not_confirm_while_the_capture_is_pending(monkeypatch):
    cap = dict(_cap(), paid=False, pending=True)
    out, claims, notes = _success(monkeypatch, order=_ord(), capture=cap)
    assert out["status"] == "pending" and claims == [] and notes == []


def test_return_page_does_not_confirm_a_completed_order_whose_capture_is_pending(monkeypatch):
    out, claims, notes = _success(monkeypatch, order=_ord(), capture=_cap(),
                                  preview=_preview(status="COMPLETED", captured_status="PENDING",
                                                   captured_amount=10.0, captured_currency="USD"))
    assert out["status"] == "pending" and claims == [] and notes == [] and _success.captured == []


def test_the_requested_amount_is_never_used_in_place_of_the_captured_amount(monkeypatch):
    # COMPLETED + COMPLETED capture, but nothing recorded as captured: must not fall back to the requested amount
    out, claims, _ = _success(monkeypatch, order=_ord(), capture=_cap(),
                              preview=_preview(status="COMPLETED", captured_status="COMPLETED",
                                               captured_amount=0.0, captured_currency=""))
    assert out["status"] == "error" and claims == []


def test_preview_reports_the_capture_status(monkeypatch):
    monkeypatch.setattr(ps, "_paypal_token", lambda: "tok")
    body = {"status": "COMPLETED", "purchase_units": [{
        "reference_id": "ORDER-9", "custom_id": "9", "amount": {"currency_code": "USD", "value": "10.00"},
        "payments": {"captures": [{"status": "PENDING", "amount": {"currency_code": "USD", "value": "10.00"}}]}}]}

    class R:
        def raise_for_status(self): pass
        def json(self): return body
    monkeypatch.setattr(ps.http, "get", lambda *a, **k: R())
    assert ps.paypal_order_preview("PP-1")["captured_status"] == "PENDING"


# -- a swallowed database failure must not look like a successful refund -----------------------
def test_refund_whose_database_update_silently_fails_is_retried_not_dropped(hook, monkeypatch):
    c, st = hook
    _paid(st)
    state = {"fail": True}

    def update(*a, **k):                      # the real function logs and returns None on a database error
        if state["fail"]:
            return None
        st["payments"].append(a)
        return {"id": a[0]}
    monkeypatch.setattr(cr.crud, "update_order_payment", update)
    res = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-S1", "10.00"))
    assert res.status_code == 503                                      # PayPal will redeliver
    assert st["db"].tables["notification_log"] == []                   # and the event claim was given back
    state["fail"] = False
    assert c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-S1", "10.00")).json()["refunded"] is True


# -- denied capture events ---------------------------------------------------------------------
@pytest.mark.parametrize("status", ["paid", "refunded"])
def test_a_denied_event_cannot_overwrite_a_paid_or_refunded_order(hook, status):
    c, st = hook
    st["order"] = _ord(payment_status=status)
    out = c.post("/payments/paypal/webhook", json={"id": "EV-D1", "event_type": "PAYMENT.CAPTURE.DENIED",
                                                   "resource": {"custom_id": "9"}})
    assert out.status_code == 200 and st["payments"] == [] and st["notes"] == []


def test_a_denied_event_on_an_unpaid_order_still_marks_it_failed(hook):
    c, st = hook
    st["order"] = _ord(payment_status="awaiting_payment")
    c.post("/payments/paypal/webhook", json={"id": "EV-D2", "event_type": "PAYMENT.CAPTURE.DENIED",
                                             "resource": {"custom_id": "9"}})
    assert st["payments"] == [(9, 1, {"payment_status": "payment_failed"})] and len(st["notes"]) == 1


# -- "I paid" with nothing to verify against ---------------------------------------------------
def test_customer_paid_message_with_no_order_is_never_confirmed(monkeypatch):
    import services._ai_payments as ap
    import services._ai_state as stt
    claims = []
    monkeypatch.setattr(ps, "get_paypal_order_details", lambda i: {
        "paid": True, "amount": 10.0, "currency": "USD", "internal_order_id": 9})
    monkeypatch.setattr(ap.crud, "claim_order_paid", lambda *a: claims.append(a) or True)
    monkeypatch.setattr(stt, "_read_state_data", lambda p, b: {"paypal_order_id": "PP-1"})
    monkeypatch.setattr(stt, "_reset_state", lambda p, b: None)
    out = ap._handle_paypal_paid_message("2637", 1, "Shop", None, "ORDER-?")
    assert "verifying" in out.lower() and claims == []
