"""Phase 3 review follow-ups: validate-before-capture, strict currency (never guess USD), refunded
orders stay refunded, refund-event handling, Stripe retry/dispatcher behaviour, contract tests."""
import logging
import types

import pytest

import crud
import crud.notification_ledger as ledger
import routes.chat_routes as cr
import services.payment_service as ps
import workflows.order_lifecycle as ol

from test_phase3_payments import (   # noqa: F401  (fixtures + helpers)
    hook, shop, _run, _success, _cap, _ord, _event, _session, _create, _ok_post, _DB,
)


def _preview(**kw):
    p = {"status": "APPROVED", "amount": 10.0, "currency": "USD", "reference": "ORDER-9",
         "internal_order_id": 9, "captured_status": "", "captured_amount": 0.0, "captured_currency": "",
         "error": ""}
    p.update(kw)
    return p


# ═══════════════ validate BEFORE capturing ═══════════════════════════════════
@pytest.mark.parametrize("preview", [
    _preview(currency="EUR"), _preview(amount=9.0), _preview(amount=10.5),
])
def test_a_mismatching_paypal_order_is_never_captured(monkeypatch, preview):
    out, claims, notes = _success(monkeypatch, order=_ord(), capture=_cap(), preview=preview)
    assert out["status"] == "error" and claims == [] and notes == []
    assert _success.captured == []                                  # no money moved


def test_a_paypal_order_whose_custom_id_points_at_a_different_order_is_never_captured(monkeypatch):
    stored = _ord(payment_status="pending")                       # our record says this PayPal order is for order 9
    out, claims, _ = _success(monkeypatch, order=stored, capture=_cap(), known=stored,
                              preview=_preview(internal_order_id=777))
    assert out["status"] == "error" and claims == [] and _success.captured == []


def test_an_unapproved_paypal_order_is_not_captured(monkeypatch):
    out, claims, _ = _success(monkeypatch, order=_ord(), capture=_cap(), preview=_preview(status="CREATED"))
    assert out["status"] == "error" and claims == [] and _success.captured == []


def test_an_already_completed_paypal_order_is_confirmed_without_capturing_again(monkeypatch):
    out, claims, notes = _success(monkeypatch, order=_ord(), capture=_cap(),
                                  preview=_preview(status="COMPLETED", captured_status="COMPLETED",
                                                   captured_amount=10.0, captured_currency="USD"))
    assert out["status"] == "ok" and len(claims) == 1 and _success.captured == []


def test_a_completed_order_captured_in_the_wrong_amount_is_flagged_not_confirmed(monkeypatch):
    out, claims, _ = _success(monkeypatch, order=_ord(), capture=_cap(),
                              preview=_preview(status="COMPLETED", captured_status="COMPLETED",
                                               captured_amount=3.0, captured_currency="USD"))
    assert out["status"] == "error" and claims == []


def test_a_paypal_error_while_looking_up_the_order_stops_before_any_capture(monkeypatch):
    out, claims, _ = _success(monkeypatch, order=_ord(), capture=_cap(), preview=_preview(error="HTTPError"))
    assert out["status"] == "error" and claims == [] and _success.captured == []


def test_a_database_error_on_the_stored_id_lookup_stops_before_any_capture(monkeypatch):
    captured = []
    monkeypatch.setattr(ps, "capture_paypal_order", lambda t: captured.append(t) or _cap())
    monkeypatch.setattr(ps, "paypal_order_preview", lambda t: _preview())

    def boom(t):
        raise RuntimeError("db down")
    monkeypatch.setattr(cr.crud, "get_order_by_paypal_id", boom)
    out = _run(cr.paypal_success(request=None, token="PP-1", PayerID="", reference=""))
    assert out["status"] == "error" and captured == []


def test_the_order_is_resolved_from_paypals_own_data_when_nothing_is_stored(monkeypatch):
    seen = []
    order = _ord()
    monkeypatch.setattr(ps, "paypal_order_preview", lambda t: _preview())
    monkeypatch.setattr(ps, "capture_paypal_order", lambda t: _cap())
    monkeypatch.setattr(cr.crud, "get_order_by_paypal_id", lambda t: None)
    monkeypatch.setattr(ol, "get_order", lambda oid: seen.append(oid) or order)
    monkeypatch.setattr(cr.crud, "claim_order_paid", lambda *a: True)

    async def notify(o, m):
        return None
    monkeypatch.setattr(cr, "_notify_customer_payment", notify)
    out = _run(cr.paypal_success(request=None, token="PP-1", PayerID="", reference="ORDER-4444"))
    assert out["status"] == "ok" and seen == [9]                    # PayPal's custom_id, NOT the query parameter


def test_a_refunded_order_cannot_be_paid_through_the_return_page(monkeypatch):
    out, claims, _ = _success(monkeypatch, order=_ord(payment_status="refunded"), capture=_cap())
    assert out["status"] == "error" and claims == [] and _success.captured == []


def test_a_zero_total_order_is_never_charged_or_confirmed(monkeypatch):
    out, claims, _ = _success(monkeypatch, order=_ord(total_price=0.0), capture=_cap(0.0),
                              preview=_preview(amount=0.0))
    assert out["status"] == "error" and claims == [] and _success.captured == []


# ═══════════════ the currency is never guessed ═══════════════════════════════
def _failing_business_lookup(monkeypatch):
    import core.db as coredb

    class Boom:
        def table(self, *_):
            raise RuntimeError("db down")
    monkeypatch.setattr(coredb, "supabase", Boom())


def test_a_failed_currency_lookup_raises_instead_of_defaulting_to_usd(monkeypatch):
    _failing_business_lookup(monkeypatch)
    with pytest.raises(ps.CurrencyLookupError):
        ps.order_currency_code({"business_id": 5, "currency_symbol": "$"})
    assert ps.order_currency_code({"currency": "PLN"}) == "PLN"        # an explicit currency needs no lookup


def test_creating_a_paypal_order_with_an_unreadable_currency_gives_manual_instructions(monkeypatch):
    _failing_business_lookup(monkeypatch)
    called = []

    def post(*a, **k):
        called.append(1)
        raise AssertionError("must not call PayPal")
    out = _create(monkeypatch, {"id": 7, "business_id": 5, "total_price": 500, "paypal_email": "o@x.com"}, post)
    assert called == [] and out["fallback_reason"] == "currency_lookup_failed" and not out.get("error")


def test_return_page_with_an_unreadable_currency_retries_and_never_captures(monkeypatch):
    _failing_business_lookup(monkeypatch)
    out, claims, _ = _success(monkeypatch, order={"id": 9, "business_id": 5, "total_price": 10.0,
                                                  "payment_status": "pending", "currency_symbol": "$"},
                              capture=_cap())
    assert out["status"] == "error" and claims == [] and _success.captured == []


def test_webhook_with_an_unreadable_currency_asks_paypal_to_retry(hook, monkeypatch):
    c, st = hook
    st["order"] = {"id": 9, "business_id": 5, "total_price": 10.0, "payment_status": "pending",
                   "currency_symbol": "$"}
    _failing_business_lookup(monkeypatch)
    assert c.post("/payments/paypal/webhook", json=_event()).status_code == 503
    assert st["claims"] == []


def test_the_charged_currency_is_remembered_on_the_order_when_a_column_exists(monkeypatch):
    seen = []
    monkeypatch.setattr(ol, "_get_orders_columns", lambda: {"id", "currency", "payment_status"})
    import crud.orders as co
    monkeypatch.setattr(co, "supabase", types.SimpleNamespace(table=lambda t: _Rec(seen)))
    co.update_order_payment(9, 1, {"payment_status": "awaiting_payment", "currency": "EUR", "bogus": 1})
    assert seen and seen[0] == {"payment_status": "awaiting_payment", "currency": "EUR"}


class _Rec:
    def __init__(self, seen):
        self.seen = seen

    def update(self, row):
        self.seen.append(row)
        return self

    def eq(self, *a):
        return self

    def execute(self):
        return types.SimpleNamespace(data=[{"id": 9}])


# ═══════════════ a refunded order stays refunded ═════════════════════════════
def test_claim_order_paid_filter_excludes_refunded_orders():
    import crud.orders as co
    expr = {}

    class Q:
        def update(self, d): return self
        def eq(self, *a): return self
        def or_(self, e):
            expr["or"] = e
            return self
        def execute(self): return types.SimpleNamespace(data=[])
    import unittest.mock as m
    with m.patch.object(co, "supabase", types.SimpleNamespace(table=lambda t: Q())), \
            m.patch("workflows.order_lifecycle._has_col", lambda c: True):
        co.claim_order_paid(1, 1, {})
    assert "payment_status.neq.refunded" in expr["or"] and "payment_status.neq.paid" in expr["or"]
    assert "payment_status.is.null" in expr["or"]


def test_a_customer_paid_message_cannot_re_confirm_a_refunded_order(monkeypatch):
    import services._ai_payments as ap
    import services._ai_state as stt
    claims = []
    monkeypatch.setattr(ps, "get_paypal_order_details", lambda i: {
        "paid": True, "amount": 10.0, "currency": "USD", "internal_order_id": 9})
    monkeypatch.setattr(ol, "get_order", lambda oid: _ord(payment_status="refunded"))
    monkeypatch.setattr(ap.crud, "claim_order_paid", lambda *a: claims.append(a) or True)
    monkeypatch.setattr(stt, "_read_state_data", lambda p, b: {"paypal_order_id": "PP-1"})
    monkeypatch.setattr(stt, "_reset_state", lambda p, b: None)
    out = ap._handle_paypal_paid_message("2637", 1, "Shop", 9, "ORDER-9")
    assert "verifying" in out.lower() and claims == []


def test_a_late_completed_event_after_a_refund_does_not_re_pay_the_order(hook, monkeypatch):
    c, st = hook
    st["order"] = _ord(payment_status="refunded")
    import crud.orders as co
    # the real atomic claim refuses a refunded order; model that
    monkeypatch.setattr(cr.crud, "claim_order_paid", lambda *a: False)
    out = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.COMPLETED", "EV-LATE")).json()
    assert out.get("detail") in ("already_paid", "duplicate_event") or out.get("paid") is not True
    assert st["notes"] == []


# ═══════════════ refund events ═══════════════════════════════════════════════
def _paid(st, **kw):
    st["order"] = _ord(payment_status="paid", **kw)


def test_full_refund_marks_the_order_refunded_without_touching_stock(hook):
    c, st = hook
    _paid(st)
    out = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R", "10.00")).json()
    assert out["refunded"] is True
    assert st["payments"] == [(9, 1, {"payment_status": "refunded"})]
    assert st["statuses"] and st["statuses"][0][1] == "refunded"


def test_partial_refund_leaves_the_order_alone(hook):
    c, st = hook
    _paid(st)
    out = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R2", "3.00")).json()
    assert out["detail"] == "partial_refund_logged" and st["payments"] == []


def test_refund_of_an_order_that_was_never_paid_changes_nothing(hook):
    c, st = hook
    st["order"] = _ord(payment_status="awaiting_payment")
    out = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R3", "10.00")).json()
    assert out["detail"] == "order_not_paid" and st["payments"] == []


def test_refund_for_a_paypal_order_that_is_not_the_stored_one_is_rejected(hook):
    c, st = hook
    _paid(st, paypal_order_id="PP-OTHER")
    out = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R4", "10.00", pp="PP-1")).json()
    assert out["status"] == "error" and st["payments"] == []


def test_refund_replay_after_success_is_a_no_op(hook):
    c, st = hook
    _paid(st)
    c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R5", "10.00"))
    st["order"] = _ord(payment_status="refunded")
    again = c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R5", "10.00")).json()
    assert again["detail"] == "already_refunded" and len(st["payments"]) == 1


def test_refund_event_claim_is_given_back_when_the_update_fails_so_the_retry_finishes(hook, monkeypatch):
    c, st = hook
    _paid(st)
    state = {"fail": True}

    def update(*a, **k):
        if state["fail"]:
            raise RuntimeError("db down")
        st["payments"].append(a)
        return {"id": a[0]}
    monkeypatch.setattr(cr.crud, "update_order_payment", update)
    assert c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R6", "10.00")).status_code == 503
    assert st["db"].tables["notification_log"] == []                  # released: PayPal's retry is not dropped
    state["fail"] = False
    assert c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REFUNDED", "EV-R6", "10.00")).json()["refunded"] is True


def test_refund_event_without_a_usable_amount_is_ignored(hook):
    c, st = hook
    _paid(st)
    ev = _event("PAYMENT.CAPTURE.REFUNDED", "EV-R7")
    ev["resource"].pop("amount")
    assert c.post("/payments/paypal/webhook", json=ev).json()["detail"] == "refund_amount_unusable"
    assert st["payments"] == []


def test_refund_order_link_fallback_only_trusts_real_order_links(hook, monkeypatch):
    c, st = hook
    _paid(st)
    seen = []
    monkeypatch.setattr(cr.crud, "get_order_by_paypal_id", lambda t: seen.append(t) or st["order"])
    ev = _event("PAYMENT.CAPTURE.REFUNDED", "EV-R8", "10.00")
    ev["resource"]["supplementary_data"] = {}
    ev["resource"]["links"] = [{"href": "https://api.paypal.com/v2/payments/captures/CAP-1", "rel": "up"},
                               {"href": "https://api.paypal.com/v2/checkout/orders/PP-REAL", "rel": "up"}]
    c.post("/payments/paypal/webhook", json=ev)
    assert seen == ["PP-REAL"]


def test_refund_for_an_unknown_order_is_harmless(hook, monkeypatch):
    c, st = hook
    monkeypatch.setattr(cr.crud, "get_order_by_paypal_id", lambda t: None)
    monkeypatch.setattr(ol, "get_order", lambda oid: None)
    assert c.post("/payments/paypal/webhook", json=_event("PAYMENT.CAPTURE.REVERSED", "EV-R9")).json()["detail"] == "order_not_found"


# ═══════════════ rate limiting uses the proxy-added address ══════════════════
def test_a_spoofed_leftmost_forwarded_address_does_not_bypass_the_limit():
    cr._PP_RETURN_HITS.clear()
    results = []
    for i in range(cr._PP_RETURN_LIMIT + 3):
        req = types.SimpleNamespace(headers={"x-forwarded-for": f"10.0.0.{i}, 203.0.113.9"}, client=None)
        results.append(cr._paypal_return_rate_ok(req))
    assert results[-1] is False                                       # same real client behind rotating fakes
    cr._PP_RETURN_HITS.clear()


# ═══════════════ contracts that keep the verifier honest ═════════════════════
def test_paypal_order_preview_and_details_return_the_fields_the_verifiers_rely_on(monkeypatch):
    monkeypatch.setattr(ps, "_paypal_token", lambda: "tok")
    body = {"status": "COMPLETED", "purchase_units": [{
        "reference_id": "ORDER-9", "custom_id": "9", "amount": {"currency_code": "EUR", "value": "12.50"},
        "payments": {"captures": [{"amount": {"currency_code": "EUR", "value": "12.50"}}]}}]}

    class R:
        def raise_for_status(self): pass
        def json(self): return body
    monkeypatch.setattr(ps.http, "get", lambda *a, **k: R())
    p = ps.paypal_order_preview("PP-1")
    assert (p["status"], p["amount"], p["currency"], p["internal_order_id"], p["reference"]) == \
        ("COMPLETED", 12.5, "EUR", 9, "ORDER-9")
    assert (p["captured_amount"], p["captured_currency"], p["error"]) == (12.5, "EUR", "")
    d = ps.get_paypal_order_details("PP-1")
    assert {"paid", "amount", "currency", "internal_order_id"} <= set(d)
    assert d["currency"] == "EUR" and d["internal_order_id"] == 9 and d["paid"] is True


def test_paypal_order_preview_reports_errors_instead_of_raising(monkeypatch):
    monkeypatch.setattr(ps, "_paypal_token", lambda: "tok")

    def boom(*a, **k):
        raise RuntimeError("network")
    monkeypatch.setattr(ps.http, "get", boom)
    p = ps.paypal_order_preview("PP-1")
    assert p["error"] and p["status"] == "unknown" and p["amount"] == 0.0


@pytest.mark.parametrize("captured,expected,ok", [
    (10.00, 10.00, True), (10.01, 10.00, True), (10.02, 10.00, False), (9.99, 10.00, True),
    (1500.0, 1500.0, True), (1499.0, 1500.0, False),
])
def test_paypal_amount_match_boundaries(captured, expected, ok):
    assert ps.paypal_amount_matches(captured, expected) is ok


# ═══════════════ the notification ledger in table_only mode ══════════════════
def test_table_only_claim_never_touches_a_business_row_when_the_table_is_missing(monkeypatch, caplog):
    ledger._reset_probe_for_tests()
    db = _DB(notification_log=[], businesses=[{"id": 4, "features_json": {"x": 1}}])

    def missing(q):
        raise Exception({"code": "42P01", "message": 'relation "public.notification_log" does not exist'})
    db.hooks[("notification_log", "insert")] = missing
    monkeypatch.setattr(ledger, "supabase", db)
    caplog.set_level(logging.WARNING)
    assert ledger.claim(4, "k", "p", table_only=True) is True
    assert ledger.claim(4, "k", "p", table_only=True) is True          # no recording at all
    assert db.tables["businesses"] == [{"id": 4, "features_json": {"x": 1}}]
    assert "notification_log table not found" in caplog.text


# ═══════════════ Stripe: retries and the real webhook entry point ════════════
def _stripe_env(monkeypatch, shop, event):
    ss = shop["ss"]
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    fake = types.SimpleNamespace(
        Webhook=types.SimpleNamespace(construct_event=lambda p, s, sec: event),
        checkout=types.SimpleNamespace(Session=types.SimpleNamespace(
            list_line_items=lambda *a, **k: {"data": []})))
    monkeypatch.setattr(ss, "_stripe", lambda: fake)
    monkeypatch.setattr(ss, "_key_mode", lambda: "test")
    import crud.stripe_webhook_events as sw
    marks = {"failed": [], "processed": []}
    monkeypatch.setattr(sw, "already_processed", lambda i: False)
    monkeypatch.setattr(sw, "claim_event", lambda i, t: "claimed")
    monkeypatch.setattr(sw, "mark_failed", lambda i, t, m: marks["failed"].append(i))
    monkeypatch.setattr(sw, "mark_processed", lambda i, t: marks["processed"].append(i))
    return marks


def _ev(eid, etype, session):
    return {"id": eid, "type": etype, "livemode": False, "data": {"object": session}}


def test_a_transient_fulfilment_failure_makes_the_webhook_answer_500_and_the_retry_succeeds(monkeypatch, shop):
    ss = shop["ss"]
    ev = _ev("evt_1", "checkout.session.completed", _session())
    marks = _stripe_env(monkeypatch, shop, ev)
    shop["create_error"] = RuntimeError("database unavailable")
    res = ss.handle_stripe_webhook(b"{}", "sig")
    assert res["status"] == 500 and marks["failed"] == ["evt_1"] and marks["processed"] == []
    shop["create_error"] = None
    res2 = ss.handle_stripe_webhook(b"{}", "sig")                      # Stripe redelivers
    assert res2.get("ok") is True and len(shop["orders"]) == 1


def test_completed_unpaid_then_async_succeeded_through_the_webhook_creates_one_order(monkeypatch, shop):
    ss = shop["ss"]
    marks = _stripe_env(monkeypatch, shop, _ev("evt_a", "checkout.session.completed", _session(pay="unpaid")))
    assert ss.handle_stripe_webhook(b"{}", "sig").get("ok") is True
    assert shop["orders"] == []
    _stripe_env(monkeypatch, shop, _ev("evt_b", "checkout.session.async_payment_succeeded", _session(pay="paid")))
    assert ss.handle_stripe_webhook(b"{}", "sig").get("ok") is True
    _stripe_env(monkeypatch, shop, _ev("evt_c", "checkout.session.completed", _session(pay="paid")))
    assert ss.handle_stripe_webhook(b"{}", "sig").get("ok") is True    # a third event for the same session
    assert len(shop["orders"]) == 1 and len(shop["emails"]) == 1


def test_a_failed_line_item_fetch_after_payment_is_retried_not_guessed(monkeypatch, shop):
    ss = shop["ss"]

    class Sess:
        @staticmethod
        def list_line_items(*a, **k):
            raise RuntimeError("stripe unavailable")
    fake = types.SimpleNamespace(checkout=types.SimpleNamespace(Session=Sess))
    monkeypatch.setattr(ss, "_stripe", lambda: fake)
    with pytest.raises(ss._FulfilmentRetry):
        ss._on_checkout_completed(_session())
    assert shop["orders"] == [] and shop["db"].tables["notification_log"] == []     # claim released
    monkeypatch.setattr(ss, "_stripe", lambda: None)                                  # Stripe is back (no client = no fetch)
    ss._on_checkout_completed(_session())
    assert len(shop["orders"]) == 1


def test_a_session_without_a_phone_does_not_crash_and_still_emails(monkeypatch, shop):
    shop["ss"]._on_checkout_completed(_session(phone=""))
    assert shop["orders"] == [] and len(shop["emails"]) == 1


def test_the_business_is_told_when_a_paid_order_could_not_be_created(monkeypatch, shop):
    shop["create_error"] = ValueError("Insufficient stock for 'Tea'")
    shop["ss"]._on_checkout_completed(_session())
    assert len(shop["biz_notes"]) == 1 and shop["biz_notes"][0][1].get("id") is None
