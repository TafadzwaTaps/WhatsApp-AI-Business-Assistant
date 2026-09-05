"""Tests for WaziBot's public SEO endpoints."""
import xml.etree.ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.seo_routes import router, PUBLIC_URLS


def _client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_sitemap_is_valid_xml_and_contains_only_public_urls():
    response = _client().get("/sitemap.xml")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")

    root = ET.fromstring(response.content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = [node.text for node in root.findall("sm:url/sm:loc", ns)]

    assert urls == list(PUBLIC_URLS)
    assert "/dashboard" not in "".join(urls)
    assert "/inbox" not in "".join(urls)
    assert "/onboarding" not in "".join(urls)


def test_robots_points_to_sitemap():
    response = _client().get("/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "User-agent: *" in response.text
    assert "Allow: /" in response.text
    assert "Sitemap: https://wazibothq.com/sitemap.xml" in response.text
