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


_ALWAYS_SAFE_FIELDS = "id,name,category,currency_symbol,features_json"
_OPTIONAL_FIELDS     = ("tagline", "logo_url", "theme_colour")
_columns_cache: set | None = None

_ALWAYS_SAFE_PRODUCT_FIELDS = "id,name,price"
_OPTIONAL_PRODUCT_FIELDS    = ("description", "image_url", "category", "stock")
_product_columns_cache: set | None = None


# ── Site Generator customization presets ────────────────────────────────────
# Stored in businesses.features_json.site_generator (existing JSONB column —
# same pattern already used for cart_recovery_enabled, translation_enabled,
# etc.) No schema migration required. Falls back to the original Dark Modern
# look if a business hasn't configured anything yet — fully backward
# compatible with every site already generated before this feature existed.

THEME_PRESETS = {
    "dark_modern": {
        "label": "Dark Modern",
        "bg": "#0a0a0a", "surface": "#141414", "surface2": "#1e1e1e",
        "text": "#f0f0f0", "muted": "#888", "border": "rgba(255,255,255,0.08)",
        "header_bg": "#141414",
    },
    "light_clean": {
        "label": "Light Clean",
        "bg": "#ffffff", "surface": "#f7f7f8", "surface2": "#eeeeef",
        "text": "#1a1a1a", "muted": "#666", "border": "rgba(0,0,0,0.08)",
        "header_bg": "#ffffff",
    },
    "vibrant": {
        "label": "Vibrant",
        "bg": "#1a0f2e", "surface": "#241541", "surface2": "#2e1b52",
        "text": "#f5f0ff", "muted": "#a895c9", "border": "rgba(255,255,255,0.1)",
        "header_bg": "#241541",
    },
    "warm": {
        "label": "Warm",
        "bg": "#1f1410", "surface": "#2b1d16", "surface2": "#36251c",
        "text": "#fdf3ea", "muted": "#c4a385", "border": "rgba(255,255,255,0.08)",
        "header_bg": "#2b1d16",
    },
    "minimal": {
        "label": "Minimal",
        "bg": "#fafafa", "surface": "#ffffff", "surface2": "#f0f0f0",
        "text": "#111111", "muted": "#777", "border": "rgba(0,0,0,0.06)",
        "header_bg": "#fafafa",
    },
    "luxury": {
        "label": "Luxury",
        "bg": "#0d0d0d", "surface": "#161616", "surface2": "#1f1f1f",
        "text": "#f0e6d2", "muted": "#9c8f72", "border": "rgba(212,175,55,0.2)",
        "header_bg": "#161616",
    },
}

FONT_PRESETS = {
    "poppins":    "'Poppins', sans-serif",
    "inter":      "'Inter', sans-serif",
    "montserrat": "'Montserrat', sans-serif",
    "open_sans":  "'Open Sans', sans-serif",
}

FONT_GOOGLE_FAMILIES = {
    "poppins":    "Poppins:wght@400;500;600;700;800",
    "inter":      "Inter:wght@400;500;600;700;800",
    "montserrat": "Montserrat:wght@400;500;600;700;800",
    "open_sans":  "Open+Sans:wght@400;500;600;700;800",
}

LAYOUT_PRESETS = {
    "standard": {"max_width": "1200px", "grid_min": "260px"},
    "wide":     {"max_width": "1440px", "grid_min": "300px"},
    "compact":  {"max_width": "920px",  "grid_min": "220px"},
}


def _get_site_settings(features_json: dict | None) -> dict:
    """
    Read Site Generator customization from features_json.site_generator,
    with safe defaults that reproduce the original (pre-customization)
    appearance exactly — so existing sites never change unless the owner
    explicitly configures them in Settings → Appearance.
    """
    cfg = (features_json or {}).get("site_generator") or {}
    return {
        "theme_style":   cfg.get("theme_style", "dark_modern"),
        "font":          cfg.get("font", "inter"),
        "layout":        cfg.get("layout", "standard"),
        "show_hours":    cfg.get("show_hours", True),
        "show_location": cfg.get("show_location", True),
        "show_reviews":  cfg.get("show_reviews", False),
        "show_ordering": cfg.get("show_ordering", True),
        "business_hours": cfg.get("business_hours", ""),
        "location":       cfg.get("location", ""),
    }


def _get_businesses_columns() -> set:
    """Probe the live businesses table schema once, cache the result."""
    global _columns_cache
    if _columns_cache is None:
        try:
            from core.db import supabase
            res = supabase.table("businesses").select("*").limit(1).execute()
            _columns_cache = set(res.data[0].keys()) if res.data else set(_ALWAYS_SAFE_FIELDS.split(","))
        except Exception:
            _columns_cache = set()
    return _columns_cache


def _get_products_columns() -> set:
    """Probe the live products table schema once, cache the result."""
    global _product_columns_cache
    if _product_columns_cache is None:
        try:
            from core.db import supabase
            res = supabase.table("products").select("*").limit(1).execute()
            _product_columns_cache = set(res.data[0].keys()) if res.data else set(_ALWAYS_SAFE_PRODUCT_FIELDS.split(","))
        except Exception:
            _product_columns_cache = set()
    return _product_columns_cache


def _get_business_and_products(slug: str) -> tuple[dict, list]:
    """Fetch public business data and products. Returns ({}, []) on error."""
    try:
        from core.db import supabase
        name_pattern = _slug_to_name(slug)

        # Build the select field list from columns confirmed to exist —
        # tagline/logo_url/theme_colour were previously hardcoded even
        # though they don't exist on the live table yet. Supabase rejects
        # a .select() naming any unknown column for the WHOLE query, which
        # the except below silently swallowed, making every /site/{slug}
        # page show the "not found" fallback even for real, active
        # businesses with products. This degrades gracefully today and
        # auto-upgrades once the optional columns are migrated.
        cols   = _get_businesses_columns()
        extra  = [f for f in _OPTIONAL_FIELDS if f in cols]
        fields = ",".join(_ALWAYS_SAFE_FIELDS.split(",") + extra)

        biz_res = (
            supabase.table("businesses")
            .select(fields)
            .eq("is_active", True)
            .ilike("name", f"%{name_pattern}%")
            .limit(1)
            .execute()
        )
        if not biz_res.data:
            return {}, []
        biz = biz_res.data[0]

        # Same fix as the businesses select above — description doesn't
        # exist on the live products table yet, and was previously
        # hardcoded here too. This broke /site/{slug} for every business
        # with products (uncaught 400 -> swallowed by except -> empty page).
        prod_cols  = _get_products_columns()
        prod_extra = [f for f in _OPTIONAL_PRODUCT_FIELDS if f in prod_cols]
        prod_fields = ",".join(_ALWAYS_SAFE_PRODUCT_FIELDS.split(",") + prod_extra)

        prod_res = (
            supabase.table("products")
            .select(prod_fields)
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
    category  = p.get("category", "")
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
    cat_attr  = f' data-category="{category}"' if category else ' data-category="other"'
    return (
        f'<div class="prod-card"{cat_attr}>'
        f'{img_html}'
        f'<div class="prod-body">'
        f'<h3 class="prod-name">{name}</h3>'
        f'{desc_html}'
        f'<div class="prod-foot">'
        f'<span class="prod-price">{currency_sym}{price:.2f}</span>'
        f'{avail_badge}'
        f'<a class="btn-order" href="https://wa.me/?text={wa_text.replace(" ", "%20")}" target="_blank">+ Order</a>'
        f'</div></div></div>'
    )


def _category_filter_html(products: list) -> str:
    """
    Build category filter pills from real product category data.
    Returns empty string if no products have a category set — never shows
    a filter bar with only "All" in it.
    """
    cats = sorted({p.get("category") for p in products if p.get("category")})
    if not cats:
        return ""
    pills = ['<button class="cat-pill active" data-filter="all" onclick="_wzFilterCat(this,\'all\')">All</button>']
    for c in cats:
        pills.append(
            f'<button class="cat-pill" data-filter="{c}" onclick="_wzFilterCat(this,\'{c}\')">{c}</button>'
        )
    return f'<div class="cat-filters">{"".join(pills)}</div>'


def generate_site_html(slug: str) -> str:
    """
    Generate a complete branded HTML landing page for a business.
    Fully self-contained single-file HTML — no external dependencies except fonts.

    Theme/font/layout/section-toggle customization is read from
    businesses.features_json.site_generator (see _get_site_settings). If a
    business hasn't configured anything, every output is byte-identical to
    the original pre-customization version — this is purely additive.
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

    settings   = _get_site_settings(biz.get("features_json"))
    palette    = THEME_PRESETS.get(settings["theme_style"], THEME_PRESETS["dark_modern"])
    font_stack = FONT_PRESETS.get(settings["font"], FONT_PRESETS["inter"])
    font_google= FONT_GOOGLE_FAMILIES.get(settings["font"], FONT_GOOGLE_FAMILIES["inter"])
    layout     = LAYOUT_PRESETS.get(settings["layout"], LAYOUT_PRESETS["standard"])

    logo_html = (
        f'<img src="{logo_url}" alt="{name}" class="logo">'
        if logo_url else
        f'<div class="logo-placeholder">{name[0].upper()}</div>'
    )

    cat_filter_html = _category_filter_html(products)
    products_html = (
        "\n".join(_product_card_html(p, currency_sym) for p in products)
        if products else
        '<p class="no-products">Products coming soon. Contact us on WhatsApp!</p>'
    )

    cat_badge = f'<span class="category-badge">{category}</span>' if category else ""
    wa_name   = name.replace(" ", "%20")

    # ── Optional info sections (Business Hours / Location / Reviews) ───────
    # Only rendered when the owner has both enabled the toggle AND provided
    # the corresponding text — never shows an empty "Hours: " line.
    info_chips = []
    if settings["show_hours"] and settings["business_hours"]:
        info_chips.append(f'<span class="info-chip">&#x1F551; {settings["business_hours"]}</span>')
    if settings["show_location"] and settings["location"]:
        info_chips.append(f'<span class="info-chip">&#x1F4CD; {settings["location"]}</span>')
    info_chips_html = (
        f'<div class="info-chips">{"".join(info_chips)}</div>' if info_chips else ""
    )

    reviews_section_html = ""
    if settings["show_reviews"]:
        reviews_section_html = """
  <section class="reviews-section">
    <h2>&#x2B50; Customer Reviews</h2>
    <p class="reviews-placeholder">Reviews from WaziBot orders will appear here soon.</p>
  </section>"""

    ordering_cta = (
        f'<a class="wa-btn" href="https://wa.me/?text=Hi%21%20I%27d%20like%20to%20browse%20your%20menu." '
        f'target="_blank" style="display:inline-flex;margin:0 auto;">'
        f'&#x1F4AC; Start Ordering on WhatsApp</a>'
    ) if settings["show_ordering"] else ""

    css = f"""
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    :root{{--brand:{theme};--brand-dark:{theme_dark};--bg:{palette['bg']};--surface:{palette['surface']};
           --surface2:{palette['surface2']};--text:{palette['text']};--muted:{palette['muted']};
           --border:{palette['border']};--r:12px;--maxw:{layout['max_width']};--gridmin:{layout['grid_min']}}}
    body{{font-family:{font_stack};background:var(--bg);color:var(--text);min-height:100vh}}
    header{{background:{palette['header_bg']};border-bottom:1px solid var(--border);padding:16px 24px;
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
    .info-chips{{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;margin-bottom:20px}}
    .info-chip{{background:rgba(255,255,255,.15);color:#fff;padding:6px 14px;border-radius:20px;
                font-size:13px}}
    .cat-filters{{display:flex;gap:8px;flex-wrap:wrap;padding:0 24px;max-width:var(--maxw);
                  margin:0 auto 20px}}
    .cat-pill{{background:var(--surface);border:1px solid var(--border);color:var(--muted);
               padding:7px 16px;border-radius:20px;font-size:13px;font-weight:600;cursor:pointer;
               transition:all .15s}}
    .cat-pill.active,.cat-pill:hover{{background:var(--brand);color:#fff;border-color:var(--brand)}}
    .products-section{{padding:48px 24px;max-width:var(--maxw);margin:0 auto}}
    .products-section h2{{font-size:24px;font-weight:700;margin-bottom:24px;
                           border-left:4px solid var(--brand);padding-left:12px}}
    .products-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(var(--gridmin),1fr));gap:20px}}
    .prod-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
                overflow:hidden;transition:transform .2s,box-shadow .2s}}
    .prod-card:hover{{transform:translateY(-4px);box-shadow:0 8px 32px rgba(0,0,0,.4)}}
    .prod-card.hidden{{display:none}}
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
    .reviews-section{{padding:48px 24px;max-width:var(--maxw);margin:0 auto;text-align:center}}
    .reviews-section h2{{font-size:24px;font-weight:700;margin-bottom:16px}}
    .reviews-placeholder{{color:var(--muted);font-size:14px}}
    footer{{background:var(--surface);border-top:1px solid var(--border);padding:24px;
            text-align:center;color:var(--muted);font-size:13px}}
    footer a{{color:var(--brand);text-decoration:none}}
    @media(max-width:600px){{.products-grid{{grid-template-columns:1fr}}.hero{{padding:40px 16px}}}}
    """

    filter_js = """
  <script>
    function _wzFilterCat(btn, cat) {
      document.querySelectorAll('.cat-pill').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      document.querySelectorAll('.prod-card').forEach(card => {
        const c = card.getAttribute('data-category');
        card.classList.toggle('hidden', cat !== 'all' && c !== cat);
      });
    }
  </script>""" if cat_filter_html else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{name} — WaziBot Store</title>
  <meta name="description" content="{tagline}">
  <meta property="og:title" content="{name}">
  <meta property="og:description" content="{tagline}">
  <link href="https://fonts.googleapis.com/css2?family={font_google}&display=swap" rel="stylesheet">
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
    {info_chips_html}
    {ordering_cta}
  </section>
  {cat_filter_html}
  <section class="products-section">
    <h2>&#x1F6CD;&#xFE0F; Our Products</h2>
    <div class="products-grid">
      {products_html}
    </div>
  </section>{reviews_section_html}
  <footer>
    <p>Powered by <a href="https://wazibot-api-assistant.onrender.com" target="_blank">WaziBot</a>
       &mdash; AI Employee for WhatsApp Businesses</p>
  </footer>{filter_js}
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
