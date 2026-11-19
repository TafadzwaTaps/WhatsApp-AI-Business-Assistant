"""Phase 2 review follow-ups: duplicate cart lines, repeated refunds, cancel retry, probe
refresh, RPC result shapes, scoped release, manual-confirm completion."""
import asyncio
import threading
import types

import pytest
from fastapi import HTTPException

import workflows.order_lifecycle as ol
import services.stock_restore as sr

from test_phase2_stock_restore import (   # noqa: F401  (fixtures + helpers)
    world, _order, _items, _paid_order, _refund, _DB,
)


# -- the same product on two cart lines ---------------------------------------
@pytest.mark.parametrize("ledger", [False, True])
def test_a_product_on_two_cart_lines_is_restored_in_full(world, ledger):
    db = world(ledger)
    oid = _order(db, cart=[{"name": "Tea", "qty": 1, "price": 2.0}, {"name": "Tea", "qty": 2, "price": 2.0}])
    assert db.stock(1) == 2                                            # 5 - 1 - 2
    out = ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert db.stock(1) == 5
    assert out["_stock_restore"]["restored"] == [{"name": "Tea", "product_id": 1, "qty": 3}]
    assert sorted(i["stock_restored_qty"] for i in _items(db, oid)) == [1, 2]
    again = ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert db.stock(1) == 5 and again["_stock_restore"]["restored"] == []


# -- a refund happens once ------------------------------------------------------
def test_second_refund_is_refused_and_never_restocks_again(world):
    db = world()
    oid = _paid_order(db)
    first = _refund(db, oid, restock=True)
    assert db.stock(1) == 5 and first["stock"]["restored"]
    with pytest.raises(HTTPException) as e:
        _refund(db, oid, restock=True)
    assert e.value.status_code == 409 and db.stock(1) == 5


def test_two_simultaneous_refunds_restock_once(world, monkeypatch):
    import routes.admin_routes as ar
    db = world()
    oid = _paid_order(db)

    async def noop(order, message):
        return None
    # patched ONCE here (monkeypatch undoes it at teardown); never inside the racing threads
    monkeypatch.setattr(ar, "get_order", ol.get_order)
    monkeypatch.setattr(ar, "crud", types.SimpleNamespace(
        update_order_payment=lambda *a, **k: None, get_business_by_id=lambda b: {"currency_symbol": "$"}))
    monkeypatch.setattr(ar, "_notify_customer_payment", noop)
    gate = threading.Barrier(2, timeout=5)
    seen = {"n": 0}
    lock = threading.Lock()

    def both_read(q):
        with lock:
            seen["n"] += 1
            mine = seen["n"] <= 2
        if mine:
            gate.wait()
    db.post_hooks[("orders", "select")] = both_read
    outcomes = []

    def worker():
        try:
            asyncio.run(ar.admin_refund_order(oid, ar.OrderAdminAction(restock=True), user={"business_id": 1}))
            outcomes.append("ok")
        except HTTPException as e:
            outcomes.append(e.status_code)
    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert db.stock(1) == 5                      # restored once, never twice
    assert outcomes.count("ok") == 1


# -- a failed restore can be retried safely -----------------------------------
def test_cancel_retry_restores_after_a_failed_first_attempt_when_the_ledger_exists(world, monkeypatch):
    db = world(ledger=True)
    oid = _order(db)
    real = sr._add_stock
    monkeypatch.setattr(sr, "_add_stock", lambda *a, **k: False)
    first = ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert first["_stock_restore"]["failed"] and db.stock(1) == 3
    monkeypatch.setattr(sr, "_add_stock", real)
    retry = ol.update_order_status_supabase(oid, "cancelled", business_id=1)      # same status: a retry
    assert retry["_stock_restore"]["restored"] and db.stock(1) == 5
    again = ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert db.stock(1) == 5 and again["_stock_restore"]["restored"] == []


def test_cancel_retry_is_refused_without_the_ledger_so_it_can_never_double_restore(world, monkeypatch):
    db = world(ledger=False)
    oid = _order(db)
    real = sr._add_stock
    monkeypatch.setattr(sr, "_add_stock", lambda *a, **k: False)
    ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    monkeypatch.setattr(sr, "_add_stock", real)
    retry = ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert db.stock(1) == 3
    assert retry["_stock_restore"]["skipped"][0]["reason"] == "ledger_required"


# -- probes are refreshed, not cached forever ---------------------------------
def test_missing_ledger_is_rechecked_after_a_minute_so_the_migration_takes_effect(world):
    db = world(ledger=False)
    assert sr._claim(1, 10, 1, 1, "t") == "no_ledger"
    db.hooks.clear()                                                    # the migration is applied
    assert sr._claim(1, 10, 1, 1, "t") == "no_ledger"                   # still cached inside the minute
    sr._ledger_failed_at -= sr._REPROBE_SECONDS + 1
    assert sr._claim(1, 10, 1, 1, "t") == "claimed"
    assert sr._claim(1, 10, 1, 1, "t") == "duplicate"


def test_missing_rpc_is_rechecked_after_a_minute(world):
    db = world(rpc=False)
    assert sr._add_stock(1, 1, 1) is True and db.rpc_calls == []        # fell back to compare-and-set
    db.rpc_enabled = True
    assert sr._add_stock(1, 1, 1) is True and db.rpc_calls == []        # still on the fallback
    sr._rpc_failed_at -= sr._REPROBE_SECONDS + 1
    assert sr._add_stock(1, 1, 1) is True and len(db.rpc_calls) == 1    # now uses the function


# -- RPC result shapes and error shapes ---------------------------------------
class _FakeRpcDB:
    def __init__(self, data):
        self.data = data

    def rpc(self, name, params):
        return types.SimpleNamespace(execute=lambda: types.SimpleNamespace(data=self.data))


@pytest.mark.parametrize("data,expected", [
    (None, None), ([], None), ([None], None), ({}, None), ("", None),
    (0, True), (7, True), ([7], True),
])
def test_rpc_result_shapes(monkeypatch, data, expected):
    sr._reset_probes_for_tests()
    monkeypatch.setattr(sr, "supabase", _FakeRpcDB(data))
    assert sr._add_stock(1, 1, 1) is expected                          # a stock of 0 is a SUCCESS


def test_duplicate_error_with_a_code_attribute_is_recognised(world):
    class APIError(Exception):
        code = "23505"
    db = world(ledger=True)

    def boom(q):
        raise APIError("duplicate")
    db.hooks[("stock_movements", "insert")] = boom
    assert sr._claim(1, 1, 1, 1, "t") == "duplicate"


def test_release_is_tenant_scoped(world):
    db = world(ledger=True)
    sr._claim(1, 10, 1, 1, "t")
    sr._release(2, 10, 1)                                              # other business: must not delete
    assert len(db.tables["stock_movements"]) == 1
    sr._release(1, 10, 1)
    assert db.tables["stock_movements"] == []


# -- manual confirmation finishes an interrupted earlier attempt --------------
def test_manual_confirm_already_paid_still_advances_the_status_without_renotifying(monkeypatch):
    import routes.chat_routes as cr
    calls, notified = [], []

    class Req:
        async def json(self):
            return {"order_id": 9, "reference": "ORDER-9", "amount": 5}
    monkeypatch.setattr(ol, "get_order", lambda oid: {"id": 9, "business_id": 1, "customer_phone": "2637"})
    monkeypatch.setattr(cr.crud, "claim_order_paid", lambda *a: False)         # already paid
    monkeypatch.setattr(ol, "update_order_status_supabase", lambda *a, **k: calls.append((a, k)) or {})

    async def notify(o, m):
        notified.append(m)
    monkeypatch.setattr(cr, "_notify_customer_payment", notify)
    out = asyncio.run(cr.manual_payment_confirm(Req(), user={"business_id": 1}))
    assert "already" in out["message"].lower()
    assert calls and calls[0][0][1] == "paid" and calls[0][1]["business_id"] == 1
    assert notified == []


def test_manual_confirm_already_paid_survives_a_rejected_status_change(monkeypatch):
    import routes.chat_routes as cr

    class Req:
        async def json(self):
            return {"order_id": 9, "reference": "ORDER-9", "amount": 5}
    monkeypatch.setattr(ol, "get_order", lambda oid: {"id": 9, "business_id": 1})
    monkeypatch.setattr(cr.crud, "claim_order_paid", lambda *a: False)

    def reject(*a, **k):
        raise ValueError("Cannot transition")
    monkeypatch.setattr(ol, "update_order_status_supabase", reject)
    out = asyncio.run(cr.manual_payment_confirm(Req(), user={"business_id": 1}))
    assert out["ok"] is True
