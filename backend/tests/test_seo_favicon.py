"""SEO + favicon regression tests (static pages, root icon routes, sitemap/robots, JSON-LD, solutions, H1s)."""
from __future__ import annotations

import glob
import io
import json
import os
import re
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from main import app, STATIC_DIR

client = TestClient(app)
DOMAIN = "https://wazibothq.com"
ICON_PATHS = ["/favicon.ico", "/favicon-16x16.png", "/favicon-32x32.png", "/apple-touch-icon.png",
              "/android-chrome-192x192.png", "/android-chrome-512x512.png", "/site.webmanifest"]
# store.html is the customer-branded storefront template: WaziBot branding must not be forced on it.
# maintenance.html carries its favicon inline (data URI) because the Cloudflare Worker serves it when the origin is down.
CUSTOMER_BRANDED = {"store.html"}
PRIVATE = {"dashboard.html", "inbox.html", "onboarding.html", "multidash.html", "forgot-password.html",
           "reset-password.html", "verify-email.html"}
LD_RE = r'<script type="application/ld\+json">(.*?)</script>'


def _all_html():
    return sorted(glob.glob(os.path.join(STATIC_DIR, "*.html")) + glob.glob(os.path.join(STATIC_DIR, "solutions", "*.html")))


def _pages():
    return [p for p in _all_html() if os.path.basename(p) not in CUSTOMER_BRANDED | {"maintenance.html"}]


def _read(p):
    return open(p, encoding="utf-8").read()


def _public_pages():
    return [p for p in _pages() if os.path.basename(p) not in PRIVATE]


def test_icon_files_served_unauthenticated_with_mime_and_cache():
    expected = {"/favicon.ico": "image/x-icon", "/site.webmanifest": "application/manifest+json"}
    for path in ICON_PATHS:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].split(";")[0] == expected.get(path, "image/png"), path
        assert "max-age" in r.headers.get("cache-control", ""), path
        assert len(r.content) > 100


def test_icon_dimensions():
    from PIL import Image
    for path, size in [("/favicon-16x16.png", 16), ("/favicon-32x32.png", 32), ("/apple-touch-icon.png", 180),
                       ("/android-chrome-192x192.png", 192), ("/android-chrome-512x512.png", 512)]:
        assert Image.open(io.BytesIO(client.get(path).content)).size == (size, size)
    ico = Image.open(io.BytesIO(client.get("/favicon.ico").content))
    assert {(16, 16), (32, 32), (48, 48)} <= set(ico.info.get("sizes", ()))


def test_png_favicons_contain_brand_green():
    from PIL import Image
    im = Image.open(io.BytesIO(client.get("/android-chrome-512x512.png").content)).convert("RGBA")
    greens = sum(1 for px in im.resize((64, 64)).getdata() if px[1] > 150 and px[0] < 90)
    assert greens > 100


def test_manifest_valid_and_icons_resolve():
    m = client.get("/site.webmanifest").json()
    assert m["name"] == "WaziBot"
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_every_owned_page_references_favicons():
    for p in _pages():
        h = _read(p)
        for needle in ('href="/favicon.ico"', 'href="/favicon-32x32.png"', 'href="/apple-touch-icon.png"',
                       'href="/site.webmanifest"'):
            assert needle in h, f"{os.path.basename(p)} missing {needle}"


def test_maintenance_page_has_inline_favicon_and_worker_embeds_it():
    h = _read(os.path.join(STATIC_DIR, "maintenance.html"))
    assert 'rel="icon"' in h and "data:image/png;base64," in h
    w = open(os.path.join(os.path.dirname(STATIC_DIR), "cloudflare", "maintenance-worker.js"), encoding="utf-8").read()
    assert "data:image/png;base64," in w


def test_storefront_template_not_branded_with_wazibot_favicon():
    assert "favicon-32x32.png" not in _read(os.path.join(STATIC_DIR, "store.html"))


def test_public_titles_and_descriptions_unique_and_present():
    titles, descs = {}, {}
    for p in _public_pages():
        h = _read(p); n = os.path.basename(p)
        t = re.search(r"<title>(.*?)</title>", h, re.S).group(1).strip()
        m = re.search(r'<meta name="description" content="([^"]+)"', h)
        assert m, f"{n} missing description"
        assert 0 < len(t) <= 80, n
        assert len(m.group(1)) <= 320, n
        assert t not in titles, f"duplicate title {n} vs {titles.get(t)}"
        assert m.group(1) not in descs, f"duplicate description {n}"
        titles[t] = n; descs[m.group(1)] = n


def test_landing_and_pricing_metadata():
    land = _read(os.path.join(STATIC_DIR, "landing.html"))
    assert "<title>WaziBot | AI WhatsApp Business Assistant &amp; Automation</title>" in land
    pr = _read(os.path.join(STATIC_DIR, "pricing.html"))
    assert "<title>WaziBot Pricing | AI WhatsApp Business Automation Plans</title>" in pr


def test_canonicals_on_verified_domain_and_social_tags():
    for p in _public_pages():
        h = _read(p); n = os.path.basename(p)
        c = re.search(r'<link rel="canonical" href="([^"]+)"', h)
        assert c and c.group(1).startswith(DOMAIN + "/"), n
        assert 'property="og:title"' in h and 'name="twitter:card"' in h, n
        assert f'property="og:image" content="{DOMAIN}/static/og-image.png"' in h, n


def test_private_pages_noindex():
    for n in PRIVATE | {"maintenance.html"}:
        assert re.search(r'<meta name="robots" content="[^"]*noindex', _read(os.path.join(STATIC_DIR, n))), n


def test_social_image_resolves_and_is_large_enough():
    from PIL import Image
    r = client.get("/static/og-image.png")
    assert r.status_code == 200
    assert Image.open(io.BytesIO(r.content)).size == (1200, 630)


def test_json_ld_parses_and_has_no_invented_ratings():
    for p in _pages():
        for blob in re.findall(LD_RE, _read(p), re.S):
            data = json.loads(blob)
            assert data["@context"] == "https://schema.org"
            assert "aggregateRating" not in blob and '"review"' not in blob.lower()


def test_landing_offers_match_stripe_pricing_config():
    from billing.stripe_service import PRICING_REGIONS
    ld = [json.loads(b) for b in re.findall(LD_RE, _read(os.path.join(STATIC_DIR, "landing.html")), re.S)]
    offers = next(d for d in ld if d["@type"] == "SoftwareApplication")["offers"]
    got = {o["name"].split()[0].lower(): float(o["price"]) for o in offers}
    assert all(o["priceCurrency"] == "USD" for o in offers)
    for key, plan in (("starter", "starter_monthly"), ("growth", "growth_monthly")):
        assert {float(r[plan]) for r in PRICING_REGIONS.values()} == {got[key]}


def test_sitemap_urls_all_resolve_and_are_public():
    root = ET.fromstring(client.get("/sitemap.xml").content)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [e.text for e in root.findall("sm:url/sm:loc", ns)]
    assert len(locs) == len(set(locs))
    for loc in locs:
        assert loc.startswith(DOMAIN)
        r = client.get(loc[len(DOMAIN):] or "/")
        assert r.status_code == 200, loc
        assert "noindex" not in r.text.lower().split("</head>")[0], loc


def test_robots_does_not_block_site_and_references_sitemap():
    t = client.get("/robots.txt").text
    assert "Disallow: /\n" not in t + "\n" and "Allow: /" in t
    assert f"Sitemap: {DOMAIN}/sitemap.xml" in t


def test_csp_unchanged_by_icon_routes():
    csp = client.get("/favicon.ico").headers["content-security-policy"]
    assert "script-src 'self' 'unsafe-inline'" in csp and "default-src 'self'" in csp
    assert "https://wazibothq.com" in csp


# ── Solutions pages, signup, H1s ────────────────────────────────────────────
SOLUTION_PATHS = ["/solutions", "/solutions/restaurants", "/solutions/retail", "/solutions/beauty"]


def test_solution_pages_served_with_unique_metadata_and_single_h1():
    seen = set()
    for path in SOLUTION_PATHS:
        r = client.get(path)
        assert r.status_code == 200, path
        h = r.text
        assert h.count("<h1") == 1, path
        assert f'<link rel="canonical" href="{DOMAIN}{path}"' in h
        assert 'href="/favicon-32x32.png"' in h and 'name="viewport"' in h
        t = re.search(r"<title>(.*?)</title>", h).group(1)
        assert t not in seen
        seen.add(t)
        assert "noindex" not in h.lower()
        for blob in re.findall(LD_RE, h, re.S):
            json.loads(blob)
        assert "testimonial" not in h.lower() and "aggregateRating" not in h


def test_solution_pages_in_sitemap_and_linked_from_public_pages():
    locs = client.get("/sitemap.xml").text
    for path in SOLUTION_PATHS:
        assert f"{DOMAIN}{path}</loc>" in locs
    for n in ("landing.html", "features.html", "whatsapp-order-management.html"):
        assert 'href="/solutions"' in _read(os.path.join(STATIC_DIR, n)), n
    hub = client.get("/solutions").text
    for path in SOLUTION_PATHS[1:]:
        assert f'href="{path}"' in hub


def test_signup_indexable_with_canonical_title_description():
    h = _read(os.path.join(STATIC_DIR, "signup.html"))
    assert "noindex" not in h.lower().split("</head>")[0]
    assert f'<link rel="canonical" href="{DOMAIN}/signup"' in h
    assert "<title>" in h and 'name="description"' in h
    assert f"{DOMAIN}/signup</loc>" in client.get("/sitemap.xml").text


def test_one_h1_on_public_marketing_pages_and_onboarding():
    for p in _public_pages() + [os.path.join(STATIC_DIR, "onboarding.html")]:
        assert _read(p).count("<h1") == 1, os.path.basename(p)


def test_private_app_pages_have_h1():
    for n in ("dashboard.html", "inbox.html", "multidash.html"):
        assert "<h1" in _read(os.path.join(STATIC_DIR, n)), n


def test_marketing_pages_link_to_canonical_pricing_and_signup_routes():
    for p in _public_pages():
        h = _read(p)
        assert 'href="/static/pricing.html' not in h and 'href="/static/signup.html' not in h, os.path.basename(p)
    # legacy URLs keep working for bookmarks and external links
    assert client.get("/static/pricing.html").status_code == 200
    assert client.get("/static/signup.html").status_code == 200


def test_internal_links_and_images_on_public_pages_resolve():
    bad = []
    for p in _public_pages():
        h = _read(p)
        for u in set(re.findall(r'(?:href|src)="(/[^"#?]*)', h)):
            if u.startswith("//") or u.startswith("/auth") or u.startswith("/api"):
                continue
            if client.get(u, follow_redirects=False).status_code >= 400:
                bad.append((os.path.basename(p), u))
    assert not bad, bad
