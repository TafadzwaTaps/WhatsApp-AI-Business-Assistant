"""
tests/test_products_customers_crosslink.py

UI/UX audit — Phase 9 recommendation ("Products -> Customers: who bought
this?"), originally flagged as needing a new backend query and left for a
follow-up. Covers the new crud.get_customers_for_product() aggregation and
the GET /products/{id}/customers route built on top of it.
"""

import pytest
from fastapi.testclient import TestClient

import crud
from crud.orders import get_customers_for_product


# ── crud.get_customers_for_product() ──────────────────────────────────────

def test_aggregates_orders_by_customer_for_the_matched_product(fake_supabase):
    fake_supabase.results["orders"] = [
        {"customer_phone": "+15550001", "total_price": 10.0, "created_at": "2026-01-01T00:00:00Z"},
        {"customer_phone": "+15550001", "total_price": 12.5, "created_at": "2026-02-01T00:00:00Z"},
        {"customer_phone": "+15550002", "total_price": 8.0,  "created_at": "2026-01-15T00:00:00Z"},
    ]

    rows = get_customers_for_product(business_id=1, product_name="Chicken Wrap")

    by_phone = {r["phone"]: r for r in rows}
    assert by_phone["+15550001"]["order_count"] == 2
    assert by_phone["+15550001"]["total_spent"] == pytest.approx(22.5)
    assert by_phone["+15550001"]["last_order_at"] == "2026-02-01T00:00:00Z"
    assert by_phone["+15550002"]["order_count"] == 1
    # Busiest customer sorts first.
    assert rows[0]["phone"] == "+15550001"


def test_empty_product_name_returns_empty_list_without_querying(fake_supabase):
    # No results seeded for "orders" — if this ever queried, it'd get [].
    # The real assertion is that it doesn't blow up on a falsy name.
    assert get_customers_for_product(business_id=1, product_name="") == []
    assert get_customers_for_product(business_id=1, product_name=None) == []


def test_rows_missing_customer_phone_are_skipped(fake_supabase):
    fake_supabase.results["orders"] = [
        {"customer_phone": None, "total_price": 5.0, "created_at": "2026-01-01T00:00:00Z"},
        {"customer_phone": "+15550003", "total_price": 5.0, "created_at": "2026-01-01T00:00:00Z"},
    ]
    rows = get_customers_for_product(business_id=1, product_name="Soda")
    assert len(rows) == 1
    assert rows[0]["phone"] == "+15550003"


def test_query_failure_returns_empty_list_not_an_exception(fake_supabase, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("db unreachable")
    monkeypatch.setattr(fake_supabase, "execute", _boom)
    assert get_customers_for_product(business_id=1, product_name="Anything") == []


# ── GET /products/{id}/customers ──────────────────────────────────────────

@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _fake_dep():
    return {"business_id": 1}


def test_route_404s_for_unknown_product(monkeypatch, client):
    import routes.business_routes as br
    monkeypatch.setattr(crud, "get_product_by_id", lambda pid, bid: None)

    main_app = client.app
    main_app.dependency_overrides[br.require_business] = _fake_dep
    resp = client.get("/products/999/customers")
    main_app.dependency_overrides.clear()

    assert resp.status_code == 404


def test_route_returns_aggregated_customers_with_crm_enrichment(monkeypatch, client):
    import routes.business_routes as br
    monkeypatch.setattr(crud, "get_product_by_id", lambda pid, bid: {"id": pid, "name": "Chicken Wrap"})
    monkeypatch.setattr(
        crud, "get_customers_for_product",
        lambda bid, name: [{"phone": "+15550001", "order_count": 2, "total_spent": 22.5, "last_order_at": "2026-02-01T00:00:00Z"}],
    )
    monkeypatch.setattr(
        crud, "get_customers_for_business",
        lambda bid: [{"phone": "+15550001", "customer_name": "Amai T", "last_seen": "2026-02-01T00:00:00Z"}],
    )

    main_app = client.app
    main_app.dependency_overrides[br.require_business] = _fake_dep
    resp = client.get("/products/42/customers")
    main_app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["product_id"] == 42
    assert body["product_name"] == "Chicken Wrap"
    assert body["customers"][0]["phone"] == "+15550001"
    assert body["customers"][0]["customer_name"] == "Amai T"
