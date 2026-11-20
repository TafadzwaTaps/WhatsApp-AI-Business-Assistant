"""Phase 6.2: country-aware wording. EcoCash stays (and stays the default); other methods name themselves."""
import os

import pytest

import services.invoice_service as inv
import services.payment_labels as pl

STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "static")


@pytest.mark.parametrize("order,label", [
    ({}, "EcoCash / Mobile Money"), (None, "EcoCash / Mobile Money"), ({"payment_method": None}, "EcoCash / Mobile Money"),
    ({"payment_method": ""}, "EcoCash / Mobile Money"), ({"payment_method": "ecocash"}, "EcoCash / Mobile Money"),
    ({"payment_method": "EcoCash"}, "EcoCash / Mobile Money"),
    ({"payment_method": "paypal"}, "PayPal"), ({"payment_method": "PayPal"}, "PayPal"), ({"payment_method": "cash"}, "Cash"),
    ({"payment_method": "bank_transfer"}, "Bank transfer"), ({"payment_method": "bank transfer"}, "Bank transfer"),
    ({"payment_method": "blik"}, "BLIK"), ({"payment_method": "mpesa"}, "Mpesa"), ({"payment_method": "mobile-money"}, "Mobile Money"),
])
def test_payment_method_labels(order, label):
    assert pl.payment_method_label(order) == label


def test_only_ecocash_or_unset_counts_as_ecocash():
    assert pl.is_ecocash({}) and pl.is_ecocash({"payment_method": "ecocash"}) and pl.is_ecocash({"payment_method": None})
    assert not any(pl.is_ecocash({"payment_method": m}) for m in ("paypal", "cash", "bank_transfer", "blik"))


def test_label_is_bounded_and_never_raises():
    assert len(pl.payment_method_label({"payment_method": "x" * 500})) <= 40
    assert pl.payment_method_label({"payment_method": 12345})


@pytest.fixture
def invoice(monkeypatch):
    monkeypatch.setattr(inv, "_get_business_payment", lambda b: {"business_name": "Shop", "ecocash_number": "0771234567", "ecocash_name": "Shop Ltd"})

    def make(method=None, **extra):
        order = {"id": 7, "items": [{"name": "Bread", "qty": 2, "price": 2.5}], "total_price": 5.0, "currency_symbol": "$",
                 "customer_phone": "111", "payment_status": "pending", **extra}
        if method is not None:
            order["payment_method"] = method
        return inv.generate_invoice(order, business_id=1)
    return make


def test_existing_ecocash_invoices_are_unchanged(invoice):
    for text in (invoice(), invoice("ecocash")):
        assert "Method     : EcoCash / Mobile Money" in text and "Pay to     : *0771234567*" in text


@pytest.mark.parametrize("method,label", [("paypal", "PayPal"), ("blik", "BLIK"), ("bank_transfer", "Bank transfer"), ("cash", "Cash")])
def test_other_methods_are_named_and_never_show_an_ecocash_number(invoice, method, label):
    text = invoice(method)
    assert f"Method     : {label}" in text
    assert "EcoCash" not in text and "0771234567" not in text and "Pay to" not in text
    assert "ORDER-7" in text                                       # the reference is still there


def test_pdf_invoice_names_the_real_method(monkeypatch, tmp_path):
    pytest.importorskip("reportlab")
    import services.pdf_invoice as pdf
    seen = []
    real = pdf.Paragraph

    def spy(text, *a, **k):
        seen.append(text)
        return real(text, *a, **k)
    monkeypatch.setattr(pdf, "Paragraph", spy)
    monkeypatch.setattr(pdf, "INVOICES_DIR", str(tmp_path), raising=False)
    base = {"id": 9, "items": "[]", "total_price": 5, "customer_phone": "111", "status": "pending"}
    for order, expected in (({**base, "payment_method": "paypal"}, "Pay via PayPal"), (base, "Pay via EcoCash / Mobile Money")):
        seen.clear()
        try:
            pdf.generate_pdf_invoice(order)
        except Exception:
            pass                                                    # file output location is not what is tested
        assert expected in seen


def test_help_bot_and_marketing_copy_are_not_zimbabwe_only():
    import services.support_assistant as sa
    import services.marketing_service as ms
    text = repr(sa.__dict__.get("KNOWLEDGE_BASE") or sa.__dict__.get("FAQ") or open(sa.__file__, encoding="utf8").read())
    assert "three payment methods" not in text and "bank transfer" in text and "BLIK" in text and "EcoCash" in text
    for t in ("restaurant", "general"):
        feats = " ".join(ms._get_features_for_type(t))
        assert "Accepts EcoCash, PayPal and cash" not in feats and "via EcoCash, PayPal or cash" not in feats


def test_inbox_fallback_label_and_dashboard_placeholder_are_neutral():
    assert "|| 'EcoCash')" not in open(os.path.join(STATIC, "inbox.js"), encoding="utf8").read()
    assert "Flavoury Foods (Pvt) Ltd" not in open(os.path.join(STATIC, "dashboard.html"), encoding="utf8").read()


def test_ecocash_capability_is_still_there():
    html = open(os.path.join(STATIC, "dashboard.html"), encoding="utf8").read()
    assert "set-ecocash-name" in html and "set-ecocash-number" in html
    import services.payment_service as ps
    assert "ecocash" in open(ps.__file__, encoding="utf8").read().lower()
