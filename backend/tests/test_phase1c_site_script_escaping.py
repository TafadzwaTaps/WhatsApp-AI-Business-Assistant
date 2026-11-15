"""Phase 1c: tenant text embedded in the public site's inline <script> JSON must not be able to close the script tag."""
from services import site_generator as sg


def test_dynamic_i18n_cannot_break_out_of_script():
    payload = "</script><img src=x onerror=alert(1)>"
    out = sg._i18n_script({"en": {"about": payload, "k2": "a&b\u2028c"}})
    assert "</script><img" not in out
    assert "onerror=alert(1)>" not in out          # the closing '>' is escaped
    assert "\\u003c/script\\u003e" in out
    assert "\u2028" not in out
    # the page's own script element is still closed exactly once
    assert out.count("</script>") == 1


def test_dynamic_i18n_values_roundtrip_via_json():
    import json, re
    out = sg._i18n_script({"en": {"k": "<b>hi</b> & \"q\""}})
    m = re.search(r"const WZ_I18N_DYNAMIC = (\{.*?\});\n", out, re.S)
    assert m
    assert json.loads(m.group(1)) == {"en": {"k": "<b>hi</b> & \"q\""}}
