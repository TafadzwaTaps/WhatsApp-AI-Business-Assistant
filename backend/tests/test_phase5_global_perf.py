"""Phase 5 stages 1-3: currency/wording (GL), narrow+paged reads (PF1/4/5), business cache (PF3)."""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf8")


# ── Globalisation ─────────────────────────────────────────────────────────────
def test_order_currency_sym_prefers_order_then_business_then_dollar(monkeypatch):
    import crud
    from routes import admin_routes as ar
    assert ar._order_currency_sym({"currency_symbol": "zł"}, 1) == "zł"
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"currency_symbol": "€"})
    assert ar._order_currency_sym({}, 1) == "€"
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {})
    assert ar._order_currency_sym({}, 1) == "$"

    def boom(b):
        raise RuntimeError("db")
    monkeypatch.setattr(crud, "get_business_by_id", boom)
    assert ar._order_currency_sym({}, 1) == "$"


def test_customer_messages_no_longer_hardcode_dollar_or_harare():
    admin = _src("routes/admin_routes.py")
    assert "Amount: ${total" not in admin and "— ${total" not in admin
    ai = _src("services/ai.py")
    assert "Amount: ${float(recent" not in ai
    assert "Harare Street" not in ai
    assert "Total: *${total" not in _src("crud/orders.py")


def test_campaign_personalise_uses_given_symbol_default_dollar():
    from services.campaign_service import personalise
    mem = {"customer_name": "A", "order_count": 2, "total_spent": 12.5}
    assert personalise("{spent}", mem, "B") == "$12.50"
    assert personalise("{spent}", mem, "B", currency_symbol="zł") == "zł12.50"


# ── Paged reads ───────────────────────────────────────────────────────────────
class _Paged:
    def __init__(self, rows):
        self.rows, self.lo, self.hi = rows, 0, None
        self.calls = 0

    def range(self, lo, hi):
        self.lo, self.hi = lo, hi
        return self

    def execute(self):
        self.calls += 1

        class R:
            pass
        r = R()
        r.data = self.rows[self.lo:self.hi + 1] if self.hi is not None else self.rows
        return r


def test_fetch_all_pages_past_the_row_cap():
    from crud._helpers import fetch_all
    rows = [{"id": i} for i in range(2500)]
    out = fetch_all(lambda: _Paged(rows), page=1000)
    assert len(out) == 2500 and out[-1]["id"] == 2499


def test_fetch_all_exact_multiple_and_empty():
    from crud._helpers import fetch_all
    assert len(fetch_all(lambda: _Paged([{"id": i} for i in range(2000)]), page=1000)) == 2000
    assert fetch_all(lambda: _Paged([]), page=1000) == []


def test_fetch_all_falls_back_without_range():
    from crud._helpers import fetch_all

    class NoRange:
        def execute(self):
            class R:
                data = [{"id": 1}]
            return R()
    assert fetch_all(lambda: NoRange()) == [{"id": 1}]


class _TableFake:
    """Minimal filter/order/range-aware fake of the supabase client for stats tests."""
    def __init__(self, tables):
        self.tables, self._t, self._order, self._rng, self._eq = tables, None, None, None, {}

    def table(self, name):
        f = _TableFake(self.tables)
        f._t = name
        return f

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def range(self, lo, hi):
        self._rng = (lo, hi)
        return self

    def execute(self):
        rows = [r for r in self.tables.get(self._t, []) if all(r.get(c) == v for c, v in self._eq.items())]
        if self._order:
            rows = sorted(rows, key=lambda r: r[self._order[0]], reverse=self._order[1])
        if self._rng:
            rows = rows[self._rng[0]:self._rng[1] + 1]

        class R:
            pass
        r = R()
        r.data = rows
        return r


def test_dashboard_stats_totals_and_recent_orders_beyond_1000_rows(monkeypatch):
    from crud import orders as co
    orders = [{"id": i, "business_id": 1, "total_price": 2.0, "status": "pending",
               "created_at": "2026-11-01T00:00:00", "customer_phone": "1", "product_name": "tea"}
              for i in range(1, 2501)]
    orders.append({"id": 9999, "business_id": 2, "total_price": 500.0, "status": "pending",
                   "created_at": "2026-11-01T00:00:00"})                       # other tenant
    custs = [{"id": i, "business_id": 1, "created_at": "2026-11-01T00:00:00"} for i in range(1, 1201)]
    monkeypatch.setattr(co, "supabase", _TableFake({"orders": orders, "customers": custs}))
    out = co.get_dashboard_stats(1)
    assert out["total_orders"] == 2500 and out["total_revenue"] == 5000.0
    assert out["total_customers"] == 1200
    assert [o["id"] for o in out["recent_orders"]][:3] == [2500, 2499, 2498]


def test_business_and_admin_stats_beyond_1000_rows(monkeypatch):
    from crud import analytics as an
    biz = [{"id": i, "name": f"b{i}", "is_active": True, "billing_status": "active"} for i in range(1, 6)]
    orders = [{"id": i, "business_id": 1, "total_price": 1.0, "payment_status": "paid",
               "status": "confirmed"} for i in range(1, 1501)]
    msgs = [{"id": i, "business_id": 1, "direction": "outgoing", "sender_type": "ai"} for i in range(1, 1101)]
    custs = [{"id": i, "business_id": 1} for i in range(1, 1051)]
    fake = _TableFake({"businesses": biz, "orders": orders, "messages": msgs, "customers": custs})
    monkeypatch.setattr(an, "supabase", fake)
    s = an.get_business_stats(1)
    assert s["total_orders"] == 1500 and s["total_revenue"] == 1500.0
    assert s["active_customers"] == 1050 and s["ai_handled"] == 1100
    a = an.get_admin_stats()
    assert a["businesses"] == 5 and a["total_orders"] == 1500 and a["total_revenue"] == 1500.0


def test_admin_stats_falls_back_when_narrow_select_fails(monkeypatch):
    from crud import analytics as an

    class Boom(_TableFake):
        def table(self, name):
            f = Boom(self.tables)
            f._t = name
            return f

        def execute(self):
            if self._t == "businesses":
                raise RuntimeError("column does not exist")
            return super().execute()
    monkeypatch.setattr(an, "supabase", Boom({"orders": []}))
    monkeypatch.setattr(an, "get_all_businesses", lambda: [{"id": 1, "is_active": True}])
    assert an.get_admin_stats()["businesses"] == 1


def test_onboarding_checks_read_one_row_not_whole_tables():
    from routes import ux_routes
    calls = []

    class Q:
        def __init__(self, t):
            self.t = t

        def select(self, cols):
            calls.append(("select", self.t, cols))
            return self

        def eq(self, *a):
            return self

        def limit(self, n):
            calls.append(("limit", self.t, n))
            return self

        def execute(self):
            class R:
                data = [{"id": 1}]
            return R()

    class DB:
        def table(self, t):
            return Q(t)
    import core.db as db
    old = db.supabase
    db.supabase = DB()
    try:
        assert ux_routes._has_rows("products", 7) is True
    finally:
        db.supabase = old
    assert ("select", "products", "id") in calls and ("limit", "products", 1) in calls
    src = _src("routes/ux_routes.py")
    i = src.index("def onboarding_status")
    assert "crud.get_products(" not in src[i:i + 3000] and "crud.get_orders(" not in src[i:i + 3000]


# ── Business lookup cache ─────────────────────────────────────────────────────
def _biz_db(monkeypatch, rows):
    from crud import businesses as cb
    cb._biz_cache_clear()
    n = {"reads": 0}

    class Q:
        def select(self, *a):
            return self

        def eq(self, *a):
            return self

        def limit(self, *a):
            return self

        def execute(self):
            n["reads"] += 1

            class R:
                data = rows
            return R()

    class DB:
        def table(self, t):
            return Q()
    monkeypatch.setattr(cb, "supabase", DB())
    return cb, n


def test_business_lookup_cached_and_copied(monkeypatch):
    cb, n = _biz_db(monkeypatch, [{"id": 1, "name": "A"}])
    a = cb.get_business_by_phone_id("p1")
    a["name"] = "mutated"                       # the webhook mutates what it receives
    b = cb.get_business_by_phone_id("p1")
    assert n["reads"] == 1 and b["name"] == "A"


def test_business_miss_is_not_cached(monkeypatch):
    cb, n = _biz_db(monkeypatch, [])
    assert cb.get_business_by_phone_id("new") is None
    assert cb.get_business_by_phone_id("new") is None
    assert n["reads"] == 2


def test_business_cache_expires_and_is_cleared_by_update(monkeypatch):
    cb, n = _biz_db(monkeypatch, [{"id": 1}])
    cb.get_business_by_phone_id("p")
    monkeypatch.setattr(cb, "_BIZ_PHONE_TTL", -1.0)
    cb.get_business_by_phone_id("p")
    assert n["reads"] == 2
    monkeypatch.setattr(cb, "_BIZ_PHONE_TTL", 60.0)
    cb.get_business_by_phone_id("p")
    assert n["reads"] == 2                      # served from cache
    try:
        cb.update_business(1, {})
    except Exception:
        pass
    reads = n["reads"]                          # (update itself may issue queries)
    cb.get_business_by_phone_id("p")
    assert n["reads"] == reads + 1              # cache was cleared: one fresh read


def test_heavy_async_routes_use_threadpool():
    src = _src("routes/business_routes.py")
    assert "run_in_threadpool(\n        CampaignService.run" in src.replace("\r\n", "\n")
    assert src.count("run_in_threadpool(lambda") >= 2


# ── Review follow-ups ─────────────────────────────────────────────────────────
def test_approve_payment_route_is_the_real_handler():
    """Regression: a helper once got inserted between the decorator and the handler."""
    from routes import admin_routes as ar
    hits = [r for r in ar.router.routes
            if getattr(r, "path", "").endswith("/orders/{order_id}/approve-payment")]
    assert len(hits) == 1
    assert hits[0].endpoint.__name__ == "admin_approve_payment"
    assert "POST" in hits[0].methods


def test_refund_route_still_points_at_refund_handler():
    from routes import admin_routes as ar
    hits = [r for r in ar.router.routes if getattr(r, "path", "").endswith("/refund")]
    for r in hits:
        assert r.endpoint.__name__ != "_order_currency_sym"


def test_no_route_is_registered_on_a_private_helper():
    import main
    bad = [r.path for r in main.app.routes
           if getattr(getattr(r, "endpoint", None), "__name__", "").startswith("_order_currency")]
    assert bad == []


def test_campaign_callers_pass_the_business_symbol():
    src = _src("services/campaign_service.py")
    assert src.count('business.get("currency_symbol") or "$")') == 2


def test_business_cache_is_deep_copied(monkeypatch):
    cb, n = _biz_db(monkeypatch, [{"id": 1, "features_json": {"a": 1}}])
    first = cb.get_business_by_phone_id("p")
    first["features_json"]["a"] = 99
    assert cb.get_business_by_phone_id("p")["features_json"]["a"] == 1


def test_billing_and_admin_writes_clear_the_business_cache():
    assert "_biz_cache_clear" in _src("billing/stripe_service.py")
    assert "_biz_cache_clear" in _src("routes/saas_admin_routes.py")
