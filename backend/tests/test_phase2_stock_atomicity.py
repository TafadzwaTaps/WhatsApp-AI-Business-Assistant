"""Phase 2: stock decrement must not lose updates, and an order that cannot be completed
must give its stock back."""
import types

import pytest

import services.inventory as inv
import workflows.order_lifecycle as ol


class _Q:
    def __init__(self, db): self.db, self.f, self.patch = db, [], None
    def select(self, *_): return self
    def eq(self, c, v): self.f.append((c, v)); return self
    def ilike(self, c, v): self.f.append((c, v)); return self
    def limit(self, *_): return self
    def update(self, patch): self.patch = patch; return self
    def execute(self):
        self.db.calls += 1
        if self.patch is not None and self.db.interfere and self.db.calls == self.db.interfere[0]:
            self.db.rows[0]["stock"] = self.db.interfere[1]        # a concurrent order changes stock first
        hit = [r for r in self.db.rows if all(r.get(c) == v for c, v in self.f)]
        if self.patch is not None:
            for r in hit: r.update(self.patch)
        return types.SimpleNamespace(data=[dict(r) for r in hit])


class _Db:
    def __init__(self, rows, interfere=None): self.rows, self.interfere, self.calls = rows, interfere, 0
    def table(self, _): return _Q(self)


def _prod(stock, pid=1, biz=1, name="Cake"):
    return {"id": pid, "business_id": biz, "name": name, "stock": stock, "low_stock_threshold": 1}


def test_reduce_stock_normal(monkeypatch):
    db = _Db([_prod(10)]); monkeypatch.setattr(inv, "supabase", db)
    assert inv.reduce_stock(1, 3)["stock"] == 7 and db.rows[0]["stock"] == 7


def test_concurrent_change_is_not_overwritten(monkeypatch):
    # We read stock=10, but another order drops it to 4 before our write: the compare-and-set
    # must NOT write 7 (lost update); it re-reads and applies our 3 on top of 4.
    db = _Db([_prod(10)], interfere=(2, 4)); monkeypatch.setattr(inv, "supabase", db)
    assert inv.reduce_stock(1, 3)["stock"] == 1 and db.rows[0]["stock"] == 1


def test_concurrent_change_that_leaves_too_little_stock_is_refused(monkeypatch):
    db = _Db([_prod(10)], interfere=(2, 2)); monkeypatch.setattr(inv, "supabase", db)
    with pytest.raises(ValueError, match="Insufficient stock"):
        inv.reduce_stock(1, 3)
    assert db.rows[0]["stock"] == 2                       # untouched, never driven negative


def test_unlimited_stock_untouched_and_insufficient_refused(monkeypatch):
    db = _Db([_prod(None)]); monkeypatch.setattr(inv, "supabase", db)
    assert inv.reduce_stock(1, 99)["stock"] is None
    db = _Db([_prod(1)]); monkeypatch.setattr(inv, "supabase", db)
    with pytest.raises(ValueError):
        inv.reduce_stock(1, 2)


def test_restore_stock_adds_back_and_skips_unlimited(monkeypatch):
    db = _Db([_prod(5)]); monkeypatch.setattr(inv, "supabase", db)
    assert inv.restore_stock(1, 2) is True and db.rows[0]["stock"] == 7
    db = _Db([_prod(None)]); monkeypatch.setattr(inv, "supabase", db)
    assert inv.restore_stock(1, 2) is True and db.rows[0]["stock"] is None   # unlimited stays unlimited


def test_restore_stock_never_raises(monkeypatch):
    class Boom:
        def table(self, *_): raise RuntimeError("db down")
    monkeypatch.setattr(inv, "supabase", Boom())
    assert inv.restore_stock(1, 2) is False


# ── order creation gives stock back on failure ──────────────────────────────

def _order_env(monkeypatch, rows, insert_fails=False):
    db = _Db(rows)
    monkeypatch.setattr(inv, "supabase", db)
    monkeypatch.setattr(ol, "_has_col", lambda c: False)

    class OrdersTable:
        def insert(self, row):
            if insert_fails: raise RuntimeError("insert failed")
            self.row = dict(row, id=77); return self
        def execute(self): return types.SimpleNamespace(data=[self.row])
    real_table = db.table

    class Router:
        def table(self, name):
            if name == "orders": return OrdersTable()
            if name == "businesses": return _Q(_Db([{"id": 1, "is_service_business": False}]))
            return real_table(name)
    monkeypatch.setattr(ol, "supabase", Router())
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", Router())
    return db


CART2 = [{"name": "Cake", "qty": 2, "price": 5.0}, {"name": "Tea", "qty": 5, "price": 1.0}]


def test_second_item_out_of_stock_restores_the_first(monkeypatch):
    rows = [_prod(10, 1, name="Cake"), _prod(1, 2, name="Tea")]
    _order_env(monkeypatch, rows)
    with pytest.raises(ValueError, match="Insufficient stock"):
        ol.create_order_supabase(1, "263700000000", CART2)
    assert [r["stock"] for r in rows] == [10, 1]          # Cake came back


def test_order_insert_failure_restores_all_stock(monkeypatch):
    rows = [_prod(10, 1, name="Cake"), _prod(10, 2, name="Tea")]
    _order_env(monkeypatch, rows, insert_fails=True)
    with pytest.raises(ValueError):
        ol.create_order_supabase(1, "263700000000", CART2)
    assert [r["stock"] for r in rows] == [10, 10]


def test_successful_order_keeps_stock_reduced(monkeypatch):
    rows = [_prod(10, 1, name="Cake"), _prod(10, 2, name="Tea")]
    _order_env(monkeypatch, rows)
    order = ol.create_order_supabase(1, "263700000000", CART2)
    assert order["id"] == 77 and [r["stock"] for r in rows] == [8, 5]


def test_non_positive_quantity_never_adds_stock(monkeypatch):
    db = _Db([_prod(10)]); monkeypatch.setattr(inv, "supabase", db)
    for bad in (0, -5):
        with pytest.raises(ValueError, match="positive"):
            inv.reduce_stock(1, bad)
    assert db.rows[0]["stock"] == 10


def test_failure_after_decrement_but_before_insert_still_restores(monkeypatch):
    rows = [_prod(10, 1, name="Cake")]
    _order_env(monkeypatch, rows)
    import json as _json
    monkeypatch.setattr(ol, "_has_col", lambda c: c == "items")          # exercise the items branch...
    monkeypatch.setattr(ol.json, "dumps", lambda *a, **k: (_ for _ in ()).throw(TypeError("boom")))
    with pytest.raises(TypeError):                                      # ...which now raises after the decrement
        ol.create_order_supabase(1, "263700000000", [{"name": "Cake", "qty": 2, "price": 5.0}])
    assert rows[0]["stock"] == 10


def test_negative_cart_quantity_rejected_and_earlier_items_restored(monkeypatch):
    rows = [_prod(10, 1, name="Cake"), _prod(10, 2, name="Tea")]
    _order_env(monkeypatch, rows)
    with pytest.raises(ValueError, match="Invalid quantity"):
        ol.create_order_supabase(1, "263700000000",
                                 [{"name": "Cake", "qty": 2, "price": 5.0}, {"name": "Tea", "qty": -3, "price": 1.0}])
    assert [r["stock"] for r in rows] == [10, 10]
