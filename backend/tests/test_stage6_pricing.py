"""Stage 6: regional pricing display. USD / Stripe stay authoritative; local currency is a labelled estimate only."""
import json
import os
import re
import subprocess

import pytest

from services import pricing_display as pd

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
RATES = json.dumps({"as_of": "2026-11-01", "rates": {"GBP": 0.8, "EUR": 0.9, "JPY": 150, "PLN": 4.0, "ZAR": 18.0}})
PRICES = {"starter_monthly": 4.99, "starter_annual": 48, "growth_monthly": 12, "growth_annual": 115}


@pytest.fixture
def rates(monkeypatch):
    monkeypatch.setenv("PRICING_DISPLAY_RATES", RATES)


def endpoint(country=""):
    from routes_saas.billing_routes import billing_pricing
    return billing_pricing(country=country, request=None)


# ── authoritative prices are untouched ────────────────────────────────────────
@pytest.mark.parametrize("cc", ["", "GB", "US", "ZW", "ZA", "PL", "JP", "XX"])
def test_usd_amounts_and_currency_are_unchanged_for_every_country(monkeypatch, rates, cc):
    out = endpoint(cc)
    assert out["currency"] == "USD" and out["billing_currency"] == "USD"
    for k, v in PRICES.items():
        assert out[k] == v


def test_without_configured_rates_no_estimate_is_returned_and_nothing_is_invented(monkeypatch):
    monkeypatch.delenv("PRICING_DISPLAY_RATES", raising=False)
    assert endpoint("GB")["local_estimate"] is None and endpoint("ZA")["local_estimate"] is None


def test_estimate_is_computed_from_the_usd_price_and_labelled(rates):
    est = endpoint("GB")["local_estimate"]
    assert est["currency"] == "GBP" and est["estimate"] is True and est["rates_as_of"] == "2026-11-01"
    assert est["prices"] == {"starter_monthly": 3.99, "starter_annual": 38.4, "growth_monthly": 9.6, "growth_annual": 92.0}
    assert "Estimate only" in est["disclaimer"] and "USD" in est["disclaimer"]


def test_estimate_does_not_change_the_usd_amounts_it_is_based_on(rates):
    out = endpoint("GB")
    assert out["starter_monthly"] == 4.99 and out["growth_annual"] == 115


@pytest.mark.parametrize("cc", ["US", "ZW", "", "XX", "KE"])
def test_no_estimate_for_usd_markets_unknown_countries_or_currencies_without_a_rate(rates, cc):
    assert endpoint(cc)["local_estimate"] is None


def test_zero_decimal_currencies_are_rounded_to_whole_units(rates):
    e = pd.local_estimate("JP", PRICES)
    assert e["currency"] == "JPY" and e["prices"]["starter_monthly"] == 748 and isinstance(e["prices"]["starter_monthly"], (int, float))
    assert e["prices"]["starter_monthly"] == round(e["prices"]["starter_monthly"])


@pytest.mark.parametrize("bad", ["not json", "[]", '{"rates": {"GBP": 0.8}}', '{"as_of": "yesterday", "rates": {"GBP": 0.8}}',
                                 '{"as_of": "2026-11-01", "rates": {"GBP": -1}}', '{"as_of": "2026-11-01", "rates": {"GBP": "0.8"}}',
                                 '{"as_of": "2026-11-01", "rates": {"GBP": true}}', '{"as_of": "2026-11-01", "rates": {"GBP": 0}}', ""])
def test_malformed_rate_settings_give_no_estimate_and_never_break_the_page(monkeypatch, bad):
    monkeypatch.setenv("PRICING_DISPLAY_RATES", bad)
    out = endpoint("GB")
    assert out["local_estimate"] is None and out["starter_monthly"] == 4.99


def test_a_failure_in_the_estimate_code_never_breaks_the_pricing_endpoint(monkeypatch, rates):
    monkeypatch.setattr(pd, "local_estimate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = endpoint("GB")
    assert out["local_estimate"] is None and out["starter_monthly"] == 4.99


def test_country_header_detection_still_works(monkeypatch, rates):
    import types
    from routes_saas.billing_routes import billing_pricing
    req = types.SimpleNamespace(headers={"CF-IPCountry": "gb"})
    assert billing_pricing(country="", request=req)["local_estimate"]["currency"] == "GBP"


def test_plan_definitions_and_checkout_inputs_are_unchanged():
    from billing import stripe_service as ss
    for region in ss.PRICING_REGIONS.values():
        assert (region["starter_monthly"], region["starter_annual"], region["growth_monthly"], region["growth_annual"]) == (4.99, 48, 12, 115)


def test_billing_code_never_uses_display_estimates():
    base = os.path.join(os.path.dirname(__file__), "..")
    for rel in ("billing/stripe_service.py", "routes_saas/billing_routes.py"):
        src = open(os.path.join(base, rel), encoding="utf8").read()
        uses = [l for l in src.splitlines() if re.match(r"\s*(from|import)\s+services\.pricing_display", l)]
        if rel.endswith("stripe_service.py"):
            assert uses == []
        else:
            assert len(uses) == 1 and "BILLING_CURRENCY, local_estimate" in uses[0]
    # the checkout route computes nothing from the estimate
    src = open(os.path.join(base, "routes_saas", "billing_routes.py"), encoding="utf8").read()
    start = src.index('@router.post("/billing/checkout")')
    end = src.index("@router.", start + 10)                # the next route decorator ends the checkout function
    checkout_src = src[start:end]
    assert "billing_checkout" in checkout_src and "local_estimate" not in checkout_src and "pricing_display" not in checkout_src


# ── front end ─────────────────────────────────────────────────────────────────
def _page(name):
    return open(os.path.join(ROOT, "static", name), encoding="utf8").read()


def test_checkout_request_never_carries_an_estimate_or_a_converted_amount():
    html = _page("pricing.html")
    body = html[html.index("async function checkout"):]
    body = body[:body.index("catch(e)")]
    assert "local_estimate" not in body and "est." not in body and "Estimate" not in body
    assert re.search(r"JSON\.stringify\(\{\s*tier:\s*plan,\s*billing_period:\s*period,\s*country_code:", body)


def test_pricing_page_labels_estimates_and_states_the_charged_currency():
    html = _page("pricing.html")
    assert "(estimate)" in html and "est-disclaimer" in html and "USD per month" in html
    assert "Regional pricing applied automatically" not in html and "uses regional pricing to make" not in html
    assert "charged in USD" in html
    assert "textContent = String(est.disclaimer" in html            # backend text is never injected as HTML


def test_estimate_rendering_only_trusts_well_formed_data():
    html = _page("pricing.html")
    assert "/^[A-Z]{3}$/.test(String(est.currency))" in html and "Number.isFinite(est.prices[k])" in html
    assert "Intl.NumberFormat" in html


def test_landing_keeps_static_usd_prices_and_seo_markup():
    html = _page("landing.html")
    assert '"price": "4.99"' in html and '"priceCurrency": "USD"' in html
    assert "<span>$</span>4.99" in html and "<span>$</span>12" in html and html.count("USD per month") >= 2


def test_pricing_page_inline_scripts_parse():
    html = _page("pricing.html")
    js = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    path = os.path.join(os.environ.get("TEMP", "/tmp"), "wazi_pricing_check.js")
    open(path, "w", encoding="utf8").write(js)
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
    except FileNotFoundError:
        pytest.skip("node not installed")
    assert r.returncode == 0, r.stderr
