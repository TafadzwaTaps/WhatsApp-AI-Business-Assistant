"""Phase 3: PayPal currency/binding/replay protections, Stripe storefront fulfilment (paid only,
once per session, retry on transient failure), impersonation block on billing mutations.
PayPal, Stripe, WhatsApp and the database are all faked; nothing is sent or charged."""
import asyncio
import logging
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import crud
import crud.notification_ledger as ledger
import routes.chat_routes as cr
import services.payment_service as ps
import workflows.order_lifecycle as ol

from test_phase1c_job_idempotency import _DB   # stateful fake supabase (unique constraints etc.)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═══════════════════════ currency / amount helpers ═══════════════════════════
def test_order_currency_comes_from_the_order_then_the_business_then_the_symbol(monkeypatch):
    assert ps.order_currency_code({"currency": "eur"}) == "EUR"
    import core.db as coredb
    db = _DB(businesses=[{"id": 5, "currency": "PLN"}])
    monkeypatch.setattr(coredb, "supabase", db)
    assert ps.order_currency_code({"business_id": 5, "currency_symbol": "$"}) == "PLN"
    assert ps.order_currency_code({"business_id": 99, "currency_symbol": "€"}) == "EUR"
    assert ps.order_currency_code({"currency_symbol": "$"}) == "USD"
    assert ps.order_currency_code({}) == "USD"


@pytest.mark.parametrize("cur,total,expected", [
    ("USD", 12.5, ("USD", "12.50", 12.5)),
    ("EUR", 9.999, ("EUR", "10.00", 10.0)),
    ("JPY", 1500.4, ("JPY", "1500", 1500.0)),          # zero-decimal currency: no cents
    ("HUF", 999.6, ("HUF", "1000", 1000.0)),
])
def test_paypal_charge_formatting(cur, total, expected):
    assert ps.paypal_charge_for({"currency": cur, "total_price": total}) == expected


@pytest.mark.parametrize("cur", ["ZAR", "NGN", "KES", "ZWL", "GHS", "UGX", "BWP"])
def test_currencies_paypal_cannot_charge_return_none(cur):
    assert ps.paypal_charge_for({"currency": cur, "total_price": 500}) is None


def test_amount_matching_is_to_the_cent():
    assert ps.paypal_amount_matches(10.00, 10.00)
    assert ps.paypal_amount_matches(10.004, 10.00)
    assert not ps.paypal_amount_matches(9.50, 10.00)
    assert not ps.paypal_amount_matches(10.10, 10.00)          # the old 0.10 tolerance is gone


# ═══════════════════════ PayPal order creation ═══════════════════════════════
def _create(monkeypatch, order, post):
    monkeypatch.setattr(ps, "_paypal_token", lambda: "tok")
    monkeypatch.setattr(ps.http, "post", post)
    return ps.create_paypal_order(order)


def _ok_post(sent):
    def post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        r = types.SimpleNamespace(raise_for_status=lambda: None)
        r.json = lambda: {"id": "PP-1", "links": [{"rel": "approve", "href": "https://pay/pp"}]}
        return r
    return post


@pytest.mark.parametrize("cur,total,value", [("USD", 10, "10.00"), ("EUR", 10, "10.00"), ("JPY", 1500, "1500")])
def test_paypal_order_is_created_in_the_business_currency(monkeypatch, cur, total, value):
    sent = []
    out = _create(monkeypatch, {"id": 7, "currency": cur, "total_price": total}, _ok_post(sent))
    amt = sent[0]["purchase_units"][0]["amount"]
    assert amt == {"currency_code": cur, "value": value}
    assert out["paypal_order_id"] == "PP-1"


def test_unsupported_currency_never_reaches_paypal_as_usd(monkeypatch):
    called = []

    def post(*a, **k):
        called.append(1)
        raise AssertionError("PayPal must not be called for an unsupported currency")
    out = _create(monkeypatch, {"id": 7, "currency": "ZAR", "total_price": 500,
                                "paypal_email": "owner@example.com"}, post)
    assert called == [] and out["fallback_reason"] == "currency_not_supported_by_paypal"
    assert not out.get("error")                                           # valid instructions: not a payment error
    assert out["auto_verified"] is False and out.get("message")           # the customer still gets instructions


# ═══════════════════════ PayPal return path ══════════════════════════════════
def _success(monkeypatch, *, order, capture, known=None, request=None, token="PP-LONG-TOKEN-1234", preview=None):
    claims, notes = [], []
    captured = []
    monkeypatch.setattr(ps, "capture_paypal_order", lambda t: captured.append(t) or capture)
    monkeypatch.setattr(ps, "paypal_order_preview", lambda t: preview if preview is not None else {
        "status": "APPROVED", "amount": capture.get("amount", 0), "currency": capture.get("currency", ""),
        "reference": capture.get("reference", ""), "internal_order_id": capture.get("internal_order_id"),
        "captured_amount": 0.0, "captured_currency": "", "error": ""})
    monkeypatch.setattr(ol, "get_order", lambda oid: order)
    monkeypatch.setattr(cr.crud, "get_order_by_paypal_id", lambda t: known)

    def claim(oid, bid, data):
        claims.append((oid, bid, data))
        return True
    monkeypatch.setattr(cr.crud, "claim_order_paid", claim)

    async def notify(o, m):
        notes.append(m)
    monkeypatch.setattr(cr, "_notify_customer_payment", notify)
    out = _run(cr.paypal_success(request=request, token=token, PayerID="X", reference=""))
    _success.captured = captured
    return out, claims, notes


def _cap(amount=10.0, currency="USD", internal=9):
    return {"paid": True, "reference": "ORDER-9", "internal_order_id": internal,
            "amount": amount, "currency": currency}


def _ord(**kw):
    o = {"id": 9, "business_id": 1, "total_price": 10.0, "payment_status": "pending", "currency": "USD"}
    o.update(kw)
    return o


def test_success_accepts_a_matching_capture(monkeypatch):
    out, claims, notes = _success(monkeypatch, order=_ord(), capture=_cap())
    assert out["status"] == "ok" and len(claims) == 1 and len(notes) == 1
    assert "USD 10.00" in notes[0]


def test_success_works_for_a_non_usd_business(monkeypatch):
    out, claims, notes = _success(monkeypatch, order=_ord(currency="EUR", total_price=25.0),
                                  capture=_cap(25.0, "EUR"))
    assert out["status"] == "ok" and "EUR 25.00" in notes[0]


@pytest.mark.parametrize("capture", [_cap(10.0, "EUR"), _cap(10.0, ""), _cap(9.5), _cap(10.5)])
def test_success_rejects_wrong_currency_or_amount_and_marks_nothing_paid(monkeypatch, capture):
    out, claims, notes = _success(monkeypatch, order=_ord(), capture=capture)
    assert out["status"] == "error" and claims == [] and notes == []


def test_success_rejects_a_paypal_order_stored_on_a_different_order(monkeypatch):
    out, claims, _ = _success(monkeypatch, order=_ord(), capture=_cap(),
                              known={"id": 777, "payment_status": "pending"})
    assert out["status"] == "error" and claims == []


def test_success_rejects_when_the_order_has_a_different_stored_paypal_id(monkeypatch):
    out, claims, _ = _success(monkeypatch, order=_ord(paypal_order_id="PP-OTHER"), capture=_cap())
    assert out["status"] == "error" and claims == []


def test_success_for_an_already_paid_order_does_not_capture_again(monkeypatch):
    captured = []
    monkeypatch.setattr(ps, "capture_paypal_order", lambda t: captured.append(t) or _cap())
    monkeypatch.setattr(cr.crud, "get_order_by_paypal_id",
                        lambda t: {"id": 9, "payment_status": "paid", "payment_reference": "ORDER-9"})
    out = _run(cr.paypal_success(request=None, token="PP-1", PayerID="", reference=""))
    assert out["status"] == "ok" and out["paid"] is True and captured == []


def test_success_never_logs_the_full_paypal_token(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _success(monkeypatch, order=_ord(), capture=_cap(), token="SECRET-PAYPAL-TOKEN-9876")
    assert "SECRET-PAYPAL-TOKEN-9876" not in caplog.text


def test_success_is_rate_limited_per_address(monkeypatch):
    cr._PP_RETURN_HITS.clear()
    req = types.SimpleNamespace(headers={"x-forwarded-for": "1.2.3.4"}, client=None)
    results = [cr._paypal_return_rate_ok(req) for _ in range(cr._PP_RETURN_LIMIT + 3)]
    assert results[:cr._PP_RETURN_LIMIT] == [True] * cr._PP_RETURN_LIMIT
    assert results[cr._PP_RETURN_LIMIT:] == [False] * 3
    other = types.SimpleNamespace(headers={"x-forwarded-for": "5.6.7.8"}, client=None)
    assert cr._paypal_return_rate_ok(other) is True
    cr._PP_RETURN_HITS.clear()


def test_rate_limited_request_does_not_capture(monkeypatch):
    cr._PP_RETURN_HITS.clear()
    req = types.SimpleNamespace(headers={"x-forwarded-for": "9.9.9.9"}, client=None)
    cr._PP_RETURN_HITS["9.9.9.9"] = [__import__("time").monotonic()] * cr._PP_RETURN_LIMIT
    captured = []
    monkeypatch.setattr(ps, "capture_paypal_order", lambda t: captured.append(t) or _cap())
    out = _run(cr.paypal_success(request=req, token="PP-1", PayerID="", reference=""))
    assert out["status"] == "error" and captured == []
    cr._PP_RETURN_HITS.clear()


# ═══════════════════════ PayPal webhook ══════════════════════════════════════
@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "1")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID", raising=False)
    ledger._reset_probe_for_tests()
    db = _DB(notification_log=[], businesses=[])
    db.unique["notification_log"] = ("business_id", "kind", "period_key")
    monkeypatch.setattr(ledger, "supabase", db)
    state = {"order": _ord(), "claims": [], "payments": [], "notes": [], "statuses": []}
    monkeypatch.setattr(cr.crud, "get_order_by_paypal_id", lambda t: state["order"])
    monkeypatch.setattr(ol, "get_order", lambda oid: state["order"])
    monkeypatch.setattr(cr.crud, "claim_order_paid", lambda *a: state["claims"].append(a) or True)
    monkeypatch.setattr(cr.crud, "update_order_payment",
                        lambda *a, **k: state["payments"].append(a) or {"id": a[0]})   # a real update returns the row
    monkeypatch.setattr(ol, "update_order_status_supabase", lambda *a, **k: state["statuses"].append(a) or {})

    async def notify(o, m):
        state["notes"].append(m)
    monkeypatch.setattr(cr, "_notify_customer_payment", notify)
    app = FastAPI()
    app.include_router(cr.router)
    state["db"] = db
    return TestClient(app), state


def _event(kind="PAYMENT.CAPTURE.COMPLETED", eid="EV-1", amount="10.00", currency="USD", pp="PP-1"):
    return {"id": eid, "event_type": kind, "resource": {
        "amount": {"value": amount, "currency_code": currency},
        "supplementary_data": {"related_ids": {"order_id": pp}}, "custom_id": "9"}}


def test_webhook_accepts_a_matching_capture_and_ignores_a_replay_of_the_event(hook):
    c, st = hook
    assert c.post("/payments/paypal/webhook", json=_event()).json()["paid"] is True
    again = c.post("/payments/paypal/webhook", json=_event()).json()
    assert again["detail"] == "duplicate_event"
    assert len(st["claims"]) == 1 and len(st["notes"]) == 1


def test_webhook_rejects_wrong_currency(hook):
    c, st = hook
    out = c.post("/payments/paypal/webhook", json=_event(currency="EUR")).json()
    assert out["status"] == "error" and st["claims"] == []


def test_webhook_accepts_a_non_usd_order_in_its_own_currency(hook):
    c, st = hook
    st["order"] = _ord(currency="PLN", total_price=40.0)
    assert c.post("/payments/paypal/webhook", json=_event(amount="40.00", currency="PLN")).json()["paid"] is True


def test_webhook_rejects_a_wrong_amount(hook):
    c, st = hook
    assert c.post("/payments/paypal/webhook", json=_event(amount="9.00")).status_code == 400
    assert st["claims"] == []


def test_webhook_rejects_a_paypal_order_that_does_not_belong_to_the_order(hook):
    c, st = hook
    st["order"] = _ord(paypal_order_id="PP-OTHER")
    out = c.post("/payments/paypal/webhook", json=_event(pp="PP-1")).json()
    assert out["status"] == "error" and st["claims"] == []


def test_webhook_gives_the_event_claim_back_when_the_payment_claim_fails(hook, monkeypatch):
    c, st = hook

    def boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(cr.crud, "claim_order_paid", boom)
    assert c.post("/payments/paypal/webhook", json=_event()).status_code == 503
    assert st["db"].tables["notification_log"] == []                  # released: PayPal's retry can succeed
    monkeypatch.setattr(cr.crud, "claim_order_paid", lambda *a: st["claims"].append(a) or True)
    assert c.post("/payments/paypal/webhook", json=_event()).json()["paid"] is True


def test_webhook_without_the_ledger_table_still_relies_on_the_atomic_paid_claim(hook, monkeypatch):
    c, st = hook
    ledger._reset_probe_for_tests()

    def missing(q):
        raise Exception({"code": "42P01", "message": 'relation "public.notification_log" does not exist'})
    st["db"].hooks[("notification_log", "insert")] = missing
    assert c.post("/payments/paypal/webhook", json=_event()).json()["paid"] is True
    assert st["db"].tables["businesses"] == []                        # nothing written to a business row


# -- refunds -------------------------------------------------------------------
def test_full_refund_event_marks_the_order_refunded_without_touching_stock(hook):
    c, st = hook
    st["order"] = _ord(payment_status="paid")
    out = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R", "10.00")).json()
    assert out["refunded"] is True
    assert st["payments"] == [(9, 1, {"payment_status": "refunded"})]
    assert st["statuses"] and st["statuses"][0][1] == "refunded"


def test_partial_refund_leaves_the_order_alone(hook):
    c, st = hook
    st["order"] = _ord(payment_status="paid")
    out = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R2", "3.00")).json()
    assert out["detail"] == "partial_refund_logged" and st["payments"] == []


def test_refund_event_replay_and_unknown_order_are_harmless(hook, monkeypatch):
    c, st = hook
    st["order"] = _ord(payment_status="paid")
    c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R3", "10.00"))
    again = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R3", "10.00")).json()
    assert again["detail"] == "duplicate_event" and len(st["payments"]) == 1
    monkeypatch.setattr(cr.crud, "get_order_by_paypal_id", lambda t: None)
    monkeypatch.setattr(ol, "get_order", lambda oid: None)
    assert c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REVERSED", "EV-R4")).json()["detail"] == "order_not_found"


# ═══════════════════════ customer says "paid" (PayPal) ═══════════════════════
def _paid_message(monkeypatch, details, order=None):
    import services._ai_payments as ap
    claims = []
    monkeypatch.setattr(ps, "get_paypal_order_details", lambda i: details)
    monkeypatch.setattr(ol, "get_order", lambda oid: order or _ord())
    monkeypatch.setattr(ap.crud, "claim_order_paid", lambda *a: claims.append(a) or True)
    import services._ai_state as st
    monkeypatch.setattr(st, "_read_state_data", lambda p, b: {"paypal_order_id": "PP-1"})
    monkeypatch.setattr(st, "_reset_state", lambda p, b: None)
    out = ap._handle_paypal_paid_message("2637", 1, "Shop", 9, "ORDER-9")
    return out, claims


def _details(**kw):
    d = {"paid": True, "amount": 10.0, "currency": "USD", "internal_order_id": 9}
    d.update(kw)
    return d


def test_customer_paid_message_confirms_only_a_verified_payment(monkeypatch):
    out, claims = _paid_message(monkeypatch, _details())
    assert "Confirmed" in out and len(claims) == 1 and "USD 10.00" in out


@pytest.mark.parametrize("bad", [
    {"internal_order_id": 777}, {"currency": "EUR"}, {"amount": 1.0}, {"amount": 99.0},
])
def test_customer_paid_message_does_not_confirm_a_mismatching_payment(monkeypatch, bad):
    out, claims = _paid_message(monkeypatch, _details(**bad))
    assert "verifying" in out.lower() and claims == []


def test_customer_paid_message_refuses_another_businesses_order(monkeypatch):
    out, claims = _paid_message(monkeypatch, _details(), order=_ord(business_id=2))
    assert "verifying" in out.lower() and claims == []


# ═══════════════════════ Stripe storefront fulfilment ════════════════════════
@pytest.fixture
def shop(monkeypatch):
    import billing.stripe_service as ss
    import services._ai_payments as ap
    import services.email_service as es
    import core.db as coredb
    ledger._reset_probe_for_tests()
    db = _DB(notification_log=[], businesses=[{"id": 4, "name": "Shop", "currency_symbol": "$"}], orders=[])
    db.unique["notification_log"] = ("business_id", "kind", "period_key")
    monkeypatch.setattr(ledger, "supabase", db)
    monkeypatch.setattr(coredb, "supabase", db)
    st = {"orders": [], "invoices": [], "biz_notes": [], "emails": [], "create_error": None}

    def create_order(business_id, customer_phone, cart, payment_method):
        if st["create_error"]:
            raise st["create_error"]
        o = {"id": 100 + len(st["orders"]), "business_id": business_id, "total_price": 10.0}
        st["orders"].append(o)
        return dict(o)
    monkeypatch.setattr(ol, "create_order_supabase", create_order)
    monkeypatch.setattr(ap, "_send_pdf_invoice", lambda o, p, b: st["invoices"].append(o["id"]))
    monkeypatch.setattr(ap, "_notify_business_new_order", lambda *a, **k: st["biz_notes"].append(a))
    monkeypatch.setattr(es, "send_purchase_receipt", lambda **k: st["emails"].append(k) or True)
    st["ss"], st["db"] = ss, db
    return st


def _session(sid="cs_1", pay="paid", bid=4, phone="263700", email="c@example.com"):
    return {"id": sid, "mode": "payment", "payment_status": pay, "currency": "usd", "amount_total": 1000,
            "metadata": {"wazibot_business_id": str(bid)},
            "customer_details": {"email": email, "phone": phone, "name": "Cust"}}


def test_an_unpaid_session_creates_no_order_and_sends_nothing(shop):
    shop["ss"]._on_checkout_completed(_session(pay="unpaid"))
    assert shop["orders"] == [] and shop["emails"] == [] and shop["biz_notes"] == []
    assert shop["db"].tables["notification_log"] == []                # and does not use up the session


def test_the_paid_follow_up_event_then_creates_exactly_one_order(shop):
    shop["ss"]._on_checkout_completed(_session(pay="unpaid"))                 # checkout.session.completed
    shop["ss"]._on_checkout_completed(_session(pay="paid"))                   # async_payment_succeeded
    assert len(shop["orders"]) == 1 and len(shop["emails"]) == 1


def test_the_same_session_delivered_again_under_a_new_event_id_makes_no_second_order(shop):
    shop["ss"]._on_checkout_completed(_session())
    shop["ss"]._on_checkout_completed(_session())
    shop["ss"]._on_checkout_completed(_session())
    assert len(shop["orders"]) == 1 and len(shop["emails"]) == 1 and len(shop["invoices"]) == 1


def test_a_different_session_is_a_different_purchase(shop):
    shop["ss"]._on_checkout_completed(_session("cs_1"))
    shop["ss"]._on_checkout_completed(_session("cs_2"))
    assert len(shop["orders"]) == 2


def test_no_payment_required_counts_as_paid(shop):
    shop["ss"]._on_checkout_completed(_session(pay="no_payment_required"))
    assert len(shop["orders"]) == 1


def test_a_transient_failure_gives_the_session_back_so_the_retry_creates_the_order_once(shop):
    shop["create_error"] = RuntimeError("database unavailable")
    with pytest.raises(shop["ss"]._FulfilmentRetry):
        shop["ss"]._on_checkout_completed(_session())
    assert shop["db"].tables["notification_log"] == []                # claim released
    shop["create_error"] = None
    shop["ss"]._on_checkout_completed(_session())                     # Stripe's redelivery
    assert len(shop["orders"]) == 1


def test_a_permanent_failure_is_not_retried_but_the_business_is_still_told(shop, caplog):
    caplog.set_level(logging.ERROR)
    shop["create_error"] = ValueError("Insufficient stock for 'Tea'")
    shop["ss"]._on_checkout_completed(_session())                     # must NOT raise: retrying cannot help
    assert shop["orders"] == [] and len(shop["biz_notes"]) == 1
    assert "PAID BUT NO ORDER CREATED" in caplog.text
    shop["ss"]._on_checkout_completed(_session())                     # a redelivery does not repeat the alert
    assert len(shop["biz_notes"]) == 1


def test_a_session_without_a_phone_still_gets_the_email_receipt_once(shop):
    shop["ss"]._on_checkout_completed(_session(phone=""))
    shop["ss"]._on_checkout_completed(_session(phone=""))
    assert shop["orders"] == [] and len(shop["emails"]) == 1


def test_subscription_checkouts_are_untouched_by_the_storefront_gate(shop, monkeypatch):
    # a subscription-mode session is not routed to the storefront handler at all
    called = []
    monkeypatch.setattr(shop["ss"], "_on_product_purchase_completed", lambda o: called.append(o))
    shop["ss"]._on_checkout_completed({"id": "cs_sub", "mode": "subscription", "payment_status": "unpaid",
                                       "metadata": {"wazibot_business_id": "4"}})
    assert called == []


def test_fulfilment_without_the_ledger_table_still_works(shop):
    ledger._reset_probe_for_tests()

    def missing(q):
        raise Exception({"code": "PGRST205", "message": "Could not find the table 'public.notification_log'"})
    shop["db"].hooks[("notification_log", "insert")] = missing
    shop["ss"]._on_checkout_completed(_session())
    assert len(shop["orders"]) == 1
    assert shop["db"].tables["businesses"] == [{"id": 4, "name": "Shop", "currency_symbol": "$"}]   # untouched


# ═══════════════════════ impersonation on billing mutations ══════════════════
def test_impersonation_tokens_are_refused_on_money_moving_billing_routes():
    import routes_saas.billing_routes as br
    with pytest.raises(HTTPException) as e:
        br.require_own_business({"business_id": 4, "role": "business", "impersonated_by": "admin"})
    assert e.value.status_code == 403
    ok = {"business_id": 4, "role": "business"}
    assert br.require_own_business(ok) is ok


def test_every_mutating_billing_route_uses_the_impersonation_guard_and_reads_stay_open():
    import routes_saas.billing_routes as br
    guarded = {"/billing/checkout", "/billing/cancel", "/billing/resume", "/billing/change-plan",
               "/billing/portal", "/billing/connect", "/billing/connect/dashboard"}
    open_reads = {"/billing/status", "/billing/invoices", "/billing/analytics", "/billing/connect/status"}
    seen = {}
    for r in br.router.routes:
        deps = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
        seen[r.path] = deps
    for path in guarded:
        assert "require_own_business" in seen[path], path
    for path in open_reads:
        assert "require_own_business" not in seen[path], path
