"""Phase 6.3: translation correctness (masking round trip, storefront language switching)."""
import json
import os
import re
import shutil
import subprocess

import pytest

import services.translation_layer as tl
import services.site_generator as sg


# -- masking: protected values survive, even many of them ----------------------------------------
def _many(n):
    return " ".join(f"ORDER-{i}00 costs ${i}.50" for i in range(1, n + 1))


@pytest.mark.parametrize("n", [1, 2, 9, 10, 11, 25, 60])
def test_masking_round_trips_exactly_with_many_protected_values(n):
    text = _many(n)
    masked, tokens = tl._mask_structured_values(text)
    assert "ORDER-" not in masked and "$" not in masked
    assert len(tokens) >= 2 * n
    assert tl._unmask_structured_values(masked, tokens) == text


def test_token_one_is_never_found_inside_token_ten():
    masked, tokens = tl._mask_structured_values(_many(12))
    keys = list(tokens)
    for a in keys:
        for b in keys:
            if a != b:
                assert a not in b, (a, b)
    assert all(k.startswith("") and k.endswith("") for k in keys)       # delimited, as documented


def test_unmasking_survives_the_engine_changing_the_words_around_tokens():
    masked, tokens = tl._mask_structured_values("Your order ORDER-77 total $16.00 is ready. Call +263771234567")
    translated = masked.replace("Your order", "Votre commande").replace("is ready", "est prete").replace("total", "montant")
    out = tl._unmask_structured_values(translated, tokens)
    assert "ORDER-77" in out and "$16.00" in out and "+263771234567" in out and "Votre commande" in out


def test_product_names_are_protected_longest_first():
    masked, tokens = tl._mask_structured_values("Chicken Burger Combo and Chicken Burger", ("Chicken Burger", "Chicken Burger Combo"))
    assert "Chicken" not in masked
    assert tl._unmask_structured_values(masked, tokens) == "Chicken Burger Combo and Chicken Burger"


def test_text_without_protected_values_is_untouched():
    assert tl._mask_structured_values("Hello there") == ("Hello there", {})


# -- storefront language switching ----------------------------------------------------------------
def _apply_lang_js():
    src = open(sg.__file__, encoding="utf8").read()
    a = src.index("function _wzApplyLang(lang) {{")
    b = src.index("\n}}\n", a) + 3
    return src[a:b].replace("{{", "{").replace("}}", "}")


class _El:
    def __init__(self, attrs, text=""): self.attrs, self.text = dict(attrs), text


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_switching_to_a_language_without_a_translation_restores_the_original_text(tmp_path):
    js = _apply_lang_js()
    harness = r"""
const els = [
  {a: {'data-i18n-dyn': 'tagline'}, text: 'Fresh bread daily'},
  {a: {'data-i18n-dyn-content': 'desc'}, text: '', content: 'Fresh bread daily'},
  {a: {'data-i18n': 'order_btn'}, text: 'Order'},
];
function mk(e) { return { _a: e.a, textContent: e.text, _c: e.content,
  hasAttribute(n) { return n in this._a; }, getAttribute(n) { return n === 'content' ? this._c : this._a[n]; },
  setAttribute(n, v) { if (n === 'content') this._c = v; else this._a[n] = v; } }; }
const nodes = els.map(mk);
const document = { querySelectorAll(sel) { const attr = sel.slice(1, -1); return nodes.filter(n => attr in n._a); },
  getElementById() { return null; }, documentElement: { setAttribute() {} } };
const WZ_I18N = { en: { order_btn: 'Order' }, fr: { order_btn: 'Commander' }, pl: { order_btn: 'Zamow' } };
const WZ_I18N_DYNAMIC = { fr: { tagline: 'Pain frais chaque jour', desc: 'Pain frais' }, pl: {} };
const WZ_I18N_LANG_OK = {}; const WZ_I18N_RTL = ['ar']; var _wzCurrentLang = 'en';
""" + js + r"""
const eq = (a, b, l) => { if (a !== b) { console.error(l + ': got ' + JSON.stringify(a) + ' expected ' + JSON.stringify(b)); process.exit(1); } };
_wzApplyLang('fr');
eq(nodes[0].textContent, 'Pain frais chaque jour', 'translated to French'); eq(nodes[1]._c, 'Pain frais', 'content translated');
_wzApplyLang('pl');                                  // Polish has NO dynamic translation
eq(nodes[0].textContent, 'Fresh bread daily', 'falls back to the ORIGINAL text, not the French');
eq(nodes[1]._c, 'Fresh bread daily', 'content attribute restored too'); eq(nodes[2].textContent, 'Zamow', 'static keys still switch');
_wzApplyLang('fr'); eq(nodes[0].textContent, 'Pain frais chaque jour', 'back to French');
_wzApplyLang('en'); eq(nodes[0].textContent, 'Fresh bread daily', 'English restores the original');
console.log('lang switching OK');
"""
    path = tmp_path / "lang_check.js"
    path.write_text(harness, encoding="utf8")
    r = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr or r.stdout
    assert "lang switching OK" in r.stdout


def test_all_storefront_languages_have_the_same_keys():
    base = set(sg.SITE_I18N["en"])
    codes = [x[0] for x in sg.SITE_I18N_LANGS]
    assert {"en", "pl", "fr", "pt", "es", "de", "ar"} <= set(codes)
    for lang in codes:
        assert set(sg.SITE_I18N[lang]) == base, lang


def test_arabic_is_marked_right_to_left():
    page_src = open(sg.__file__, encoding="utf8").read()
    assert "WZ_I18N_RTL" in page_src and "'dir'" in page_src
