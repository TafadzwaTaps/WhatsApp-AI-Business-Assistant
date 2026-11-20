"""Phase 7, stage 3: reporting correctness. One definition of revenue everywhere (paid, not cancelled/refunded),
paged reads, exact counts, bounded inputs, tenant scoping."""
import types
from datetime import datetime, timedelta, timezone

import pytest

from crud._helpers import counts_as_revenue


class _Q:
    def __init__(self, db, t):
        self.db, self.t, self.f, self.order_col, self.rng, self.lim, self.count = db, t, [], None, None, None, False

    def select(self, cols="*", count=None): self.count = bool(count); return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def gte(self, c, v): self.f.append(lambda r: str(r.get(c)) >= str(v)); return self
    def lt(self, c, v): self.f.append(lambda r: str(r.get(c)) < str(v)); return self
    def gt(self, c, v): self.f.append(lambda r: (r.get(c) or 0) > v); return self
    def in_(self, c, vs): self.f.append(lambda r: r.get(c) in vs); return self
    def order(self, c, desc=False): self.order_col = (c, desc); return self
    def range(self, lo, hi): self.rng = (lo, hi); return self
    def limit(self, n): self.lim = n; self.db.limits.append(n); return self

    def execute(self):
        rows = [r for r in self.db.tables.get(self.t, []) if all(f(r) for f in self.f)]
        total = len(rows)
        if self.order_col:
            rows = sorted(rows, key=lambda r: r[self.order_col[0]], reverse=self.order_col[1])
        if self.rng:
            rows = rows[self.rng[0]:self.rng[1] + 1]
        elif self.lim is not None:
            rows = rows[: self.lim]
        else:
            rows = rows[:1000]                      # PostgREST silently caps an unpaged select
        return types.SimpleNamespace(data=[dict(r) for r in rows], count=total if self.count else None)


class _DB:
    def __init__(self, **tables):
        self.tables, self.limits = tables, []

    def table(self, name): return _Q(self, name)


def order(i, biz=1, price=10.0, pay="paid", status="confirmed", day="2026-11-05", **kw):
    return {"id": i, "business_id": biz, "total_price": price, "payment_status": pay, "status": status,
            "created_at": f"{day}T10:00:00+00:00", "customer_phone": "1", "product_name": kw.get("product_name", "tea"),
            "items": kw.get("items", [])}


MIXED = [
    order(1, price=10), order(2, price=20, pay="pending", status="pending"),
    order(3, price=30, status="cancelled"), order(4, price=40, pay="refunded", status="refunded"),
    order(5, price=50, pay="paid", status="cancelled"), order(6, price=5, pay="confirmed", status="delivered"),
    order(7, price=900, biz=2),
]


# ── the one revenue rule ──────────────────────────────────────────────────────
@pytest.mark.parametrize("pay,status,expected", [
    ("paid", "confirmed", True), ("paid", "delivered", True), ("confirmed", "completed", True), ("paid", "pending", True),
    ("paid", "cancelled", False), ("paid", "refunded", False), ("refunded", "refunded", False), ("refunded", "completed", False),
    ("pending", "pending", False), ("awaiting_payment", "pending", False), ("awaiting_confirmation", "pending", False),
    (None, None, False), ("PAID", "Confirmed", True), ("PAID", "CANCELLED", False),
])
def test_counts_as_revenue(pay, status, expected):
    assert counts_as_revenue({"payment_status": pay, "status": status}) is expected


# ── dashboard (/analytics/{id}) ───────────────────────────────────────────────
def test_dashboard_revenue_is_paid_only_and_the_old_total_is_kept_as_gross(monkeypatch):
    from crud import orders as co
    monkeypatch.setattr(co, "supabase", _DB(orders=MIXED, customers=[]))
    out = co.get_dashboard_stats(1)
    assert out["total_revenue"] == 15.0                       # orders 1 and 6 only
    assert out["gross_order_value"] == 155.0                  # what the dashboard used to call revenue
    assert out["total_orders"] == 6                           # other tenant excluded
    assert out["revenue_per_day"] == {"2026-11-05": 15.0}
    assert out["orders_by_status"]["cancelled"] == 2


def test_dashboard_revenue_per_day_ignores_unpaid_days(monkeypatch):
    from crud import orders as co
    rows = [order(1, price=10, day="2026-11-01"), order(2, price=99, pay="pending", status="pending", day="2026-11-02")]
    monkeypatch.setattr(co, "supabase", _DB(orders=rows, customers=[]))
    out = co.get_dashboard_stats(1)
    assert out["revenue_per_day"].get("2026-11-02", 0) == 0 and out["orders_per_day"]["2026-11-02"] == 1


# ── /analytics/stats and admin stats agree with it ────────────────────────────
def test_business_stats_use_the_same_rule(monkeypatch):
    from crud import analytics as an
    monkeypatch.setattr(an, "supabase", _DB(orders=MIXED, customers=[], messages=[]))
    s = an.get_business_stats(1)
    assert s["total_revenue"] == 15.0 and s["paid_orders"] == 2 and s["total_orders"] == 6


def test_dashboard_and_stats_endpoints_report_the_same_revenue(monkeypatch):
    from crud import analytics as an, orders as co
    db = _DB(orders=MIXED, customers=[], messages=[])
    monkeypatch.setattr(an, "supabase", db)
    monkeypatch.setattr(co, "supabase", db)
    assert an.get_business_stats(1)["total_revenue"] == co.get_dashboard_stats(1)["total_revenue"]


def test_admin_stats_revenue_excludes_unpaid_cancelled_and_refunded(monkeypatch):
    from crud import analytics as an
    biz = [{"id": 1, "name": "a", "is_active": True, "billing_status": "active"}]
    monkeypatch.setattr(an, "supabase", _DB(businesses=biz, orders=MIXED))
    a = an.get_admin_stats()
    assert a["total_revenue"] == 915.0 and a["gross_order_value"] == 1055.0 and a["total_orders"] == 7


def test_admin_stats_selects_the_columns_the_rule_needs():
    import inspect
    from crud import analytics as an
    assert 'select("id,total_price,status,payment_status")' in inspect.getsource(an.get_admin_stats)


# ── monthly report ────────────────────────────────────────────────────────────
import services.weekly_report_service as wr


def _window():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    return start, datetime(2026, 12, 1, tzinfo=timezone.utc)


def test_monthly_stats_exclude_cancelled_and_refunded_and_other_tenants(monkeypatch):
    import core.db as coredb
    rows = MIXED + [order(8, price=7, day="2026-10-20"), order(9, price=8, day="2026-12-01")]    # outside the window
    mem = [{"phone": "a", "business_id": 1, "created_at": "2026-11-03T00:00:00+00:00", "order_count": 3},
           {"phone": "b", "business_id": 1, "created_at": "2026-10-03T00:00:00+00:00", "order_count": 1},
           {"phone": "c", "business_id": 2, "created_at": "2026-11-03T00:00:00+00:00", "order_count": 9}]
    monkeypatch.setattr(coredb, "supabase", _DB(orders=rows, user_memory=mem))
    s = wr._get_weekly_stats(1, *_window())
    assert s["orders_7d"] == 3                      # orders 1, 2 and 6: cancelled/refunded, other tenant, other months excluded
    assert s["revenue_7d"] == 15.0
    assert s["new_customers_7d"] == 1 and s["repeat_customers"] == 1


def test_monthly_stats_read_every_order_not_just_the_first_1000(monkeypatch):
    import core.db as coredb
    rows = [order(i, price=1.0) for i in range(1, 2501)]
    monkeypatch.setattr(coredb, "supabase", _DB(orders=rows, user_memory=[]))
    s = wr._get_weekly_stats(1, *_window())
    assert s["orders_7d"] == 2500 and s["revenue_7d"] == 2500.0


def test_customer_counts_are_exact_beyond_1000_rows(monkeypatch):
    import core.db as coredb
    mem = [{"phone": str(i), "business_id": 1, "created_at": "2026-11-03T00:00:00+00:00", "order_count": 2}
           for i in range(1, 1801)]
    monkeypatch.setattr(coredb, "supabase", _DB(orders=[], user_memory=mem))
    s = wr._get_weekly_stats(1, *_window())
    assert s["new_customers_7d"] == 1800 and s["repeat_customers"] == 1800


def test_top_product_ignores_cancelled_orders(monkeypatch):
    import core.db as coredb
    rows = [order(1, items=[{"name": "Tea"}]), order(2, items=[{"name": "Tea"}]),
            order(3, status="cancelled", items=[{"name": "Cake"}]), order(4, status="cancelled", items=[{"name": "Cake"}]),
            order(5, status="cancelled", items=[{"name": "Cake"}])]
    monkeypatch.setattr(coredb, "supabase", _DB(orders=rows, user_memory=[]))
    assert wr._get_weekly_stats(1, *_window())["top_product"] == "Tea"


def test_a_failing_stats_read_returns_safe_defaults(monkeypatch):
    import core.db as coredb

    class Boom:
        def table(self, n): raise RuntimeError("db down")
    monkeypatch.setattr(coredb, "supabase", Boom())
    s = wr._get_weekly_stats(1, *_window())
    assert s["orders_7d"] == 0 and s["revenue_7d"] == 0.0 and s["top_product"] is None


def test_exact_count_falls_back_to_the_returned_rows():
    q = types.SimpleNamespace(execute=lambda: types.SimpleNamespace(data=[1, 2, 3], count=None))
    assert wr._exact_count(q) == 3
    q = types.SimpleNamespace(execute=lambda: types.SimpleNamespace(data=[1], count=42))
    assert wr._exact_count(q) == 42


# ── bounded inputs ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("asked,used", [(10, 10), (10**9, 100), (0, 1), (-5, 1), (100, 100)])
def test_top_customers_limit_is_clamped(monkeypatch, asked, used):
    from crud import analytics as an
    db = _DB(user_memory=[])
    monkeypatch.setattr(an, "supabase", db)
    an.get_top_customers(1, limit=asked)
    assert db.limits[-1] == used


def test_top_customers_is_tenant_scoped(monkeypatch):
    from crud import analytics as an
    mem = [{"business_id": 1, "phone": "a", "order_count": 5}, {"business_id": 2, "phone": "b", "order_count": 9}]
    monkeypatch.setattr(an, "supabase", _DB(user_memory=mem))
    assert [r["phone"] for r in an.get_top_customers(1)] == ["a"]


def test_analytics_by_id_route_still_denies_other_tenants():
    import routes.business_routes as br
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        br.get_analytics(5, user={"role": "business", "business_id": 6})
    assert e.value.status_code == 403
