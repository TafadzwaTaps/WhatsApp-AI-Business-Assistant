"""Stage 5b: business-specific phone and address handling. No +263 (or any other country) assumption."""
import os
import types

import pytest

from services import phone_utils as pu
from services import address_utils as au

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


# ── normalisation ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,dial,expected", [
    ("+44 7700 900123", None, "447700900123"),               # already international: no default needed
    ("+44 7700 900123", "263", "447700900123"),              # an explicit + wins over the default
    ("0044 7700 900123", None, "447700900123"),              # 00 international prefix
    ("07700 900123", "44", "447700900123"),                  # national trunk 0 replaced by the business's code
    ("(0)7700-900123", "44", "447700900123"),
    ("+44 (0) 7700 900123", None, "447700900123"),           # trunk zero in brackets after +44
    ("771000111", "263", "263771000111"),                    # bare national number
    ("0771 000 111", "263", "263771000111"),
    ("263771000111", "263", "263771000111"),                 # already carries the code: unchanged
    ("2025550123", "1", "12025550123"),                      # US 10-digit
    ("12025550123", "1", "12025550123"),
    ("447700900123", None, "447700900123"),                  # WhatsApp id style, no default: kept
])
def test_normalise_phone(raw, dial, expected):
    assert pu.normalise_phone(raw, dial) == expected


@pytest.mark.parametrize("raw", ["", None, "abc", "123", "771000111", "07700900123", "+12", "+" + "9" * 16])
def test_numbers_that_cannot_be_made_international_with_confidence_are_not_guessed(raw):
    assert pu.normalise_phone(raw, None) is None


def test_no_zimbabwe_assumption_anywhere():
    assert pu.normalise_phone("0771000111", None) is None               # no default: never prepends +263
    assert pu.business_dialing_code({}) is None
    assert pu.business_dialing_code({"features_json": {}}) is None
    assert pu.normalise_phone("07700900123", "44") == "447700900123"    # a UK business is not given 263


def test_default_dialing_code_resolution_order():
    assert pu.business_dialing_code({"features_json": {"country": "GB"}}) == "44"
    assert pu.business_dialing_code({"features_json": {"country": "gb"}}) == "44"
    assert pu.business_dialing_code({"features_json": {"country": "GB", "default_dialing_code": "+263"}}) == "263"
    assert pu.business_dialing_code({"features_json": {"country": "ZW"}}) == "263"
    assert pu.business_dialing_code({"features_json": {"country": "OTHER"}}) is None
    assert pu.business_dialing_code({"features_json": {"default_dialing_code": "abc"}}) is None
    assert pu.business_dialing_code({"features_json": "not a dict"}) is None
    assert pu.business_dialing_code(None) is None


@pytest.mark.parametrize("v,out", [("+263", "263"), ("00263", "263"), (" 44 ", "44"), ("0", None), ("", None),
                                   ("12345", None), ("+", None), ("abc", None), (None, None), ("1", "1")])
def test_clean_dialing_code(v, out):
    assert pu.clean_dialing_code(v) == out


def test_comparison_and_display_helpers():
    assert pu.same_number("+263 77 100 0111", "263771000111") and not pu.same_number("", "")
    assert not pu.same_number("263771000111", "263771000112")
    assert pu.display_number("447700900123") == "+447700900123" and pu.display_number("junk") == "junk"


def test_every_country_code_in_the_table_is_valid():
    for iso, code in pu.COUNTRY_DIAL.items():
        assert len(iso) == 2 and pu.clean_dialing_code(code) == code


# ── addresses ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("country,code,out", [
    ("US", "90210", "90210"), ("US", "90210-1234", "90210-1234"), ("US", "9021", None),
    ("GB", "sw1a1aa", "SW1A 1AA"), ("GB", "SW1A 1AA", "SW1A 1AA"), ("GB", "12345", None),
    ("CA", "k1a0b1", "K1A 0B1"), ("PL", "00-950", "00-950"), ("PL", "00950", None),
    ("ZA", "2196", "2196"), ("IE", "d02 af30", "D02 AF30"),
    ("ZW", "anything goes", "anything goes"),                        # no postcode system: accepted as typed
    ("XX", "12 345", "12 345"),                                      # unknown country: never rejected
    ("US", "", None), ("US", None, None),
])
def test_postal_code_normalisation_is_country_aware(country, code, out):
    assert au.normalise_postal_code(code, country) == out


def test_postal_code_is_only_required_where_the_country_has_one():
    assert au.requires_postal_code("GB") and au.requires_postal_code("US") and au.requires_postal_code("pl")
    assert not au.requires_postal_code("ZW") and not au.requires_postal_code("") and not au.requires_postal_code("OTHER")
    assert not au.requires_postal_code(None)


def test_labels_and_prompts_follow_the_country():
    assert au.postal_label("US") == "ZIP code" and au.postal_label("GB") == "postcode" and au.postal_label("") == "postal code"
    assert "ZIP code" in au.address_hint("US") and "postcode" in au.address_hint("GB")
    assert "ZIP" not in au.address_hint("ZW") and "postal" not in au.address_hint("ZW")
    assert "ZIP code" in au.address_prompt("US") and "postal" not in au.address_prompt("ZW")


def test_tidy_address_only_touches_whitespace():
    raw = "  12  Main   Street \r\n\r\n  Springfield\t IL  "
    assert au.tidy_address(raw) == "12 Main Street\nSpringfield IL"
    assert au.tidy_address("Flat 3B, O'Connell St.") == "Flat 3B, O'Connell St."
    assert au.tidy_address(None) == ""


# ── stored numbers are never rewritten; dedupe and filters compare digits ─────
class _Q:
    def __init__(self, rows):
        self.rows = rows
    def select(self, *a, **k): return self
    def eq(self, *a, **k): return self
    def execute(self): return types.SimpleNamespace(data=self.rows)


class _DB:
    def __init__(self, rows): self.rows = rows
    def table(self, name): return _Q(self.rows)


@pytest.mark.parametrize("modname", ["crud.crud", "crud.customers"])
def test_customer_phone_dedupe_treats_formats_as_one_customer_and_keeps_the_stored_form(monkeypatch, modname):
    import importlib
    mod = importlib.import_module(modname)
    rows = [{"phone": "263771000111"}, {"phone": "+263 77 100 0111"}, {"phone": "447700900123"},
            {"phone": "263771000111"}, {"phone": None}, {"phone": "+44-7700-900123"}]
    monkeypatch.setattr(mod, "supabase", _DB(rows))
    out = mod.get_all_customer_phones(1)
    assert out == ["263771000111", "447700900123"]                  # first stored form kept, nothing rewritten


def test_broadcast_filter_matches_numbers_ignoring_formatting():
    import inspect
    import routes.business_routes as br
    src = inspect.getsource(br)
    assert "_digits(p) in filter_set" in src and 'lstrip("+")' not in src[src.index("phone_filter"):src.index("phone_filter") + 800]


def test_custom_audience_uses_the_business_code_and_falls_back_to_the_typed_number(monkeypatch):
    import crud
    import services.campaign_service as cs
    seen = []
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"features_json": {"country": "GB"}})
    monkeypatch.setattr(crud, "get_user_memory", lambda phone, b: seen.append(phone) or {})
    out = cs.resolve_audience(1, "custom", ["07700 900123", "+263 771 000 111", "garbage"])
    assert [r["phone"] for r in out] == ["447700900123", "263771000111", "garbage"]
    assert seen == ["447700900123", "263771000111", "garbage"]


def test_custom_audience_without_any_country_never_prepends_a_code(monkeypatch):
    import crud
    import services.campaign_service as cs
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"features_json": {}})
    monkeypatch.setattr(crud, "get_user_memory", lambda phone, b: {})
    out = cs.resolve_audience(1, "custom", ["0771000111", "263771000111"])
    assert [r["phone"] for r in out] == ["0771000111", "263771000111"]


# ── profile settings ──────────────────────────────────────────────────────────
@pytest.fixture
def profile(monkeypatch):
    import crud
    import routes.business_routes as br
    store = {"fj": {"country": "GB"}}
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "features_json": dict(store["fj"])})

    def update(bid, d):
        store["fj"] = d.dict()["features_json"]
        return True
    monkeypatch.setattr(crud, "update_business", update)
    return types.SimpleNamespace(store=store, br=br)


def _patch(profile, **kw):
    return profile.br.update_user_profile(profile.br.UserProfileUpdate(**kw), user={"business_id": 1})


def test_profile_saves_a_valid_default_dialing_code(profile):
    _patch(profile, default_dialing_code="+263")
    assert profile.store["fj"]["default_dialing_code"] == "263"


@pytest.mark.parametrize("bad", ["abc", "0", "12345", "+"])
def test_profile_rejects_an_invalid_dialing_code(profile, bad):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _patch(profile, default_dialing_code=bad)
    assert e.value.status_code == 422 and "default_dialing_code" not in profile.store["fj"]


def test_profile_empty_dialing_code_clears_it(profile):
    profile.store["fj"]["default_dialing_code"] = "263"
    _patch(profile, default_dialing_code="")
    assert profile.store["fj"]["default_dialing_code"] == ""


def test_profile_phone_is_stored_internationally_using_the_country_or_the_override(profile):
    _patch(profile, user_phone="07700 900123")
    assert profile.store["fj"]["user_phone"] == "+447700900123"             # GB business
    _patch(profile, user_phone="0771 000 111", default_dialing_code="263")
    assert profile.store["fj"]["user_phone"] == "+263771000111"             # explicit override beats country


def test_profile_phone_that_cannot_be_normalised_is_kept_as_typed(profile):
    profile.store["fj"] = {}                                                  # no country, no code
    _patch(profile, user_phone="0771 000 111")
    assert profile.store["fj"]["user_phone"] == "0771 000 111"


def test_profile_changes_do_not_touch_unrelated_settings(profile):
    profile.store["fj"] = {"country": "GB", "timezone": "Europe/London", "pref_dark_mode": False}
    _patch(profile, default_dialing_code="44")
    assert profile.store["fj"]["timezone"] == "Europe/London" and profile.store["fj"]["pref_dark_mode"] is False


def test_profile_read_exposes_the_effective_code_and_postal_label():
    import routes.business_routes as br
    assert br._effective_dial({"country": "ZW"}) == "263" and br._effective_dial({"country": "GB", "default_dialing_code": "353"}) == "353"
    assert br._effective_dial({}) == "" and br._postal_label("US") == "ZIP code"


# ── frontend wiring ───────────────────────────────────────────────────────────
def test_dashboard_has_the_dialing_code_field_and_consent_skip_rendering():
    js = open(os.path.join(ROOT, "static", "dashboard.js"), encoding="utf8").read()
    html = open(os.path.join(ROOT, "static", "dashboard.html"), encoding="utf8").read()
    assert 'id="up-dial"' in html and "up-dial" in js and "default_dialing_code" in js
    assert "consentSkipCount" in js and "skipped_no_consent_numbers" in js
    assert js.count("consentSkipNote(") >= 2


def test_address_prompts_in_the_ai_use_the_country_aware_hint():
    import inspect
    import services.ai as ai
    src = inspect.getsource(ai)
    assert "_addr_hint" in src and "(street, suburb, city)._" not in src
