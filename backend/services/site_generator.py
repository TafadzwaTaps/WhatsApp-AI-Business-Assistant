"""
services/site_generator.py
══════════════════════════
AI Website Generator — generates a complete, branded landing page
for any WaziBot business from their existing product + profile data.

PLACEMENT: backend/services/site_generator.py

Called by:
  routes/marketplace_routes.py  GET /site/{slug}

No AI API calls — uses template rendering from existing business data.
Falls back gracefully if data is unavailable.
Never modifies any existing data.
"""
from __future__ import annotations

import logging
import re
import os

log = logging.getLogger("wazibot")


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ")


def _hex_darken(hex_colour: str, amount: int = 30) -> str:
    """Darken a hex colour slightly for contrast."""
    hex_colour = hex_colour.lstrip("#")
    if len(hex_colour) != 6:
        return "#009c3b"
    r, g, b = int(hex_colour[0:2], 16), int(hex_colour[2:4], 16), int(hex_colour[4:6], 16)
    r = max(0, r - amount)
    g = max(0, g - amount)
    b = max(0, b - amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def _get_business_and_products(slug: str) -> tuple[dict, list]:
    """Fetch public business data and products. Returns ({}, []) on error."""
    try:
        from core.db import supabase
        name_pattern = _slug_to_name(slug)
        biz_res = (
            supabase.table("businesses")
            .select("id,name,category,tagline,logo_url,theme_colour,currency_symbol")
            .eq("is_active", True)
            .ilike("name", f"%{name_pattern}%")
            .limit(1)
            .execute()
        )
        if not biz_res.data:
            return {}, []
        biz = biz_res.data[0]
        prod_res = (
            supabase.table("products")
            .select("id,name,price,description,image_url,category,stock")
            .eq("business_id", biz["id"])
            .execute()
        )
        return biz, prod_res.data or []
    except Exception as exc:
        log.warning("site_generator fetch error: %s", exc)
        return {}, []


def _product_card_html(p: dict, currency_sym: str) -> str:
    name      = p.get("name", "Product")
    price     = float(p.get("price") or 0)
    desc      = p.get("description", "")
    image_url = p.get("image_url", "")
    stock     = p.get("stock")
    available = stock is None or stock > 0
    avail_badge = (
        '<span class="badge available">&#x2705; Available</span>'
        if available else
        '<span class="badge oos">&#x274C; Out of stock</span>'
    )
    img_html = (
        f'<img src="{image_url}" alt="{name}" class="prod-img" loading="lazy">'
        if image_url else
        '<div class="prod-img-placeholder">&#x1F4E6;</div>'
    )
    desc_html = f'<p class="prod-desc">{desc}</p>' if desc else ""
    wa_text   = f"Hi! I'd like to order {name}"
    return (
        f'<div class="prod-card">'
        f'{img_html}'
        f'<div class="prod-body">'
        f'<h3 class="prod-name">{name}</h3>'
        f'{desc_html}'
        f'<div class="prod-foot">'
        f'<span class="prod-price">{currency_sym}{price:.2f}</span>'
        f'{avail_badge}'
        f'<a class="btn-order" href="https://wa.me/?text={wa_text.replace(" ", "%20")}" target="_blank">Order</a>'
        f'</div></div></div>'
    )


def generate_site_html(slug: str) -> str:
    """
    Generate a complete branded HTML landing page for a business.
    Fully self-contained single-file HTML — no external dependencies except fonts.
    """
    biz, products = _get_business_and_products(slug)

    if not biz:
        return _fallback_html(slug)

    name         = biz.get("name", "Our Business")
    category     = biz.get("category", "")
    tagline      = biz.get("tagline") or f"Order {category or 'products'} on WhatsApp"
    logo_url     = biz.get("logo_url", "")
    theme        = biz.get("theme_colour") or "#00c853"
    theme_dark   = _hex_darken(theme)
    currency_sym = biz.get("currency_symbol", "$")

    logo_html = (
        f'<img src="{logo_url}" alt="{name}" class="logo">'
        if logo_url else
        f'<div class="logo-placeholder">{name[0].upper()}</div>'
    )

    products_html = (
        "\n".join(_product_card_html(p, currency_sym) for p in products)
        if products else
        '<p class="no-products">Products coming soon. Contact us on WhatsApp!</p>'
    )

    cat_badge = f'<span class="category-badge">{category}</span>' if category else ""
    wa_name   = name.replace(" ", "%20")

    css = f"""
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    :root{{--brand:{theme};--brand-dark:{theme_dark};--bg:#0a0a0a;--surface:#141414;
           --surface2:#1e1e1e;--text:#f0f0f0;--muted:#888;--border:rgba(255,255,255,0.08);--r:12px}}
    body{{font-family:'Inter',sans-serif;background:var(--bg);color:var(--text);min-height:100vh}}
    header{{background:var(--surface);border-bottom:1px solid var(--border);padding:16px 24px;
            display:flex;align-items:center;gap:16px;position:sticky;top:0;z-index:100}}
    .logo{{height:48px;width:48px;object-fit:contain;border-radius:10px}}
    .logo-placeholder{{height:48px;width:48px;border-radius:10px;background:var(--brand);
                        display:flex;align-items:center;justify-content:center;
                        font-size:24px;font-weight:800;color:#fff}}
    .biz-name{{font-size:20px;font-weight:700}}.biz-cat{{font-size:13px;color:var(--muted)}}
    .wa-btn{{margin-left:auto;background:#25D366;color:#fff;border:none;padding:10px 18px;
             border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;
             display:flex;align-items:center;gap:6px;transition:opacity .2s}}
    .wa-btn:hover{{opacity:0.85}}
    .hero{{background:linear-gradient(135deg,var(--brand) 0%,var(--brand-dark) 100%);
           padding:64px 24px;text-align:center}}
    .hero h1{{font-size:clamp(28px,5vw,48px);font-weight:800;color:#fff;margin-bottom:12px}}
    .hero p{{font-size:18px;color:rgba(255,255,255,.85);max-width:500px;margin:0 auto 24px}}
    .category-badge{{background:rgba(255,255,255,.2);color:#fff;padding:4px 12px;
                      border-radius:20px;font-size:13px;display:inline-block;margin-bottom:16px}}
    .products-section{{padding:48px 24px;max-width:1200px;margin:0 auto}}
    .products-section h2{{font-size:24px;font-weight:700;margin-bottom:24px;
                           border-left:4px solid var(--brand);padding-left:12px}}
    .products-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px}}
    .prod-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
                overflow:hidden;transition:transform .2s,box-shadow .2s}}
    .prod-card:hover{{transform:translateY(-4px);box-shadow:0 8px 32px rgba(0,0,0,.4)}}
    .prod-img{{width:100%;height:200px;object-fit:cover}}
    .prod-img-placeholder{{width:100%;height:200px;background:var(--surface2);
                             display:flex;align-items:center;justify-content:center;font-size:48px}}
    .prod-body{{padding:16px}}.prod-name{{font-size:16px;font-weight:700;margin-bottom:6px}}
    .prod-desc{{font-size:13px;color:var(--muted);margin-bottom:12px;line-height:1.5}}
    .prod-foot{{display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
    .prod-price{{font-size:18px;font-weight:700;color:var(--brand)}}
    .badge{{font-size:11px;padding:2px 8px;border-radius:4px}}
    .badge.available{{background:rgba(0,200,83,.15);color:#00c853}}
    .badge.oos{{background:rgba(239,68,68,.15);color:#ef4444}}
    .btn-order{{margin-left:auto;background:var(--brand);color:#fff;padding:8px 14px;
                border-radius:6px;font-size:13px;font-weight:600;text-decoration:none;
                white-space:nowrap;transition:opacity .2s}}
    .btn-order:hover{{opacity:0.85}}
    .no-products{{color:var(--muted);text-align:center;padding:48px;font-size:16px}}
    footer{{background:var(--surface);border-top:1px solid var(--border);padding:24px;
            text-align:center;color:var(--muted);font-size:13px}}
    footer a{{color:var(--brand);text-decoration:none}}
    @media(max-width:600px){{.products-grid{{grid-template-columns:1fr}}.hero{{padding:40px 16px}}}}
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{name} — WaziBot Store</title>
  <meta name="description" content="{tagline}">
  <meta property="og:title" content="{name}">
  <meta property="og:description" content="{tagline}">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap" rel="stylesheet">
  <style>{css}</style>
</head>
<body>
  <header>
    {logo_html}
    <div>
      <div class="biz-name">{name}</div>
      <div class="biz-cat">{category}</div>
    </div>
    <a class="wa-btn" href="https://wa.me/?text=Hi%20{wa_name}%2C%20I%20want%20to%20order%21" target="_blank">
      &#x1F4AC; WhatsApp Us
    </a>
  </header>
  <section class="hero">
    {cat_badge}
    <h1>{name}</h1>
    <p>{tagline}</p>
    <a class="wa-btn" href="https://wa.me/?text=Hi%21%20I%27d%20like%20to%20browse%20your%20menu."
       target="_blank" style="display:inline-flex;margin:0 auto;">
      &#x1F4AC; Start Ordering on WhatsApp
    </a>
  </section>
  <section class="products-section">
    <h2>&#x1F6CD;&#xFE0F; Our Products</h2>
    <div class="products-grid">
      {products_html}
    </div>
  </section>
  <footer>
    <p>Powered by <a href="https://wazibot-api-assistant.onrender.com" target="_blank">WaziBot</a>
       &mdash; AI Employee for WhatsApp Businesses</p>
  </footer>
</body>
</html>"""


def _fallback_html(slug: str) -> str:
    return (
        "<!DOCTYPE html><html><head><title>Store not found</title>"
        "<meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>body{font-family:sans-serif;background:#0a0a0a;color:#f0f0f0;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "text-align:center;padding:24px}</style></head><body>"
        f"<div><h1 style='color:#00c853'>WaziBot</h1>"
        f"<p>Business <strong>{slug}</strong> not found or not yet public.</p>"
        "<a href='/directory' style='color:#00c853'>&#x2190; Browse all businesses</a>"
        "</div></body></html>"
    )
