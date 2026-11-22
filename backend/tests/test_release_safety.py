"""Release-safety review of the chat-fix work: fulfilment never creates "paid"; manual payment confirmation trusts the
stored order, not dashboard input; automatic customer updates are OFF by default and fail closed without the ledger;
accepted-by-WhatsApp is not "delivered"; a cancelled booking gives back deducted stock exactly once.
WhatsApp and the database are mocked; nothing is sent."""
import asyncio
import json
import types

import pytest
from fastapi import HTTPException

import crud
import routes.admin_routes as ar
import routes.chat_routes as cr
import services.order_notifications as on
import services.stock_restore as sr
import workflows.order_lifecycle as ol
from crud import notification_ledger as ledger
from test_phase7_job_claims import _DB, db   # noqa: F401  (fake with the notification_log unique constraint)

BIZ_ON = {"id": 1, "name": "Shop", "whatsapp_phone_id": "PID", "features_json": {"order_status_notifications": True}}
BIZ_OFF = {"id": 1, "name": "Shop", "whatsapp_phone_id": "PID"}


def _order(**kw):
    o = {"id": 9, "business_id": 1, "customer_phone": "263771000001", "status": "pending", "payment_status": "pending_cash",
         "fulfillment_method": "pickup", "total_price": 7.53, "currency_symbol": "zł", "currency": "PLN"}
    o.update(kw)
    return o


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ═════════ 1. lifecycle route never marks an order paid ═════════
def test_lifecycle_push_never_marks_the_order_paid(monkeypatch):
    paid_calls = []
    monkeypatch.setattr(ar, "get_order", lambda oid: _order())
    monkeypatch.setattr(ar, "update_order_status_supabase", lambda *a, **k: {})
    monkeypatch.setattr(crud, "update_order_payment", lambda *a, **k: paid_calls.append(a) or {})
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: BIZ_OFF)
    monkeypatch.setattr(ar, "send_whatsapp", lambda *a: {})
    for st in ("preparing", "ready", "out_for_delivery", "delivered", "completed"):
        body = ar.OrderLifecycleUpdate(order_id=9, status=st)
        out = _run(ar.push_lifecycle_update(9, body, user={"business_id": 1, "role": "business"}))
        assert out["ok"] is True
    assert paid_calls == [], "a fulfilment change must never write a payment state"


def test_lifecycle_source_has_no_paid_write():
    import inspect
    src = inspect.getsource(ar.push_lifecycle_update)
    assert '"payment_status": "paid"' not in src and "update_order_payment" not in src


# ═════════ 2. manual_payment_confirm trusts the stored order ═════════
@pytest.fixture
def mc(monkeypatch):
    st = types.SimpleNamespace(msgs=[], claimed=[], order=_order(), refs=[])
    monkeypatch.setattr(ol, "get_order", lambda oid: st.order)
    monkeypatch.setattr(cr.crud, "claim_order_paid", lambda oid, bid, data: st.claimed.append(data) or True)
    monkeypatch.setattr(ol, "update_order_status_supabase", lambda *a, **k: {})
    monkeypatch.setattr(cr.crud, "get_business_by_id", lambda b: {"id": 1, "currency": "PLN", "currency_symbol": "zł"})

    async def notify(o, m):
        st.msgs.append(m)
    monkeypatch.setattr(cr, "_notify_customer_payment", notify)
    return st


def _confirm(body):
    class Req:
        async def json(self):
            return body
    return _run(cr.manual_payment_confirm(Req(), user={"business_id": 1}))


def test_confirmation_without_an_amount_uses_the_stored_total(mc):
    assert _confirm({"order_id": 9})["ok"] is True
    assert "7,53 zł" in mc.msgs[0]


def test_matching_amount_is_accepted_but_message_uses_the_stored_total(mc):
    assert _confirm({"order_id": 9, "amount": 7.53, "currency": "pln"})["ok"] is True
    assert "7,53 zł" in mc.msgs[0]


@pytest.mark.parametrize("amount", [1, 7.0, 100, 7.54])
def test_a_different_amount_is_refused_and_nothing_is_recorded(mc, amount):
    with pytest.raises(HTTPException) as e:
        _confirm({"order_id": 9, "amount": amount})
    assert e.value.status_code == 409 and mc.claimed == [] and mc.msgs == []


def test_a_different_currency_is_refused(mc):
    with pytest.raises(HTTPException) as e:
        _confirm({"order_id": 9, "currency": "USD"})
    assert e.value.status_code == 409 and mc.claimed == []


def test_non_numeric_input_is_a_400_not_a_crash(mc):
    for body in ({"order_id": "abc"}, {"order_id": 9, "amount": "lots"}):
        with pytest.raises(HTTPException) as e:
            _confirm(body)
        assert e.value.status_code == 400


def test_another_business_cannot_confirm_this_order(mc):
    mc.order = _order(business_id=2)
    with pytest.raises(HTTPException) as e:
        _confirm({"order_id": 9})
    assert e.value.status_code == 403 and mc.claimed == []


def test_reference_is_cleaned_and_capped(mc):
    _confirm({"order_id": 9, "reference": "ORDER-9\n\x00" + "x" * 500})
    ref = mc.claimed[0]["payment_reference"]
    assert len(ref) <= 80 and "\n" not in ref and "\x00" not in ref


def test_cancelled_orders_still_cannot_be_confirmed(mc):
    mc.order = _order(status="cancelled")
    with pytest.raises(HTTPException) as e:
        _confirm({"order_id": 9})
    assert e.value.status_code == 409


# ═════════ 3. ledger: duplicates, retries, restarts, missing table ═════════
@pytest.fixture
def notif(monkeypatch, db):
    sent = []
    from services import wa_templates, wa_window
    monkeypatch.setattr(wa_templates, "resolve_credentials", lambda b: ("PID", "TOK"))
    monkeypatch.setattr(wa_window, "closed_phones", lambda bid, phones: set())
    monkeypatch.setattr(on, "order_money", lambda order, business: "7,53 zł")
    st = types.SimpleNamespace(sent=sent, db=db)
    st.sender = lambda pid, tok, phone, text: sent.append(text) or {"messages": [{"id": "w"}]}
    st.go = lambda status="ready", business=BIZ_ON, **k: on.notify_order_status(
        _order(), status, business=business, sender=k.pop("sender", st.sender), **k)
    return st


def test_a_restart_cannot_cause_a_repeat(notif):
    assert notif.go()["outcome"] == "sent"
    ledger._reset_probe_for_tests()                 # a deploy wipes every in-process memory; the table row remains
    assert notif.go()["outcome"] == "duplicate" and len(notif.sent) == 1


def test_retry_after_a_failed_send_is_allowed_once(notif):
    r = notif.go(sender=lambda *a: {"error": {"code": 1}})
    assert r["outcome"] == "failed" and notif.db.claims("order_status") == []
    assert notif.go()["outcome"] == "sent" and notif.go()["outcome"] == "duplicate"
    assert len(notif.sent) == 1


def test_missing_ledger_table_sends_nothing(notif):
    notif.db.missing = {"notification_log"}
    r = notif.go()
    assert r["outcome"] == "failed" and r["reason"] == "ledger_unavailable" and notif.sent == []
    r2 = notif.go()                                  # still inside the re-probe window: still nothing
    assert r2["reason"] == "ledger_unavailable" and notif.sent == []


def test_once_the_table_exists_updates_start_flowing(notif):
    notif.db.missing = {"notification_log"}
    assert notif.go()["notified"] is False
    notif.db.missing = set()
    ledger._reset_probe_for_tests()
    assert notif.go()["notified"] is True and len(notif.sent) == 1


def test_default_claim_behaviour_for_other_callers_is_unchanged(db):
    db.missing = {"notification_log"}
    assert ledger.claim(1, "k", "p", table_only=True) is True        # non-strict callers keep their own guards
    with pytest.raises(ledger.LedgerUnavailable):
        ledger.claim(1, "k", "p", table_only=True, strict=True)


def test_notification_log_migration_has_the_uniqueness_constraint():
    from pathlib import Path
    sql = (Path(__file__).resolve().parents[1] / "migrations" / "notification_ledger.sql").read_text(encoding="utf8")
    assert "UNIQUE (business_id, kind, period_key)" in sql and "notification_log" in sql


# ═════════ 4. the flag: OFF by default, explicit enablement ═════════
@pytest.mark.parametrize("fj", [None, {}, {"order_status_notifications": False}, {"order_status_notifications": "true"},
                                {"order_status_notifications": 1}])
def test_notifications_are_off_unless_explicitly_true(notif, fj):
    biz = {**BIZ_OFF, "features_json": fj}
    r = notif.go(business=biz)
    assert r["outcome"] == "skipped_disabled" and notif.sent == [] and notif.db.claims("order_status") == []
    assert on.updates_promised({**biz, "whatsapp_phone_id": "PID"}) is False


def test_explicit_true_enables_notifications(notif):
    assert notif.go()["outcome"] == "sent"


def test_a_disabled_business_is_not_promised_updates():
    assert "message you" not in on.updates_promise_line(BIZ_OFF)
    assert "message you" in on.updates_promise_line(BIZ_ON)


def test_disabled_status_change_does_not_touch_the_status_itself(notif):
    # the notifier is advisory: a disabled business still gets its status change (route code unchanged)
    assert notif.go(business=BIZ_OFF)["notified"] is False


# ═════════ 5. skipped / attempted / accepted are different things ═════════
def test_stage_distinguishes_every_outcome(notif, monkeypatch):
    from services import wa_window, wa_outbound
    assert notif.go()["stage"] == "accepted_by_whatsapp"
    assert notif.go()["stage"] == "duplicate"
    assert notif.go(business=BIZ_OFF)["stage"] == "not_attempted"
    assert notif.go(status="out_for_delivery")["stage"] == "not_attempted"
    assert notif.go(status="preparing", sender=lambda *a: {"error": {"code": 1}})["stage"] == "failed"
    monkeypatch.setattr(wa_window, "closed_phones", lambda bid, phones: set(phones))
    monkeypatch.setattr(wa_outbound, "template_for_closed_window", lambda *a, **k: {"ok": False, "skipped": "no_template"})
    r = notif.go(status="completed")
    assert r["outcome"] == "skipped_window_no_template" and r["stage"] == "not_attempted" and r["notified"] is False


def test_outside_the_window_free_text_is_never_sent(notif, monkeypatch):
    from services import wa_window, wa_outbound
    monkeypatch.setattr(wa_window, "closed_phones", lambda bid, phones: set(phones))
    monkeypatch.setattr(wa_outbound, "template_for_closed_window", lambda *a, **k: {"ok": False, "skipped": "no_template"})
    notif.go()
    assert notif.sent == []


def test_dashboard_wording_does_not_claim_delivery():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "static" / "dashboard.js").read_text(encoding="utf8")
    assert "delivery not confirmed" in js and "' · customer notified'" not in js


# ═════════ 6. booking cancellation gives back deducted stock once ═════════
class _BQ:
    def __init__(self, db, t):
        self.db, self.t, self.f, self.op, self.p = db, t, [], "select", None

    def select(self, *a, **k): self.op = "select"; return self
    def update(self, row): self.op, self.p = "update", row; return self
    def insert(self, row): self.op, self.p = "insert", row; return self
    def delete(self): self.op = "delete"; return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def neq(self, c, v): self.f.append(lambda r: r.get(c) is not None and r.get(c) != v); return self
    def or_(self, expr):                      # only the form used by cancel_booking: "status.is.null,status.neq.X"
        col, _, rest = expr.split(",")[0].partition(".")
        val = expr.split(",")[1].rsplit(".", 1)[1]
        self.f.append(lambda r: r.get(col) is None or r.get(col) != val)
        return self

    def limit(self, n): return self
    def order(self, *a, **k): return self

    def execute(self):
        rows = self.db.tables.setdefault(self.t, [])
        if self.op == "insert":
            rows.append(dict(self.p))
            return types.SimpleNamespace(data=[dict(self.p)])
        hit = [r for r in rows if all(f(r) for f in self.f)]
        if self.op == "update":
            for r in hit:
                r.update(self.p)
        if self.op == "delete":
            for r in hit:
                rows.remove(r)
        return types.SimpleNamespace(data=[dict(r) for r in hit])


class _BDB:
    def __init__(self, **t): self.tables = t
    def table(self, n): return _BQ(self, n)
    def rpc(self, *a, **k):
        e = Exception("missing function"); e.code = "42883"; raise e


@pytest.fixture
def bk(monkeypatch):
    import core.db as coredb
    import services.booking_service as bs
    items = [{"name": "Cake", "qty": 2, "product_id": 5, "stock_deducted": True},
             {"name": "Consult", "qty": 1, "product_id": None, "stock_deducted": False}]
    d = _BDB(bookings=[{"id": 1, "business_id": 1, "status": "confirmed", "order_id": 9}],
             orders=[{"id": 9, "business_id": 1, "status": "confirmed", "items": json.dumps(items)}],
             products=[{"id": 5, "business_id": 1, "stock": 3}], stock_movements=[])
    monkeypatch.setattr(coredb, "supabase", d)
    monkeypatch.setattr(sr, "supabase", d)
    sr._reset_probes_for_tests()
    return types.SimpleNamespace(db=d, bs=bs)


def _stock(bk): return bk.db.tables["products"][0]["stock"]


def test_booking_cancel_gives_back_deducted_stock(bk):
    assert bk.bs.cancel_booking(1, 1)["status"] == "cancelled"
    assert bk.db.tables["orders"][0]["status"] == "cancelled" and _stock(bk) == 5


def test_repeated_booking_cancel_never_restores_twice(bk):
    bk.bs.cancel_booking(1, 1)
    bk.bs.cancel_booking(1, 1)
    bk.bs.cancel_booking(1, 1)
    assert _stock(bk) == 5


def test_a_service_item_with_no_deduction_is_never_restored(bk):
    bk.db.tables["orders"][0]["items"] = json.dumps([{"name": "Consult", "qty": 1, "product_id": None, "stock_deducted": False}])
    bk.bs.cancel_booking(1, 1)
    assert _stock(bk) == 3


def test_an_order_created_before_deduction_was_recorded_is_left_alone(bk):
    bk.db.tables["orders"][0]["items"] = json.dumps([{"name": "Cake", "qty": 2, "product_id": 5}])
    bk.bs.cancel_booking(1, 1)
    assert _stock(bk) == 3


def test_refunded_order_is_cancelled_as_before_but_stock_is_untouched(bk):
    bk.db.tables["orders"][0]["status"] = "refunded"
    bk.bs.cancel_booking(1, 1)
    assert bk.db.tables["orders"][0]["status"] == "cancelled" and _stock(bk) == 3


def test_already_cancelled_order_is_not_restored_again(bk):
    bk.db.tables["orders"][0]["status"] = "cancelled"
    bk.bs.cancel_booking(1, 1)
    assert _stock(bk) == 3


def test_other_business_booking_is_untouched(bk):
    assert bk.bs.cancel_booking(1, 2) is None and _stock(bk) == 3 and bk.db.tables["orders"][0]["status"] == "confirmed"


def test_a_restore_failure_never_blocks_the_booking_cancel(bk, monkeypatch):
    monkeypatch.setattr(sr, "restore_order_stock", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert bk.bs.cancel_booking(1, 1)["status"] == "cancelled"


# ═════════ final staging items ═════════
def test_booking_cancel_handles_an_order_with_no_status_once(bk):
    bk.db.tables["orders"][0]["status"] = None
    bk.bs.cancel_booking(1, 1)
    assert bk.db.tables["orders"][0]["status"] == "cancelled" and _stock(bk) == 5
    bk.bs.cancel_booking(1, 1)
    assert _stock(bk) == 5


def _lifecycle(monkeypatch, start_status):
    state = {"status": start_status, "calls": []}

    def upd(oid, status, business_id=None):
        cur = state["status"]
        if status != cur and status not in ol.VALID_TRANSITIONS.get(cur, []):
            raise ValueError("bad transition")
        state["calls"].append(status)
        state["status"] = status
        return {}
    monkeypatch.setattr(ar, "get_order", lambda oid: _order(status=state["status"]))
    monkeypatch.setattr(ar, "update_order_status_supabase", upd)
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: BIZ_OFF)
    monkeypatch.setattr(ar, "send_whatsapp", lambda *a: {})

    def push(s):
        return _run(ar.push_lifecycle_update(9, ar.OrderLifecycleUpdate(order_id=9, status=s),
                                             user={"business_id": 1, "role": "business"}))
    return state, push


@pytest.mark.parametrize("s", ["preparing", "ready", "out_for_delivery", "delivered", "completed"])
def test_lifecycle_stores_the_real_fulfilment_status(monkeypatch, s):
    state, push = _lifecycle(monkeypatch, "confirmed")
    push(s)
    assert state["status"] == s


def test_lifecycle_confirms_an_unconfirmed_order_first_then_moves_it_on(monkeypatch):
    state, push = _lifecycle(monkeypatch, "pending")
    push("preparing")
    assert state["calls"] == ["confirmed", "preparing"] and state["status"] == "preparing"


def test_lifecycle_walks_a_full_delivery_in_order(monkeypatch):
    state, push = _lifecycle(monkeypatch, "pending_cash")
    for s in ("preparing", "ready", "out_for_delivery", "delivered"):
        push(s)
        assert state["status"] == s


def test_lifecycle_never_moves_an_order_backwards(monkeypatch):
    state, push = _lifecycle(monkeypatch, "delivered")
    assert push("preparing")["ok"] is True          # reported, not raised (as before)
    assert state["status"] == "delivered"


def test_customer_tracking_shows_the_stored_fulfilment_state():
    from services._ai_payments import build_order_progress
    for st, word in (("preparing", "Preparing"), ("ready", "Ready"), ("out_for_delivery", "Out for delivery")):
        out = build_order_progress(st, "pending_cash", "delivery")
        assert word in out["progress"] or word in out["header"][1]
        assert out["payment"][1] != "paid"


def test_template_path_uses_the_durable_strict_ledger(db, monkeypatch):
    from services import wa_templates
    monkeypatch.setattr(wa_templates, "get_template", lambda b, p: {"name": "order_update", "category": "utility"})
    monkeypatch.setattr(wa_templates, "build_payload", lambda *a, **k: {})
    sent = []
    snd = lambda business, payload: sent.append(1) or {"ok": True, "id": "w"}
    call = lambda: wa_templates.send_template(1, "2637", "order_update", {}, ref="order:9:ready",
                                              business=BIZ_ON, sender=snd, strict_ledger=True)
    assert call()["ok"] is True
    ledger._reset_probe_for_tests()
    assert call() == {"ok": False, "skipped": "duplicate"} and len(sent) == 1
    db.missing = {"notification_log"}
    ledger._reset_probe_for_tests()
    assert call()["skipped"] == "ledger_unavailable" and len(sent) == 1
    # the features_json marker is never used for this path
    assert not any("_notif_sent" in str(b.get("features_json")) for b in db.tables.get("businesses", []))


def test_notifier_requests_the_strict_template_ledger(notif, monkeypatch):
    from services import wa_window, wa_outbound
    seen = {}
    monkeypatch.setattr(wa_window, "closed_phones", lambda bid, phones: set(phones))
    monkeypatch.setattr(wa_outbound, "template_for_closed_window",
                        lambda *a, **k: seen.update(k) or {"ok": True})
    assert notif.go()["outcome"] == "sent_template" and seen.get("strict_ledger") is True


def test_template_path_without_the_table_sends_nothing_and_is_not_marked_sent(notif, monkeypatch):
    from services import wa_window, wa_templates
    monkeypatch.setattr(wa_window, "closed_phones", lambda bid, phones: set(phones))
    monkeypatch.setattr(wa_templates, "get_template", lambda b, p: {"name": "order_update", "category": "utility"})
    monkeypatch.setattr(wa_templates, "build_payload", lambda *a, **k: {})
    out = notif.go()
    assert out["notified"] is False and notif.sent == []


def test_delivery_is_never_claimed_by_the_notifier():
    assert on._outcome("sent")["stage"] == "accepted_by_whatsapp"
    assert "delivered" not in set(on._outcome(o)["stage"] for o in
                                  ("sent", "sent_template", "failed", "duplicate", "skipped_disabled"))
