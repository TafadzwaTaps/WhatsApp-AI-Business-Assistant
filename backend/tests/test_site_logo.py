"""Website generator logo: safe URL handling, hero rendering, save/remove helper."""
from services import site_generator as sg


def test_logo_url_only_accepts_plain_http_urls():
    assert sg._safe_logo_url("https://cdn.example.com/a.png") == "https://cdn.example.com/a.png"
    for bad in ("javascript:alert(1)", "data:image/png;base64,AAA", 'https://x.co/a" onerror=1',
                "https://x.co/a b", "https://x.co/<x>", None, 123, "https://x.co/" + "a" * 700):
        assert sg._safe_logo_url(bad) == ""


def test_settings_expose_logo_and_default_empty():
    assert sg._get_site_settings({})["logo_url"] == ""
    s = sg._get_site_settings({"site_generator": {"logo_url": "https://cdn.example.com/l.webp"}})
    assert s["logo_url"] == "https://cdn.example.com/l.webp"
    assert sg._get_site_settings({"site_generator": {"logo_url": "javascript:x"}})["logo_url"] == ""


def test_hero_uses_logo_else_initial():
    biz = {"id": 1, "name": "Zed Shop", "category": "Food"}
    with_logo = sg._hero_html(biz, sg._get_site_settings({"site_generator": {"logo_url": "https://cdn.example.com/l.png"}}), "")
    assert '<img src="https://cdn.example.com/l.png"' in with_logo
    assert "hero-logo-placeholder" not in with_logo
    without = sg._hero_html(biz, sg._get_site_settings({}), "")
    assert 'class="hero-logo-placeholder">Z<' in without


def test_save_site_logo_preserves_other_site_settings(monkeypatch):
    import routes.business_routes as br
    store = {"features_json": {"x": 1, "site_generator": {"theme_style": "luxury"}}}
    monkeypatch.setattr(br.crud, "get_business_by_id", lambda _id: {"features_json": store["features_json"]})
    captured = {}
    monkeypatch.setattr(br.crud, "update_business", lambda bid, d: captured.update(d.dict()))
    br._save_site_logo(5, "https://cdn.example.com/l.png")
    assert captured["features_json"]["site_generator"] == {"theme_style": "luxury", "logo_url": "https://cdn.example.com/l.png"}
    assert captured["features_json"]["x"] == 1
    br._save_site_logo(5, "")
    assert "logo_url" not in captured["features_json"]["site_generator"]
