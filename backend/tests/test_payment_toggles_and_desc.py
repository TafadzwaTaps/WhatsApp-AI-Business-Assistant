"""Per-business payment menu, owner toggles, product description in text menu."""
import pytest
from services.payment_service import available_methods


def _biz(**kw):
    return {"business_id": 1, **kw}


def test_platform_env_does_not_leak_into_business_menu(monkeypatch):
    monkeypatch.setenv("ECOCASH_NUMBER", "+263700000000")
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_EMAIL", "platform@x.com")
    assert available_methods(_biz()) == ["cash"]


def test_configured_methods_listed_in_order():
    m = available_methods(_biz(ecocash_number="+263771", bank_transfer_details="IBAN", blik_number="+48500", paypal_email="a@b.com"))
    assert m == ["ecocash", "banktransfer", "blik", "paypal", "cash"]


def test_toggle_hides_method():
    m = available_methods(_biz(ecocash_number="+263771", blik_number="+48500",
                               payment_toggles={"ecocash": False}))
    assert m == ["blik", "cash"]


def test_never_empty_menu():
    assert available_methods(_biz(payment_toggles={"cash": False})) == ["cash"]
    assert available_methods(_biz(blik_number="+48500", payment_toggles={"blik": False, "cash": False})) == ["cash"]


def test_legacy_callers_without_business_id_unchanged(monkeypatch):
    monkeypatch.setenv("ECOCASH_NUMBER", "+263700000000")
    assert "ecocash" in available_methods({})


def test_payment_menu_text_respects_toggles(monkeypatch):
    import crud
    from services import _ai_products as ap
    monkeypatch.setattr(crud, "get_business_payment_settings",
                        lambda bid: {"ecocash_number": "+263771", "blik_number": "+48500",
                                     "payment_toggles": {"ecocash": False}}, raising=False)
    out = ap._build_payment_menu([{"name": "cut", "qty": 1, "price": 40}], 1, "zł", True)
    assert "EcoCash" not in out and "BLIK" in out and "Cash" in out
    assert "*ecocash*" not in out
