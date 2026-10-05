"""Owner-limited site text customization + social links (TikTok/Telegram...)."""
from services import site_generator as sg


def test_text_overrides_clipped_and_defaults_empty():
    s = sg._get_site_settings({"site_generator": {"hero_title": "x" * 500, "cta_text": 5}})
    assert len(s["hero_title"]) == 80
    assert s["cta_text"] == ""
    assert sg._get_site_settings(None)["hero_tagline"] == ""


def test_hero_uses_overrides_and_escapes():
    biz = {"id": 1, "name": "Salon", "category": "Hair Salon"}
    st = sg._get_site_settings({"site_generator": {
        "hero_title": "<script>alert(1)</script>", "hero_tagline": "Best cuts", "cta_text": "Book now"}})
    html = sg._hero_html(biz, st, "447700000000")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html and "Best cuts" in html and "Book now" in html


def test_hero_defaults_unchanged_without_overrides():
    biz = {"id": 1, "name": "Salon", "category": "Hair Salon"}
    html = sg._hero_html(biz, sg._get_site_settings(None), "447700000000")
    assert '<h1 class="hero-title">Salon</h1>' in html and 'data-i18n="hero_cta"' in html


def test_social_url_normalisation():
    assert sg._social_url("tiktok", "@mybiz") == "https://tiktok.com/@mybiz"
    assert sg._social_url("telegram", "t.me/mybiz") == "https://t.me/mybiz"
    assert sg._social_url("telegram", "https://t.me/mybiz") == "https://t.me/mybiz"
    assert sg._social_url("tiktok", "javascript:alert(1)") == ""
    assert sg._social_url("tiktok", "https://evil.com/x") == ""
    assert sg._social_url("tiktok", "a b<c>") == ""
    assert sg._social_url("nope", "x") == ""


def test_contact_renders_social_links_only_when_set():
    biz = {"name": "Salon", "features_json": {"social_links": {"tiktok": "@mybiz", "telegram": "mybiz", "x": ""}}}
    html = sg._contact_html(biz, sg._get_site_settings(None), "447700000000")
    assert "https://tiktok.com/@mybiz" in html and "https://t.me/mybiz" in html
    assert "x.com" not in html
    plain = sg._contact_html({"name": "S"}, sg._get_site_settings(None), "1")
    assert "social-links" not in plain.split("</h2>")[1]
