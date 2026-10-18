"""
1. Transient network errors (httpx.ReadError / Errno 11) on GET are retried, writes are not.
2. Sessions with no business (superadmin: business_id None) no longer send
   "business_id=eq.null" queries to Supabase (HTTP 400 + error spam).
"""
import httpx
import pytest

from core.transport_retry import install


def _cls(fail_times, method="GET", exc=None):
    exc = exc or httpx.ReadError("[Errno 11] Resource temporarily unavailable")

    class Cfg:
        http_method = method
        path = "/products"
        calls = 0

        def send(self, headers):
            Cfg.calls += 1
            if Cfg.calls <= fail_times:
                raise exc
            return "ok"
    return Cfg


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("core.transport_retry.time.sleep", lambda s: None)


def test_get_retried_then_succeeds():
    C = _cls(2); assert install(C)
    assert C().send({}) == "ok" and C.calls == 3


def test_get_gives_up_after_bounded_retries():
    C = _cls(99); install(C)
    with pytest.raises(httpx.ReadError):
        C().send({})
    assert C.calls == 3            # 1 try + 2 retries, not infinite


def test_writes_are_never_retried():
    C = _cls(1, method="POST"); install(C)
    with pytest.raises(httpx.ReadError):
        C().send({})
    assert C.calls == 1


def test_non_transient_errors_not_swallowed_and_install_idempotent():
    C = _cls(1, exc=ValueError("bad")); install(C)
    assert install(C) is False     # already wrapped
    with pytest.raises(ValueError):
        C().send({})
    assert C.calls == 1


class _Boom:
    """Any DB access means a query was attempted."""
    def __getattr__(self, n):
        raise AssertionError("DB must not be queried when business_id is None")


@pytest.fixture
def no_db(monkeypatch):
    import sys
    monkeypatch.setattr(sys.modules["core.db"], "supabase", _Boom(), raising=False)
    import crud.products, crud.businesses, crud.analytics
    for m in (crud.products, crud.businesses, crud.analytics):
        monkeypatch.setattr(m, "supabase", _Boom(), raising=False)


def test_crud_functions_skip_db_when_no_business(no_db):
    import crud
    assert crud.get_products(None) == []
    assert crud.get_business_by_id(None) is None
    assert crud.get_low_stock_products(None) == []
    assert crud.get_stale_payment_orders(None) == []


def test_referral_helpers_skip_db_when_no_business(no_db):
    from services import growth_service as g
    assert g.get_or_create_referral_code(None) == ""
    stats = g.get_referral_stats(None)
    assert stats["referral_code"] == "" and stats["total_referrals"] == 0 and stats["can_withdraw"] is False


def test_onboarding_status_for_no_business(no_db):
    from routes.ux_routes import onboarding_status
    out = onboarding_status(user={"business_id": None, "role": "superadmin"})
    assert out["completed"] == 0 and out["show_wizard"] is False and out["steps"][1] is False
