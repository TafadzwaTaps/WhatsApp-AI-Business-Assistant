"""
tests/test_seo_routes.py — /sitemap.xml and /robots.txt.

Mounts only routes.seo_routes.router on a bare FastAPI app (not the full
main.app), the same isolation pattern already used elsewhere in this
suite — it avoids pulling in every other router's import-time side
effects while still exercising the real route handlers unmodified.
Relies on the autouse `_fake_core_db` fixture in conftest.py so the
dynamic /store/{slug} slugs never hit a real database.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.seo_routes import router as seo_router

app = FastAPI()
app.include_router(seo_router)
client = TestClient(app)


def test_sitemap_returns_200_and_xml_content_type():
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")


def test_sitemap_is_valid_xml_with_expected_static_urls():
    resp = client.get("/sitemap.xml")
    root = ET.fromstring(resp.content)  # raises if malformed

    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = {el.text for el in root.findall("sm:url/sm:loc", ns)}

    for path in ("/", "/pricing", "/signup", "/directory", "/privacy", "/terms"):
        assert f"https://wazibothq.com{path}" in locs


def test_sitemap_never_lists_private_or_authenticated_pages():
    resp = client.get("/sitemap.xml")
    body = resp.text
    for private_path in ("/dashboard", "/inbox", "/onboarding", "/admin", "/api/"):
        assert private_path not in body


def test_sitemap_urls_use_https_and_no_trailing_slash_duplicates():
    resp = client.get("/sitemap.xml")
    root = ET.fromstring(resp.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [el.text for el in root.findall("sm:url/sm:loc", ns)]

    assert len(locs) == len(set(locs)), "sitemap contains duplicate URLs"
    for loc in locs:
        assert loc.startswith("https://wazibothq.com")


def test_sitemap_survives_database_failure(monkeypatch):
    """A broken/unreachable DB must not take the whole sitemap down —
    it should still return 200 with just the static pages."""
    import core.db as fake_db

    def _boom(*a, **kw):
        raise RuntimeError("db unreachable")

    monkeypatch.setattr(fake_db.supabase, "table", _boom)
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert "https://wazibothq.com/pricing" in resp.text


def test_robots_txt_returns_200_and_plain_text():
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")


def test_robots_txt_allows_root_and_links_sitemap():
    body = client.get("/robots.txt").text
    assert "User-agent: *" in body
    assert "Allow: /" in body
    assert "Sitemap: https://wazibothq.com/sitemap.xml" in body


def test_robots_txt_disallows_private_paths_only():
    body = client.get("/robots.txt").text
    for private_path in ("/dashboard", "/inbox", "/onboarding", "/admin", "/api/"):
        assert f"Disallow: {private_path}" in body
    # Must not accidentally block the homepage, static assets, or public pages.
    assert "Disallow: /\n" not in body
    assert "Disallow: /static" not in body
    assert "Disallow: /pricing" not in body
