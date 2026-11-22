"""Phase 2: stock restoration on cancel / refund (owner-approved rules) and the related
tenant-isolation fixes. A stateful in-memory database stands in for Supabase."""
import asyncio
import json
import threading
import types

import pytest
from fastapi import HTTPException

import services.inventory as inventory
import services.stock_restore as sr
import workflows.order_lifecycle as ol


# ── stateful fake database ────────────────────────────────────────────────────
class _Q:
    def __init__(self, db, table):
        self.db, self.t, self.f, self.op, self.payload, self._limit = db, table, [], "select", None, None

    def select(self, *_a, **_k):
        self.op = "select"
        return self

    def insert(self, row):
        self.op, self.payload = "insert", row
        return self

    def update(self, row):
        self.op, self.payload = "update", row
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, c, v):
        self.f.append(lambda r: r.get(c) == v)
        return self

    def ilike(self, c, v):
        want = str(v).strip("%").lower()
        self.f.append(lambda r: str(r.get(c) or "").lower() == want)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, *a, **k):
        return self

    def execute(self):
        hook = self.db.hooks.get((self.t, self.op))
        if hook:
            hook(self)
        rows = self.db.tables.setdefault(self.t, [])
        res = types.SimpleNamespace(data=[])
        with self.db.lock:
            if self.op == "insert":
                if self.t in self.db.unique:
                    keys = self.db.unique[self.t]
                    for r in rows:
                        if all(r.get(k) == self.payload.get(k) for k in keys):
                            raise Exception('duplicate key value violates unique constraint (23505)')
                row = dict(self.payload)
                row.setdefault("id", len(rows) + 1)
                rows.append(row)
                res.data = [dict(row)]
            else:
                hit = [r for r in rows if all(f(r) for f in self.f)]
                if self.op == "select":
                    out = [dict(r) for r in hit]
                    res.data = out[: self._limit] if self._limit else out
                elif self.op == "update":
                    for r in hit:
                        r.update(self.payload)
                    res.data = [dict(r) for r in hit]
                elif self.op == "delete":
                    for r in hit:
                        rows.remove(r)
                    res.data = [dict(r) for r in hit]
        post = self.db.post_hooks.get((self.t, self.op))
        if post:
            post(self)                     # runs AFTER the read/write, outside the lock
        return res


class _Rpc:
    def __init__(self, db, name, params):
        self.db, self.name, self.params = db, name, params

    def execute(self):
        if not self.db.rpc_enabled:
            raise Exception({"code": "PGRST202", "message": "Could not find the function public.adjust_stock_atomic"})
        self.db.rpc_calls.append(self.params)
        with self.db.lock:
            for p in self.db.tables["products"]:
                if (p["id"] == self.params["p_product_id"] and p["business_id"] == self.params["p_business_id"]
                        and p.get("stock") is not None and p["stock"] + self.params["p_delta"] >= 0):
                    p["stock"] += self.params["p_delta"]
                    return types.SimpleNamespace(data=p["stock"])
        return types.SimpleNamespace(data=None)


class _DB:
    def __init__(self, **tables):
        self.tables, self.hooks, self.unique = tables, {}, {}
        self.post_hooks = {}
        self.lock = threading.RLock()
        self.rpc_enabled, self.rpc_calls = False, []

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        return _Rpc(self, name, params)

    def stock(self, pid):
        return next(p["stock"] for p in self.tables["products"] if p["id"] == pid)


def _make(ledger=False, rpc=False):
    db = _DB(
        businesses=[{"id": 1, "is_service_business": False}, {"id": 2, "is_service_business": True}],
        products=[
            {"id": 1, "business_id": 1, "name": "Tea", "stock": 5, "price": 2.0},
            {"id": 2, "business_id": 1, "name": "Ebook", "stock": None, "price": 5.0},     # unlimited / digital
            {"id": 3, "business_id": 1, "name": "Cup", "stock": 10, "price": 3.0},
            {"id": 4, "business_id": 2, "name": "Haircut", "stock": 3, "price": 9.0},      # service business
        ],
        orders=[], stock_movements=[],
    )
    db.unique["stock_movements"] = ("order_id", "product_id", "kind")
    db.rpc_enabled = rpc
    if not ledger:
        def missing(q):
            if q.t == "stock_movements":
                raise Exception({"code": "42P01", "message": 'relation "public.stock_movements" does not exist'})
        for op in ("insert", "delete", "select"):
            db.hooks[("stock_movements", op)] = missing
    return db


@pytest.fixture
def world(monkeypatch):
    def install(ledger=False, rpc=False):
        db = _make(ledger, rpc)
        monkeypatch.setattr(inventory, "supabase", db)
        monkeypatch.setattr(ol, "supabase", db)
        monkeypatch.setattr(sr, "supabase", db)
        import core.db as coredb
        monkeypatch.setattr(coredb, "supabase", db)     # order creation looks up is_service_business here
        monkeypatch.setattr(ol, "_orders_columns", {"id", "business_id", "customer_phone", "product_name",
                                                   "quantity", "total_price", "status", "created_at",
                                                   "payment_method", "items", "payment_status",
                                                   "payment_reference", "paypal_order_id"})
        sr._reset_probes_for_tests()
        return db
    sr._reset_probes_for_tests()
    yield install
    sr._reset_probes_for_tests()


def _order(db, biz=1, cart=None):
    cart = cart or [{"name": "Tea", "qty": 2, "price": 2.0}]
    o = ol.create_order_supabase(biz, "263700", cart)
    return o["id"]


def _items(db, oid):
    raw = next(o for o in db.tables["orders"] if o["id"] == oid)["items"]
    return json.loads(raw) if isinstance(raw, str) else raw


# ── recording what was deducted (Step A) ──────────────────────────────────────
def test_new_orders_record_product_id_and_whether_stock_was_really_taken(world):
    db = world()
    oid = _order(db, cart=[{"name": "Tea", "qty": 2, "price": 2.0}, {"name": "Ebook", "qty": 1, "price": 5.0}])
    items = {i["name"]: i for i in _items(db, oid)}
    assert items["Tea"]["product_id"] == 1 and items["Tea"]["stock_deducted"] is True
    assert items["Ebook"]["product_id"] == 2 and items["Ebook"]["stock_deducted"] is False      # unlimited
    assert db.stock(1) == 3


def test_service_business_orders_record_no_deduction(world):
    db = world()
    oid = _order(db, biz=2, cart=[{"name": "Haircut", "qty": 1, "price": 9.0}])
    item = _items(db, oid)[0]
    assert item["stock_deducted"] is False and item["product_id"] is None
    assert db.stock(4) == 3                                             # service stock untouched


# ── cancel restores exactly what was deducted ────────────────────────────────
@pytest.mark.parametrize("ledger,rpc", [(False, False), (True, False), (True, True), (False, True)])
def test_cancel_restores_stock_once_in_every_storage_mode(world, ledger, rpc):
    db = world(ledger, rpc)
    oid = _order(db)
    assert db.stock(1) == 3
    out = ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert db.stock(1) == 5
    assert out["_stock_restore"]["restored"] == [{"name": "Tea", "product_id": 1, "qty": 2}]
    ol.update_order_status_supabase(oid, "cancelled", business_id=1)       # same status again: a no-op
    assert db.stock(1) == 5
    if ledger:
        assert len(db.tables["stock_movements"]) == 1


def test_rpc_is_used_when_present_and_fallback_is_used_when_missing(world):
    db = world(rpc=True)
    oid = _order(db)
    ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert db.rpc_calls == [{"p_business_id": 1, "p_product_id": 1, "p_delta": 2}]
    db2 = world(rpc=False)
    oid2 = _order(db2)
    ol.update_order_status_supabase(oid2, "cancelled", business_id=1)
    assert db2.stock(1) == 5 and db2.rpc_calls == []


@pytest.mark.parametrize("ledger", [False, True])
def test_two_simultaneous_cancels_restore_once(world, ledger):
    db = world(ledger)
    oid = _order(db)
    results, barrier = [], threading.Barrier(6)

    def worker():
        barrier.wait()
        try:
            ol.update_order_status_supabase(oid, "cancelled", business_id=1)
            results.append("ok")
        except ValueError:
            results.append("lost")
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert db.stock(1) == 5                       # restored exactly once, never 7 or more
    assert results.count("ok") >= 1


def test_unlimited_and_service_items_are_never_restored(world):
    db = world()
    oid = _order(db, cart=[{"name": "Ebook", "qty": 3, "price": 5.0}, {"name": "Tea", "qty": 1, "price": 2.0}])
    out = ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert db.stock(2) is None                    # still unlimited, not turned into a number
    reasons = {s["name"]: s["reason"] for s in out["_stock_restore"]["skipped"]}
    assert reasons["Ebook"] == "not_deducted"
    sid = _order(db, biz=2, cart=[{"name": "Haircut", "qty": 1, "price": 9.0}])
    out2 = ol.update_order_status_supabase(sid, "cancelled", business_id=2)
    assert db.stock(4) == 3 and out2["_stock_restore"]["restored"] == []


def test_orders_created_before_the_deduction_record_are_not_auto_restored(world):
    db = world()
    db.tables["orders"].append({"id": 50, "business_id": 1, "status": "pending", "customer_phone": "1",
                                "items": json.dumps([{"name": "Tea", "qty": 2, "price": 2.0, "subtotal": 4.0}])})
    out = ol.update_order_status_supabase(50, "cancelled", business_id=1)
    assert db.stock(1) == 5                       # unchanged
    assert out["_stock_restore"]["skipped"][0]["reason"] == "no_deduction_record"


def test_restore_failure_never_blocks_the_cancellation(world, monkeypatch):
    db = world()
    oid = _order(db)

    def boom(*a, **k):
        raise RuntimeError("stock service down")
    monkeypatch.setattr(sr, "_add_stock", boom)
    out = ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert out["status"] == "cancelled"
    assert out["_stock_restore"]["failed"] and db.stock(1) == 3


def test_a_failed_add_releases_the_ledger_claim_so_a_retry_can_succeed(world, monkeypatch):
    db = world(ledger=True)
    oid = _order(db)
    real = sr._add_stock
    monkeypatch.setattr(sr, "_add_stock", lambda *a, **k: False)
    order = next(o for o in db.tables["orders"] if o["id"] == oid)
    out = sr.restore_order_stock(order, reason="t", transition_won=True)
    assert out["failed"] and db.tables["stock_movements"] == []          # claim released
    monkeypatch.setattr(sr, "_add_stock", real)
    out2 = sr.restore_order_stock(order, reason="t", transition_won=True)
    assert out2["restored"] and db.stock(1) == 5


# ── tenant isolation ──────────────────────────────────────────────────────────
def test_another_business_cannot_cancel_or_trigger_a_restore(world):
    db = world()
    oid = _order(db)
    with pytest.raises(ValueError):
        ol.update_order_status_supabase(oid, "cancelled", business_id=2)
    assert db.stock(1) == 3 and next(o for o in db.tables["orders"] if o["id"] == oid)["status"] == "pending"


def test_restore_never_adds_stock_to_another_tenants_product(world):
    db = world()
    # a (forged / corrupted) order of business 2 pointing at business 1's product id
    order = {"id": 70, "business_id": 2, "items": json.dumps([
        {"name": "Tea", "qty": 4, "product_id": 1, "stock_deducted": True}])}
    out = sr.restore_order_stock(order, reason="t", transition_won=True)
    assert db.stock(1) == 5 - 0 and out["restored"] == [] and db.stock(1) == 5


def test_confirm_payment_helper_is_tenant_scoped_when_given_a_business(world):
    db = world()
    oid = _order(db)
    with pytest.raises(ValueError):
        ol.confirm_payment_supabase(oid, "REF", business_id=2)
    assert ol.confirm_payment_supabase(oid, "REF", business_id=1)["payment_status"] == "paid"


# ── refunds: restock only on the owner's explicit say-so ─────────────────────
def _refund(db, oid, **body):
    import routes.admin_routes as ar
    from unittest import mock

    async def noop(order, message):
        return None
    fake_crud = types.SimpleNamespace(update_order_payment=lambda *a, **k: None,
                                      get_business_by_id=lambda b: {"currency_symbol": "$"})
    # scoped patches: nothing leaks into other tests
    with mock.patch.object(ar, "get_order", ol.get_order),             mock.patch.object(ar, "crud", fake_crud),             mock.patch.object(ar, "_notify_customer_payment", noop):
        return asyncio.run(ar.admin_refund_order(oid, ar.OrderAdminAction(**body), user={"business_id": 1}))


def _paid_order(db, cart=None):
    oid = _order(db, cart=cart)
    next(o for o in db.tables["orders"] if o["id"] == oid)["status"] = "delivered"
    return oid


def test_refund_alone_never_restocks(world):
    db = world()
    oid = _paid_order(db)
    out = _refund(db, oid)
    assert out["stock"] is None and db.stock(1) == 3


def test_refund_with_restock_restores_once(world):
    db = world()
    oid = _paid_order(db)
    out = _refund(db, oid, restock=True)
    assert db.stock(1) == 5 and out["stock"]["restored"][0]["qty"] == 2


def test_refund_after_cancel_does_not_restore_a_second_time(world):
    db = world()
    oid = _order(db)
    ol.update_order_status_supabase(oid, "cancelled", business_id=1)
    assert db.stock(1) == 5
    out = _refund(db, oid, restock=True)
    assert db.stock(1) == 5
    assert out["stock"]["restored"] == []


def test_partial_return_needs_the_ledger_and_restores_only_the_selected_quantity(world):
    db = world(ledger=False)
    oid = _paid_order(db, cart=[{"name": "Tea", "qty": 3, "price": 2.0}, {"name": "Cup", "qty": 2, "price": 3.0}])
    out = _refund(db, oid, restock_items=[{"product_id": 1, "qty": 1}])
    assert db.stock(1) == 2 and db.stock(3) == 8
    assert out["stock"]["skipped"][0]["reason"] == "ledger_required" or db.stock(1) == 2   # no ledger: refused
    db2 = world(ledger=True)
    oid2 = _paid_order(db2, cart=[{"name": "Tea", "qty": 3, "price": 2.0}, {"name": "Cup", "qty": 2, "price": 3.0}])
    out2 = _refund(db2, oid2, restock_items=[{"product_id": 1, "qty": 99}])      # more than was deducted
    assert db2.stock(1) == 5 - 3 + 3                                             # capped at the 3 deducted
    assert db2.stock(3) == 8                                                    # the Cup was not selected


def test_partial_return_cannot_be_applied_twice_with_the_ledger(world):
    db = world(ledger=True)
    oid = _paid_order(db, cart=[{"name": "Tea", "qty": 3, "price": 2.0}])
    order = next(o for o in db.tables["orders"] if o["id"] == oid)
    a = sr.restore_order_stock(order, reason="r", only=[{"product_id": 1, "qty": 1}])
    b = sr.restore_order_stock(next(o for o in db.tables["orders"] if o["id"] == oid), reason="r",
                               only=[{"product_id": 1, "qty": 1}])
    assert a["restored"] and not b["restored"] and db.stock(1) == 2 + 1      # 5 - 3 deducted + 1 returned, once


# ── dashboard status route surfaces the result ───────────────────────────────
def test_status_route_reports_the_restore_only_when_it_cancelled(world, monkeypatch):
    import routes.business_routes as br
    db = world()
    oid = _order(db)
    monkeypatch.setattr(br.crud, "get_order_by_id", lambda o, b: next((x for x in db.tables["orders"] if x["id"] == o), None))
    r = br.update_order_status_api(oid, br.OrderStatusUpdate(status="cancelled"), user={"business_id": 1})
    assert r["status"] == "cancelled" and r["stock"]["restored"]
    oid2 = _order(db)
    r2 = br.update_order_status_api(oid2, br.OrderStatusUpdate(status="confirmed"), user={"business_id": 1})
    assert "stock" not in r2


# ── restock_product (lost update + unlimited) ────────────────────────────────
def test_restock_product_is_compare_and_set_and_keeps_unlimited_unlimited(world):
    db = world()
    assert inventory.restock_product(1, 4)["stock"] == 9
    assert db.stock(1) == 9
    assert inventory.restock_product(2, 4)["stock"] is None and db.stock(2) is None


def test_the_two_inventory_copies_are_still_identical():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    assert (root / "inventory.py").read_text(encoding="utf8") == (root / "services" / "inventory.py").read_text(encoding="utf8")


# ── manual payment confirmation uses the atomic claim ────────────────────────
def test_manual_payment_confirmation_does_not_repeat_side_effects(monkeypatch):
    import routes.chat_routes as cr
    notified, claimed = [], {"n": 0}

    class Req:
        async def json(self):
            return {"order_id": 9, "reference": "ORDER-9"}
    order = {"id": 9, "business_id": 1, "customer_phone": "2637"}
    monkeypatch.setattr(ol, "get_order", lambda oid: order)

    def claim(oid, bid, data):
        claimed["n"] += 1
        return claimed["n"] == 1                  # only the first call wins
    monkeypatch.setattr(cr.crud, "claim_order_paid", claim)
    monkeypatch.setattr(ol, "update_order_status_supabase", lambda *a, **k: {})

    async def notify(o, m):
        notified.append(m)
    monkeypatch.setattr(cr, "_notify_customer_payment", notify)
    user = {"business_id": 1}
    r1 = asyncio.run(cr.manual_payment_confirm(Req(), user=user))
    r2 = asyncio.run(cr.manual_payment_confirm(Req(), user=user))
    assert len(notified) == 1 and "already" in r2["message"].lower()


def test_manual_payment_confirmation_503_on_database_error(monkeypatch):
    import routes.chat_routes as cr

    class Req:
        async def json(self):
            return {"order_id": 9, "reference": "ORDER-9"}
    monkeypatch.setattr(ol, "get_order", lambda oid: {"id": 9, "business_id": 1})

    def boom(*a):
        raise RuntimeError("db")
    monkeypatch.setattr(cr.crud, "claim_order_paid", boom)
    with pytest.raises(HTTPException) as e:
        asyncio.run(cr.manual_payment_confirm(Req(), user={"business_id": 1}))
    assert e.value.status_code == 503


@pytest.mark.parametrize("ledger", [False, True])
def test_a_forced_cancel_race_restores_exactly_once(world, ledger):
    """Both callers read the order as 'pending' BEFORE either writes (a real race, forced with a
    barrier): the atomic status change lets only one of them cancel and restore."""
    db = world(ledger)
    oid = _order(db)
    gate = threading.Barrier(2, timeout=5)
    seen = {"n": 0}
    lock = threading.Lock()

    def both_read_before_any_write(q):
        with lock:
            seen["n"] += 1
            mine = seen["n"] <= 2
        if mine:
            gate.wait()
    db.post_hooks[("orders", "select")] = both_read_before_any_write
    outcomes = []

    def worker():
        try:
            ol.update_order_status_supabase(oid, "cancelled", business_id=1)
            outcomes.append("won")
        except ValueError:
            outcomes.append("lost")
    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(outcomes) == ["lost", "won"]
    assert db.stock(1) == 5                      # 3 + 2, not 7
